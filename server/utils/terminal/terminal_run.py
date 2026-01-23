"""Drop-in replacement for subprocess.run() that executes in terminal pods.

This module provides a terminal_run() function that mimics subprocess.run() API
but executes commands in isolated terminal pods via kubectl exec.
"""

import logging
import os
import shlex
import subprocess
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
        **kwargs: Other subprocess.run() arguments
    
    Returns:
        CompletedProcess with returncode, stdout, stderr
    """
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
            # Use subprocess.run() directly
            result = subprocess.run(
                cmd,
                capture_output=capture_output,
                text=text,
                shell=shell if isinstance(cmd, str) else False,
                timeout=timeout or 300,
                cwd=cwd,
                env=env,  # Use env directly (already contains PATH, HOME, etc.)
                **kwargs
            )
            
            return CompletedProcess(
                args=args,
                returncode=result.returncode,
                stdout=result.stdout if capture_output else "",
                stderr=result.stderr if capture_output else ""
            )
        except subprocess.TimeoutExpired as e:
            logger.error(f"Command timed out after {timeout}s: {cmd}")
            return CompletedProcess(
                args=args,
                returncode=124,  # Standard timeout exit code
                stdout=e.stdout.decode() if e.stdout else "",
                stderr=f"Command timed out after {timeout} seconds"
            )
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
    if env:
        env_setup_commands = []
        for key, value in env.items():
            # Escape value for shell
            escaped_value = value.replace("'", "'\"'\"'")
            env_setup_commands.append(f"export {key}='{escaped_value}'")
        
        # Prepend env setup to command
        command = '; '.join(env_setup_commands) + f'; {command}'
    
    # Execute command in terminal pod
    logger.info(f"Executing command in terminal pod for user {user_id}, session {session_id}: {command[:100]}")
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

