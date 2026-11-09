"""PII redaction for logs and journals (LM02).

``redact(text)`` NFKC-normalises the text (so NBSP, full-width digits and other
compatibility forms collapse to plain ASCII), then masks, in this order:

  - email addresses                          -> ``[EMAIL]``
  - Indian PAN (``ABCDE1234F``)              -> ``[PAN]``
  - digit runs of 9+ digits, allowing up to two separator characters
    (space, tab, ``.``, ``-``, ``/``) or a spaced dash between digits (never a
    newline), classified by count:
      * ``+91`` / ``91`` prefix + 10-digit mobile, 10-digit mobile starting
        6-9, or ``0`` + 10-digit mobile      -> ``[PHONE]``
      * exactly 12 digits                    -> ``[AADHAAR]``
      * anything else with 9+ digits         -> ``[ACCOUNT]`` (bank/card numbers)

Inside ``http(s)://`` URLs the PATH is left alone for digit runs (citations carry
ISBNs and document ids such as ``/item/9789240015128``); emails/PANs are still
masked there. In the query string and fragment, where personal data travels, only
a single ``-`` may join digits and 9+-digit runs are masked (SPEC-GAP LM-G34).

Redaction is deliberately aggressive: a false positive (masking a long
non-sensitive number) is cheap, a false negative leaks PII. If nothing is
masked the ORIGINAL text is returned unchanged (normalisation alone never
alters text). Placeholders contain no digits, so ``redact`` is idempotent.
Dates (``2026-10-11``) and ordinary amounts (< 9 digits, or comma-grouped)
are left alone.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["redact"]

# \w (Unicode) so internationalised domains (a@例え.jp) and apostrophes (o'brien@) match.
_EMAIL = re.compile(r"[\w.%+'\-]+@[\w\-]+(?:\.[\w\-]+)+")
_PAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Za-z0-9])", re.IGNORECASE)
# A run of 9+ digits, optionally with a leading "+", allowing up to two
# separator characters between digits. Not glued to letters/digits on either side.
# Separators: up to two of space/tab/./-// , or a spaced dash ("98765 - 43210").
# Newlines never join digits (a list bullet after an amount is not a phone number).
_DIGIT_RUN = re.compile(r"(?<![A-Za-z0-9+])\+?\d(?:(?:[ \t.\-/]{0,2}|[ \t]-[ \t])\d){8,}(?![A-Za-z0-9])")
# One or more ISO dates joined by separators: not an identifier.
_DATES_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}(?:[\s./\-]{1,3}\d{4}-\d{2}-\d{2})*")
# Hosts whose URL PATH is a phone number.
_PHONE_PATH_HOSTS = ("wa.me/", "api.whatsapp.com/")
# Inside URLs: only a single hyphen may join digits.
_DIGIT_RUN_URL = re.compile(r"(?<![A-Za-z0-9+])\+?\d(?:-?\d){8,}(?![A-Za-z0-9])")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)


def _classify(match: re.Match[str]) -> str:
    raw = match.group(0)
    if _DATES_ONLY.fullmatch(raw):
        return raw
    digits = re.sub(r"\D", "", raw)
    n = len(digits)
    mobile = re.compile(r"[6-9]\d{9}")
    if n == 10 and mobile.fullmatch(digits):
        return "[PHONE]"
    if n == 11 and digits.startswith("0") and mobile.fullmatch(digits[1:]):
        return "[PHONE]"
    if (
        n == 12
        and digits.startswith("91")
        and mobile.fullmatch(digits[2:])
        and (raw.startswith("+") or raw[2:3] in (" ", "-"))
    ):
        return "[PHONE]"
    if n == 12:
        return "[AADHAAR]"
    return "[ACCOUNT]"


def redact(text: str) -> str:
    """Return *text* with PAN/Aadhaar/phone/email/account numbers masked."""
    if not isinstance(text, str):
        raise TypeError("redact() expects str")
    norm = unicodedata.normalize("NFKC", text)
    pieces: list[str] = []
    pos = 0
    for url in _URL.finditer(norm):
        raw_url = url.group(0)
        cut = min((i for i in (raw_url.find("?"), raw_url.find("#")) if i >= 0), default=len(raw_url))
        pieces.append(_mask(norm[pos : url.start()], _DIGIT_RUN))
        pieces.append(_mask_url_path(raw_url[:cut]))
        # The query/fragment is masked TOGETHER with the text after the URL, so a
        # number split by a space at the URL's end ("?phone=98765 43210") is still
        # seen as one run.
        pos = url.start() + cut
        if cut < len(raw_url):
            tail_end = _next_url_start(norm, url.end())
            pieces.append(_mask(norm[pos:tail_end], _DIGIT_RUN))
            pos = tail_end
    pieces.append(_mask(norm[pos:], _DIGIT_RUN))
    out = "".join(pieces)
    return text if out == norm else out


def _next_url_start(text: str, start: int) -> int:
    m = _URL.search(text, start)
    return m.start() if m else len(text)


def _mask_url_path(path: str) -> str:
    """Path keeps numeric identifiers (ISBNs, document ids) except on phone-link hosts."""
    masked = _PAN.sub("[PAN]", _EMAIL.sub("[EMAIL]", path))
    if any(h in masked.casefold() for h in _PHONE_PATH_HOSTS):
        return _DIGIT_RUN_URL.sub(_classify, masked)
    return masked


def _mask(segment: str, digit_run: re.Pattern[str]) -> str:
    out = _EMAIL.sub("[EMAIL]", segment)
    out = _PAN.sub("[PAN]", out)
    return digit_run.sub(_classify, out)
