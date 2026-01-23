import logging
import shlex
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SSHJumpConfig:
    bastion_user: Optional[str]
    bastion_host: str
    bastion_port: int
    target_user: Optional[str]
    target_host: str
    target_port: Optional[int]


def _parse_user_host_port(spec: str) -> tuple[Optional[str], str, Optional[int]]:
    user = None
    host_port = spec
    if "@" in spec:
        user, host_port = spec.split("@", 1)

    host = host_port
    port = None
    if ":" in host_port:
        host, port_str = host_port.rsplit(":", 1)
        if port_str:
            try:
                port = int(port_str)
            except ValueError as exc:
                raise ValueError(f"Invalid port in host spec '{spec}'") from exc

    host = host.strip()
    if not host:
        raise ValueError(f"Host is missing in spec '{spec}'")

    return user.strip() if user else None, host, port


def parse_ssh_jump_command(command: str) -> SSHJumpConfig:
    """Parse an SSH command with a ProxyJump (-J) configuration.

    Supported patterns (single jump hop):
      - ssh -J user@bastion:22 user@target:22
      - ssh -o ProxyJump=user@bastion:22 user@target
      - ssh -J bastion target
      - ssh -J bastion -p 2200 user@target
    """
    tokens = shlex.split(command or "")
    if not tokens:
        raise ValueError("Empty SSH command")

    if tokens[0] == "ssh":
        tokens = tokens[1:]
    if not tokens:
        raise ValueError("Invalid SSH command: missing arguments")

    jump_spec: Optional[str] = None
    target_spec: Optional[str] = None
    target_port_override: Optional[int] = None

    options_with_args = {"-i", "-F", "-l"}

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-J":
            if i + 1 >= len(tokens):
                raise ValueError("Invalid SSH command: -J provided without value")
            jump_spec = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("-J") and len(tok) > 2:
            jump_spec = tok[2:]
            i += 1
            continue
        if tok == "-o" and i + 1 < len(tokens) and tokens[i + 1].startswith("ProxyJump="):
            jump_spec = tokens[i + 1].split("=", 1)[1]
            i += 2
            continue
        if tok.startswith("-o") and "ProxyJump=" in tok:
            jump_spec = tok.split("ProxyJump=", 1)[1]
            i += 1
            continue

        if tok == "-p":
            if i + 1 >= len(tokens):
                raise ValueError("Invalid SSH command: -p provided without value")
            try:
                target_port_override = int(tokens[i + 1])
            except ValueError as exc:
                raise ValueError(f"Invalid port value for -p: '{tokens[i + 1]}'") from exc
            i += 2
            continue
        if tok.startswith("-p") and len(tok) > 2:
            try:
                target_port_override = int(tok[2:])
            except ValueError as exc:
                raise ValueError(f"Invalid port value for -p: '{tok[2:]}'") from exc
            i += 1
            continue

        if tok in options_with_args:
            if i + 1 >= len(tokens):
                raise ValueError(f"Invalid SSH command: {tok} provided without value")
            i += 2
            continue
        if tok.startswith("-"):
            # Skip other options
            i += 1
            continue

        if target_spec is None:
            target_spec = tok
        i += 1

    if not jump_spec:
        raise ValueError("Jump host (-J/ProxyJump) not found in SSH command")
    if not target_spec:
        raise ValueError("Target host missing in SSH command")

    bastion_user, bastion_host, bastion_port = _parse_user_host_port(jump_spec)
    target_user, target_host, target_port = _parse_user_host_port(target_spec)
    target_port_final = target_port_override if target_port_override is not None else target_port

    return SSHJumpConfig(
        bastion_user=bastion_user,
        bastion_host=bastion_host,
        bastion_port=bastion_port or 22,
        target_user=target_user,
        target_host=target_host,
        target_port=target_port_final,
    )
