"""India income tax for salaried individuals, FY2026-27, new and old regime (LM04).

EVERY slab, rate, limit, and rounding multiple is read from the dated rule pack
``life_mentor/rules/in_income_tax_fy2026_27.json`` through the LM03 loader -
this module contains no tax numbers. Changing a value means editing the cited
pack, never this code.

Computation order (per regime):
  1. taxable income = gross salary - min(salary, standard deduction)
                      - other_deductions (old regime only, user-supplied);
     or the caller supplies taxable income directly.
     Rounded to the pack's ``rounding_total_income`` multiple (s.288A style).
  2. slab tax on the slabs (old regime: basic exemption raised by age band).
  3. s.87A rebate: income <= limit -> rebate up to max; above the limit, where
     the regime allows marginal relief, tax is capped at the income in excess
     of the limit.
  4. surcharge by band on total income, with marginal relief: tax+surcharge
     never exceeds tax+surcharge at the band threshold plus the income above it.
  5. cess on (tax + surcharge).
  6. total rounded to the pack's ``rounding_tax`` multiple (s.288B style; see
     ``money.round_288``).

Scope (always repeated in the report assumptions): resident individual,
salary income only at slab rates, no capital-gains/special-rate income, no
HRA/80C/80D computation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from life_os.core.gate import BLOCK, SHIP, Check

from ..profile import (
    ASSUMPTION,
    ConsentRequired,
    Fact,
    Profile,
    Requirement,
    profile_gate,
)
from ..research import RulePack, RulePackBlocked, load_rule_pack, rule_status
from .inputs import InputValue, Raw, assumption_notes, input_checks, resolve
from .money import format_inr, quantize_paise, round_288
from .report import finalize, gate_reason_checks, rule_checks, rule_citations

__all__ = [
    "AGE_KEY",
    "GROSS_SALARY_KEY",
    "OTHER_DEDUCTIONS_KEY",
    "REGIMES",
    "TAX_PACK_PATH",
    "AgeExemption",
    "RegimeRules",
    "TaxBreakdown",
    "compute_tax",
    "financial_year_for",
    "financial_year_start",
    "income_tax",
    "income_tax_from_profile",
    "load_tax_pack",
    "regime_rules",
    "take_home",
]

TAX_PACK_PATH = Path(__file__).resolve().parent.parent / "rules" / "in_income_tax_fy2026_27.json"
REGIMES = ("new", "old")
MAX_PLAUSIBLE_AGE = 130

GROSS_SALARY_KEY = "gross_salary_annual"
AGE_KEY = "age"
OTHER_DEDUCTIONS_KEY = "other_deductions_annual"

TAX_SCOPE = (
    (
        "Resident individual assumed; the s.87A rebate and its marginal relief are available "
        "only to residents."
    ),
    (
        "Salary income only, taxed at normal slab rates; no capital-gains or other "
        "special-rate income is included."
    ),
    (
        "No HRA, 80C, 80D or other deduction is computed. Old regime: only the user-supplied "
        "other_deductions total is subtracted. New regime: only the standard deduction applies "
        "(employer NPS and similar are out of scope)."
    ),
    (
        "Total income is rounded to the pack's multiple (s.288A convention) and tax to the "
        "pack's multiple (s.288B convention): paise ignored, a last figure of 5 or more rounds up."
    ),
    (
        "Surcharge applies on total income above each band threshold with marginal relief; "
        "health and education cess applies on tax plus surcharge."
    ),
)
UNVERIFIED_NOTE = (
    "Rule values used are not human-verified (UNVERIFIED); check them against the cited "
    "sources before relying on them."
)
TAKE_HOME_SCOPE = (
    (
        "Professional tax and the employee EPF contribution are not computed; they are the "
        "user-supplied payroll_deductions (annual total)."
    ),
    (
        "Take-home = gross salary - income tax - payroll_deductions; employer contributions, "
        "reimbursements and bonuses not in gross salary are ignored."
    ),
)

_COMMON_IDS = ("surcharge_marginal_relief", "health_education_cess", "rounding_total_income", "rounding_tax")
_OLD_AGE_IDS = ("old_regime_exemption_senior_60_79", "old_regime_exemption_super_senior_80_plus")


# -- rules ------------------------------------------------------------------


@dataclass(frozen=True)
class AgeExemption:
    age_min: int
    age_max: int | None
    limit: Decimal


@dataclass(frozen=True)
class RegimeRules:
    regime: str
    slabs: tuple[tuple[Decimal, Decimal], ...]
    std_deduction: Decimal
    rebate_max: Decimal
    rebate_limit: Decimal
    rebate_marginal_relief: bool
    surcharge_bands: tuple[tuple[Decimal, Decimal], ...]
    surcharge_marginal_relief: bool
    cess_rate: Decimal
    income_rounding: Decimal
    tax_rounding: Decimal
    age_exemptions: tuple[AgeExemption, ...]
    rule_ids: tuple[str, ...]


def load_tax_pack(path: str | Path = TAX_PACK_PATH) -> RulePack:
    """Load the bundled FY2026-27 rule pack (raises RulePackBlocked if invalid)."""
    return load_rule_pack(path)


def _dec(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise RulePackBlocked([f"{label}: expected a number"])
    return value


def _rate(value: Any, label: str) -> Decimal:
    r = _dec(value, label)
    if not 0 <= r <= 1:
        raise RulePackBlocked([f"{label}: rate {r} outside 0..1"])
    return r


def _pairs(value: Any, label: str, *, first_zero: bool) -> tuple[tuple[Decimal, Decimal], ...]:
    if not isinstance(value, list) or not value:
        raise RulePackBlocked([f"{label}: expected a non-empty list of [amount, rate]"])
    out: list[tuple[Decimal, Decimal]] = []
    for i, pair in enumerate(value):
        if not isinstance(pair, list) or len(pair) != 2:
            raise RulePackBlocked([f"{label}[{i}]: expected [amount, rate]"])
        out.append((_dec(pair[0], f"{label}[{i}]"), _rate(pair[1], f"{label}[{i}]")))
    lowers = [lo for lo, _ in out]
    if lowers != sorted(set(lowers)):
        raise RulePackBlocked([f"{label}: amounts must be strictly ascending"])
    if first_zero and lowers[0] != 0:
        raise RulePackBlocked([f"{label}: first slab must start at 0"])
    return tuple(out)


def _get(pack: RulePack, rid: str) -> Any:
    try:
        return pack.get(rid).value
    except KeyError as exc:
        raise RulePackBlocked([f"rule pack {pack.pack_id!r} lacks required rule {rid!r}"]) from exc


def _obj(value: Any, label: str, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict) or any(k not in value for k in keys):
        raise RulePackBlocked([f"{label}: expected object with {keys}"])
    return value


def _flag(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise RulePackBlocked([f"{label}: expected true/false"])
    return value


def _whole(value: Any, label: str) -> int:
    d = _dec(value, label)
    if d != d.to_integral_value() or d < 0:
        raise RulePackBlocked([f"{label}: expected a non-negative whole number"])
    return int(d)


def regime_rules(pack: RulePack, regime: str) -> RegimeRules:
    """Parse and validate one regime's rules. Raises RulePackBlocked (-> BLOCK)."""
    if regime not in REGIMES:
        raise RulePackBlocked([f"unknown regime {regime!r}; expected one of {REGIMES}"])
    p = f"{regime}_regime_"
    ids = [p + "slabs", p + "standard_deduction", p + "rebate_87a", p + "surcharge_bands", *_COMMON_IDS]
    rebate = _obj(
        _get(pack, p + "rebate_87a"), p + "rebate_87a", ("max_rebate", "income_limit", "marginal_relief")
    )
    exemptions: list[AgeExemption] = []
    if regime == "old":
        ids.extend(_OLD_AGE_IDS)
        for rid in _OLD_AGE_IDS:
            ex = _obj(_get(pack, rid), rid, ("age_min", "age_max", "exemption_limit"))
            age_max = None if ex["age_max"] is None else _whole(ex["age_max"], rid)
            exemptions.append(
                AgeExemption(_whole(ex["age_min"], rid), age_max, _dec(ex["exemption_limit"], rid))
            )
    rounding_income = _dec(_get(pack, "rounding_total_income"), "rounding_total_income")
    rounding_tax = _dec(_get(pack, "rounding_tax"), "rounding_tax")
    for label, m in (("rounding_total_income", rounding_income), ("rounding_tax", rounding_tax)):
        if m <= 0 or m != m.to_integral_value():
            raise RulePackBlocked([f"{label}: must be a positive whole number"])
    return RegimeRules(
        regime=regime,
        slabs=_pairs(_get(pack, p + "slabs"), p + "slabs", first_zero=True),
        std_deduction=_dec(_get(pack, p + "standard_deduction"), p + "standard_deduction"),
        rebate_max=_dec(rebate["max_rebate"], p + "rebate_87a.max_rebate"),
        rebate_limit=_dec(rebate["income_limit"], p + "rebate_87a.income_limit"),
        rebate_marginal_relief=_flag(rebate["marginal_relief"], p + "rebate_87a.marginal_relief"),
        surcharge_bands=_pairs(_get(pack, p + "surcharge_bands"), p + "surcharge_bands", first_zero=False),
        surcharge_marginal_relief=_flag(_get(pack, "surcharge_marginal_relief"), "surcharge_marginal_relief"),
        cess_rate=_rate(_get(pack, "health_education_cess"), "health_education_cess"),
        income_rounding=rounding_income,
        tax_rounding=rounding_tax,
        age_exemptions=tuple(exemptions),
        rule_ids=tuple(ids),
    )


# -- pure computation --------------------------------------------------------


@dataclass(frozen=True)
class TaxBreakdown:
    taxable_income: Decimal
    slab_tax: Decimal
    rebate_87a: Decimal
    rebate_marginal_relief: Decimal
    tax_after_rebate: Decimal
    surcharge_rate: Decimal
    surcharge: Decimal
    surcharge_marginal_relief: Decimal
    tax_and_surcharge: Decimal
    cess: Decimal
    tax_before_rounding: Decimal
    total_tax: Decimal

    def as_dict(self) -> dict[str, Decimal]:
        return dict(self.__dict__)


def _slabs_for_age(rules: RegimeRules, age: int | None) -> tuple[tuple[Decimal, Decimal], ...]:
    if age is None or not rules.age_exemptions:
        return rules.slabs
    match = [e for e in rules.age_exemptions if e.age_min <= age and (e.age_max is None or age <= e.age_max)]
    if not match:
        return rules.slabs
    limit = max(match, key=lambda e: e.age_min).limit
    out: list[tuple[Decimal, Decimal]] = [(Decimal(0), Decimal(0))]
    for i, (lower, rate) in enumerate(rules.slabs):
        if rate == 0:
            continue
        upper = rules.slabs[i + 1][0] if i + 1 < len(rules.slabs) else None
        start = max(lower, limit)
        if upper is None or start < upper:
            out.append((start, rate))
    return tuple(out)


def _slab_tax(slabs: tuple[tuple[Decimal, Decimal], ...], income: Decimal) -> Decimal:
    tax = Decimal(0)
    for i, (lower, rate) in enumerate(slabs):
        if income <= lower:
            break
        upper = slabs[i + 1][0] if i + 1 < len(slabs) else income
        tax += (min(income, upper) - lower) * rate
    return tax


def _after_rebate(
    rules: RegimeRules, slabs: tuple[tuple[Decimal, Decimal], ...], income: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (slab_tax, rebate, marginal_relief)."""
    slab = _slab_tax(slabs, income)
    if income <= rules.rebate_limit:
        return slab, min(slab, rules.rebate_max), Decimal(0)
    excess = income - rules.rebate_limit
    if rules.rebate_marginal_relief and slab > excess:
        return slab, Decimal(0), slab - excess
    return slab, Decimal(0), Decimal(0)


def _band(rules: RegimeRules, income: Decimal) -> tuple[Decimal, Decimal] | None:
    above = [b for b in rules.surcharge_bands if income > b[0]]
    return above[-1] if above else None


def _tax_and_surcharge(
    rules: RegimeRules, slabs: tuple[tuple[Decimal, Decimal], ...], income: Decimal
) -> Decimal:
    slab, rebate, relief = _after_rebate(rules, slabs, income)
    base = slab - rebate - relief
    band = _band(rules, income)
    if band is None:
        return base
    threshold, rate = band
    total = base * (1 + rate)
    if rules.surcharge_marginal_relief:
        total = min(total, _tax_and_surcharge(rules, slabs, threshold) + (income - threshold))
    return total


def compute_tax(rules: RegimeRules, taxable_income: Decimal, *, age: int | None) -> TaxBreakdown:
    """Pure tax computation on (pre-rounding) taxable income. No I/O, no clock."""
    if taxable_income < 0:
        raise ValueError("taxable income cannot be negative")
    income = round_288(taxable_income, rules.income_rounding)
    slabs = _slabs_for_age(rules, age)
    slab, rebate, relief = _after_rebate(rules, slabs, income)
    base = slab - rebate - relief
    band = _band(rules, income)
    rate = band[1] if band else Decimal(0)
    with_sc = _tax_and_surcharge(rules, slabs, income)
    full_sc = base * rate
    surcharge = with_sc - base
    cess = with_sc * rules.cess_rate
    before = with_sc + cess
    return TaxBreakdown(
        taxable_income=income,
        slab_tax=slab,
        rebate_87a=rebate,
        rebate_marginal_relief=relief,
        tax_after_rebate=base,
        surcharge_rate=rate,
        surcharge=surcharge,
        surcharge_marginal_relief=full_sc - surcharge,
        tax_and_surcharge=with_sc,
        cess=cess,
        tax_before_rounding=before,
        total_tax=round_288(before, rules.tax_rounding),
    )


# -- tax year (LM-G21) --------------------------------------------------------

_FY = re.compile(r"FY(\d{4})-(\d{2})")


def financial_year_for(day: date) -> str:
    """Indian financial year (1 April - 31 March) containing *day*, e.g. 'FY2026-27'."""
    start = day.year if day.month >= 4 else day.year - 1
    return f"FY{start}-{(start + 1) % 100:02d}"


def financial_year_start(tax_year: str) -> date:
    """1 April of *tax_year* ('FY2026-27' -> 2026-04-01). ValueError if malformed."""
    m = _FY.fullmatch(tax_year) if isinstance(tax_year, str) else None
    if m is None or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
        raise ValueError(f"tax_year {tax_year!r} must look like 'FY2026-27'")
    return date(int(m.group(1)), 4, 1)


# -- report-producing API ----------------------------------------------------


@dataclass
class _Parts:
    inputs: list[InputValue]
    assumptions: list[str]
    result: dict[str, Any] | None
    checks: list[Check]
    rules_used: list[dict[str, Any]]
    tax_year: str = ""


def _fixed(disposition: str, name: str, reason: str) -> Check:
    return Check(name=name, fn=lambda: (disposition, reason))


def _ok(iv: InputValue) -> bool:
    return iv.error is None and iv.value is not None and (iv.allow_negative or iv.value >= 0)


def _tax_parts(
    pack: RulePack,
    *,
    as_of: date,
    regime: str,
    gross_salary: Raw,
    taxable_income: Raw,
    age: Raw,
    other_deductions: Raw,
    require_verified: bool,
    tax_year: str | None = None,
) -> _Parts:
    inputs: list[InputValue] = []
    checks: list[Check] = []
    assumptions = list(TAX_SCOPE)
    rules_used: list[dict[str, Any]] = []
    fy = tax_year if tax_year is not None else financial_year_for(as_of)
    try:
        fy_start = financial_year_start(fy)
    except ValueError as exc:
        checks.append(_fixed(BLOCK, "tax_year", str(exc)))
        return _Parts(inputs, assumptions, None, checks, rules_used, str(fy))
    assumptions.append(
        f"Tax year {fy} ({'given' if tax_year is not None else 'derived from as_of'}): rules "
        f"must be in effect on {fy_start.isoformat()}; their staleness is judged as of "
        f"{as_of.isoformat()}."
    )
    if regime not in REGIMES:
        checks.append(_fixed(BLOCK, "regime", f"unknown regime {regime!r}; use one of {REGIMES}"))
        return _Parts(inputs, assumptions, None, checks, rules_used, fy)
    checks.append(_fixed(SHIP, "regime", f"{regime} regime"))

    salary_mode = gross_salary is not None
    if salary_mode and taxable_income is not None:
        checks.append(
            _fixed(BLOCK, "income", "supply gross_salary OR taxable_income, not both (contradictory)")
        )
    income_iv: InputValue | None = None
    if salary_mode:
        income_iv = resolve("gross_salary", gross_salary)
    elif taxable_income is not None:
        income_iv = resolve("taxable_income", taxable_income)
    if income_iv is None:
        income_iv = resolve("gross_salary", None)
    inputs.append(income_iv)

    age_iv = resolve("age", age, integer=True)
    if regime == "old" or age is not None:
        inputs.append(age_iv)
    if age_iv.value is not None and age_iv.value > MAX_PLAUSIBLE_AGE:
        checks.append(_fixed(BLOCK, "age_range", "age is implausible (contradictory)"))
    if regime == "old" and age_iv.value is None:
        assumptions.append("age is required in the old regime: the basic exemption depends on it.")

    ded_iv: InputValue | None = None
    if other_deductions is not None:
        ded_iv = resolve("other_deductions", other_deductions)
        inputs.append(ded_iv)
        if regime == "new":
            checks.append(
                _fixed(BLOCK, "other_deductions", "the new regime does not accept a generic deductions total")
            )
        elif not salary_mode:
            checks.append(
                _fixed(BLOCK, "other_deductions", "deductions are already reflected in taxable_income")
            )
    elif regime == "old" and salary_mode:
        ded_iv = InputValue(
            "other_deductions",
            Decimal(0),
            ASSUMPTION,
            "not supplied; treated as no deductions beyond the standard deduction",
        )
        inputs.append(ded_iv)

    checks.extend(input_checks(inputs))
    assumptions.extend(assumption_notes(inputs))

    rules: RegimeRules | None = None
    try:
        rules = regime_rules(pack, regime)
    except RulePackBlocked as exc:
        for i, reason in enumerate(exc.reasons):
            checks.append(_fixed(BLOCK, f"rule_pack:{i}", reason))
    if rules is not None:
        checks.extend(
            rule_checks(pack, rules.rule_ids, as_of, require_verified=require_verified, effective_on=fy_start)
        )
        rules_used = rule_citations(pack, rules.rule_ids, as_of, effective_on=fy_start)
        if any(r["unverified"] for r in rules_used):
            assumptions.append(UNVERIFIED_NOTE)

    structural_block = any(c.fn()[0] == BLOCK for c in checks)
    # A rule not in effect for the tax year means the pack does not cover that year: no
    # result at all (a stale-only HOLD still computes, so the numbers can be explained).
    not_in_effect = rules is not None and any(
        not st.in_effect for st in rule_status(pack, as_of=as_of, ids=rules.rule_ids, effective_on=fy_start)
    )
    if rules is None or structural_block or not_in_effect or not all(_ok(iv) for iv in inputs):
        return _Parts(inputs, assumptions, None, checks, rules_used, fy)
    if regime == "old" and age_iv.value is None:
        return _Parts(inputs, assumptions, None, checks, rules_used, fy)

    assert income_iv.value is not None
    std = Decimal(0)
    deductions = Decimal(0)
    gross: Decimal | None = None
    if salary_mode:
        gross = income_iv.value
        std = min(gross, rules.std_deduction)
        deductions = ded_iv.value if ded_iv is not None and ded_iv.value is not None else Decimal(0)
        if deductions > gross - std:
            checks.append(
                _fixed(
                    BLOCK,
                    "deductions_exceed_income",
                    f"other_deductions {format_inr(deductions)} exceed salary after "
                    f"standard deduction {format_inr(gross - std)} (contradictory)",
                )
            )
            return _Parts(inputs, assumptions, None, checks, rules_used, fy)
        taxable = gross - std - deductions
    else:
        taxable = income_iv.value
    age_int = int(age_iv.value) if age_iv.value is not None else None
    bd = compute_tax(rules, taxable, age=age_int)
    result: dict[str, Any] = {
        "regime": regime,
        "gross_salary": gross,
        "standard_deduction": std if salary_mode else None,
        "other_deductions": deductions if salary_mode else None,
        **bd.as_dict(),
        # Total tax (incl. surcharge and cess) as a % of TAXABLE income - not of gross
        # salary; named explicitly so it is not mistaken for an "effective rate" on gross.
        "tax_as_percent_of_taxable_income": (
            quantize_paise(bd.total_tax / bd.taxable_income * 100) if bd.taxable_income else None
        ),
    }
    checks.append(_fixed(SHIP, "computed", f"total tax {format_inr(bd.total_tax)}"))
    result["tax_year"] = fy
    return _Parts(inputs, assumptions, result, checks, rules_used, fy)


def income_tax(
    pack: RulePack,
    *,
    as_of: date,
    regime: str,
    gross_salary: Raw = None,
    taxable_income: Raw = None,
    age: Raw = None,
    other_deductions: Raw = None,
    require_verified: bool = False,
    tax_year: str | None = None,
) -> dict[str, Any]:
    """Income tax report for one regime. Supply gross_salary OR taxable_income.

    ``tax_year`` ('FY2026-27') selects which rules must be in effect; default: the
    financial year containing ``as_of``. ``as_of`` alone decides rule staleness.
    """
    parts = _tax_parts(
        pack,
        as_of=as_of,
        regime=regime,
        gross_salary=gross_salary,
        taxable_income=taxable_income,
        age=age,
        other_deductions=other_deductions,
        require_verified=require_verified,
        tax_year=tax_year,
    )
    return finalize(
        "income_tax",
        inputs=parts.inputs,
        assumptions=parts.assumptions,
        result=parts.result,
        checks=parts.checks,
        rules_used=parts.rules_used,
        extra={"as_of": as_of.isoformat(), "tax_year": parts.tax_year},
    )


def take_home(
    pack: RulePack,
    *,
    as_of: date,
    regime: str,
    gross_salary: Raw,
    payroll_deductions: Raw = None,
    age: Raw = None,
    other_deductions: Raw = None,
    require_verified: bool = False,
    tax_year: str | None = None,
) -> dict[str, Any]:
    """Annual and monthly take-home after income tax and user-supplied payroll deductions."""
    parts = _tax_parts(
        pack,
        as_of=as_of,
        regime=regime,
        gross_salary=gross_salary,
        taxable_income=None,
        age=age,
        other_deductions=other_deductions,
        require_verified=require_verified,
        tax_year=tax_year,
    )
    pd_iv = resolve("payroll_deductions", payroll_deductions)
    inputs = [*parts.inputs, pd_iv]
    checks = [*parts.checks, *input_checks([pd_iv])]
    assumptions = [*parts.assumptions, *TAKE_HOME_SCOPE, *assumption_notes([pd_iv])]
    result: dict[str, Any] | None = None
    if parts.result is not None and _ok(pd_iv):
        assert pd_iv.value is not None
        gross = parts.result["gross_salary"]
        tax = parts.result["total_tax"]
        annual = gross - tax - pd_iv.value
        if annual < 0:
            checks.append(
                _fixed(
                    BLOCK, "take_home_negative", "payroll_deductions exceed salary after tax (contradictory)"
                )
            )
        else:
            result = {
                "regime": regime,
                "gross_annual": gross,
                "income_tax": tax,
                "payroll_deductions": pd_iv.value,
                "take_home_annual": annual,
                "take_home_monthly": quantize_paise(annual / 12),
                "tax_breakdown": parts.result,
            }
    return finalize(
        "take_home",
        inputs=inputs,
        assumptions=assumptions,
        result=result,
        checks=checks,
        rules_used=parts.rules_used,
        extra={"as_of": as_of.isoformat(), "tax_year": parts.tax_year},
    )


def income_tax_from_profile(
    profile: Profile,
    pack: RulePack,
    *,
    as_of: date,
    regime: str,
    require_verified: bool = False,
    tax_year: str | None = None,
) -> dict[str, Any]:
    """Income tax from consented profile facts; adds the questions that clear a HOLD.

    Facts: ``gross_salary_annual`` (finance), ``age`` (identity; old regime) and
    optional ``other_deductions_annual`` (finance; old regime).
    """
    reqs = [
        Requirement(
            GROSS_SALARY_KEY,
            "finance",
            "What is your gross annual salary before tax, and how do you know it?",
        )
    ]
    if regime == "old":
        reqs.append(
            Requirement(
                AGE_KEY, "identity", "What is your age? The old-regime basic exemption depends on it."
            )
        )
    outcome = profile_gate(profile, reqs, allow_assumptions=True)
    questions = outcome.questions

    def fetch(key: str) -> Fact | None:
        try:
            return profile.get(key)
        except ConsentRequired:
            return None

    parts = _tax_parts(
        pack,
        as_of=as_of,
        regime=regime,
        gross_salary=fetch(GROSS_SALARY_KEY)
        or Fact(GROSS_SALARY_KEY, None, "finance", "UNKNOWN", "not in profile", "-"),
        taxable_income=None,
        age=fetch(AGE_KEY) if regime == "old" else None,
        other_deductions=fetch(OTHER_DEDUCTIONS_KEY) if regime == "old" else None,
        require_verified=require_verified,
        tax_year=tax_year,
    )
    # Carry the profile gate's own HOLD/BLOCK reasons (e.g. a category mismatch BLOCKs).
    checks = [
        *parts.checks,
        *gate_reason_checks(
            {**r, "check": f"profile:{r['check']}"} for r in outcome.gate.reasons if r["disposition"] != SHIP
        ),
    ]
    return finalize(
        "income_tax",
        inputs=parts.inputs,
        assumptions=parts.assumptions,
        result=parts.result,
        checks=checks,
        rules_used=parts.rules_used,
        extra={"questions": questions, "as_of": as_of.isoformat(), "tax_year": parts.tax_year},
    )
