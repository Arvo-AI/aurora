"""Centralized guardrails configuration.

Reads guardrails env vars once at import time. Other modules import ``config``
from here instead of scattering os.getenv() calls.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class GuardrailsConfig:
    enabled: bool
    llm_model: str


def _load() -> GuardrailsConfig:
    return GuardrailsConfig(
        enabled=os.getenv("GUARDRAILS_ENABLED", "true").lower() != "false",
        llm_model=os.getenv("GUARDRAILS_LLM_MODEL", ""),
    )


config = _load()
