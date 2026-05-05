"""General-purpose terminal execution tool.

Provides direct terminal pod access for operations not covered by specialized tools.
"""

import json
import logging
import os
import re
import shlex
from typing import Optional, Dict
from utils.terminal.terminal_run import terminal_run
from .cloud_exec_tool import cloud_exec
from .iac_tool import run_iac_tool

logger = logging.getLogger(__name__)


# Keys that are safe to pass through to child processes.
# Everything else (VAULT_TOKEN, DATABASE_URL, SECRET_KEY, etc.) is stripped.
_SAFE_ENV_KEYS = {
    "PATH", "HOME", "USER", "SHELL", "TERM", "LANG", "LC_ALL", "LC_CTYPE",
    "TZ", "HOSTNAME", "PWD", "LOGNAME",
    "ENABLE_POD_ISOLATION",
    "TMPDIR", "TEMP", "TMP",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
}


def _build_sanitized_env() -> Dict[str, str]:
    """Build a minimal environment dict from the current process env.

    Only passes through safe, non-secret variables so that commands
    executed via terminal_exec cannot inspect server secrets like
    VAULT_TOKEN, DATABASE_URL, or cloud credentials.
    """
    sanitized = {}
    for key in _SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            sanitized[key] = value
    return sanitized


def _has_shell_metacharacters(command: str) -> bool:
    """Return True if the command includes shell syntax that requires a real shell."""
    patterns = [
        "|", "||", "&&", ";", "$(", "`",
        " 2>", "2>&1", " > ", " >> ", " < ", " & "
    ]
    return any(pat in command for pat in patterns) or command.lstrip().startswith((">", "2>", ">>", "<"))


def _transform_ssh_jump_to_proxy(command: str) -> str:
    """
    Transform SSH -J commands to ProxyCommand for proper key propagation.

    The -J (ProxyJump) flag has no mechanism to pass -i (identity file) to the
    jump host connection. This causes authentication failures when using managed
    SSH keys. Converting to ProxyCommand allows explicit -i specification for
    both the bastion and target connections.

    Example:
        Input:  ssh -i ~/.ssh/key -J user@bastion user@target "cmd"
        Output: ssh -i ~/.ssh/key -o ProxyCommand="ssh -i ~/.ssh/key -W %h:%p user@bastion -p 22" user@target "cmd"
    """
    # Only process ssh commands
    if not command.strip().startswith('ssh '):
        return command

    # Check for -J flag (case sensitive - SSH uses -J not -j)
    if ' -J ' not in command:
        # Also check for -Jvalue format (no space)
        if not re.search(r'-J\S', command):
            return command

    try:
        tokens = shlex.split(command)
    except ValueError:
        return command  # Can't parse, return as-is

    if not tokens or tokens[0] != 'ssh':
        return command

    # Extract components
    identity_file = None
    jump_spec = None
    target_port = None
    other_options = []
    target = None
    remote_command = []

    i = 1
    while i < len(tokens):
        tok = tokens[i]

        # -i <keyfile>
        if tok == '-i' and i + 1 < len(tokens):
            identity_file = tokens[i + 1]
            i += 2
            continue
        if tok.startswith('-i') and len(tok) > 2:
            identity_file = tok[2:]
            i += 1
            continue

        # -J <jump_spec>
        if tok == '-J' and i + 1 < len(tokens):
            jump_spec = tokens[i + 1]
            i += 2
            continue
        if tok.startswith('-J') and len(tok) > 2:
            jump_spec = tok[2:]
            i += 1
            continue

        # -p <port>
        if tok == '-p' and i + 1 < len(tokens):
            target_port = tokens[i + 1]
            i += 2
            continue
        if tok.startswith('-p') and len(tok) > 2:
            target_port = tok[2:]
            i += 1
            continue

        # Other -o options (preserve them)
        if tok == '-o' and i + 1 < len(tokens):
            other_options.extend(['-o', tokens[i + 1]])
            i += 2
            continue
        if tok.startswith('-o'):
            other_options.append(tok)
            i += 1
            continue

        # Skip other flags (single letter options)
        if tok.startswith('-') and len(tok) == 2:
            other_options.append(tok)
            i += 1
            continue

        # First non-option is target
        if target is None:
            target = tok
            i += 1
            # Everything after target is remote command
            remote_command = tokens[i:]
            break

        i += 1

    # If no -J found or missing required parts, return original
    if not jump_spec or not target:
        return command

    # Parse jump spec: [user@]host[:port]
    jump_user = None
    jump_host = jump_spec
    jump_port = "22"

    if '@' in jump_spec:
        jump_user, jump_host = jump_spec.split('@', 1)
    if ':' in jump_host:
        jump_host, jump_port = jump_host.rsplit(':', 1)

    # Build ProxyCommand
    proxy_parts = ["ssh"]
    if identity_file:
        proxy_parts.extend(["-i", identity_file])
    proxy_parts.extend(["-o", "StrictHostKeyChecking=no"])
    proxy_parts.extend(["-o", "UserKnownHostsFile=/dev/null"])
    proxy_parts.append("-W %h:%p")
    if jump_user:
        proxy_parts.append(f"{jump_user}@{jump_host}")
    else:
        proxy_parts.append(jump_host)
    proxy_parts.extend(["-p", jump_port])

    proxy_cmd = " ".join(proxy_parts)

    # Build final command
    final_parts = ["ssh"]
    if identity_file:
        final_parts.extend(["-i", identity_file])
    final_parts.extend(["-o", f'ProxyCommand="{proxy_cmd}"'])
    final_parts.extend(other_options)
    if target_port:
        final_parts.extend(["-p", target_port])
    final_parts.append(target)
    if remote_command:
        final_parts.extend(remote_command)

    return " ".join(final_parts)


def terminal_exec(
    command: str,
    working_dir: Optional[str] = None,
    timeout: Optional[int] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """
    Execute an arbitrary command in the terminal pod.
    
    Use this tool for operations not covered by cloud_exec or iac_tool:
    - File operations (cat, echo, sed, grep, etc.)
    - Arbitrary Terraform commands (import, taint, workspace, etc.)
    - Other IaC tools (pulumi, helm, ansible, etc.)
    - Development tools (git, npm, pip, make, etc.)
    
    Args:
        command: Shell command to execute
        working_dir: Working directory (default: current directory)
        timeout: Command timeout in seconds (default: 300)
        user_id: User context (auto-injected by framework)
        session_id: Session context (auto-injected by framework)
    
    Returns:
        JSON string with execution results (success, stdout, stderr, returncode)
    """
    
    if not user_id or not session_id:
        logger.error("terminal_exec: user_id and session_id are required")
        return json.dumps({
            "success": False,
            "error": "User context is required but not available"
        })
    
    if not command or not command.strip():
        return json.dumps({
            "success": False,
            "error": "Command cannot be empty"
        })

    # Allow users to force shell execution via prefix (bypass routing)
    force_shell = False
    if command.lower().startswith("sh:"):
        force_shell = True
        command = command.split(":", 1)[1].lstrip()
    elif command.lower().startswith("shell:"):
        force_shell = True
        command = command.split(":", 1)[1].lstrip()

    # Transform SSH -J to ProxyCommand for proper key propagation
    # The -J flag doesn't propagate -i (identity file) to the jump host
    if command.strip().startswith('ssh '):
        original_command = command
        command = _transform_ssh_jump_to_proxy(command)
        if command != original_command:
            logger.info(f"[SSH] Transformed -J to ProxyCommand: {command[:150]}")

    # ROUTING: Check if command should be handled by specialized tools
    cmd_lower = command.lower().strip()
    has_shell_syntax = _has_shell_metacharacters(command)
    allow_routing = not force_shell and not has_shell_syntax

    # Unified gate: signature + org policy + LLM judge + HITL (foreground).
    from utils.auth.command_gate import gate_command
    gate = gate_command(user_id=user_id, tool_name="terminal_exec", command=command)
    if not gate.allowed:
        logger.warning("terminal_exec blocked for user %s (%s): %s",
                       user_id, gate.code, gate.block_reason[:200])
        return json.dumps({
            "success": False,
            "error": gate.block_reason,
            "code": gate.code,
        })

    # Define routing table for cloud commands
    # Provider=None means "use user's provider preference" (for kubectl which works with any cloud)
    CLOUD_ROUTES = [
        ('gcloud ', lambda cmd: ('gcp', cmd[7:])),
        ('gsutil ', lambda cmd: ('gcp', cmd)),
        ('bq ', lambda cmd: ('gcp', cmd)),
        ('kubectl ', lambda cmd: (None, cmd)),  # Inherit user's provider (aurora/gcp/aws/azure)
        ('aws ', lambda cmd: ('aws', cmd[4:])),
        ('az ', lambda cmd: ('azure', cmd[3:])),
    ]
    
    # Route cloud commands to cloud_exec
    if allow_routing:
        for prefix, route_fn in CLOUD_ROUTES:
            if cmd_lower.startswith(prefix):
                provider, transformed_cmd = route_fn(command)
                # If provider is None, get user's provider preference
                if provider is None:
                    from utils.cloud.cloud_utils import get_provider_preference
                    prefs = get_provider_preference()
                    provider = prefs[0] if prefs else 'gcp'  # Default to gcp if no preference
                logger.info(f"[ROUTE] Routing to cloud_exec ({provider}): {command[:60]}")
                return cloud_exec(provider, transformed_cmd, user_id, session_id, timeout=timeout)
    
    # Route terraform workflows to iac_tool
    if allow_routing and cmd_lower.startswith('terraform '):
        parts = cmd_lower.split(maxsplit=2)
        if len(parts) >= 2:
            op = parts[1]
            if op in ['fmt', 'validate', 'refresh', 'plan', 'apply', 'destroy']:
                logger.info(f"[ROUTE] Routing to iac_tool ({op}): {command[:60]}")
                return run_iac_tool(action=op, directory=working_dir or "", user_id=user_id, session_id=session_id)
            if op == 'output':
                logger.info(f"[ROUTE] Routing to iac_tool (outputs): {command[:60]}")
                return run_iac_tool(action='outputs', directory=working_dir or "", user_id=user_id, session_id=session_id)
            if op == 'state' and len(parts) >= 3:
                subcmd = parts[2].split()[0]
                if subcmd in ['list', 'show', 'pull']:
                    logger.info(f"[ROUTE] Routing to iac_tool (state_{subcmd}): {command[:60]}")
                    return run_iac_tool(action=f'state_{subcmd}', directory=working_dir or "", user_id=user_id, session_id=session_id)
    
    try:
        logger.info("Executing terminal command for user %s: %s", user_id, command[:100])
        
        sanitized_env = _build_sanitized_env()
        
        result = terminal_run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout or 60,  # 60s default timeout
            cwd=working_dir,  # None uses current directory (works for both local & K8s)
            env=sanitized_env
        )
        
        success = result.returncode == 0
        output = result.stdout if success else (result.stderr or result.stdout)
        
        # Truncate large outputs (1MB limit)
        MAX_OUTPUT = 1_000_000
        if len(output) > MAX_OUTPUT:
            output = output[:MAX_OUTPUT] + f"\n\n[Output truncated - exceeded {MAX_OUTPUT} bytes]"
        
        # Match cloud_exec format for consistent frontend parsing
        response = {
            "success": success,
            "command": command,
            "final_command": command,
            "return_code": result.returncode,
            "chat_output": output,
            "working_dir": working_dir or "/home/appuser",
            "provider": "terminal"
        }
        
        if not success:
            logger.warning(
                "Terminal command failed (exit code %s): %s",
                result.returncode, command[:100]
            )
        
        return json.dumps(response, indent=2)
    
    except Exception as e:
        logger.error("Error executing terminal command: %s", e)
        return json.dumps({
            "success": False,
            "error": f"Command execution failed: {str(e)}",
            "command": command,
            "final_command": command,
            "provider": "terminal"
        })