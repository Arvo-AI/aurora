"""Tests for the custom MCP server connector (DEV-1604).

Pure functions only -- no network, no DB, no fixtures. The storage helpers are
exercised against a fake in-memory Vault blob.
"""

import asyncio
import base64
import hashlib
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from connectors.mcp_connector import oauth, store  # noqa: E402
from connectors.mcp_connector.client import (  # noqa: E402
    MAX_DESCRIPTION_CHARS,
    MAX_TOOLS_PER_SERVER,
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


@pytest.mark.parametrize("name", [
    "resolve-library-id",   # Context7's mandatory lookup
    "ask_wiki_question",    # DeepWiki
    "find_services", "show_config", "lookup_host", "explain_query",
])
def test_lookup_verbs_are_reads(name):
    """These read like writes to a naive prefix list but mutate nothing.

    'resolve-library-id' is the regression: classifying Context7's lookup as
    destructive meant a read-only registration dropped it, and query-docs is
    useless without the ID it returns -- so the agent reported it could not
    find anything.
    """
    assert store.is_read_tool(name)


def test_server_annotations_beat_the_name_heuristic():
    """A server that declares readOnlyHint knows better than our verb list."""
    assert store.is_read_tool(
        {"name": "purge_everything", "annotations": {"readOnlyHint": True}}
    )


def test_destructive_hint_overrides_a_read_looking_name():
    assert not store.is_read_tool(
        {"name": "get_and_delete_all", "annotations": {"destructiveHint": True}}
    )


def test_destructive_hint_wins_over_read_only_hint():
    """A server sending both contradicts itself; resolve toward safety."""
    assert not store.is_read_tool({
        "name": "whatever",
        "annotations": {"readOnlyHint": True, "destructiveHint": True},
    })


def test_name_heuristic_still_applies_without_annotations():
    """DeepWiki sends no annotations at all, so the verb list must still work."""
    assert store.is_read_tool({"name": "read_wiki_contents"})
    assert not store.is_read_tool({"name": "delete_wiki", "annotations": {}})


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


READ = {"name": "list_devices", "description": "List devices", "inputSchema": {}}
WRITE = {"name": "restart_device", "description": "Restart", "inputSchema": {}}


def test_background_withholds_writes_but_keeps_reads():
    srv = _server()
    assert _tool_allowed(srv, READ, is_background=True, is_pr_review=False)
    assert not _tool_allowed(srv, WRITE, is_background=True, is_pr_review=False)
    assert not _tool_allowed(srv, WRITE, is_background=False, is_pr_review=True)
    # Foreground chat may offer the write (gate_action prompts the human).
    assert _tool_allowed(srv, WRITE, is_background=False, is_pr_review=False)


def test_allow_in_background_readmits_a_named_write():
    srv = _server(allow_in_background=["restart_device"])
    assert _tool_allowed(srv, WRITE, is_background=True, is_pr_review=False)


def test_read_only_server_withholds_writes_even_in_chat():
    """read_only is enforced at build time now that registration keeps every tool.

    Without this the switch would be decorative: the tools are stored, so
    nothing else would stop the agent from calling them.
    """
    srv = _server(read_only=True)
    assert _tool_allowed(srv, READ, is_background=False, is_pr_review=False)
    assert not _tool_allowed(srv, WRITE, is_background=False, is_pr_review=False)


# --------------------------------------------------------------------------- #
# Per-tool overrides
# --------------------------------------------------------------------------- #

def test_always_beats_every_other_restriction():
    """The user said always, so read_only and RCA must not override them."""
    srv = _server(read_only=True, tool_modes={"restart_device": "always"})
    assert _tool_allowed(srv, WRITE, is_background=True, is_pr_review=False)
    assert _tool_allowed(srv, WRITE, is_background=False, is_pr_review=True)


def test_never_hides_a_tool_the_classifier_called_safe():
    srv = _server(tool_modes={"list_devices": "never"})
    assert not _tool_allowed(srv, READ, is_background=False, is_pr_review=False)


def test_always_suppresses_the_confirmation_prompt():
    """``needs_gate`` is what makes a write prompt; always must clear it."""
    srv = _server(tool_modes={"restart_device": "always"})
    assert store.tool_mode(srv, "restart_device") == "always"
    assert store.tool_mode(srv, "list_devices") == "auto", "default is auto"


def test_set_tool_mode_rejects_junk(fake_vault):
    store.upsert_server("u1", _server())
    assert store.set_tool_mode("u1", "netbox", "restart_device", "sometimes")[0] is False
    assert store.set_tool_mode("u1", "nope", "restart_device", "always")[0] is False
    # A tool the server does not expose is a typo, not a setting to remember.
    assert store.set_tool_mode("u1", "netbox", "no_such_tool", "always")[0] is False


def test_set_tool_mode_persists_without_touching_the_tool_list(fake_vault):
    store.upsert_server("u1", _server())
    assert store.set_tool_mode("u1", "netbox", "restart_device", "always") == (True, "")
    stored = fake_vault["blob"]["servers"][0]
    assert stored["tool_modes"] == {"restart_device": "always"}
    assert len(stored["tools"]) == 2, "changing a mode must not re-probe or drop tools"


def test_legacy_allow_in_background_still_reads_as_always():
    """Servers registered before per-tool modes existed must keep working."""
    assert store.tool_mode(_server(allow_in_background=["restart_device"]),
                           "restart_device") == "always"


def test_ask_mode_keeps_custom_mcp_reads_but_not_writes():
    """Ask mode blocks the whole ``mcp_`` prefix; reads must opt back in.

    Regression: every Context7 tool was dropped after being built, so the agent
    answered from guesswork instead of the server. The classification has to
    travel with the tool because the filter only sees its name.
    """
    from chat.backend.agent.access.mode_access_controller import ModeAccessController
    from langchain_core.tools import StructuredTool

    def _tool(name, read_only):
        return StructuredTool.from_function(
            func=lambda **_: "ok", name=name, description="d",
            metadata={"mcp_read_only": read_only},
        )

    read, write = _tool("mcp_c7_query_docs", True), _tool("mcp_c7_purge", False)
    kept = [t.name for t in ModeAccessController.filter_tools("ask", [read, write])]
    assert kept == ["mcp_c7_query_docs"]
    # Agent mode is unaffected: both remain available.
    assert len(ModeAccessController.filter_tools("agent", [read, write])) == 2


def test_custom_tools_carry_their_read_classification():
    """The metadata tag is what survives into ``filter_tools``."""
    source = (Path(__file__).parents[2] / "chat" / "backend" / "agent" / "tools"
              / "custom_mcp_tools.py").read_text()
    assert '"metadata": {"mcp_read_only": is_read_tool(tool_def)}' in source


def test_fingerprint_changes_when_the_tool_list_would(fake_vault):
    """The tool-list cache keys on this; it must move whenever tools change.

    Regression: registering a server left the agent on a stale cached tool list
    for 10 minutes, so it insisted the new tools did not exist.
    """
    store.upsert_server("u1", _server())
    before = store.fingerprint("u1")
    assert before, "a registered server must produce a non-empty fingerprint"

    store.upsert_server("u1", _server(label="linear"))
    after_add = store.fingerprint("u1")
    assert after_add != before, "registering a server must invalidate"

    store.set_tool_mode("u1", "netbox", "restart_device", "never")
    assert store.fingerprint("u1") != after_add, "a mode change must invalidate"

    store.remove_server("u1", "linear")
    assert store.fingerprint("u1") != after_add, "removal must invalidate"


def test_fingerprint_is_stable_and_fails_soft(monkeypatch):
    """Stable across calls, and a Vault outage must not break tool building."""
    monkeypatch.setattr(store, "list_servers", lambda uid: [_server()])
    assert store.fingerprint("u1") == store.fingerprint("u1")

    def _boom(uid):
        raise RuntimeError("vault down")

    monkeypatch.setattr(store, "list_servers", _boom)
    assert store.fingerprint("u1") == ""


def test_refresh_preserves_tool_modes():
    """A refresh re-probes the server; it must not reset the user's choices.

    Asserted against the source text rather than by calling the route, because
    importing the blueprint needs a live Redis for the OAuth state cache.
    """
    source = (Path(__file__).parents[2] / "routes" / "mcp" / "mcp_routes.py").read_text()
    refresh = source.split("def refresh_server")[1].split("\n@")[0]
    register = source.split("def _register")[1].split("\n@")[0]
    assert "toolModes" in refresh, "refresh must pass the stored modes through"
    assert '"tool_modes": tool_modes' in register, "_register must persist them"


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
# Auth-vs-transport discrimination (gates the OAuth refresh retry)
# --------------------------------------------------------------------------- #

def _http_error(status):
    class Resp:
        status_code = status

    class Err(Exception):
        response = Resp()

    return Err()


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_detected(status):
    assert _is_auth_failure(_http_error(status))


def test_auth_failure_found_through_exception_group_and_cause():
    """The SDK buries the real HTTPStatusError inside an ExceptionGroup."""
    assert _is_auth_failure(BaseExceptionGroup("tg", [_http_error(401)]))

    wrapper = RuntimeError("request failed")
    wrapper.__cause__ = _http_error(403)
    assert _is_auth_failure(wrapper)


@pytest.mark.parametrize(
    "exc",
    [
        Exception("Connection refused"),
        # Status code only. A message mentioning 401 is not a 401: a tool result
        # or proxy body can say it, and treating that as an auth failure both
        # misdirects the user and triggers a pointless token refresh.
        Exception("HTTP 403 Forbidden"),
        Exception("Unauthorized"),
        _http_error(500),
        _http_error(200),
    ],
)
def test_non_auth_failures_are_not_misread(exc):
    assert not _is_auth_failure(exc)


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


def test_probe_returns_every_tool_untruncated():
    """Capping belongs to the caller, which sorts reads first before cutting.

    A server listing 30 writes then 5 reads would otherwise strand the reads
    outside the cap and report "no read-only tools".
    """
    import inspect

    from connectors.mcp_connector import client as mcp_client

    assert "MAX_TOOLS_PER_SERVER" not in inspect.getsource(mcp_client.probe), (
        "probe truncates before the route can sort reads ahead of writes"
    )


def test_registration_cap_keeps_reads_ahead_of_writes():
    """The route's cap must not strand reads behind a wall of writes."""
    from connectors.mcp_connector.client import MAX_TOOLS_PER_SERVER

    tools = (
        [{"name": f"delete_{i}"} for i in range(MAX_TOOLS_PER_SERVER)]
        + [{"name": "get_the_one_that_matters"}]
    )
    kept = sorted(tools, key=lambda t: not store.is_read_tool(t))[:MAX_TOOLS_PER_SERVER]
    assert any(t["name"] == "get_the_one_that_matters" for t in kept)


def test_description_cap_is_sane():
    assert MAX_DESCRIPTION_CHARS <= 2000


# --------------------------------------------------------------------------- #
# tools/list pagination
# --------------------------------------------------------------------------- #

class _Page:
    def __init__(self, names, next_cursor=None):
        self.tools = [
            SimpleNamespace(name=n, description="d", inputSchema={"type": "object"})
            for n in names
        ]
        self.nextCursor = next_cursor


class _PagingSession:
    """Minimal ClientSession stand-in that serves a fixed list of pages."""

    def __init__(self, pages):
        self.pages = pages
        self.cursors = []

    async def list_tools(self, cursor=None):
        self.cursors.append(cursor)
        return self.pages[len(self.cursors) - 1]


def _probe_with(session, monkeypatch):
    """Run probe() against a fake session, bypassing the network."""
    from connectors.mcp_connector import client as mcp_client

    async def fake_attempt(url, auth, chosen, op):
        return await op(session)

    monkeypatch.setattr(mcp_client, "_attempt", fake_attempt)
    monkeypatch.setattr(mcp_client, "assert_allowed_target", lambda url: None)
    return asyncio.run(mcp_client.probe("https://example.com/mcp"))


def test_probe_follows_the_pagination_cursor(monkeypatch):
    """A server that pages its tools must not be registered half-discovered.

    The SDK does not follow nextCursor for us, so without the loop a paginated
    server silently loses every tool after the first page.
    """
    session = _PagingSession([
        _Page(["get_a", "get_b"], next_cursor="p2"),
        _Page(["get_c"], next_cursor=None),
    ])
    tools, _ = _probe_with(session, monkeypatch)

    assert [t["name"] for t in tools] == ["get_a", "get_b", "get_c"]
    assert session.cursors == [None, "p2"]


def test_pagination_stops_on_a_repeating_cursor(monkeypatch):
    """A server returning a constant cursor must not loop forever."""
    from connectors.mcp_connector import client as mcp_client

    session = _PagingSession([_Page(["get_a"], next_cursor="same")] * 50)
    tools, _ = _probe_with(session, monkeypatch)

    assert len(session.cursors) == mcp_client.MAX_TOOL_PAGES
    # Same tool on every page: deduped by name, not listed once per page.
    assert [t["name"] for t in tools] == ["get_a"]


def test_tool_cap_fits_real_servers():
    """GitHub's MCP server exposes 49 tools; a cap below that silently drops them."""
    assert MAX_TOOLS_PER_SERVER >= 49


# --------------------------------------------------------------------------- #
# OAuth 2.1: PKCE, resource binding, refresh
# --------------------------------------------------------------------------- #

AUTH_SERVER = oauth.AuthServer(
    issuer="https://mcp.example.com",
    authorization_endpoint="https://mcp.example.com/authorize",
    token_endpoint="https://mcp.example.com/token",
    registration_endpoint="https://mcp.example.com/register",
    scopes_supported=("read", "write"),
)


def test_authorize_url_carries_pkce_and_resource():
    url, verifier = oauth.build_authorize_url(
        AUTH_SERVER, "client-123", "https://aurora.test/mcp/callback",
        "state-abc", "https://mcp.example.com/mcp",
    )
    params = parse_qs(urlparse(url).query)

    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == ["state-abc"]
    # RFC 8707: without this the token is not bound to one MCP server and can
    # be replayed against another resource behind the same provider.
    assert params["resource"] == ["https://mcp.example.com/mcp"]
    # The challenge is the hash; the verifier must never appear in the URL.
    assert verifier not in url
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    assert params["code_challenge"] == [expected]


def test_authorize_url_preserves_an_existing_query_string():
    server = replace(
        AUTH_SERVER, authorization_endpoint="https://mcp.example.com/authorize?tenant=acme"
    )
    url, _ = oauth.build_authorize_url(
        server, "c", "https://aurora.test/cb", "s", "https://mcp.example.com/mcp"
    )
    params = parse_qs(urlparse(url).query)
    assert params["tenant"] == ["acme"]
    assert params["client_id"] == ["c"]


def test_each_flow_gets_a_fresh_verifier():
    _, first = oauth.build_authorize_url(AUTH_SERVER, "c", "r", "s", "res")
    _, second = oauth.build_authorize_url(AUTH_SERVER, "c", "r", "s", "res")
    assert first != second


def test_refresh_keeps_the_old_token_when_the_server_does_not_rotate(monkeypatch):
    """Servers that do not rotate send refresh_token only on the first grant.

    Dropping it here would disconnect the server at the next expiry.
    """
    async def fake_token_request(endpoint, form):
        assert form["grant_type"] == "refresh_token"
        return {"access_token": "new-access", "expires_in": 3600}

    monkeypatch.setattr(oauth, "_token_request", fake_token_request)
    result = asyncio.run(oauth.refresh_token({
        "type": "oauth", "refresh_token": "keep-me",
        "token_endpoint": "https://mcp.example.com/token", "client_id": "c",
    }))

    assert result["access_token"] == "new-access"
    assert result["refresh_token"] == "keep-me"
    assert result["expires_at"] > time.time()


def test_refresh_adopts_a_rotated_token(monkeypatch):
    async def fake_token_request(endpoint, form):
        return {"access_token": "a2", "refresh_token": "r2"}

    monkeypatch.setattr(oauth, "_token_request", fake_token_request)
    result = asyncio.run(oauth.refresh_token({
        "type": "oauth", "refresh_token": "r1",
        "token_endpoint": "https://mcp.example.com/token", "client_id": "c",
    }))
    assert result["refresh_token"] == "r2"
    # No expires_in: nothing to pre-empt, so a 401 drives the next refresh.
    assert result["expires_at"] is None


def test_refresh_without_a_grant_tells_the_user_to_reconnect():
    with pytest.raises(oauth.OAuthDiscoveryError):
        asyncio.run(oauth.refresh_token({"type": "oauth", "client_id": "c"}))


def test_expiry_uses_a_skew_and_tolerates_a_missing_expiry():
    assert oauth.is_expired({"type": "oauth", "expires_at": time.time() - 1})
    # Inside the skew window: refresh now rather than mid-call.
    assert oauth.is_expired({"type": "oauth", "expires_at": time.time() + 5})
    assert not oauth.is_expired({"type": "oauth", "expires_at": time.time() + 3600})
    assert not oauth.is_expired({"type": "oauth"})
    assert not oauth.is_expired({"type": "bearer", "expires_at": 0})
    assert not oauth.is_expired(None)


def test_oauth_tokens_are_sent_as_bearer():
    assert build_headers({"type": "oauth", "access_token": "tok"}) == {
        "Authorization": "Bearer tok"
    }
    assert build_headers({"type": "oauth"}) == {}


def test_cross_origin_authorization_server_is_rejected(monkeypatch):
    """A server must not redirect consent to a host it does not own.

    Honouring an arbitrary issuer would let a malicious MCP server phish the
    user's credentials for an unrelated provider.
    """
    async def fake_get_json(client, url):
        if "oauth-protected-resource" in url:
            return {"authorization_servers": ["https://evil.example.net"]}
        return None

    monkeypatch.setattr(oauth, "_get_json", fake_get_json)
    monkeypatch.setattr(oauth, "assert_allowed_target", lambda url: None)
    with pytest.raises(oauth.OAuthDiscoveryError, match="different origin"):
        asyncio.run(oauth.discover("https://mcp.example.com/mcp"))


def test_registration_without_an_endpoint_asks_for_a_client_id():
    server = replace(AUTH_SERVER, registration_endpoint=None)
    assert not server.supports_dcr
    with pytest.raises(oauth.OAuthRegistrationUnsupported):
        asyncio.run(oauth.register_client(server, "https://aurora.test/cb"))


def test_oauth_cannot_be_registered_through_the_plain_form():
    """OAuth needs the browser flow; the token field cannot stand in for it."""
    from routes.mcp.mcp_routes import _parse_auth

    _, error = _parse_auth({"authType": "oauth", "token": "pasted"})
    assert "OAuth flow" in error


def test_oauth_endpoints_are_also_ssrf_checked():
    """Discovery, registration, and token calls all follow customer-supplied URLs.

    A server pointing its token endpoint at the metadata service would be a
    clean bypass if only the MCP connection were guarded.
    """
    import inspect

    for fn in (oauth._get_json, oauth.register_client, oauth._token_request):
        assert "assert_allowed_target" in inspect.getsource(fn), fn.__name__


# --------------------------------------------------------------------------- #
# Refresh-on-401 (the whole point of storing a refresh token)
# --------------------------------------------------------------------------- #

OAUTH_AUTH = {
    "type": "oauth",
    "access_token": "stale",
    "refresh_token": "r1",
    "token_endpoint": "https://mcp.example.com/token",
    "client_id": "c",
}


def _run_with_attempts(monkeypatch, attempts, auth, on_refresh=None):
    """Drive _run() with a scripted sequence of attempt outcomes."""
    from connectors.mcp_connector import client as mcp_client

    seen = []

    async def fake_attempt(url, attempt_auth, chosen, op):
        seen.append(attempt_auth)
        outcome = attempts.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(mcp_client, "_attempt", fake_attempt)
    monkeypatch.setattr(mcp_client, "assert_allowed_target", lambda url: None)
    result = asyncio.run(
        mcp_client._run("https://mcp.example.com/mcp", auth, "streamable_http",
                        lambda s: None, on_refresh)
    )
    return result, seen


def test_a_401_refreshes_the_token_and_retries_once(monkeypatch):
    from connectors.mcp_connector import client as mcp_client

    async def fake_refresh(auth):
        return {**auth, "access_token": "fresh", "refresh_token": "r2"}

    monkeypatch.setattr(mcp_client, "refresh_token", fake_refresh)
    stored = []
    (result, _), seen = _run_with_attempts(
        monkeypatch,
        [mcp_client.MCPAuthError("401"), "ok"],
        OAUTH_AUTH,
        on_refresh=stored.append,
    )

    assert result == "ok"
    assert [a["access_token"] for a in seen] == ["stale", "fresh"]
    # The rotated refresh token must be persisted, or the next expiry fails.
    assert stored == [{**OAUTH_AUTH, "access_token": "fresh", "refresh_token": "r2"}]


def test_an_already_expired_token_is_refreshed_before_the_call(monkeypatch):
    """Skip the doomed request when the token is known to be expired.

    Without this every call after expiry pays a guaranteed 401 round-trip to
    the customer's server before refreshing.
    """
    from connectors.mcp_connector import client as mcp_client

    async def fake_refresh(auth):
        return {**auth, "access_token": "fresh"}

    monkeypatch.setattr(mcp_client, "refresh_token", fake_refresh)
    expired = {**OAUTH_AUTH, "expires_at": time.time() - 10}
    (result, _), seen = _run_with_attempts(monkeypatch, ["ok"], expired)

    assert result == "ok"
    # One attempt only, and it already carried the new token.
    assert [a["access_token"] for a in seen] == ["fresh"]


def test_a_valid_token_is_not_refreshed_preemptively(monkeypatch):
    from connectors.mcp_connector import client as mcp_client

    async def explode(_auth):
        raise AssertionError("refreshed a token that had not expired")

    monkeypatch.setattr(mcp_client, "refresh_token", explode)
    valid = {**OAUTH_AUTH, "expires_at": time.time() + 3600}
    (result, _), seen = _run_with_attempts(monkeypatch, ["ok"], valid)
    assert result == "ok"
    assert [a["access_token"] for a in seen] == ["stale"]


def test_a_second_401_is_not_retried_again(monkeypatch):
    """One retry only: looping would hammer the provider's token endpoint."""
    from connectors.mcp_connector import client as mcp_client

    async def fake_refresh(auth):
        return {**auth, "access_token": "fresh"}

    monkeypatch.setattr(mcp_client, "refresh_token", fake_refresh)
    with pytest.raises(mcp_client.MCPAuthError):
        _run_with_attempts(
            monkeypatch,
            [mcp_client.MCPAuthError("401"), mcp_client.MCPAuthError("401")],
            OAUTH_AUTH,
        )


@pytest.mark.parametrize("auth", [
    {"type": "bearer", "token": "t"},
    {"type": "none"},
    None,
    # OAuth but no refresh grant: nothing to retry with.
    {"type": "oauth", "access_token": "a", "token_endpoint": "https://x/token"},
])
def test_non_refreshable_auth_fails_straight_through(monkeypatch, auth):
    """A bad static token must surface immediately, not after a bogus refresh."""
    from connectors.mcp_connector import client as mcp_client

    async def explode(_auth):
        raise AssertionError("refresh attempted for non-refreshable auth")

    monkeypatch.setattr(mcp_client, "refresh_token", explode)
    with pytest.raises(mcp_client.MCPAuthError):
        _run_with_attempts(monkeypatch, [mcp_client.MCPAuthError("401")], auth)


def test_a_failed_refresh_tells_the_user_to_reconnect(monkeypatch):
    from connectors.mcp_connector import client as mcp_client

    async def fake_refresh(auth):
        raise oauth.OAuthDiscoveryError("grant revoked")

    monkeypatch.setattr(mcp_client, "refresh_token", fake_refresh)
    with pytest.raises(mcp_client.MCPAuthError, match="[Rr]econnect"):
        _run_with_attempts(monkeypatch, [mcp_client.MCPAuthError("401")], OAUTH_AUTH)


def test_update_auth_leaves_other_servers_untouched(fake_vault):
    """A refresh must not drop a sibling server from the stored list."""
    store.upsert_server("u1", {"label": "alpha", "url": "https://a/mcp",
                               "auth": {"type": "oauth", "access_token": "old"}, "tools": []})
    store.upsert_server("u1", {"label": "beta", "url": "https://b/mcp",
                               "auth": {"type": "bearer", "token": "t"}, "tools": []})

    store.update_auth("u1", "alpha", {"type": "oauth", "access_token": "new"})

    servers = {s["label"]: s for s in store.list_servers("u1")}
    assert servers["alpha"]["auth"]["access_token"] == "new"
    assert servers["beta"]["auth"]["token"] == "t"
    assert servers["alpha"]["url"] == "https://a/mcp"


def test_update_auth_on_an_unknown_label_is_a_no_op(fake_vault):
    store.upsert_server("u1", {"label": "alpha", "url": "https://a/mcp", "tools": []})
    store.update_auth("u1", "ghost", {"type": "oauth", "access_token": "x"})
    assert len(store.list_servers("u1")) == 1


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
