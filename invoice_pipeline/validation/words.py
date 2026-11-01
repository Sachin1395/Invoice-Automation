"""'Four Hundred Sixty-eight only' -> Decimal('468'). Indian number words (lakh, crore) supported."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 20 + 10 * i for i, w in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split())}
_SCALES = {"thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
           "crore": 10_000_000, "crores": 10_000_000, "million": 1_000_000}
_IGNORE = {"rupees", "rupee", "rs", "inr", "only", "indian", "of", "and"}


def _number(tokens: list[str]) -> Optional[int]:
    total = current = 0
    seen = False
    for w in tokens:
        if w in _IGNORE:
            continue
        seen = True
        if w in _UNITS:
            current += _UNITS[w]
        elif w in _TENS:
            current += _TENS[w]
        elif w == "hundred":
            current = (current or 1) * 100
        elif w in _SCALES:
            total += (current or 1) * _SCALES[w]
            current = 0
        else:
            return None  # a word we don't understand: better to skip the check than guess
    return total + current if seen else None


def words_to_amount(text: Optional[str]) -> Optional[tuple[Decimal, bool]]:
    """Returns (amount, has_paise) or None when the text can't be understood."""
    if not text:
        return None
    t = text.lower().replace("-", " ").replace(",", " ").replace(".", " ")
    tokens = t.split()
    if not tokens:
        return None
    paise_idx = next((i for i, w in enumerate(tokens) if w in ("paise", "paisa")), None)
    if paise_idx is None:
        rupees = _number(tokens)
        return None if rupees is None else (Decimal(rupees), False)
    split = max((i for i, w in enumerate(tokens[:paise_idx]) if w == "and"), default=None)
    if split is None:
        return None
    rupees, paise = _number(tokens[:split]), _number(tokens[split + 1:paise_idx])
    if rupees is None or paise is None:
        return None
    return Decimal(rupees) + Decimal(paise) / 100, True
