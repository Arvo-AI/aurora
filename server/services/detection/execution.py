"""Execute one check and judge its output. No Celery, no database.

Kept apart from the scheduler in runner.py so the rules that decide what a
check may run, and when a run counts as healthy, can be tested on their own.
"""

import json
import logging

from services.detection import PROVIDER_ONPREM_KUBECTL, STATUS_ERROR
from services.detection.assertion import Verdict, evaluate
from utils.log_sanitizer import sanitize
from utils.security.read_only_classifier import describe_rejection

logger = logging.getLogger(__name__)

COMMAND_TIMEOUT_SECONDS = 60
# Checks only ever run in the read-only mode cloud_exec already enforces.
_READ_ONLY_MODE = "ask"


def _is_onprem(provider: str) -> bool:
    return provider == PROVIDER_ONPREM_KUBECTL


def _kubectl_args(command: str) -> str:
    """*command* without a leading `kubectl`, as on_prem_kubectl normalises it."""
    command = command.strip()
    return command[8:].strip() if command.lower().startswith("kubectl ") else command


def gated_form(provider: str, command: str) -> str:
    """*command* exactly as its executor hands it to the gate.

    The judge exemption is bound to this string, so it has to be built the way
    the executor builds it: cloud_exec prefixes the provider CLI, on_prem_kubectl
    prefixes `kubectl`.
    """
    if _is_onprem(provider):
        return "kubectl " + _kubectl_args(command)
    from chat.backend.agent.tools.cloud_exec_tool import (
        _command_for_policy,
        _normalize_cloud_exec_provider,
    )
    return _command_for_policy(_normalize_cloud_exec_provider(provider), command)


def _execute(check: dict, user_id: str, check_id: str) -> Verdict | str:
    """Run the check's command and return its output, or an error Verdict.

    The command goes through the same executor an agent would use, in Ask mode,
    so the org's command policy, connector grants and the read-only gate all
    apply to a check exactly as they apply to a person's read-only question.
    """
    from utils.auth.command_gate import judged_at_save
    from utils.cloud.cloud_utils import set_user_context

    provider, command, target = check["provider"], check["command"], check.get("target") or ""
    session_id = "detection-check-%s" % check_id
    set_user_context(
        user_id=user_id, session_id=session_id,
        provider_preference=provider, mode=_READ_ONLY_MODE,
    )
    # The judge needs a conversation to compare the command with, and a
    # scheduled run has none: it judged this command when the check was saved
    # (detection_check_tool). The signature matcher and org policy still run.
    with judged_at_save(gated_form(provider, command)):
        if _is_onprem(provider):
            from chat.backend.agent.tools.kubectl_onprem_tool import on_prem_kubectl
            raw = on_prem_kubectl(
                command, cluster_id=target, user_id=user_id, session_id=session_id,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        else:
            from chat.backend.agent.tools.cloud_exec_tool import cloud_exec
            raw = cloud_exec(
                provider, command, user_id=user_id, session_id=session_id,
                provider_preference=provider, timeout=COMMAND_TIMEOUT_SECONDS,
                account_id=target or None,
            )
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        return Verdict(STATUS_ERROR, detail="command returned an unreadable result")
    if not isinstance(result, dict) or not result.get("success"):
        error = result.get("error") if isinstance(result, dict) else None
        return Verdict(STATUS_ERROR, detail=str(error or "command failed"))
    output = result.get("output")
    if output is None:
        output = result.get("chat_output")
    return output if isinstance(output, str) else ""


def judge(check: dict, user_id: str, check_id: str) -> Verdict:
    """Produce the verdict for one run of *check*. Never raises."""
    # Checked on every run, not only when the check was saved: a row changed
    # some other way must still be unable to run a write.
    rejection = describe_rejection(check["command"])
    if rejection:
        return Verdict(STATUS_ERROR, detail="command is not read-only: " + rejection)
    try:
        outcome = _execute(check, user_id, check_id)
    except Exception as exc:
        logger.exception("[DetectionExecution] Check %s raised while executing", sanitize(check_id))
        return Verdict(STATUS_ERROR, detail="execution raised %s" % type(exc).__name__)
    if isinstance(outcome, Verdict):
        return outcome
    return evaluate(check["assertion"], outcome)
