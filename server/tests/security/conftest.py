"""Shared helpers for security tests."""

import re

from utils.security.signature_match import check_signature
from utils.auth.command_policy import _UNIVERSAL_DENY_RULES


def sig_blocks(cmd: str) -> bool:
    """Return True if the signature matcher catches *cmd*."""
    return check_signature(cmd).matched


def deny_blocks(cmd: str) -> bool:
    """Return True if any universal deny rule matches *cmd*."""
    for raw in _UNIVERSAL_DENY_RULES:
        if re.search(raw["pattern"], cmd):
            return True
    return False


def any_layer_blocks(cmd: str) -> bool:
    """Return True if either the signature matcher or denylist catches *cmd*."""
    return sig_blocks(cmd) or deny_blocks(cmd)
