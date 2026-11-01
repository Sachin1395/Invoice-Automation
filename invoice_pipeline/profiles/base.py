"""An extraction profile = schema + versioned prompt. New customer/country = new profile."""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class Profile:
    name: str
    prompt_path: Path
    prompt_version: str          # stored with every extraction; bump when the prompt changes
    result_schema: type[BaseModel]
    rules: Any = None            # module with REQUIRED, vendor_id(), sanity_checks(); see india_gst/rules.py

    def load_prompt(self) -> str:
        return self.prompt_path.read_text(encoding="utf-8")


def load_profile(name: str) -> Profile:
    try:
        module = importlib.import_module(f"invoice_pipeline.profiles.{name}")
    except ModuleNotFoundError as exc:
        raise ValueError(f"unknown extraction profile: {name!r}") from exc
    return module.PROFILE
