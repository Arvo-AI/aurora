"""General-purpose terminal execution tool.

Provides direct terminal pod access for operations not covered by specialized tools.
"""

import json
import logging
import re
import shlex
from typing import Optional
from utils.terminal.terminal_run import terminal_run
from .cloud_exec_tool import cloud_exec
from .iac_tool import run_iac_tool

logger = logging.getLogger(__name__)


def _has_shell_metacharacters(command: str) -> bool:
    """Return True if the command includes shell syntax that requires a real shell."""
    patterns = [
        "|", "||", "&&", ";", "$(", "`",
        " 2>", "2>&1", " > ", " >> ", " < ", " & "
    ]
    return any(pat in command for pat in patterns) or command.lstrip().startswith((">", "2>", ">>", "<"))


def _contains_forbidden_elevation(command: str) -> bool:
    """Block privilege escalation attempts such as sudo/pkexec.
    
    EXCEPTION: sudo is allowed INSIDE SSH commands (for remote VM execution).
    e.g., 'ssh user@host "sudo apt update"' is allowed because sudo runs on the remote VM.
    But 'sudo ssh user@host' is blocked because sudo runs locally.
    Also blocked: 'ssh user@host "cmd" && sudo apt update' (chained local sudo after SSH)
    
    Handles mixed quote types: ssh 'user@host' "sudo apt" is correctly allowed.
    """
    lowered = command.lower()
    elevation_pattern = r'(?<!\w)(sudo|pkexec|doas|su)\b'
    
    # Check if this is an SSH command
    if lowered.strip().startswith('ssh '):
        # For SSH commands, only allow sudo INSIDE quoted sections (remote commands)
        # Strategy: Remove ALL quoted content, then check what remains (the local parts)
        
        # Remove all single-quoted and double-quoted sections
        # This regex matches 'anything' or "anything" (non-greedy)
        unquoted_parts = re.sub(r'"[^"]*"', '', lowered)  # Remove double-quoted
        unquoted_parts = re.sub(r"'[^']*'", '', unquoted_parts)  # Remove single-quoted
        
        # Now check only the unquoted (local) parts for elevation commands
        if re.search(elevation_pattern, unquoted_parts):
            return True
        
        # No elevation in local parts - allowed (sudo inside quotes is OK)
        return False
    
    # For non-SSH commands, block any elevation attempt
    return re.search(elevation_pattern, lowered) is not None


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
    
    # Safety check for dangerous patterns
    # Note: Most attacks (privilege escalation, container escape, network attacks)
    # are already blocked by pod security context, network policies, and capabilities.
    # These patterns provide defense-in-depth for resource exhaustion and workspace corruption.
    dangerous_patterns = [
        # Destructive filesystem operations
        "rm -rf /",
        "rm -rf /home/appuser",
        "rm -rf ~",
        "rm -rf .",
        
        # Resource exhaustion (disk fill)
        "dd if=/dev/zero",
        "dd if=/dev/urandom",
        
        # Fork bombs and infinite loops
        ":(){ :|:& };:",
        "while true",
        "while :",
        
        # Crypto mining
        "xmrig",
        "cpuminer",
        "minerd",
        "stratum+tcp",
    ]
    
    for pattern in dangerous_patterns:
        if pattern in command:
            logger.warning(f"Blocked dangerous command for user {user_id}: {command}")
            return json.dumps({
                "success": False,
                "error": f"Command blocked for safety: contains dangerous pattern"
            })

    if _contains_forbidden_elevation(command):
        logger.warning(f"Blocked privilege escalation attempt for user {user_id}: {command}")
        return json.dumps({
            "success": False,
            "error": "Command blocked: privilege escalation is not permitted"
        })
    
    try:
        logger.info(f"Executing terminal command for user {user_id}: {command[:100]}")
        
        result = terminal_run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout or 60,  # 60s default timeout
            cwd=working_dir  # None uses current directory (works for both local & K8s)
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
                f"Terminal command failed (exit code {result.returncode}): {command[:100]}"
            )
        
        return json.dumps(response, indent=2)
    
    except Exception as e:
        logger.error(f"Error executing terminal command: {e}")
        return json.dumps({
            "success": False,
            "error": f"Command execution failed: {str(e)}",
            "command": command,
            "final_command": command,
            "provider": "terminal"
        })