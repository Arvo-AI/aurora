"""Command-execution gate — unified policy + safety enforcement with HITL.

Centralizes the four-layer defense-in-depth check that every shell tool must
run before execution:

    1. Signature match  (utils/security/signature_match.py via command_safety)
    2. Org allow/deny   (utils/auth/command_policy.py)
    3. LLM safety judge (utils/security/command_safety.py)

Behavior:

* **Background chats** (``State.is_background == True``): on any block, returns
  a deny decision with the layer's reason. Matches the pre-existing invariant
  that destructive/denied actions cannot execute without an interactive user.
* **Foreground chats**: on any block, prompts the user via the WebSocket HITL
  channel with Yes / No / (optionally) Yes-Always.
  - **Yes**: approve this single invocation. Tool result looks like a normal
    success to the agent (intentionally: we don't teach the LLM to reason
    about the gate).
  - **No**: abort with ``code="USER_DECLINED"``, distinct from policy/safety
    codes so the agent sees explicit user rejection rather than a static
    rule failure.
  - **Yes-Always**: only offered when the block originated from
    ``org_command_policies`` (deny rule hit or allowlist exhausted). Applies
    the (possibly user-edited) policy mutation and then allows this
    invocation. Future runs — including background RCAs — inherit the change.

The gate has no independent on/off switch: when ``GUARDRAILS_ENABLED=false``
and both org lists are disabled, no layer blocks anything, so the prompt
never fires. The gate is strictly the interactive surface of the existing
security layers.

Two contextvars prevent duplicate prompts and duplicate guardrail LLM calls
for a single logical command as it passes through multiple tool layers
(e.g. ``terminal_exec`` routing into ``cloud_exec``):

* ``_gate_inflight_command`` — set to the command hash during gating; re-entry
  with a matching hash is a no-op "already approved" result.
* ``_guardrails_approved_command`` — read by ``terminal_run._check_guardrails``
  to skip the redundant signature+judge call on the agent path. Direct callers
  (no contextvar set) still run the full check.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_gate_inflight_command: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_gate_inflight_command", default=None,
)
_guardrails_approved_command: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_guardrails_approved_command", default=None,
)


def _hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8", errors="replace")).hexdigest()


def guardrails_approved_hash() -> Optional[str]:
    """Accessor for ``terminal_run._check_guardrails`` to skip duplicate checks."""
    return _guardrails_approved_command.get()


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    code: str = ""           # "" on allow, otherwise POLICY_DENIED / SAFETY_BLOCKED /
                             # SIGNATURE_MATCHED / USER_DECLINED
    block_reason: str = ""


_ALLOWED = GateDecision(allowed=True)


def _block(code: str, reason: str) -> GateDecision:
    return GateDecision(allowed=False, code=code, block_reason=reason)


def _is_foreground() -> bool:
    """Return True iff the current execution is a foreground (interactive) chat."""
    try:
        from utils.cloud.cloud_utils import get_state_context
        state = get_state_context()
        if state is None:
            return False
        return not bool(getattr(state, "is_background", False))
    except Exception:
        return False


def _session_id() -> Optional[str]:
    try:
        from utils.cloud.cloud_utils import get_state_context
        state = get_state_context()
        return getattr(state, "session_id", None) if state else None
    except Exception:
        return None


def gate_command(
    *,
    user_id: Optional[str],
    tool_name: str,
    command: str,
) -> GateDecision:
    """Run the full pre-execution gauntlet for *command*.

    Returns a :class:`GateDecision`. The caller is responsible for converting a
    blocked decision into the tool's error response (``{"success": False,
    "error": decision.block_reason, "code": decision.code}``).
    """
    if not user_id:
        # Without a user there is no org context and no HITL channel; defer to
        # existing per-tool behavior by allowing through. Individual tools
        # still enforce their own auth.
        return _ALLOWED

    cmd_hash = _hash(command)
    if _gate_inflight_command.get() == cmd_hash:
        # Re-entry for the same command (e.g. terminal_exec -> cloud_exec).
        return _ALLOWED

    token = _gate_inflight_command.set(cmd_hash)
    try:
        return _gate_impl(user_id=user_id, tool_name=tool_name, command=command,
                          cmd_hash=cmd_hash)
    finally:
        _gate_inflight_command.reset(token)


def _gate_impl(*, user_id: str, tool_name: str, command: str, cmd_hash: str) -> GateDecision:
    from utils.auth.command_policy import (
        evaluate_compound_command, CommandVerdict, plan_yes_always,
        apply_yes_always, validate_pattern, PolicyChange,
    )
    from utils.auth.stateless_auth import get_org_id_for_user
    from utils.security.command_safety import evaluate_command as safety_evaluate

    org_id = get_org_id_for_user(user_id)
    foreground = _is_foreground()
    session_id = _session_id()

    # --- Layer 1 & 3: signature_match + LLM judge ----------------------------
    # command_safety.evaluate_command bundles both: signature first (fast,
    # cached), LLM judge second (network call). We read decision.layer to know
    # which fired.
    safety_decision = safety_evaluate(
        command, tool=tool_name, user_id=user_id, session_id=session_id,
    )
    if safety_decision.blocked:
        layer = safety_decision.layer or "llm_judge"
        code = "SIGNATURE_MATCHED" if layer == "signature_match" else "SAFETY_BLOCKED"
        reason = f"Command blocked by safety guardrail: {safety_decision.reason}"
        if not foreground:
            return _block(code, reason)
        # Foreground: ask user. No Yes-Always for this layer because there is
        # no policy slot to flip.
        return _prompt_user(
            user_id=user_id,
            session_id=session_id,
            tool_name=tool_name,
            command=command,
            block_code=code,
            block_reason=reason,
            block_layer=layer,
            allow_yes_always=False,
            yes_always_changes=[],
            org_id=None,
            cmd_hash=cmd_hash,
        )

    # --- Layer 2: org allow/deny --------------------------------------------
    policy_verdict: CommandVerdict = evaluate_compound_command(org_id, command)
    if policy_verdict.allowed:
        # Tell terminal_run._check_guardrails it may skip re-running
        # signature+judge for this command on the same invocation.
        _guardrails_approved_command.set(cmd_hash)
        return _ALLOWED

    reason = (policy_verdict.rule_description or "Matched organization policy")[:200]
    block_reason = f"Command blocked by organization policy: {reason}"
    layer = (
        "policy_both" if policy_verdict.deny_rule_id and policy_verdict.allowlist_exhausted
        else "policy_deny" if policy_verdict.deny_rule_id
        else "policy_allow_exhausted"
    )
    if not foreground or not org_id:
        return _block("POLICY_DENIED", block_reason)

    changes = plan_yes_always(policy_verdict, command)
    decision = _prompt_user(
        user_id=user_id,
        session_id=session_id,
        tool_name=tool_name,
        command=command,
        block_code="POLICY_DENIED",
        block_reason=block_reason,
        block_layer=layer,
        allow_yes_always=bool(changes),
        yes_always_changes=changes,
        org_id=org_id,
        cmd_hash=cmd_hash,
    )
    # On Yes / Yes-Always, also clear the guardrails-approved marker so the
    # downstream signature+judge re-check (if any) still runs normally for
    # commands that bypassed the policy via user consent — the safety layers
    # already passed above.
    if decision.allowed:
        _guardrails_approved_command.set(cmd_hash)
    return decision


def _prompt_user(
    *,
    user_id: str,
    session_id: Optional[str],
    tool_name: str,
    command: str,
    block_code: str,
    block_reason: str,
    block_layer: str,
    allow_yes_always: bool,
    yes_always_changes: list,
    org_id: Optional[str],
    cmd_hash: str,
) -> GateDecision:
    """Ask the user Yes / No / (Yes-Always) and apply the chosen effect."""
    from utils.cloud.infrastructure_confirmation import wait_for_user_confirmation_ex
    from utils.auth.command_policy import apply_yes_always, validate_pattern

    options = [
        {"text": "Yes", "value": "execute"},
        {"text": "No", "value": "cancel"},
    ]
    extra = {
        "block_layer": block_layer,
        "block_reason": block_reason,
        "command": command,
    }
    if allow_yes_always and yes_always_changes:
        options.append({"text": "Yes, Always", "value": "execute_always"})
        extra["yes_always_effect"] = {
            "summary": "This will modify your organization's command policy:",
            "changes": [
                {
                    "action": ch.action,
                    "rule_id": ch.rule_id,
                    "pattern": ch.pattern,
                    "description": ch.description,
                    "editable": ch.editable,
                }
                for ch in yes_always_changes
            ],
        }

    # A single-line human prompt shown in the UI alongside the options.
    message = f"{tool_name}: {block_reason}. Command: {command[:500]}"
    result = wait_for_user_confirmation_ex(
        user_id=user_id,
        message=message,
        tool_name=tool_name,
        session_id=session_id,
        options=options,
        extra=extra,
    )
    decision = result.get("decision")

    if decision == "execute":
        return _ALLOWED
    if decision == "execute_always" and allow_yes_always and org_id:
        edited = result.get("edited_patterns") or {}
        applied = []
        for idx, ch in enumerate(yes_always_changes):
            if ch.action == "add_allow_rule" and ch.editable:
                # Users may tighten/loosen the pattern via the editable input.
                # Indices are sent as strings by JSON.
                override = edited.get(str(idx)) or edited.get(idx)
                pattern = (override or ch.pattern or "").strip()
                err = validate_pattern(pattern) if pattern else "empty pattern"
                if err:
                    logger.warning(
                        "[CommandGate] Yes-Always rejected: invalid regex '%s' (%s). "
                        "Treating as cancel.", pattern, err,
                    )
                    return _block(
                        "USER_DECLINED",
                        f"Tool call not allowed by user (invalid regex: {err})",
                    )
                applied.append(type(ch)(
                    action=ch.action, rule_id=ch.rule_id, pattern=pattern,
                    description=ch.description, editable=ch.editable,
                ))
            else:
                applied.append(ch)
        try:
            apply_yes_always(org_id, applied, user_id)
            logger.info(
                "[CommandGate] Yes-Always applied %d change(s) for org %s by %s",
                len(applied), org_id, user_id,
            )
        except Exception:
            logger.exception("[CommandGate] Failed to persist Yes-Always changes")
            return _block("POLICY_DENIED",
                          "Failed to update organization policy; command not executed")
        return _ALLOWED

    # Timeout or explicit cancel: USER_DECLINED.
    return _block("USER_DECLINED", "Tool call not allowed by user")
