"""Shared fixtures for MCP unit tests."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


class FakeMCP:
    """Stand-in for FastMCP that captures registered tools instead of binding."""

    def __init__(self) -> None:
        self.tools: Dict[str, Any] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def make_captured_api_call() -> Tuple[Any, List[Tuple]]:
    """Return (api_call, captured) where api_call records every (method, path, params, body)."""
    captured: List[Tuple] = []

    async def api_call(method, path, *, params=None, body=None, timeout=30):
        captured.append((method, path, params, body))
        return {"ok": True, "method": method, "path": path}

    return api_call, captured
