"""Execute one check and judge its output. No Celery, no database.

Kept apart from the scheduler in runner.py so the rules that decide what a
check may run, and when a run counts as healthy, can be tested on their own.
"""

import json
import re
import logging
from contextlib import contextmanager

from services.detection import PROVIDER_ONPREM_KUBECTL, STATUS_ERROR
from services.detection import window
from services.detection.assertion import Verdict, evaluate
from utils.log_sanitizer import sanitize
from utils.security.read_only_classifier import describe_rejection

logger = logging.getLogger(__name__)

# Short on purpose: checks share workers with investigations, and a check that
# needs longer than this is the wrong shape for something run every few minutes.
COMMAND_TIMEOUT_SECONDS = 30
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


@contextmanager
def _own_context(user_id: str, session_id: str, provider: str):
    """Run the block as a read-only detection run, then put the caller's context back.

    The execution context is per thread and has no reset. A check can be run
    from inside an agent's turn (a replay while the agent saves it), and that
    agent's session and mode must be what they were when the run returns.

    Each field is restored to exactly what it was, including "not set":
    set_user_context skips empty values, so it could install this run's session
    and mode but never take them away again.
    """
    from utils.cloud import cloud_utils

    before = cloud_utils.get_user_context()
    saved = {
        "user_id": before.get("user_id"),
        "session_id": before.get("session_id"),
        "provider_preference": cloud_utils.get_provider_preference(),
        "mode": cloud_utils.get_mode_from_context(),
    }
    cloud_utils.set_user_context(
        user_id=user_id, session_id=session_id,
        provider_preference=provider, mode=_READ_ONLY_MODE,
    )
    try:
        yield
    finally:
        for field, value in saved.items():
            cloud_utils._set_ctx(field, value)


def _execute(check: dict, user_id: str, check_id: str, at=None) -> Verdict | str:
    """Run the check's command and return its output, or an error Verdict.

    The command goes through the same executor an agent would use, in Ask mode,
    so the org's command policy, connector grants and the read-only gate all
    apply to a check exactly as they apply to a person's read-only question.
    """
    from utils.auth.command_gate import judged_at_save

    provider, target = check["provider"], check.get("target") or ""
    # judge() fixed the end of a windowed check's span, so this is the command it classified.
    command = window.render(check["command"], check.get("window_seconds"), at)
    session_id = "detection-check-%s" % check_id
    # The judge needs a conversation to compare the command with, and a scheduled
    # run has none: it judged this command when the check was saved
    # (detection_check_tool). The signature matcher and org policy still run.
    with _own_context(user_id, session_id, provider), judged_at_save(gated_form(provider, command)):
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
        return Verdict(STATUS_ERROR, detail="command returned an unreadable result", ran=False)
    if not isinstance(result, dict) or not result.get("success"):
        error = result.get("error") if isinstance(result, dict) else None
        return Verdict(STATUS_ERROR, detail=str(error or "command failed"),
                       ran=isinstance(result, dict) and _ran_and_failed(result))
    output = result.get("output")
    if output is None:
        output = result.get("chat_output")
    return output if isinstance(output, str) else ""


# What a CLI says when it was turned away, or never had credentials to begin with.
# Whole phrases, matched outside quoted names: a deployment called "login" that
# does not exist is a missing resource, not a login problem.
_ACCESS_ERROR = re.compile(
    r"\b(forbidden|unauthorized|unauthenticated|access ?denied|permission denied|not authorized"
    r"|invalid credentials|unable to locate credentials|could not load credentials"
    r"|(token|credentials?|session) (has |have |is |are )?expired|expiredtoken"
    r"|must be logged in|you must log ?in|please (run .{0,40})?log ?in)\b"
)
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")


def _ran_and_failed(result: dict) -> bool:
    """Whether a failed command reached the target and failed there for a reason of its own.

    A query for a resource that does not exist is an error, but it says nothing
    about Aurora's access, and must not count toward the "I have lost access"
    message. Only the executors' own result for a command they ran carries both
    final_command and a return code; setup, gate and timeout failures do not.
    A failure that reads like an access problem still counts as not having run.
    """
    if "final_command" not in result or not isinstance(result.get("return_code"), int):
        return False
    said = " ".join(str(result.get(k) or "") for k in ("error", "chat_output", "output")).lower()
    # The command line is echoed in chat_output; what the CLI answered is what matters.
    said = said.replace(str(result.get("final_command") or "").lower(), " ")
    return not _ACCESS_ERROR.search(_QUOTED.sub(" ", said))


def judge(check: dict, user_id: str, check_id: str, at=None) -> Verdict:
    """Produce the verdict for one run of *check*. Never raises.

    *at* replays a windowed check over the span ending then.
    """
    # Checked on every run, not only when the check was saved: a row changed
    # some other way must still be unable to run a write.
    if at is None and window.is_windowed(check.get("window_seconds")):
        # Fixed once: the command classified here and the one executed below
        # must be the same string, and "now" moves between the two.
        at = window.span_end(check.get("lag_seconds"))
    rejection = describe_rejection(window.render(check["command"], check.get("window_seconds"), at))
    if rejection:
        return Verdict(STATUS_ERROR, detail="command is not read-only: " + rejection)
    try:
        outcome = _execute(check, user_id, check_id, at)
    except Exception as exc:
        logger.exception("[DetectionExecution] Check %s raised while executing", sanitize(check_id))
        return Verdict(STATUS_ERROR, detail="execution raised %s" % type(exc).__name__, ran=False)
    if isinstance(outcome, Verdict):
        return outcome
    return evaluate(check["assertion"], outcome)
