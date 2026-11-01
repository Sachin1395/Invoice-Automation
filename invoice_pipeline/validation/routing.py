"""The routing decision. Kept separate and small so the policy is easy to read and change."""
from __future__ import annotations

from typing import Optional

from ..config import Settings
from .arithmetic import D, total_confirmed
from .bank import BankMatch
from .types import Flag, Route, Severity


def is_low_risk(inv: dict, settings: Settings, expected_currency: str = "INR") -> bool:
    grand = D(inv.get("grand_total"))
    return (grand is not None and inv.get("currency") == expected_currency
            and grand <= D(settings.low_risk_limit))


def decide_route(
    inv: dict, flags: list[Flag], confirmations: list[str], score: float,
    bank_match: Optional[BankMatch], settings: Settings, expected_currency: str = "INR",
) -> tuple[Route, list[str], bool]:
    """Returns (route, reasons it is not an autopass, low_risk)."""
    low_risk = is_low_risk(inv, settings, expected_currency)

    if any(f.severity == Severity.BLOCKER for f in flags):
        return Route.HARD_REJECT, [f.message for f in flags if f.severity == Severity.BLOCKER], low_risk

    reasons = [f.message for f in flags if f.severity == Severity.MAJOR]

    if not total_confirmed(confirmations):
        reasons.append("The grand total could not be independently verified (rows, formula or amount in words)")

    # Trust gate: a cleared bank charge, or small enough that a mistake is cheap.
    if bank_match is None and not low_risk:
        reasons.append(f"High value ({inv.get('grand_total')} {inv.get('currency')}, above the "
                       f"{settings.low_risk_limit:g} limit) and no matching cleared bank charge")

    minimum = settings.autopass_min_low_risk if (low_risk and bank_match is None) else settings.autopass_min
    if score < minimum:
        reasons.append(f"Confidence {score:.2f} is below the {minimum:.2f} needed to autopass")

    return (Route.HUMAN_REVIEW if reasons else Route.AUTOPASS), reasons, low_risk
