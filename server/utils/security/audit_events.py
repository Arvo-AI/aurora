"""Structured audit event logging for safety guardrail blocks.

Emits a JSON-structured log line for every block event, suitable for
ingestion by SIEM systems. All safety layers (regex patterns, LLM judge,
input rail, command policy) call emit_block_event() on block.

Commands are never logged in raw form. Instead, a stable sha256 fingerprint
is recorded so incidents can be correlated across layers without exposing
attacker-controlled content (prompt-injection payloads, credentials in
args, etc.).
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
    command: str = "",
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
        "command_fp": _fingerprint(command) if command else "",
        "tool": tool,
        "reason": (reason or "")[:_REASON_MAX_LEN],
        "technique": technique,
        "rule_id": rule_id,
        "latency_ms": round(latency_ms, 1),
    }
    logger.warning("GUARDRAIL_AUDIT %s", json.dumps(event, separators=(",", ":")))
