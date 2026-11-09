"""search_runbooks / search_memory / check_change hit the recall endpoints."""

import asyncio

from aurora_mcp.tools_always_on import register_tier1_tools
from tests.mcp.conftest import FakeMCP, make_captured_api_call


def _tools():
    mcp = FakeMCP()
    api_call, captured = make_captured_api_call()
    register_tier1_tools(mcp, api_call)
    return mcp.tools, captured


def test_search_runbooks_uses_memory_search():
    tools, captured = _tools()
    res = asyncio.run(tools["search_runbooks"]("redis oom", 3))
    mem = [c for c in captured if c[1] == "/api/memory/search"]
    assert mem == [("GET", "/api/memory/search",
                    {"q": "redis oom", "category": "runbook,context", "k": 3}, None)]
    assert any(c[1] == "/sharepoint/search" for c in captured)
    assert [s["source"] for s in res["sources"]] == ["memory", "sharepoint"]


def test_search_memory_omits_empty_category_and_clamps():
    tools, captured = _tools()
    asyncio.run(tools["search_memory"]("q", "", 500))
    asyncio.run(tools["search_memory"]("q", "learned", 0))
    assert captured[0][2] == {"q": "q", "k": 20}
    assert captured[1][2] == {"q": "q", "k": 1, "category": "learned"}


def test_check_change_posts_body():
    tools, captured = _tools()
    asyncio.run(tools["check_change"](diff="d", files=["a.py"], description="x"))
    asyncio.run(tools["check_change"]())
    assert captured[0] == ("POST", "/api/memory/check-change", None,
                           {"diff": "d", "files": ["a.py"], "description": "x"})
    assert captured[1][3] == {"diff": "", "files": [], "description": ""}
