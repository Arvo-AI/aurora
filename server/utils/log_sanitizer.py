"""Sanitize user-controlled values before they reach log statements (S5145)."""

import re

_CONTROL_CHARS = re.compile(r"[\r\n\t\x00-\x1f\x7f]")


def sanitize(value: object) -> str:
    """Strip control characters that could be used for log injection."""
    return _CONTROL_CHARS.sub("", str(value))
