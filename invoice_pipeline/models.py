"""Pydantic models and enums shared across the pipeline."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class IngestStatus(str, Enum):
    OK = "ok"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


class RejectionReason(str, Enum):
    FILE_TOO_LARGE = "file_too_large"
    UNSUPPORTED_TYPE = "unsupported_type"
    CORRUPT_FILE = "corrupt_file"
    ENCRYPTED_PDF = "encrypted_pdf"
    TOO_MANY_PAGES = "too_many_pages"
    RENDER_FAILED = "render_failed"


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


class ExtractionStatus(str, Enum):
    OK = "ok"
    DEFERRED = "deferred"   # API unavailable/out of quota: file is fine, try again later
    FAILED = "failed"       # model answered but never with valid output


class PageInfo(BaseModel):
    page_no: int  # 1-based
    path: str     # JPEG on disk; base64-encoded only at the LLM call
    mime: str = "image/jpeg"
    width: int
    height: int


class PipelineState(BaseModel):
    """LangGraph state shared by all nodes. Each node adds its own fields."""

    # inputs
    job_id: int
    source_path: str

    # ingest outputs
    file_id: Optional[str] = None            # == sha256
    original_filename: Optional[str] = None
    detected_mime: Optional[str] = None
    size_bytes: Optional[int] = None
    page_count: Optional[int] = None
    pages: list[PageInfo] = Field(default_factory=list)
    text_layer: Optional[list[Optional[str]]] = None  # per page; PDFs with digital text only

    ingest_status: Optional[IngestStatus] = None
    rejection_reason: Optional[RejectionReason] = None
    rejection_detail: Optional[str] = None
    duplicate_of: Optional[int] = None       # job_id of the earlier successful job

    # extraction outputs (profile-agnostic: invoices are plain JSON dicts)
    extraction_profile: Optional[str] = None
    invoices: list[dict[str, Any]] = Field(default_factory=list)
    extraction_status: Optional[ExtractionStatus] = None
    extraction_model: Optional[str] = None
    extraction_id: Optional[int] = None
    extraction_cached: bool = False
    extraction_error: Optional[str] = None
    retry_after: Optional[str] = None        # ISO time, set when deferred

    # validation outputs: one dict per invoice (see validation.types.InvoiceValidation)
    validations: list[dict[str, Any]] = Field(default_factory=list)
    autopassed_ids: list[int] = Field(default_factory=list)   # invoice_ids, set by the route nodes
    review_ids: list[int] = Field(default_factory=list)
    rejected_ids: list[int] = Field(default_factory=list)
    exported_files: list[str] = Field(default_factory=list)  # set by the export node


IngestState = PipelineState  # backwards-compatible name


class IngestRejection(Exception):
    """Raised by ingest steps for deterministic, non-retryable failures."""

    def __init__(self, reason: RejectionReason, detail: str = ""):
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason
        self.detail = detail
