"""Agent interpretation of free-form Startup Check facts.

Users can add arbitrary name/value facts beyond the structured form. A cheap
RCA-tier model interprets each fact into the known context schema — or
REFUSES it with a reason (gibberish, vague, or nothing any check uses).
Interpreted facts never generate findings directly: they only feed the same
deterministic rules the form does, so the low-false-positive discipline holds.

Fail-closed everywhere: if the LLM errors, times out, or returns something
that fails context validation, every custom fact is refused and the scan
continues on the structured form data alone.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Input budgets — refuse deterministically before spending an LLM call.
MAX_FACTS = 10
MAX_FACT_CHARS = 300

# The fact interpreter is a tiny classification job, not an RCA. Strategy is
# premium-first with a free fallback: those who can pay for the RCA-tier
# model get it, and when it is exhausted (quota, key limits) the check
# degrades to a FREE model instead of refusing facts.
# - STARTUP_CHECK_FACT_MODEL set  -> use exactly that model, no fallback.
# - otherwise                     -> [RCA_MODEL, FREE_FALLBACK_MODEL].
# Free models churn — when the fallback slug dies too, scans refuse facts
# (fail-closed) until the env var points at a live model.
FREE_FALLBACK_MODEL = "nvidia/nemotron-3.5-lightning:free"


def _model_chain() -> List[str]:
    """Ordered models to try for fact interpretation, premium first."""
    explicit = os.getenv("STARTUP_CHECK_FACT_MODEL")
    if explicit:
        return [explicit]
    try:
        from chat.backend.agent.llm import ModelConfig

        premium = ModelConfig.RCA_MODEL
    except Exception:  # noqa: BLE001 — config import must never break scans
        logger.warning("[STARTUP-CHECK] ModelConfig unavailable; free model only")
        return [FREE_FALLBACK_MODEL]
    return [premium, FREE_FALLBACK_MODEL]

# Standard reason used whenever the fail-closed path fires.
_FAIL_CLOSED_REASON = "the agent could not interpret custom facts right now"

_SCHEMA_HINT = """Structured fields you may map facts onto (never invent values):

- nodes: [{"name": str, "cpu_cores": number, "memory_mb": number}] — node machine sizes
- service_memory_mb: {"service-name": megabytes} — measured/typical memory a container uses
- storage_classes: ["name", ...] — StorageClasses that exist in the cluster
- default_storage_class: true/false — whether the cluster has a default StorageClass
- cluster_domain: "cluster.local" — the cluster's DNS domain
- known_services: ["name", ...] — Services that legitimately live outside this repo
- known_configmaps: ["name", ...] — ConfigMaps that live outside this repo
- known_secrets: ["name", ...] — Secrets that live outside this repo (e.g. managed by Vault)
- known_workloads: ["name", ...] — workloads deployed from elsewhere"""


class _LLMNode(BaseModel):
    name: str = ""
    cpu_cores: float = 0
    memory_mb: float = 0


class _LLMDecision(BaseModel):
    """Per-fact verdict: 1-based index into the prompt's fact list."""

    index: int
    action: str  # "mapped" or "refused"
    reason: str = ""


class ParsedFacts(BaseModel):
    """Structured-output contract for the interpreting model."""

    nodes: List[_LLMNode] = Field(default_factory=list)
    service_memory_mb: Dict[str, float] = Field(default_factory=dict)
    storage_classes: List[str] = Field(default_factory=list)
    default_storage_class: Optional[bool] = None
    cluster_domain: Optional[str] = None
    known_services: List[str] = Field(default_factory=list)
    known_configmaps: List[str] = Field(default_factory=list)
    known_secrets: List[str] = Field(default_factory=list)
    known_workloads: List[str] = Field(default_factory=list)
    decisions: List[_LLMDecision] = Field(default_factory=list)


def _sanitize_fact(raw: Any) -> Optional[Dict[str, str]]:
    """Shape-check one fact. Returns {'name','value'} or None if unusable."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name", ""))[:MAX_FACT_CHARS].strip()
    value = str(raw.get("value", ""))[:MAX_FACT_CHARS].strip()
    if not name and not value:
        return None
    return {"name": name, "value": value}


def _fail_closed(
    facts: List[Dict[str, str]], reason: str = _FAIL_CLOSED_REASON
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    refusals = [{"name": f.get("name") or f.get("value") or "?", "reason": reason} for f in facts]
    return {}, refusals


def _build_prompt(facts: List[Dict[str, str]]) -> str:
    lines = [
        "You interpret free-form environment facts for a Kubernetes repository check.",
        "For EACH fact below decide exactly one of:",
        "1. MAP it onto a structured field (only if you are confident about both the",
        "   meaning and the value — never guess or invent numbers), or",
        "2. REFUSE it with a short reason.",
        "",
        "Refuse anything that is gibberish, too vague to act on, or a true fact that",
        "simply no check can use (cloud provider, region, team names, traffic, cost,",
        "deadlines, opinions).",
        "",
        "You must record one decision per fact (1-based index): action \"mapped\"",
        "if you mapped it, or \"refused\" with a short reason.",
        "",
        _SCHEMA_HINT,
        "",
        "Facts:",
    ]
    for i, f in enumerate(facts, 1):
        label = f.get("name") or "(unnamed)"
        lines.append(f"{i}. {label}: {f.get('value', '')}")
    return "\n".join(lines)


def _to_context_dict(parsed: ParsedFacts) -> Dict[str, Any]:
    """Pydantic result -> raw context dict for parse_user_context()."""
    ctx: Dict[str, Any] = {}
    nodes = [
        {"name": n.name, "cpu_cores": n.cpu_cores, "memory_mb": n.memory_mb}
        for n in parsed.nodes
        if n.cpu_cores > 0 and n.memory_mb > 0
    ]
    if nodes:
        ctx["nodes"] = nodes
    if parsed.service_memory_mb:
        ctx["service_memory_mb"] = dict(parsed.service_memory_mb)
    if parsed.storage_classes:
        ctx["storage_classes"] = list(parsed.storage_classes)
    if parsed.default_storage_class is not None:
        ctx["default_storage_class"] = parsed.default_storage_class
    if parsed.cluster_domain:
        ctx["cluster_domain"] = parsed.cluster_domain
    if parsed.known_services:
        ctx["known_services"] = list(parsed.known_services)
    if parsed.known_configmaps:
        ctx["known_configmaps"] = list(parsed.known_configmaps)
    if parsed.known_secrets:
        ctx["known_secrets"] = list(parsed.known_secrets)
    if parsed.known_workloads:
        ctx["known_workloads"] = list(parsed.known_workloads)
    return ctx


def _track_usage(
    user_id: str, prompt: str, raw_response: Any, start_time: float, model_name: str
) -> None:
    """Best-effort usage tracking — never fails the scan."""
    try:
        from chat.backend.agent.utils.llm_usage_tracker import LLMUsageTracker
        import os

        LLMUsageTracker.track_llm_call(
            user_id=user_id,
            session_id=None,
            model_name=model_name,
            request_type="startup_check_context",
            prompt=prompt,
            response=raw_response,
            start_time=start_time,
            api_provider=os.getenv("LLM_PROVIDER_MODE", "direct"),
        )
    except Exception:  # noqa: BLE001 — tracking must never break the scan
        logger.info("[STARTUP-CHECK] context LLM usage tracking failed", exc_info=True)


def _interpret_once(prompt: str, model_name: str) -> Tuple[Optional[ParsedFacts], Any]:
    """One structured-output interpretation attempt. Raises on any API
    failure; returns (parsed_or_None, raw_response)."""
    from chat.backend.agent.providers import create_chat_model

    model = create_chat_model(model_name, temperature=0, streaming=False)
    interpreter = model.with_structured_output(
        ParsedFacts, include_raw=True, method="function_calling"
    )
    result = interpreter.invoke(prompt)
    return result.get("parsed"), result.get("raw")


def parse_freeform_facts(
    user_id: str, facts: List[Dict[str, str]]
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Interpret free-form facts into context keys, refusing the rest.

    Returns ``(context_dict, refusals)``. The context_dict is raw (unvalidated)
    and must be merged with the form context before ``parse_user_context()``.
    Refusals are ``[{"name": ..., "reason": ...}]`` for display in the UI.
    """
    clean: List[Dict[str, str]] = []
    refusals: List[Dict[str, str]] = []
    for raw in facts:
        fact = _sanitize_fact(raw)
        if fact is None:
            continue
        if len(clean) >= MAX_FACTS:
            label = fact.get("name") or fact.get("value") or "?"
            refusals.append({"name": label, "reason": f"too many facts (limit {MAX_FACTS})"})
            continue
        clean.append(fact)
    if not clean:
        return {}, refusals

    prompt = _build_prompt(clean)
    start_time = time.time()

    # Premium first, free fallback: each failure (quota, 403/404, unparsable
    # output) moves to the next model. Only when every model fails do we
    # refuse the facts — the scan itself never fails here.
    parsed: Optional[ParsedFacts] = None
    raw_response = None
    used_model: Optional[str] = None
    for model_name in _model_chain():
        try:
            parsed, raw_response = _interpret_once(prompt, model_name)
            used_model = model_name
            if parsed is not None:
                break
            logger.warning(
                "[STARTUP-CHECK] %s returned unparsable output; trying next model",
                model_name,
            )
        except Exception as exc:  # noqa: BLE001 — one bad model must not end the chain
            logger.warning(
                "[STARTUP-CHECK] fact model %s failed (%s); trying next model",
                model_name, str(exc)[:200],
            )
    if user_id and used_model:
        _track_usage(user_id, prompt, raw_response, start_time, used_model)
    if parsed is None:
        return _fail_closed(clean)

    ctx = _to_context_dict(parsed)

    # Every fact must be accounted for by a decision. Anything the model
    # didn't decide on — or decided ambiguously — is refused, never silently
    # accepted.
    decided: Dict[int, _LLMDecision] = {}
    for d in parsed.decisions:
        if 1 <= d.index <= len(clean):
            decided[d.index] = d
    for i, fact in enumerate(clean, 1):
        label = fact.get("name") or fact.get("value") or "?"
        decision = decided.get(i)
        if decision is None:
            refusals.append({"name": label, "reason": "not interpreted by the agent"})
        elif decision.action != "mapped":
            refusals.append(
                {"name": label, "reason": decision.reason or "refused by the agent"}
            )

    # Post-validate what the LLM accepted through the SAME gate as the form.
    if ctx:
        from services.startup_check.context import (
            ContextValidationError,
            parse_user_context,
        )

        try:
            parse_user_context(ctx)
        except ContextValidationError as exc:
            logger.warning("[STARTUP-CHECK] LLM context failed validation: %s", exc)
            return _fail_closed(clean, f"interpreted facts failed validation: {exc}")

    return ctx, refusals
