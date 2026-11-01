"""Bank verification hook.

A cleared bank charge is independent proof that an invoice was really paid. Matching bank
statements to invoices belongs to the reconciliation stage (including one payment that covers
several invoices); until it exists the default verifier finds nothing, so only the low-risk
limit lets an invoice autopass.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class BankMatch:
    reference: str


class BankVerifier(Protocol):
    def find_match(self, invoice: dict) -> Optional[BankMatch]: ...


class NoBankData:
    def find_match(self, invoice: dict) -> Optional[BankMatch]:
        return None
