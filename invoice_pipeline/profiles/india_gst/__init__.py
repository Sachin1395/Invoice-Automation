"""India GST extraction profile."""
from pathlib import Path

from ..base import Profile
from . import rules
from .schema import ExtractionResult

PROFILE = Profile(
    name="india_gst",
    prompt_path=Path(__file__).parent / "prompt_v3.md",
    prompt_version="india_gst/v3",
    result_schema=ExtractionResult,
    rules=rules,
)
