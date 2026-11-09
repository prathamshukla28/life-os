"""The 8-step salary-based affordability analysis (LM05, plan "LM05 affordability procedure").

For the proposal, every alternative, and an automatic baseline ("do nothing -
keep the cash at the cited low-risk rate"), the engine produces one row per step:

  1 total_cost            upfront + recurring x horizon (missing -> HOLD, negative -> BLOCK)
  2 share_of_take_home    recurring as % of take-home; upfront in months of take-home
                          (take-home UNKNOWN -> HOLD)
  3 essentials_and_emergency_fund
                          surplus after the recurring cost (< 0 -> BLOCK); emergency fund
                          after the upfront vs its target (falls below -> HOLD)
  4 benefit_scenarios     conservative / base / optimistic monthly benefit with basis and
                          evidence class (missing/no basis -> HOLD; misordered -> BLOCK)
  5 downside_and_opportunity_cost
                          worst plausible loss in months of take-home; what the same cash
                          would earn at a cited low-risk RulePack rate (stale -> HOLD)
  6 payback               months per scenario = upfront / (benefit - recurring), "never"
                          when benefit <= recurring
  7 versus_alternatives   excess of net benefit over keeping the same cash at the low-risk
                          rate; side-by-side table across options
  8 goal_fit              goal_tags vs the profile's stated goals (none -> HOLD; unknown id
                          -> HOLD; untagged -> flagged)

Each option's disposition is ``run_gate`` over its 8 rows (worst wins). The report's
disposition is the proposal's (plus structural checks). There is no combined score and
no ranking number; the recommendation names the option that clears every check with
the best goal fit, or says why it cannot. Nothing is ever executed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import Any

from life_os.core.canonical import canonical_sha256
from life_os.core.evidence import EVIDENCE_CLASSES, HYPOTHESIS
from life_os.core.gate import BLOCK, HOLD, SHIP, Check, GateResult, run_gate

from ..finance import DEFAULT_EMERGENCY_MONTHS, format_inr
from ..finance.inputs import InputValue, Raw, assumption_notes, resolve
from ..finance.report import finalize, gate_reason_checks, rule_citations
from ..profile import ASSUMPTION, UNKNOWN
from ..profile.facts import encode_value
from ..research import RulePack, load_rule_pack, rules_gate
from ..security import PrivateStore, redact
from .models import Benefits, Option, Situation

__all__ = [
    "BASELINE_NAME",
    "CRITERIA",
    "DECISION_JOURNAL",
    "DEFAULT_RATE_ID",
    "RATES_PACK_PATH",
    "SCENARIOS",
    "analyze_decision",
    "load_rates_pack",
]

RATES_PACK_PATH = Path(__file__).resolve().parent.parent / "rules" / "in_low_risk_rates_2026q3.json"
DEFAULT_RATE_ID = "post_office_time_deposit_1y"
BASELINE_NAME = "do_nothing_keep_cash_low_risk"
DECISION_JOURNAL = "decisions/journal.jsonl"
SCENARIOS = ("conservative", "base", "optimistic")
CRITERIA = (
    "total_cost",
    "share_of_take_home",
    "essentials_and_emergency_fund",
    "benefit_scenarios",
    "downside_and_opportunity_cost",
    "payback",
    "versus_alternatives",
    "goal_fit",
)
_RANK = {SHIP: 0, HOLD: 1, BLOCK: 2}
_PCT = Decimal("0.01")
MAX_HORIZON_MONTHS = 1200

DECISION_ASSUMPTIONS = (
    (
        "Benefit estimates are assumptions, not promises; compare all three scenarios rather "
        "than relying on one. Losses beyond the stated downside are not modelled."
    ),
    (
        "The upfront cost is assumed to be paid from the emergency-fund balance; recurring costs "
        "come out of the monthly surplus."
    ),
    (
        "Opportunity cost = what the same cash would earn at the cited low-risk rate (nominal "
        "rate/12, monthly compounding, costs paid at the start of each month) minus the cash "
        "spent. Low-risk is not no-risk."
    ),
    (
        "The cited 1-year post office time deposit compounds quarterly; the opportunity cost "
        "here uses monthly compounding at rate/12, which slightly overstates it (a cautious "
        "bias against spending). PPF compounds annually."
    ),
    (
        "Payback months are rounded up to whole months; benefits and costs are taken as level "
        "each month over the horizon."
    ),
    "No combined score: each option is judged per criterion and by its gate disposition.",
    (
        "Life Mentor never executes a purchase, payment, investment or data share; it only "
        "analyses and recommends."
    ),
)


def load_rates_pack(path: str | Path = RATES_PACK_PATH) -> RulePack:
    """Load the bundled low-risk reference-rate pack (RulePackBlocked if invalid)."""
    return load_rule_pack(path)


# -- row builder ---------------------------------------------------------------


@dataclass
class _Row:
    step: int
    summary: str = ""
    issues: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)

    def hold(self, msg: str, missing: str | None = None) -> None:
        self.issues.append((HOLD, msg))
        if missing:
            self.missing.append(missing)

    def block(self, msg: str) -> None:
        self.issues.append((BLOCK, msg))

    def take(self, iv: InputValue) -> Decimal | None:
        """Gate one input; return its value only if usable."""
        if iv.error is not None:
            self.block(f"{iv.name}: {iv.error}")
            return None
        if iv.value is None:
            self.hold(f"{iv.name}: missing or UNKNOWN; please provide it", iv.name)
            return None
        if iv.value < 0 and not iv.allow_negative:
            self.block(f"{iv.name}: negative value is contradictory")
            return None
        return iv.value

    @property
    def disposition(self) -> str:
        disp = SHIP
        for d, _ in self.issues:
            if _RANK[d] > _RANK[disp]:
                disp = d
        return disp

    @property
    def reason(self) -> str:
        parts = [m for _, m in self.issues] + ([self.summary] if self.summary else []) + self.notes
        return "; ".join(parts) if parts else "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "criterion": CRITERIA[self.step - 1],
            "disposition": self.disposition,
            "reason": self.reason,
            "values": self.values,
        }


@dataclass
class _Ctx:
    as_of: date
    take_home: InputValue
    essentials: InputValue
    ef_balance: InputValue
    ef_months: InputValue
    goals: tuple[str, ...] | None
    rate_id: str
    rate: InputValue | None
    rate_reasons: list[dict[str, Any]]


@dataclass
class _Eval:
    name: str
    kind: str
    is_baseline: bool
    is_primary: bool
    inputs: list[InputValue]
    rows: list[_Row]
    gate: GateResult
    goal_tags: tuple[str, ...]
    upfront: Decimal | None = None
    recurring: Decimal | None = None
    horizon: int | None = None
    opportunity_cost: Decimal | None = None
    matched: list[str] = field(default_factory=list)
    base_excess: Decimal | None = None

    @property
    def missing(self) -> list[str]:
        return [m for r in self.rows for m in r.missing]

    def as_dict(self, others: list[str]) -> dict[str, Any]:
        self.rows[6].values["compared_with"] = [n for n in others if n != self.name]
        return {
            "name": self.name,
            "kind": self.kind,
            "is_baseline": self.is_baseline,
            "goal_tags": list(self.goal_tags),
            "inputs": {iv.name: iv.as_dict() for iv in self.inputs},
            "criteria": [r.as_dict() for r in self.rows],
            "gate": self.gate.as_dict(),
            "change_conditions": [c for r in self.rows for c in r.conditions],
        }


def _as_input(name: str, raw: Raw | InputValue, *, integer: bool = False) -> InputValue:
    if isinstance(raw, InputValue):
        return raw
    return resolve(name, raw, integer=integer)


def _fv_cost(upfront: Decimal, recurring: Decimal, months: int, rate_pct: Decimal) -> Decimal:
    """Future value of the cost stream at the rate (upfront at t0, recurring at month starts)."""
    with localcontext() as ctx:
        ctx.prec = 50
        i = rate_pct / 1200
        if i == 0:
            return upfront + recurring * months
        g = (1 + i) ** months
        return upfront * g + recurring * (g - 1) / i * (1 + i)


def _row_check(name: str, row: _Row) -> Check:
    disp, reason = row.disposition, row.reason

    def fn() -> tuple[str, str]:
        return disp, reason

    return Check(name=f"{name} step {row.step} {CRITERIA[row.step - 1]}", fn=fn)


def _block_check(message: str) -> Check:
    def fn() -> tuple[str, str]:
        return BLOCK, message

    return Check(name="structure", fn=fn)


# -- the eight steps -------------------------------------------------------------


def _evaluate(opt: Option, ctx: _Ctx, *, primary: bool) -> _Eval:
    name = redact(opt.name.strip())
    up_iv = resolve("upfront_cost", opt.upfront_cost)
    rec_iv = resolve("recurring_monthly_cost", opt.recurring_monthly_cost)
    down_iv = resolve("downside_loss", opt.downside_loss)
    inputs = [up_iv, rec_iv, down_iv]
    rows = [_Row(n) for n in range(1, 9)]
    r1, r2, r3, r4, r5, r6, r7, r8 = rows

    # 1 total cost
    up, rec = r1.take(up_iv), r1.take(rec_iv)
    h = opt.horizon_months
    if h is None:
        r1.hold("horizon_months: missing; how many months will this run?", "horizon_months")
    elif h <= 0:
        r1.block("horizon_months must be at least 1")
        h = None
    elif h > MAX_HORIZON_MONTHS:
        # Beyond this the compounding in _fv_cost leaves Decimal range (it used to
        # raise decimal.Overflow out of analyze_decision).
        r1.block(f"horizon_months must be at most {MAX_HORIZON_MONTHS} (100 years)")
        h = None
    total: Decimal | None = None
    if up is not None and rec is not None and h is not None:
        total = up + rec * h
        r1.values = {
            "upfront_cost": up,
            "recurring_monthly_cost": rec,
            "horizon_months": h,
            "recurring_total": rec * h,
            "total_cost": total,
        }
        r1.summary = f"total cost {format_inr(total)} over {h} months"

    # 2 share of take-home
    th = r2.take(ctx.take_home)
    _step2(r2, th, up, rec)

    # 3 essentials and emergency fund
    ess, ef = r3.take(ctx.essentials), r3.take(ctx.ef_balance)
    months = r3.take(ctx.ef_months)
    if months is not None and months <= 0:
        r3.block("emergency_fund_months must be at least 1")
        months = None
    _step3(r3, th, ess, ef, months, up, rec)

    # 4 benefit scenarios
    monthly = _step4(r4, opt.benefit_monthly, h, total, inputs)
    net = r4.values.get("net_benefit")

    # 5 downside and opportunity cost
    down = r5.take(down_iv)
    opp = _step5(r5, ctx, th, down, up, rec, h)

    # 6 payback
    _step6(r6, up, rec, h, monthly)

    # 7 versus alternatives (vs keeping the same cash at the low-risk rate)
    base_excess = _step7(r7, net, opp)

    # 8 goal fit
    matched = _step8(r8, ctx.goals, opt.goal_tags)

    gate = run_gate(f"life_os.life_mentor.decision.{name}", [_row_check(name, r) for r in rows])
    return _Eval(
        name,
        opt.kind,
        False,
        primary,
        inputs,
        rows,
        gate,
        opt.goal_tags,
        upfront=up,
        recurring=rec,
        horizon=h,
        opportunity_cost=opp,
        matched=matched,
        base_excess=base_excess,
    )


def _step2(r: _Row, th: Decimal | None, up: Decimal | None, rec: Decimal | None) -> None:
    if th is None:
        return
    if th == 0:
        r.hold("take-home is zero; affordability from income cannot be assessed", "take_home_monthly")
        return
    if up is None or rec is None:
        r.hold("not computed: cost is incomplete (step 1)")
        return
    pct = (rec / th * 100).quantize(_PCT, ROUND_HALF_UP)
    months = (up / th).quantize(_PCT, ROUND_HALF_UP)
    r.values = {
        "take_home_monthly": th,
        "recurring_percent_of_take_home": pct,
        "upfront_months_of_take_home": months,
    }
    r.summary = f"recurring {pct}% of take-home; upfront = {months} months of take-home"


def _step3(
    r: _Row,
    th: Decimal | None,
    ess: Decimal | None,
    ef: Decimal | None,
    months: Decimal | None,
    up: Decimal | None,
    rec: Decimal | None,
) -> None:
    if th is None or ess is None or ef is None or months is None:
        return
    if up is None or rec is None:
        r.hold("not computed: cost is incomplete (step 1)")
        return
    before = th - ess
    after = before - rec
    target = months * ess
    ef_after = ef - up
    shortfall = max(Decimal(0), target - ef_after)
    r.values = {
        "surplus_before": before,
        "surplus_after": after,
        "emergency_fund_target": target,
        "emergency_fund_after": ef_after,
        "emergency_fund_shortfall": shortfall,
        "months_of_cover_after": (ef_after / ess).quantize(Decimal("0.1"), ROUND_HALF_UP) if ess else None,
    }
    if after < 0:
        r.block(
            f"monthly surplus after this decision is negative ({format_inr(after)}); "
            f"essentials would not be covered"
        )
        if before > 0:
            r.conditions.append(
                f"clears step 3 only if the recurring cost is at most "
                f"{format_inr(before)} a month (current surplus)"
            )
        else:
            r.conditions.append(
                "essential spending already exceeds take-home; it must fall "
                "below take-home before any new commitment"
            )
    else:
        r.summary = f"monthly surplus after decision {format_inr(after)}"
    if ef_after < 0:
        r.block(
            f"upfront cost {format_inr(up)} exceeds the emergency fund {format_inr(ef)}; "
            f"the fund would go negative ({format_inr(ef_after)}), i.e. this needs "
            f"borrowing"
        )
        r.conditions.append(
            f"clears the funding check only if the upfront cost is at most "
            f"{format_inr(ef)} (the fund balance)"
        )
    elif ef_after < target:
        if up > 0:
            r.hold(
                f"emergency fund would fall to {format_inr(ef_after)}, below its target "
                f"{format_inr(target)} (shortfall {format_inr(shortfall)}); "
                f"a human may override"
            )
            r.conditions.append(
                f"clears the emergency-fund check once the fund holds at least "
                f"{format_inr(target + up)} before paying (target "
                f"{format_inr(target)} + upfront {format_inr(up)})"
            )
        else:
            r.notes.append(
                f"emergency fund is already below its target {format_inr(target)} "
                f"(shortfall {format_inr(shortfall)}); this option does not reduce it"
            )


def _step4(
    r: _Row, ben: Benefits | None, h: int | None, total: Decimal | None, inputs: list[InputValue]
) -> dict[str, Decimal] | None:
    if ben is None:
        r.hold(
            "benefit_monthly: no benefit estimate; give conservative/base/optimistic values with their basis",
            "benefit_monthly",
        )
        return None
    monthly: dict[str, Decimal] = {}
    classes: dict[str, str] = {}
    for s in SCENARIOS:
        est = getattr(ben, s)
        if est is None:
            r.hold(f"benefit {s}: missing", f"benefit_monthly.{s}")
            continue
        iv = resolve(f"benefit_monthly:{s}", est.monthly, provenance_override=ASSUMPTION)
        inputs.append(iv)
        v = r.take(iv)
        if not isinstance(est.basis, str) or not est.basis.strip():
            r.hold(f"benefit {s}: no basis given for the estimate", f"benefit_monthly.{s}.basis")
        if est.evidence_class not in EVIDENCE_CLASSES:
            r.block(f"benefit {s}: evidence_class {est.evidence_class!r} not in {EVIDENCE_CLASSES}")
        else:
            classes[s] = est.evidence_class
        if v is not None:
            monthly[s] = v
    if len(monthly) == len(SCENARIOS):
        vals = [monthly[s] for s in SCENARIOS]
        if vals != sorted(vals):
            r.block("benefits must satisfy conservative <= base <= optimistic")
    if classes:
        r.notes.append("evidence: " + ", ".join(f"{s}={c}" for s, c in classes.items()))
    if HYPOTHESIS in classes.values():
        r.notes.append("HYPOTHESIS estimates are plausible but unmeasured; treat with care")
    if len(monthly) != len(SCENARIOS):
        return None
    r.values = {"benefit_monthly": monthly, "basis": [redact(getattr(ben, s).basis) for s in SCENARIOS]}
    if h is not None:
        totals = {s: monthly[s] * h for s in SCENARIOS}
        r.values["benefit_total"] = totals
        if total is not None:
            r.values["net_benefit"] = {s: totals[s] - total for s in SCENARIOS}
            r.summary = "net benefit over horizon " + ", ".join(
                f"{s} {format_inr(r.values['net_benefit'][s])}" for s in SCENARIOS
            )
    return monthly


def _apply_rate_reasons(r: _Row, ctx: _Ctx) -> Decimal | None:
    for reason in ctx.rate_reasons:
        if reason["disposition"] == HOLD:
            r.hold(reason["reason"])
        elif reason["disposition"] == BLOCK:
            r.block(reason["reason"])
        else:
            r.notes.append(reason["reason"])
    r.values["rule_reasons"] = ctx.rate_reasons
    r.values["low_risk_rate_id"] = ctx.rate_id
    if ctx.rate is None:
        return None
    rate = r.take(ctx.rate)
    if rate is not None:
        r.values["low_risk_rate_percent"] = rate
    return rate


def _step5(
    r: _Row,
    ctx: _Ctx,
    th: Decimal | None,
    down: Decimal | None,
    up: Decimal | None,
    rec: Decimal | None,
    h: int | None,
) -> Decimal | None:
    rate = _apply_rate_reasons(r, ctx)
    if down is not None:
        r.values["downside_loss"] = down
        if th:
            r.values["downside_months_of_take_home"] = (down / th).quantize(_PCT, ROUND_HALF_UP)
    if rate is None or up is None or rec is None or h is None:
        return None
    opp = (_fv_cost(up, rec, h, rate) - (up + rec * h)).quantize(_PCT, ROUND_HALF_UP)
    r.values["opportunity_cost"] = opp
    r.summary = f"same cash at {rate}% would earn {format_inr(opp)} over {h} months" + (
        f"; worst plausible loss {format_inr(down)}" if down is not None else ""
    )
    return opp


def _step6(
    r: _Row, up: Decimal | None, rec: Decimal | None, h: int | None, monthly: dict[str, Decimal] | None
) -> None:
    if up is None or rec is None or h is None or monthly is None:
        r.hold("not computed: needs complete cost (step 1) and benefits (step 4)")
        return
    payback: dict[str, int | str] = {}
    within: dict[str, bool] = {}
    for s in SCENARIOS:
        net = monthly[s] - rec
        if up == 0 and net >= 0:
            payback[s] = 0
        elif net <= 0:
            payback[s] = "never"
        else:
            payback[s] = int((up / net).to_integral_value(rounding=ROUND_CEILING))
        p = payback[s]
        within[s] = isinstance(p, int) and p <= h
    r.values = {"payback_months": payback, "within_horizon": within}
    r.summary = "payback " + ", ".join(f"{s} {payback[s]}" for s in SCENARIOS)
    if not within["base"]:
        r.hold(
            f"base scenario does not pay back within the {h}-month horizon "
            f"(payback {payback['base']}); a human may override"
        )
    if h > 0:
        breakeven = (rec + up / h).quantize(_PCT, ROUND_CEILING)
        r.values["break_even_monthly_benefit"] = breakeven
        r.conditions.append(
            f"pays back within {h} months only if the monthly benefit is at "
            f"least {format_inr(breakeven)} (conservative estimate "
            f"{format_inr(monthly['conservative'])})"
        )


def _step7(r: _Row, net: dict[str, Decimal] | None, opp: Decimal | None) -> Decimal | None:
    if net is None or opp is None:
        r.hold("not computed: needs net benefit (step 4) and opportunity cost (step 5)")
        return None
    excess = {s: net[s] - opp for s in SCENARIOS}
    r.values = {"excess_over_low_risk": excess}
    r.summary = "versus keeping the same cash at the low-risk rate: " + ", ".join(
        f"{s} {format_inr(excess[s])}" for s in SCENARIOS
    )
    if excess["base"] < 0:
        r.hold(
            f"base scenario does worse than keeping the same cash at the low-risk rate "
            f"({format_inr(excess['base'])}); a human may override"
        )
    return excess["base"]


def _step8(r: _Row, goals: tuple[str, ...] | None, tags: tuple[str, ...]) -> list[str]:
    if goals is None:
        r.hold("no goals available (not recorded or no consent); state your goals", "goals")
        return []
    if not goals:
        r.hold("no stated goals in your profile; goal fit cannot be judged", "goals")
        return []
    unknown = [t for t in tags if t not in goals]
    matched = [t for t in tags if t in goals]
    r.values = {
        "stated_goals": list(goals),
        "goal_tags": list(tags),
        "matched_goals": matched,
        "unknown_goal_ids": unknown,
    }
    if unknown:
        r.hold(f"goal ids not among your stated goals: {', '.join(unknown)}; confirm or correct")
    if not tags:
        r.notes.append("supports no stated goal (flagged)")
        r.conditions.append("link this option to one of your stated goals if it serves one")
    elif matched:
        r.summary = f"supports stated goals: {', '.join(matched)}"
    return matched


def _baseline(primary: _Eval, ctx: _Ctx) -> _Eval:
    """Keep the proposal's cash at the cited low-risk rate (always present)."""
    rows = [_Row(n) for n in range(1, 9)]
    r1, r2, r3, r4, r5, r6, r7, r8 = rows
    h = primary.horizon
    r1.values = {
        "upfront_cost": Decimal(0),
        "recurring_monthly_cost": Decimal(0),
        "horizon_months": h,
        "recurring_total": Decimal(0),
        "total_cost": Decimal(0),
    }
    r1.summary = "no new cost"
    th = r2.take(ctx.take_home)
    _step2(r2, th, Decimal(0), Decimal(0))
    ess, ef = r3.take(ctx.essentials), r3.take(ctx.ef_balance)
    months = r3.take(ctx.ef_months)
    if months is not None and months <= 0:
        r3.block("emergency_fund_months must be at least 1")
        months = None
    _step3(r3, th, ess, ef, months, Decimal(0), Decimal(0))
    rate = _apply_rate_reasons(r5, ctx)
    r5.values.update({"downside_loss": Decimal(0), "opportunity_cost": Decimal(0)})
    opp = primary.opportunity_cost
    if opp is None or h is None or rate is None:
        r4.hold("not computed: the proposal's cost or the low-risk rate is unavailable")
    else:
        r4.values = {"benefit_total": {s: opp for s in SCENARIOS}, "net_benefit": {s: opp for s in SCENARIOS}}
        r4.summary = f"the proposal's cash kept at {rate}% would earn {format_inr(opp)} over {h} months"
        r4.notes.append("evidence: cited low-risk rate (UNVERIFIED until a human checks it)")
    r5.summary = "keeps the cash; no new downside beyond rate changes"
    r6.values = {"payback_months": {s: 0 for s in SCENARIOS}, "within_horizon": {s: True for s in SCENARIOS}}
    r6.summary = "nothing to pay back"
    r7.values = {"excess_over_low_risk": {s: Decimal(0) for s in SCENARIOS}}
    r7.summary = "this is the baseline every option is compared against"
    matched = _step8(r8, ctx.goals, ())
    if r8.conditions:
        r8.conditions = []
        r8.notes.append("keeps the cash available for any stated goal")
    gate = run_gate(
        f"life_os.life_mentor.decision.{BASELINE_NAME}", [_row_check(BASELINE_NAME, r) for r in rows]
    )
    return _Eval(
        BASELINE_NAME,
        "spend",
        True,
        False,
        [],
        rows,
        gate,
        (),
        upfront=Decimal(0),
        recurring=Decimal(0),
        horizon=h,
        opportunity_cost=Decimal(0),
        matched=matched,
        base_excess=Decimal(0),
    )


# -- recommendation ---------------------------------------------------------------


def _recommend(
    evals: list[_Eval], structural: list[str], questions: Sequence[dict[str, str]]
) -> dict[str, Any]:
    if structural:
        return {
            "status": "no_option_clears",
            "option": None,
            "text": "No option clears every check: " + "; ".join(structural),
            "missing": [],
        }
    missing = list(dict.fromkeys([m for e in evals for m in e.missing] + [q["key"] for q in questions]))
    if missing:
        return {
            "status": "insufficient_information",
            "option": None,
            "text": "Insufficient information to recommend an option. Missing: "
            + ", ".join(missing)
            + ". Provide these and re-run; nothing has been decided.",
            "missing": missing,
        }
    # Never prefer an option that does worse than keeping the cash in the base case
    # (step 7 already HOLDs those; this is belt and braces).
    clear = [
        e for e in evals if e.gate.disposition == SHIP and e.base_excess is not None and e.base_excess >= 0
    ]
    if clear:
        best = max(
            clear,
            key=lambda e: (
                len(e.matched),
                e.base_excess if e.base_excess is not None else Decimal("-Infinity"),
                e.is_primary,
            ),
        )
        goals = ", ".join(best.matched) if best.matched else "none of your stated goals"
        names = ", ".join(e.name for e in clear)
        text = (
            f"Options that clear every check: {names}. Recommended: '{best.name}' - it "
            f"fits your stated goals best ({goals})"
            + (
                ""
                if len(clear) == 1
                else "; ties are broken by the base-scenario result "
                "versus keeping the cash at the low-risk rate"
            )
            + ". This is a recommendation, not an instruction; nothing is executed."
        )
        return {"status": "recommend", "option": best.name, "text": text, "missing": []}
    blockers = [
        f"{e.name} ({e.gate.disposition}: " + next(r.reason for r in e.rows if r.disposition != SHIP) + ")"
        for e in evals
        if e.gate.disposition != SHIP
    ]
    return {
        "status": "no_option_clears",
        "option": None,
        "text": "No option clears every check: " + "; ".join(blockers),
        "missing": [],
    }


# -- public API ----------------------------------------------------------------------


def analyze_decision(
    option: Option,
    *,
    situation: Situation,
    rates: RulePack,
    as_of: date,
    alternatives: Sequence[Option] = (),
    opportunity_rate_id: str = DEFAULT_RATE_ID,
    store: PrivateStore | None = None,
) -> dict[str, Any]:
    """Run the 8-step analysis on *option*, each alternative, and the do-nothing baseline."""
    if not isinstance(as_of, date):
        raise TypeError("as_of must be a datetime.date")
    th = _as_input("take_home_monthly", situation.take_home_monthly)
    if situation.take_home_as_of is not None and situation.take_home_as_of != as_of.isoformat():
        th = InputValue(
            "take_home_monthly",
            None,
            UNKNOWN,
            f"take-home was computed as of {situation.take_home_as_of} but this "
            f"decision is as of {as_of.isoformat()}; recompute it",
        )
    ess = _as_input("essential_monthly", situation.essential_monthly)
    ef = _as_input("emergency_fund_balance", situation.emergency_fund_balance)
    if situation.emergency_fund_months is None:
        ef_months = InputValue(
            "emergency_fund_months",
            Decimal(DEFAULT_EMERGENCY_MONTHS),
            ASSUMPTION,
            "default planning convention",
            integer=True,
        )
        months_note = (
            f"Emergency-fund target uses {DEFAULT_EMERGENCY_MONTHS} months of "
            "essential expenses (a default ASSUMPTION; choose your own months)."
        )
    else:
        ef_months = _as_input("emergency_fund_months", situation.emergency_fund_months, integer=True)
        shown = ef_months.value if ef_months.value is not None else "the stated number of"
        months_note = f"Emergency-fund target uses {shown} months of essential expenses."
    try:
        rate_iv: InputValue | None = resolve("low_risk_rate_percent", rates.get(opportunity_rate_id))
    except KeyError:
        rate_iv = None
    rate_gate = rules_gate(rates, as_of=as_of, ids=[opportunity_rate_id])
    ctx = _Ctx(
        as_of, th, ess, ef, ef_months, situation.goals, opportunity_rate_id, rate_iv, list(rate_gate.reasons)
    )

    structural: list[str] = []
    names = [redact(o.name.strip()) for o in (option, *alternatives)]
    if BASELINE_NAME in names:
        structural.append(f"option name {BASELINE_NAME!r} is reserved for the baseline")
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        structural.append(f"duplicate option names: {', '.join(dupes)}")
    structural += [f"profile: {r['reason']}" for r in situation.profile_reasons if r["disposition"] == BLOCK]

    primary = _evaluate(option, ctx, primary=True)
    evals = [primary, *(_evaluate(a, ctx, primary=False) for a in alternatives)]
    evals.append(_baseline(primary, ctx))
    all_names = [e.name for e in evals]
    options = [e.as_dict(all_names) for e in evals]
    comparison = {
        crit: {
            o["name"]: {"disposition": o["criteria"][i]["disposition"], "summary": o["criteria"][i]["reason"]}
            for o in options
        }
        for i, crit in enumerate(CRITERIA)
    }
    recommendation = _recommend(evals, structural, situation.questions)
    change_conditions = [f"{o['name']}: {c}" for o in options for c in o["change_conditions"]]
    if situation.questions or recommendation["missing"]:
        change_conditions.append("the analysis changes once the missing facts are provided")

    rules_used = rule_citations(rates, [opportunity_rate_id], as_of)
    seen = {r["id"] for r in rules_used}
    rules_used += [r for r in situation.extra_rules_used if r["id"] not in seen]

    next_steps: list[str] = []
    if recommendation["missing"]:
        next_steps.append("Provide the missing facts: " + ", ".join(recommendation["missing"]))
    if any(r.get("unverified") for r in rules_used):
        next_steps.append(
            "Check the cited rule values against their sources (UNVERIFIED) before relying on the figures."
        )
    next_steps.append(
        "Life Mentor never executes purchases, payments or investments. If you "
        "decide to go ahead, confirm explicitly (record_confirmation) and act "
        "yourself."
    )

    assumptions = [months_note, *DECISION_ASSUMPTIONS, *assumption_notes([th, ess, ef, ef_months])]
    for e in evals:
        assumptions += [f"{e.name}: {n}" for n in assumption_notes(e.inputs)]
    if th.provenance == "DERIVED":
        assumptions.append(f"take_home_monthly is derived ({redact(th.basis)}); its own assumptions apply.")

    checks = [_row_check(f"proposal {primary.name}", r) for r in primary.rows]
    checks += [_block_check(m) for m in structural]
    checks += gate_reason_checks(r for r in situation.profile_reasons if r["disposition"] != BLOCK)
    result = {
        "options": options,
        "comparison": comparison,
        "recommendation": recommendation,
        "change_conditions": change_conditions,
        "next_steps": next_steps,
        "questions": [dict(q) for q in situation.questions],
        "execution": {
            "executed": False,
            "requires_human_confirmation": True,
            "note": "Analysis only. No purchase, payment, investment or data share is made.",
        },
    }
    report = finalize(
        "decision",
        inputs=[th, ess, ef, ef_months],
        assumptions=assumptions,
        result=result,
        checks=checks,
        rules_used=rules_used,
        extra={"as_of": as_of.isoformat()},
    )
    report["decision_id"] = canonical_sha256(encode_value(report))
    report["journal"] = {"recorded": False, "index": None}
    if store is not None:
        entry = store.journal(DECISION_JOURNAL).append(
            {
                "event": "decision_analyzed",
                "decision_id": report["decision_id"],
                "options": all_names,
                "dispositions": {e.name: e.gate.disposition for e in evals},
                "overall_disposition": report["gate"]["disposition"],
                "recommendation_status": recommendation["status"],
                "recommended": recommendation["option"],
            }
        )
        report["journal"] = {"recorded": True, "index": entry["index"]}
    return report
