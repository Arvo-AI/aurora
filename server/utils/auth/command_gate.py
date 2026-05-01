"""Command-execution gate — unified policy + safety enforcement with HITL.

Two entry points, one confirmation surface:

* :func:`gate_command` — shell-command path. Runs the four-layer
  defense-in-depth check (signature, org allow/deny, LLM judge, session
  taint) and prompts on any block.
* :func:`gate_action` — structured-action path for tools with no shell
  command (Terraform apply/destroy, Bitbucket PR merges, Notion column
  deletes, destructive MCP tools, etc). Always prompts in foreground,
  denies in background. No policy/Yes-Always (there is no regex to
  persist).

Both funnel into the same ``_prompt_user`` helper, WS message, React
panel, and DB-backed live state.

Layers evaluated by :func:`gate_command`:

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
                             # SIGNATURE_MATCHED / USER_DECLINED / BACKGROUND_DENIED /
                             # TOOL_NOT_ALLOWED
    block_reason: str = ""


_ALLOWED = GateDecision(allowed=True)


def _block(code: str, reason: str) -> GateDecision:
    return GateDecision(allowed=False, code=code, block_reason=reason)


def _get_context() -> tuple[bool, Optional[str]]:
    """Return (is_foreground, session_id) from the current execution state."""
    try:
        from utils.cloud.cloud_utils import get_state_context
        state = get_state_context()
        if state is None:
            return False, None
        return not bool(getattr(state, "is_background", False)), getattr(state, "session_id", None)
    except Exception:
        return False, None


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
    approved_token = _guardrails_approved_command.set(None)
    try:
        return _gate_impl(user_id=user_id, tool_name=tool_name, command=command,
                          cmd_hash=cmd_hash)
    finally:
        _gate_inflight_command.reset(token)
        _guardrails_approved_command.reset(approved_token)


def gate_action(
    *,
    user_id: Optional[str],
    tool_name: str,
    summary: str,
) -> GateDecision:
    """Human-in-the-loop gate for structured tool actions (no shell command).

    Foreground: prompts the user Yes / No with *summary* as the rendered
    action. Background: denies (no interactive user). There is no
    Yes-Always here -- these actions are not regex-addressable, so we
    cannot persist an allow rule; org policy does not apply.

    Returns the same :class:`GateDecision` shape as :func:`gate_command`
    so callers can treat both gates uniformly.
    """
    if not user_id:
        # Preserve prior behavior of wait_for_user_confirmation helpers,
        # which required a user and otherwise denied.
        return _block("TOOL_NOT_ALLOWED", "Tool call not allowed: no user context")

    foreground, session_id = _get_context()
    if not foreground:
        return _block("BACKGROUND_DENIED", "Tool call not allowed in background context")

    decision = _prompt_user(
        user_id=user_id,
        session_id=session_id,
        tool_name=tool_name,
        command=summary,
        block_code="ACTION_CONFIRM",
        block_reason=summary,
        block_layer="destructive_action",
        allow_yes_always=False,
        yes_always_changes=[],
        org_id=None,
        cmd_hash="",
    )
    return decision


def is_session_tainted(session_id: Optional[str], user_id: Optional[str]) -> bool:
    """Return True iff ``session_id`` has been marked tainted (NeMo input-rail
    hit on the opening user message of this foreground chat).

    Tainted sessions force every command through user confirmation, even when
    all guardrail layers pass. Reads under the caller's RLS context (matches
    ``mark_session_tainted``); fails closed (treats as untainted) on DB error
    since the gate's other layers already provide defense-in-depth.
    """
    if not session_id or not user_id:
        return False
    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            if not set_rls_context(cursor, conn, user_id, log_prefix="[CommandGate:TaintRead]"):
                return False
            cursor.execute(
                "SELECT security_tainted FROM chat_sessions WHERE id = %s AND user_id = %s",
                (session_id, user_id),
            )
            row = cursor.fetchone()
            return bool(row and row[0])
    except Exception as e:
        logger.warning(f"[CommandGate] taint lookup failed for {session_id}: {e}")
        return False


def mark_session_tainted(session_id: Optional[str], user_id: Optional[str]) -> None:
    """Flip ``security_tainted`` to true for this session. Idempotent."""
    if not session_id or not user_id:
        return
    try:
        from utils.db.connection_pool import db_pool
        from utils.auth.stateless_auth import set_rls_context
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            if not set_rls_context(cursor, conn, user_id, log_prefix="[CommandGate:Taint]"):
                return
            cursor.execute(
                "UPDATE chat_sessions SET security_tainted = true WHERE id = %s AND user_id = %s",
                (session_id, user_id),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"[CommandGate] failed to mark session {session_id} tainted: {e}")


def _gate_impl(*, user_id: str, tool_name: str, command: str, cmd_hash: str) -> GateDecision:
    from utils.auth.command_policy import (
        evaluate_compound_command, CommandVerdict, plan_yes_always,
    )
    from utils.auth.stateless_auth import get_org_id_for_user
    from utils.security.command_safety import evaluate_command as safety_evaluate

    org_id = get_org_id_for_user(user_id)
    foreground, session_id = _get_context()

    policy_verdict: CommandVerdict = evaluate_compound_command(org_id, command)
    policy_blocked = not policy_verdict.allowed

    # An explicit allowlist match is a trust signal from the org admin — skip
    # the safety judge so approved patterns don't generate spurious prompts.
    # "Policy lists are disabled" / None means default-allow, not an explicit
    # rule hit, so we still run the safety judge in those cases.
    explicitly_allowed = (
        policy_verdict.allowed
        and policy_verdict.rule_description not in (None, "Policy lists are disabled")
    )
    if not explicitly_allowed:
        safety_decision = safety_evaluate(
            command, tool=tool_name, user_id=user_id, session_id=session_id,
        )
        safety_blocked = safety_decision.blocked
    else:
        safety_decision = None
        safety_blocked = False

    tainted = foreground and is_session_tainted(session_id, user_id)

    if not (safety_blocked or policy_blocked or tainted):
        # Tell terminal_run._check_guardrails it may skip re-running
        # signature+judge for this command on the same invocation.
        _guardrails_approved_command.set(cmd_hash)
        return _ALLOWED

    # Compose the block code/reason/layer from whichever layers fired.
    safety_layer = safety_decision.layer if safety_blocked else None
    safety_code = (
        "SIGNATURE_MATCHED" if safety_layer == "signature_match"
        else "SAFETY_BLOCKED" if safety_blocked else None
    )
    policy_layer = (
        "policy_both" if policy_blocked and policy_verdict.deny_rule_id
            and policy_verdict.allowlist_exhausted
        else "policy_deny" if policy_blocked and policy_verdict.deny_rule_id
        else "policy_allow_exhausted" if policy_blocked
        else None
    )
    layers = [l for l in (safety_layer, policy_layer) if l]
    if tainted:
        layers.append("session_taint")
    if not layers:
        layers = ["unknown"]
    block_layer = "+".join(layers)

    code = safety_code or ("POLICY_DENIED" if policy_blocked else "SESSION_TAINTED")
    reasons = []
    if safety_blocked:
        reasons.append(f"safety guardrail: {safety_decision.reason}")
    if policy_blocked:
        reasons.append(
            "organization policy: "
            + (policy_verdict.rule_description or "matched organization policy")[:200]
        )
    if tainted:
        reasons.append("session flagged by input safety check; approval required")
    block_reason = "Command blocked by " + "; ".join(reasons)

    if not foreground:
        return _block(code, block_reason)

    # Always is offered iff the policy layer fired with a real mutation to
    # propose. Safety-only or taint-only blocks show Yes/No.
    changes = plan_yes_always(policy_verdict, command) if policy_blocked else []
    decision = _prompt_user(
        user_id=user_id,
        session_id=session_id,
        tool_name=tool_name,
        command=command,
        block_code=code,
        block_reason=block_reason,
        block_layer=block_layer or "unknown",
        allow_yes_always=bool(changes),
        yes_always_changes=changes,
        org_id=org_id if policy_blocked else None,
        cmd_hash=cmd_hash,
    )
    if decision.allowed:
        _guardrails_approved_command.set(cmd_hash)
    return decision


def _user_is_org_admin(user_id: str, org_id: Optional[str]) -> bool:
    """Return True iff *user_id* has admin access in *org_id*.

    Yes-Always mutates ``org_command_policies``, which the HTTP routes
    gate behind ``require_permission("admin", "access")``. We mirror that
    check here so a non-admin cannot rewrite org policy by clicking a
    chat button. Fails closed on any error.
    """
    if not user_id or not org_id:
        return False
    try:
        from utils.auth.enforcer import get_enforcer
        return bool(get_enforcer().enforce(user_id, org_id, "admin", "access"))
    except Exception as e:
        logger.warning(f"[CommandGate] admin check failed for {user_id}/{org_id}: {e}")
        return False


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
    # Yes-Always rewrites org_command_policies, which the HTTP routes gate
    # behind admin. Non-admins get Yes/No only; their approval is
    # session-scoped and no policy row changes.
    offer_always = (
        allow_yes_always
        and bool(yes_always_changes)
        and _user_is_org_admin(user_id, org_id)
    )
    if offer_always:
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

    # Compact label for the UI. The full reason and command ride along in
    # ``extra`` (``block_reason`` / ``command``) for tooltips, logging, and
    # the pattern derivation in the Yes-Always popover.
    message = "Approval needed"
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
    if decision == "execute_always" and offer_always and org_id:
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
