"""Centralized guardrails configuration.

Reads guardrails env vars once at import time. Other modules import ``config``
from here instead of scattering os.getenv() calls.
"""

import os
from dataclasses import dataclass


DEFAULT_LLM_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class GuardrailsConfig:
    enabled: bool
    sigma_enabled: bool
    llm_model: str
    llm_timeout_seconds: float


_FALSY = {"false", "0", "no", "off", "disabled"}


def _load() -> GuardrailsConfig:
    on = os.getenv("GUARDRAILS_ENABLED", "true").lower() not in _FALSY
    try:
        llm_timeout = float(
            os.getenv("GUARDRAILS_LLM_TIMEOUT_SECONDS", DEFAULT_LLM_TIMEOUT_SECONDS)
        )
    except ValueError:
        llm_timeout = DEFAULT_LLM_TIMEOUT_SECONDS
    return GuardrailsConfig(
        enabled=on,
        sigma_enabled=on and os.getenv("GUARDRAILS_SIGMA_ENABLED", "true").lower() not in _FALSY,
        llm_model=os.getenv("GUARDRAILS_LLM_MODEL", ""),
        llm_timeout_seconds=llm_timeout,
    )


config = _load()
