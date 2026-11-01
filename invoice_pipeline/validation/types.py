"""Shared types for the validation node."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    BLOCKER = "blocker"   # the invoice must not proceed (exact duplicate, not an invoice)
    MAJOR = "major"       # a human has to look
    MINOR = "minor"       # worth recording; does not change the route by itself


class Route(str, Enum):
    AUTOPASS = "autopass"
    HUMAN_REVIEW = "human_review"
    HARD_REJECT = "hard_reject"


class Flag(BaseModel):
    code: str
    severity: Severity
    message: str                                   # written for the person doing the review
    fields: list[str] = Field(default_factory=list)  # confidence fields this flag casts doubt on
    related_invoice_id: Optional[int] = None       # the earlier invoice, for duplicate flags


class InvoiceValidation(BaseModel):
    index: int                                     # position of the invoice inside its file (-1 = whole file)
    invoice_id: Optional[int] = None               # set once the row is stored
    route: Route
    score: float
    field_confidence: dict[str, Optional[float]] = Field(default_factory=dict)
    flags: list[Flag] = Field(default_factory=list)
    confirmations: list[str] = Field(default_factory=list)  # independent evidence that passed
    reasons: list[str] = Field(default_factory=list)        # why it is not an autopass
    computed_subtotal: Optional[str] = None
    duplicate_of: Optional[int] = None
    is_scan: bool = False
    low_risk: bool = False
    bank_match: Optional[str] = None


class ArithResult(BaseModel):
    flags: list[Flag] = Field(default_factory=list)
    confirmations: list[str] = Field(default_factory=list)
    computed_subtotal: Optional[str] = None
    normalized_invoice: Optional[dict] = None   # set when tax-inclusive rows were rewritten; this is what gets stored
