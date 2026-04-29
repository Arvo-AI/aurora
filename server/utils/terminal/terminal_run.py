"""Drop-in replacement for subprocess.run() that executes in terminal pods.

This module provides a terminal_run() function that mimics subprocess.run() API
but executes commands in isolated terminal pods via kubectl exec.

Safety guardrails (signature matcher + LLM judge) run automatically unless
the caller passes ``trusted=True`` for known-safe internal operations.
"""

import asyncio
import logging
import os
import re
import shlex
import signal
import subprocess
import time
from typing import Optional, List, Union, Dict
from dataclasses import dataclass

from utils.cloud.cloud_utils import get_user_context
from utils.terminal.terminal_ssh_setup_local import ensure_local_ssh_keys

logger = logging.getLogger(__name__)


@dataclass
class CompletedProcess:
    """Mimics subprocess.CompletedProcess for compatibility."""
    args: Union[str, List[str]]
    returncode: int
    stdout: str
    stderr: str


def terminal_run(
    args: Union[str, List[str]],
    *,
    capture_output: bool = False,
    text: bool = False,
    shell: bool = False,
    timeout: Optional[int] = None,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    trusted: bool = False,
    **kwargs
) -> CompletedProcess:
    """
    Drop-in replacement for subprocess.run() that executes in terminal pods.
    
    Compatible with subprocess.run() API. Usage:
        Replace: subprocess.run(...) 
        With:    terminal_run(...)
    
    When ENABLE_POD_ISOLATION=false (local dev), uses subprocess.run() directly.
    When ENABLE_POD_ISOLATION=true (k8s prod), uses terminal pods for isolation.
    
    Args:
        args: Command and arguments (string or list)
        capture_output: If True, capture stdout/stderr
        text: If True, return text instead of bytes
        shell: If True, run command through shell
        timeout: Command timeout in seconds (default: 300)
        cwd: Working directory
        env: Environment variables to set
        trusted: If True, skip safety guardrail checks (for internal infra ops)
        **kwargs: Other subprocess.run() arguments
    
    Returns:
        CompletedProcess with returncode, stdout, stderr
    """
    # --- Safety guardrails ---
    if not trusted:
        blocked = _check_guardrails(args)
        if blocked is not None:
            return blocked
    # Check if pod isolation is enabled (default: true for security)
    # Only explicitly set to "false" for local development
    enable_pod_isolation = os.getenv('ENABLE_POD_ISOLATION', 'true') == 'true'
    
    if not enable_pod_isolation:
        # LOCAL DEV MODE: Use direct subprocess execution
        logger.debug("Pod isolation disabled, using direct subprocess execution")
        
        # Setup SSH keys locally if user context available
        try:
            context = get_user_context()
            if context and context.get('user_id'):
                ensure_local_ssh_keys(context['user_id'])
        except Exception as e:
            logger.warning(f"Failed to setup SSH keys in local dev mode: {e}")
        
        # Convert args to list if needed
        if isinstance(args, str):
            if shell:
                cmd = args
            else:
                cmd = shlex.split(args)
        else:
            cmd = args
        
        try:
            # Use Popen + cancel-aware wait so the user-cancel signal can
            # SIGTERM the process group instead of letting a long terraform /
            # kubectl run to completion after the user has moved on.
            return run_with_cancel(
                cmd=cmd,
                args=args,
                capture_output=capture_output,
                text=text,
                shell=shell,
                timeout=timeout or 300,
                cwd=cwd,
                env=env,
                **kwargs,
            )
        except subprocess.TimeoutExpired as e:
            logger.error(f"Command timed out after {timeout}s: {cmd}")
            return CompletedProcess(
                args=args,
                returncode=124,  # Standard timeout exit code
                stdout=e.stdout.decode() if e.stdout else "",
                stderr=f"Command timed out after {timeout} seconds"
            )
        except asyncio.CancelledError:
            # Propagate cancel up through tool wrappers; do not swallow.
            raise
        except Exception as e:
            logger.error(f"Subprocess execution failed: {e}")
            return CompletedProcess(
                args=args,
                returncode=127,
                stdout="",
                stderr=str(e)
            )
    
    # K8S PROD MODE: Use terminal pod execution
    logger.debug("Pod isolation enabled, using terminal pod execution")
    
    # Import here to avoid circular dependencies
    from utils.tools.tool_executor import get_tool_executor
    
    # Get user context
    try:
        context = get_user_context()
        user_id = context.get('user_id')
        session_id = context.get('session_id')
    except Exception as e:
        logger.error(f"Failed to get user context: {e}")
        raise RuntimeError(f"Cannot execute command without user context: {e}") from e
    
    # Require user context for terminal pod execution
    if not user_id or not session_id:
        logger.error(f"Missing user context: user_id={user_id}, session_id={session_id}")
        raise RuntimeError("Cannot execute command: user_id and session_id required for terminal pod isolation")
    
    # Convert args to command string
    if isinstance(args, list):
        # Check if this is a bash -c command (common pattern for chaining)
        if len(args) >= 3 and args[0] == "bash" and args[1] == "-c":
            # Preserve the bash -c <script> pattern - don't just join with spaces
            command = f"bash -c {shlex.quote(args[2])}"
        else:
            # Join list into shell command
            command = ' '.join(shlex.quote(str(arg)) for arg in args)
    else:
        command = str(args)
    
    # Get tool executor
    try:
        executor = get_tool_executor()
    except Exception as e:
        logger.error(f"Failed to get tool executor: {e}")
        raise RuntimeError(f"Cannot execute command: tool executor unavailable: {e}") from e
    
    # Setup environment variables if provided
    # Keep the original user command for safe logging (before env exports are prepended)
    loggable_command = command
    if env:
        env_setup_commands = []
        for key, value in env.items():
            # Escape value for shell
            escaped_value = value.replace("'", "'\"'\"'")
            env_setup_commands.append(f"export {key}='{escaped_value}'")

        # Prepend env setup to command
        command = '; '.join(env_setup_commands) + f'; {command}'

    # Redact known sensitive flags from the loggable command
    redacted_command = re.sub(r'(--password\s+)\S+', r'\1[REDACTED]', loggable_command)

    # Execute command in terminal pod
    logger.info(f"Executing command in terminal pod for user {user_id}, session {session_id}: {redacted_command[:100]}")
    # Pre-call cancel gate: cheap check before we start a pod exec that may
    # block this thread for the full timeout window.
    try:
        from chat.backend.agent.utils.cancel_token import raise_if_cancelled
        raise_if_cancelled()
    except ImportError:
        pass
    try:
        returncode, stdout, stderr = executor.execute_command(
            user_id=user_id,
            session_id=session_id,
            command=command,
            timeout=timeout or 300,
            working_dir=cwd
        )
    except Exception as e:
        logger.error(f"Terminal pod execution failed for user {user_id}, session {session_id}: {e}")
        raise RuntimeError(f"Command execution failed in terminal pod: {e}") from e
    
    # Log execution result
    if returncode != 0:
        logger.warning(f"Command exited with code {returncode} (user={user_id}, session={session_id}): {stderr[:200]}")
    
    # Return subprocess-compatible result
    return CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr
    )


def _check_guardrails(args: Union[str, List[str]]) -> Optional[CompletedProcess]:
    """Run signature check + LLM judge. Returns CompletedProcess if blocked, else None."""
    from utils.security.command_safety import evaluate_command

    cmd = args if isinstance(args, str) else shlex.join(str(a) for a in args)

    uid, sid = None, None
    try:
        ctx = get_user_context()
        uid, sid = ctx.get("user_id"), ctx.get("session_id")
    except Exception:
        logger.debug("[Guardrails] user context unavailable; proceeding without it", exc_info=True)

    decision = evaluate_command(cmd, tool="terminal_run", user_id=uid, session_id=sid)
    if not decision.blocked:
        return None
    return CompletedProcess(
        args=args, returncode=126, stdout="",
        stderr=f"Blocked by safety guardrail: {decision.reason}",
    )


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the child's process group. POSIX-only.

    On Windows ``preexec_fn`` is unavailable; we fall back to ``proc.kill()``.
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError) as exc:
        logger.debug("kill_process_group SIGTERM failed: %s", exc)

    # Give the child a brief grace period to flush output, then escalate.
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug("kill_process_group SIGKILL failed: %s", exc)


def run_with_cancel(
    *,
    cmd: Union[str, List[str]],
    args: Union[str, List[str]],
    capture_output: bool,
    text: bool,
    shell: bool,
    timeout: int,
    cwd: Optional[str],
    env: Optional[Dict[str, str]],
    **kwargs,
) -> CompletedProcess:
    """Run ``cmd`` via ``Popen`` with cancel-token + timeout watchdog.

    Behaviour matches ``subprocess.run`` for the success / timeout cases used
    by callers, but additionally polls the cancel token (see
    ``chat.backend.agent.utils.cancel_token``) and SIGTERMs the entire
    process group when cancel is signalled.
    """
    # Lazy import to avoid pulling agent code into utility init.
    try:
        from chat.backend.agent.utils.cancel_token import is_cancelled
    except ImportError:  # pragma: no cover — utility may be used standalone
        def is_cancelled() -> bool:  # type: ignore[misc]
            return False

    use_shell = shell if isinstance(cmd, str) else False
    popen_kwargs: Dict[str, object] = {
        "stdout": subprocess.PIPE if capture_output else None,
        "stderr": subprocess.PIPE if capture_output else None,
        "shell": use_shell,
        "cwd": cwd,
        "env": env,
        "text": text,
    }
    if os.name == "posix":
        # New session/process group so we can SIGTERM the whole tree.
        popen_kwargs["start_new_session"] = True
    popen_kwargs.update(kwargs)

    proc = subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603 — args validated upstream

    deadline = time.monotonic() + timeout
    poll_interval = 0.1
    cancelled = False
    stdout: object = "" if text else b""
    stderr: object = "" if text else b""

    # Poll the process: check cancel/timeout every poll_interval, drain
    # stdio once when the child has exited.
    while True:
        if proc.poll() is not None:
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            break

        if is_cancelled():
            cancelled = True
            logger.info("[terminal_run] cancel signaled; SIGTERM-ing pid=%s", proc.pid)
            _kill_process_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            break

        if time.monotonic() >= deadline:
            _kill_process_group(proc)
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        time.sleep(poll_interval)

    if cancelled:
        # Surface as CancelledError so the tool wrapper aborts the LangGraph
        # node instead of returning a partial success payload.
        raise asyncio.CancelledError("terminal_run cancelled by user")

    def _coerce(val: object) -> str:
        if val is None:
            return ""
        if isinstance(val, bytes):
            try:
                return val.decode("utf-8", errors="replace")
            except (UnicodeDecodeError, AttributeError):
                return ""
        return str(val)

    return CompletedProcess(
        args=args,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=_coerce(stdout) if capture_output else "",
        stderr=_coerce(stderr) if capture_output else "",
    )
