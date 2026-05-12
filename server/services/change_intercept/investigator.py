"""Standalone investigator for the change-intercept pipeline.

For Phase 1a Part 2 the investigator is intentionally simpler than the
incident-RCA agent: a single LLM call against the stored snapshot, no
tool loop, no conversation history. Rationale:

  - The snapshot already carries everything the investigator needs to
    spot the Phase 1a risk taxonomy (memory leaks, missing timeouts,
    unbounded retries, etc. are all visible in the diff itself).
  - Tool-calling adds latency and variance during calibration — we
    want the severity distribution to be a function of the prompt,
    not of unpredictable tool latencies.
  - A single structured-JSON output is dramatically easier to
    validate than a multi-turn transcript.

The agentic RCA-toolset variant from the design doc is deferred to a
Part 2.5 milestone, triggered only if calibration shows certain
findings systematically need cross-service signal (Datadog health,
recent deploys, postmortem similarity) that the snapshot alone can't
provide.

The investigator surfaces are intentionally narrow:

    invoke(prompt, model=None) → InvestigatorResult

This is what the Celery task in ``tasks.py`` calls. It is fully
synchronous (Celery already provides the concurrency layer) and
exception-safe — every failure path returns a populated
:class:`InvestigatorResult` with the failure reason captured rather
than raising.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ─── Result types ────────────────────────────────────────────────────


@dataclass
class InvestigatorResult:
    """LLM invocation result, ready to feed into the validator.

    Attributes:
        parsed: parsed JSON dict from the LLM, or an empty dict on
            parse failure. The validator handles non-dict input
            gracefully — we don't need to second-guess here.
        raw_text: the LLM's raw text response. Useful for the
            calibration phase when we want to diff prompt variants
            against the same PR.
        llm_model: the model identifier the LLM provider actually
            used. May differ from the request when the provider
            performs internal aliasing.
        duration_ms: wall-clock time spent inside the ``invoke``
            call. Persisted to ``change_investigations.duration_ms``
            for cost / latency tracking.
        ok: ``True`` when the call returned text we could parse as
            JSON; ``False`` on any error (network failure, parse
            error, non-string content).
        error: short error description when ``ok=False``. Never
            contains a token or full response body — only the failure
            category + exception class name.
    """

    parsed: dict[str, Any]
    raw_text: str
    llm_model: str
    duration_ms: int
    ok: bool
    error: str | None = None
    # Tool call telemetry stays here as a forward-compatible field —
    # Part 2 always reports zero (no agentic loop), but Part 2.5 will
    # populate it if/when we add toolset access.
    tool_call_count: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


# ─── Public entry point ──────────────────────────────────────────────


def invoke(
    prompt: str,
    *,
    model: str | None = None,
    temperature: float = 0.2,
) -> InvestigatorResult:
    """Run a single LLM call and parse the response as JSON.

    Args:
        prompt: full prompt text from
            :mod:`services.change_intercept.prompts`.
        model: optional override; defaults to ``ModelConfig.RCA_MODEL``
            (the same model the incident RCA path uses, so calibration
            data is comparable).
        temperature: temperature passed to the provider. Defaults
            lower than the RCA path (0.4) because we want deterministic
            JSON output — high temperature here just produces malformed
            JSON the validator will drop.

    Returns:
        A populated :class:`InvestigatorResult`. ``ok=True`` indicates
        the LLM returned text we could JSON-parse; the validator does
        the deeper semantic checks.
    """
    start = time.monotonic()
    chosen_model = model or _default_model()

    try:
        # Lazy imports so module import doesn't pay the cost of loading
        # the LLM stack until an investigator actually runs.
        from chat.backend.agent.providers import create_chat_model
        from langchain_core.messages import HumanMessage
    except ImportError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "change_intercept_investigator=import_failed error_class=%s",
            type(exc).__name__,
        )
        return InvestigatorResult(
            parsed={},
            raw_text="",
            llm_model=chosen_model,
            duration_ms=duration_ms,
            ok=False,
            error=f"import_failed: {type(exc).__name__}",
        )

    try:
        llm = create_chat_model(chosen_model, temperature=temperature)
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "change_intercept_investigator=create_model_failed model=%s "
            "error_class=%s",
            chosen_model,
            type(exc).__name__,
        )
        return InvestigatorResult(
            parsed={},
            raw_text="",
            llm_model=chosen_model,
            duration_ms=duration_ms,
            ok=False,
            error=f"create_model_failed: {type(exc).__name__}",
        )

    try:
        response = llm.invoke([HumanMessage(content=prompt)])
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        # Defensive — never log full response bodies (could include
        # token-shaped substrings) or the prompt (large).
        logger.warning(
            "change_intercept_investigator=invoke_failed model=%s "
            "duration_ms=%d error_class=%s",
            chosen_model,
            duration_ms,
            type(exc).__name__,
        )
        return InvestigatorResult(
            parsed={},
            raw_text="",
            llm_model=chosen_model,
            duration_ms=duration_ms,
            ok=False,
            error=f"invoke_failed: {type(exc).__name__}",
        )

    duration_ms = int((time.monotonic() - start) * 1000)
    raw_text = _extract_text(response)
    parsed = _parse_json(raw_text)
    ok = bool(parsed)

    logger.info(
        "change_intercept_investigator=invoke_done model=%s duration_ms=%d "
        "raw_bytes=%d ok=%s findings_in=%d",
        chosen_model,
        duration_ms,
        len(raw_text),
        ok,
        len(parsed.get("findings") or []) if isinstance(parsed, dict) else 0,
    )

    return InvestigatorResult(
        parsed=parsed if isinstance(parsed, dict) else {},
        raw_text=raw_text,
        llm_model=chosen_model,
        duration_ms=duration_ms,
        ok=ok,
        error=None if ok else "parse_failed",
    )


# ─── Internal helpers ────────────────────────────────────────────────


def _default_model() -> str:
    """Resolve the default change-intercept model.

    Reuses the existing ``ModelConfig.RCA_MODEL`` rather than declaring
    a separate ``CHANGE_INTERCEPT_MODEL`` env var. The calibration
    phase explicitly compares verdict distributions against incident
    RCA quality, so using the same model keeps the comparison fair.
    Lazy import keeps the module unit-testable without the full LLM
    stack.
    """
    try:
        from chat.backend.agent.llm import ModelConfig

        return ModelConfig.RCA_MODEL
    except Exception:
        # Pinned fallback so a broken import doesn't take the
        # investigator offline. Calibration logs will catch the
        # mismatch.
        return "anthropic/claude-haiku-4.5"


def _extract_text(response: Any) -> str:
    """Pull the text payload out of a LangChain message response.

    LangChain's ``BaseChatModel.invoke`` returns either an
    ``AIMessage`` (whose ``.content`` is the response text) or, in
    some providers, a string directly. Handle both shapes.
    """
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Anthropic-style list-of-content-blocks. Concatenate text blocks.
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def _parse_json(text: str) -> dict[str, Any]:
    """Parse ``text`` as JSON. Returns ``{}`` on any failure.

    Handles two common LLM quirks:
      - Markdown fences around the JSON (``` ```json ... ``` ```).
      - Leading / trailing prose despite our explicit "no prose"
        instruction. We scan for the first ``{`` and the matching
        last ``}`` to extract the object.
    """
    if not text:
        return {}

    # Strip markdown fences if present.
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        candidate = fenced.group(1).strip()
    else:
        candidate = text.strip()

    # Try direct parse first — happy path.
    try:
        result = json.loads(candidate)
    except json.JSONDecodeError:
        # Fallback: locate the outermost ``{...}`` span and try again.
        start_idx = candidate.find("{")
        end_idx = candidate.rfind("}")
        if start_idx == -1 or end_idx <= start_idx:
            return {}
        candidate = candidate[start_idx : end_idx + 1]
        try:
            result = json.loads(candidate)
        except json.JSONDecodeError:
            return {}

    if not isinstance(result, dict):
        return {}
    return result
