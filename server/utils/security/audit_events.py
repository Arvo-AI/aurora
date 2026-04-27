"""Structured audit event logging for safety guardrail blocks.

Emits a JSON-structured log line for every block event, suitable for
ingestion by SIEM systems. All safety layers (regex patterns, LLM judge,
input rail, command policy) call emit_block_event() on block.

The blocked content is never logged in raw form. Instead, a stable sha256
fingerprint is recorded as ``subject_fp`` so incidents can be correlated
across layers without exposing attacker-controlled content (prompt-injection
payloads, credentials in args, etc.). ``subject`` is neutral across layers:
shell command strings for the command layers, raw user text for the input
rail.
"""

import json
import logging
import time

from utils.security.command_safety import _fingerprint

logger = logging.getLogger("guardrails.audit")

_REASON_MAX_LEN = 300


def emit_block_event(
    *,
    user_id: str,
    session_id: str,
    layer: str,
    decision: str = "blocked",
    subject: str = "",
    tool: str = "",
    reason: str = "",
    technique: str = "",
    rule_id: str = "",
    latency_ms: float = 0,
) -> None:
    """Emit a structured JSON audit log for a guardrail block event."""
    event = {
        "event_type": "guardrail_block",
        "timestamp": time.time(),
        "user_id": user_id or "",
        "session_id": session_id or "",
        "layer": layer,
        "decision": decision,
        "subject_fp": _fingerprint(subject) if subject else "",
        "tool": tool,
        "reason": (reason or "")[:_REASON_MAX_LEN],
        "technique": technique,
        "rule_id": rule_id,
        "latency_ms": round(latency_ms, 2),
    }
    logger.warning("GUARDRAIL_AUDIT %s", json.dumps(event, separators=(",", ":")))


def emit_redaction_event(
    *,
    user_id: str,
    session_id: str,
    rule_id: str,
    value_hash: str,
    location: str,
    tool: str = "",
    latency_ms: float = 0,
) -> None:
    """Emit a structured audit log for an L5 output-redaction event.

    ``location`` is the hook identifier ("tool_completion" or "db_save");
    non-zero rates at ``db_save`` indicate that tool output reached the
    persistence layer without going through the primary hook and should be
    treated as an operational signal.

    ``value_hash`` is the truncated sha256 already computed by the scanner;
    the raw secret value is never passed to this function.
    """
    event = {
        "event_type": "guardrail_redaction",
        "timestamp": time.time(),
        "user_id": user_id or "",
        "session_id": session_id or "",
        "layer": "L5",
        "decision": "redacted",
        "rule_id": rule_id,
        "value_hash": value_hash,
        "location": location,
        "tool": tool,
        "latency_ms": round(latency_ms, 2),
    }
    logger.info("GUARDRAIL_AUDIT %s", json.dumps(event, separators=(",", ":")))
