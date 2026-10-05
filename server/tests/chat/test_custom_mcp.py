"""Tests for the custom MCP server connector (DEV-1604).

Pure functions only -- no network, no DB, no fixtures. The storage helpers are
exercised against a fake in-memory Vault blob.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from connectors.mcp_connector import store  # noqa: E402
from connectors.mcp_connector.client import (  # noqa: E402
    MAX_DESCRIPTION_CHARS,
    _describe,
    _is_auth_failure,
    assert_allowed_target,
    build_headers,
    flatten_content,
)
from chat.backend.agent.tools.custom_mcp_tools import _tool_allowed  # noqa: E402
from utils.secrets.secret_ref_utils import SUPPORTED_SECRET_PROVIDERS  # noqa: E402


# --------------------------------------------------------------------------- #
# The silent-death guard
# --------------------------------------------------------------------------- #

def test_mcp_is_a_supported_secret_provider():
    """Without this, every credential read returns None and logs nothing.

    store_tokens_in_db and delete_user_secret have no allowlist check, so
    registration and disconnect would both appear to work while the agent got
    zero tools. No other test would catch it.
    """
    assert "mcp" in SUPPORTED_SECRET_PROVIDERS
    # The provider key must survive get_user_token_data's split('_')[0].
    assert store.PROVIDER.split("_")[0] in SUPPORTED_SECRET_PROVIDERS


# --------------------------------------------------------------------------- #
# SSRF guard
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/mcp",   # cloud metadata
    "http://127.0.0.1:8200/mcp",    # Vault on localhost
    "http://10.0.0.5/mcp",
    "http://192.168.1.10/mcp",
])
def test_private_targets_rejected_by_default(url, monkeypatch):
    monkeypatch.delenv("MCP_ALLOW_PRIVATE_TARGETS", raising=False)
    with pytest.raises(ValueError):
        assert_allowed_target(url)


def test_private_targets_allowed_when_opted_in(monkeypatch):
    monkeypatch.setenv("MCP_ALLOW_PRIVATE_TARGETS", "true")
    assert_allowed_target("http://10.0.0.5/mcp")  # must not raise


def test_non_http_scheme_rejected(monkeypatch):
    monkeypatch.setenv("MCP_ALLOW_PRIVATE_TARGETS", "true")
    for url in ("file:///etc/passwd", "ftp://example.com/mcp", "not-a-url"):
        with pytest.raises(ValueError):
            assert_allowed_target(url)


# --------------------------------------------------------------------------- #
# Read/write classification -- the case the built-in denylist gets wrong
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", [
    "get_zone", "list_zones", "search_logs", "read_config",
    "describe_cluster", "query_metrics", "fetch_status", "check_health",
    "list-zones",
])
def test_read_prefixes_classified_as_reads(name):
    assert store.is_read_tool(name)


@pytest.mark.parametrize("name", [
    "purge_cache",     # the denylist in mcp_tools misses all of these
    "apply",
    "restart_pod",
    "scale",
    "execute",
    "rotate_credentials",
    "",
])
def test_everything_else_is_a_write(name):
    assert not store.is_read_tool(name)


# --------------------------------------------------------------------------- #
# Tool naming -- a bad name fails the whole model request, not just one tool
# --------------------------------------------------------------------------- #

def test_tool_name_is_namespaced():
    assert store.qualified_tool_name("netbox", "list_devices") == "mcp_netbox_list_devices"


def test_long_names_stay_within_the_64_char_limit():
    name = store.qualified_tool_name(
        "observability-staging", "get_zone_analytics_by_dimension_and_region"
    )
    assert len(name) <= store.MAX_TOOL_NAME_CHARS
    assert store.TOOL_NAME_RE.match(name)


def test_truncated_names_do_not_collide():
    """Two long tools sharing a prefix must not collapse onto one name."""
    a = store.qualified_tool_name("obs", "get_a_very_long_tool_name_sharing_a_prefix_one")
    b = store.qualified_tool_name("obs", "get_a_very_long_tool_name_sharing_a_prefix_two")
    assert a != b
    assert len(a) <= store.MAX_TOOL_NAME_CHARS and len(b) <= store.MAX_TOOL_NAME_CHARS


@pytest.mark.parametrize("label,tool", [
    ("my server", "get thing"),          # spaces
    ("srv", "get.thing/sub"),            # dots and slashes
    ("srv", "get:thing"),                # colon
])
def test_illegal_characters_are_stripped(label, tool):
    assert store.TOOL_NAME_RE.match(store.qualified_tool_name(label, tool))


def test_label_slugification():
    assert store.slugify_label("  My NetBox! ") == "my-netbox"
    assert store.slugify_label("") == ""
    assert store.slugify_label("***") == ""
    assert len(store.slugify_label("x" * 100)) <= store.MAX_LABEL_CHARS


# --------------------------------------------------------------------------- #
# Context filtering
# --------------------------------------------------------------------------- #

def _server(**overrides):
    base = {
        "label": "netbox",
        "url": "https://mcp.example.com/mcp",
        "transport": "streamable_http",
        "auth": {"type": "bearer", "token": "secret-value"},
        "read_only": False,
        "allow_in_background": [],
        "tools": [
            {"name": "list_devices", "description": "List devices", "inputSchema": {}},
            {"name": "restart_device", "description": "Restart", "inputSchema": {}},
        ],
    }
    base.update(overrides)
    return base


def test_background_withholds_writes_but_keeps_reads():
    srv = _server()
    assert _tool_allowed(srv, "list_devices", is_background=True, is_pr_review=False)
    assert not _tool_allowed(srv, "restart_device", is_background=True, is_pr_review=False)
    assert not _tool_allowed(srv, "restart_device", is_background=False, is_pr_review=True)
    # Foreground chat may offer the write (gate_action prompts the human).
    assert _tool_allowed(srv, "restart_device", is_background=False, is_pr_review=False)


def test_allow_in_background_readmits_a_named_write():
    srv = _server(allow_in_background=["restart_device"])
    assert _tool_allowed(srv, "restart_device", is_background=True, is_pr_review=False)


# --------------------------------------------------------------------------- #
# Storage round-trip against a fake blob
# --------------------------------------------------------------------------- #

@pytest.fixture
def fake_vault(monkeypatch):
    """In-memory stand-in for the Vault blob."""
    state = {"blob": None}

    monkeypatch.setattr(store, "list_servers", lambda uid: list(
        (state["blob"] or {}).get("servers", [])
    ))

    def _save(user_id, servers):
        state["blob"] = {"servers": servers} if servers else None

    monkeypatch.setattr(store, "save_servers", _save)
    return state


def test_upsert_replaces_by_label(fake_vault):
    ok, err = store.upsert_server("u1", _server())
    assert ok and not err
    assert len(fake_vault["blob"]["servers"]) == 1

    ok, _ = store.upsert_server("u1", _server(url="https://new.example.com/mcp"))
    assert ok
    servers = fake_vault["blob"]["servers"]
    assert len(servers) == 1, "same label must replace, not duplicate"
    assert servers[0]["url"] == "https://new.example.com/mcp"


def test_second_distinct_label_is_added(fake_vault):
    store.upsert_server("u1", _server())
    store.upsert_server("u1", _server(label="grafana-internal"))
    assert len(fake_vault["blob"]["servers"]) == 2


def test_server_cap_enforced(fake_vault):
    for i in range(store.MAX_SERVERS):
        ok, _ = store.upsert_server("u1", _server(label=f"srv-{i}"))
        assert ok
    ok, err = store.upsert_server("u1", _server(label="one-too-many"))
    assert not ok and "At most" in err


def test_removing_last_server_clears_the_blob(fake_vault):
    store.upsert_server("u1", _server())
    assert store.remove_server("u1", "netbox") is True
    assert fake_vault["blob"] is None, "last removal must delete the secret"
    assert store.remove_server("u1", "netbox") is False


# --------------------------------------------------------------------------- #
# Credential handling
# --------------------------------------------------------------------------- #

def test_summary_never_leaks_credentials():
    summary = store.server_summary(_server())
    assert "secret-value" not in repr(summary)
    assert summary["authType"] == "bearer"
    assert summary["toolCount"] == 2
    writes = [t for t in summary["tools"] if t["write"]]
    assert [t["name"] for t in writes] == ["restart_device"]


def test_header_building():
    assert build_headers({"type": "bearer", "token": "abc"}) == {"Authorization": "Bearer abc"}
    assert build_headers(
        {"type": "header", "header_name": "X-Api-Key", "token": "k"}
    ) == {"X-Api-Key": "k"}
    assert build_headers({"type": "none"}) == {}
    assert build_headers(None) == {}
    # Missing token must not produce a malformed header.
    assert build_headers({"type": "bearer"}) == {}


# --------------------------------------------------------------------------- #
# Auth-vs-transport discrimination (controls the SSE retry)
# --------------------------------------------------------------------------- #

def test_auth_failures_are_detected():
    class Resp:
        status_code = 401

    class Err(Exception):
        response = Resp()

    assert _is_auth_failure(Err())
    assert _is_auth_failure(Exception("HTTP 403 Forbidden"))
    assert not _is_auth_failure(Exception("Connection refused"))


# --------------------------------------------------------------------------- #
# Error messages the user actually sees
# --------------------------------------------------------------------------- #

def test_taskgroup_wrapper_is_unwrapped():
    """The SDK buries real causes in an anyio ExceptionGroup.

    Left alone, a mistyped URL reports "unhandled errors in a TaskGroup
    (1 sub-exception)", which tells the user nothing.
    """
    inner = ConnectionError("All connection attempts failed")
    group = BaseExceptionGroup("unhandled errors in a TaskGroup", [inner])
    described = _describe(group)
    assert "TaskGroup" not in described
    assert "All connection attempts failed" in described


def test_describe_never_returns_empty():
    assert _describe(Exception())
    assert _describe(ValueError("bad url"))


# --------------------------------------------------------------------------- #
# Result flattening
# --------------------------------------------------------------------------- #

def test_flatten_text_content():
    class Item:
        text = "hello"

    class Result:
        content = [Item()]
        isError = False

    assert flatten_content(Result()) == "hello"


def test_flatten_marks_errors_and_empty_results():
    class Result:
        content = []
        isError = True

    assert "error" in flatten_content(Result()).lower()

    class Empty:
        content = []
        isError = False

    assert flatten_content(Empty()) == "(tool returned no content)"


def test_probe_does_not_truncate_before_read_filtering():
    """probe() must return every tool; the route caps *after* filtering.

    A server listing 30 writes followed by 5 reads would otherwise report
    "no read-only tools" when registered read-only, because the reads fell
    outside the cap.
    """
    import inspect

    from connectors.mcp_connector import client as mcp_client

    source = inspect.getsource(mcp_client.probe)
    assert "tools[:MAX_TOOLS_PER_SERVER]" not in source, (
        "probe truncates before the route can filter reads from writes"
    )
    route_source = inspect.getsource(
        __import__("routes.mcp.mcp_routes", fromlist=["_register"])._register
    )
    filter_at = route_source.index("is_read_tool")
    cap_at = route_source.index("MAX_TOOLS_PER_SERVER")
    assert filter_at < cap_at, "cap must come after the read/write filter"


def test_description_cap_is_sane():
    assert MAX_DESCRIPTION_CHARS <= 2000


# --------------------------------------------------------------------------- #
# Skill wiring
# --------------------------------------------------------------------------- #

_SKILL_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, os.pardir,
    "chat", "backend", "agent", "skills", "integrations", "mcp", "SKILL.md",
)


def _skill_text() -> str:
    with open(os.path.abspath(_SKILL_PATH), encoding="utf-8") as fh:
        return fh.read()


def test_skill_connection_check_points_at_a_real_function():
    """A typo in the frontmatter silently disables the skill with no error."""
    import importlib
    import re as _re

    text = _skill_text()
    module = _re.search(r"^\s*module:\s*(\S+)", text, _re.M).group(1)
    function = _re.search(r"^\s*function:\s*(\S+)", text, _re.M).group(1)
    assert callable(getattr(importlib.import_module(module), function))


def test_skill_template_uses_single_braces():
    """resolve_template substitutes {key}; {{key}} would leak braces to the LLM."""
    text = _skill_text()
    assert "{mcp_servers_section}" in text
    assert "{{mcp_servers_section}}" not in text


def test_servers_section_renders_and_flags_writes(monkeypatch):
    monkeypatch.setattr(store, "list_servers", lambda uid: [_server()])
    section = store.mcp_servers_section("u1")
    assert "mcp_netbox_list_devices" in section
    assert "mcp_netbox_restart_device" in section
    # Only the write tool carries the confirmation marker.
    write_line = next(ln for ln in section.splitlines() if "restart_device" in ln)
    read_line = next(ln for ln in section.splitlines() if "list_devices" in ln)
    assert "needs confirmation" in write_line
    assert "needs confirmation" not in read_line
    assert "secret-value" not in section


def test_servers_section_handles_no_servers(monkeypatch):
    monkeypatch.setattr(store, "list_servers", lambda uid: [])
    assert "no custom MCP servers" in store.mcp_servers_section("u1")
