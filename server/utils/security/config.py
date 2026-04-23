"""Centralized guardrails configuration.

Reads all GUARDRAILS_* env vars once at import time. Other modules import
``config`` from here instead of scattering os.getenv() calls.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class GuardrailsConfig:
    enabled: bool
    signature_check: bool
    llm_judge: bool
    input_rail: bool
    llm_model: str
    llm_fail_mode: str
    llm_base_url: str
    llm_api_key: str


def _load() -> GuardrailsConfig:
    on = os.getenv("GUARDRAILS_ENABLED", "false").lower() == "true"
    fail_mode = os.getenv("GUARDRAILS_LLM_FAIL_MODE", "open").strip().lower()
    if fail_mode not in {"open", "closed"}:
        fail_mode = "open"
    return GuardrailsConfig(
        enabled=on,
        signature_check=on and os.getenv("GUARDRAILS_SIGNATURE_CHECK", "true").lower() != "false",
        llm_judge=on and os.getenv("GUARDRAILS_LLM_JUDGE", "true").lower() != "false",
        input_rail=on and os.getenv("GUARDRAILS_INPUT_RAIL", "true").lower() != "false",
        llm_model=os.getenv("GUARDRAILS_LLM_MODEL", ""),
        llm_fail_mode=fail_mode,
        llm_base_url=os.getenv("GUARDRAILS_LLM_BASE_URL", ""),
        llm_api_key=os.getenv("GUARDRAILS_LLM_API_KEY", ""),
    )


config = _load()
