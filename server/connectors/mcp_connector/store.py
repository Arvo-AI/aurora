"""Storage and naming helpers for customer-registered MCP servers.

All servers for an org live in a single Vault secret under the ``mcp`` provider
(one secret per ``(org_id, provider)`` is what ``store_tokens_in_db`` supports).
Shared by the routes and the agent tool builder so both agree on labels, tool
names, and the read/write split.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

PROVIDER = "mcp"

MAX_SERVERS = 10
MAX_LABEL_CHARS = 32

# Model APIs (Anthropic, OpenAI) require ^[a-zA-Z0-9_-]{1,64}$ and reject the
# *entire request* when any tool name violates it -- not just the bad tool. So a
# single malformed customer tool name would break every agent turn for that user.
MAX_TOOL_NAME_CHARS = 64
TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
TOOL_PREFIX = "mcp"

# Read-verb allowlist, used only when the server declares no annotations.
# Inverted relative to the built-in servers' denylist in
# mcp_tools.is_destructive_mcp_tool: that one knows its servers' naming, while a
# customer server can call a destructive tool anything ("purge_cache", "apply",
# "scale"). Unknown names must therefore be treated as writes, not reads.
READ_VERBS = (
    "get", "list", "search", "read", "describe", "query", "fetch", "check",
    # Verbs real servers use for pure lookups: Context7 names its lookup
    # "resolve-library-id", DeepWiki names one "ask_wiki_question". Omitting
    # these classified both as destructive.
    "resolve", "ask", "find", "show", "view", "inspect", "lookup", "count",
    "diff", "explain", "summarize", "analyze", "validate",
)
READ_PREFIXES = tuple(f"{verb}{sep}" for verb in READ_VERBS for sep in ("_", "-"))

# Per-tool user override of the automatic classification.
#   auto   -- follow the classifier (reads free, writes gated, writes off in RCA)
#   always -- the user vouched for this tool: no prompt, available in RCA too
#   never  -- never expose it, whatever the classifier or the server says
TOOL_MODES = ("auto", "always", "never")


def slugify_label(raw: Any) -> str:
    """Normalise a user-supplied label to ``[a-z0-9-]``, or '' if unusable."""
    text = re.sub(r"[^a-z0-9]+", "-", str(raw or "").strip().lower()).strip("-")
    return text[:MAX_LABEL_CHARS].strip("-")


def is_read_tool(tool: Any) -> bool:
    """Whether a tool is safe to run without confirmation.

    Accepts a tool dict (preferred) or a bare name. Precedence:

    1. ``annotations.destructiveHint`` -- an explicit "this mutates" always wins.
    2. ``annotations.readOnlyHint`` -- trust the server author over our guess.
       Context7 sets this on both its tools; ignoring it classified its
       mandatory ``resolve-library-id`` lookup as destructive and withheld it.
    3. Name verb, for servers that send no annotations at all (DeepWiki).

    A hostile server can of course claim ``readOnlyHint`` on a destructive tool.
    That is what the per-tool ``never`` override and the read-only registration
    switch exist for; annotations buy accuracy, not trust.
    """
    if isinstance(tool, dict):
        name = tool.get("name", "")
        annotations = tool.get("annotations")
        if isinstance(annotations, dict):
            if annotations.get("destructiveHint") is True:
                return False
            if annotations.get("readOnlyHint") is True:
                return True
    else:
        name = tool
    return str(name or "").lower().startswith(READ_PREFIXES)


def qualified_tool_name(label: str, tool_name: str) -> str:
    """Build a unique, API-legal LangChain tool name for a server's tool.

    ``mcp_<label>_<tool>`` truncated to 64 chars. When truncation is needed the
    tail is replaced by a short hash of the full name, so two long tool names
    sharing a prefix cannot collapse onto each other.
    """
    safe_label = re.sub(r"[^a-zA-Z0-9]+", "_", label).strip("_")
    safe_tool = re.sub(r"[^a-zA-Z0-9]+", "_", str(tool_name or "")).strip("_")
    name = f"{TOOL_PREFIX}_{safe_label}_{safe_tool}"
    if len(name) <= MAX_TOOL_NAME_CHARS:
        return name
    digest = hashlib.sha256(f"{label}/{tool_name}".encode()).hexdigest()[:8]
    return f"{name[: MAX_TOOL_NAME_CHARS - 9]}_{digest}"


def tool_mode(server: Dict[str, Any], tool_name: str) -> str:
    """The user's override for one tool, defaulting to ``auto``.

    Reads the legacy ``allow_in_background`` list as ``always`` so servers
    registered before per-tool modes existed keep working.
    """
    modes = server.get("tool_modes")
    if isinstance(modes, dict):
        mode = modes.get(tool_name)
        if mode in TOOL_MODES:
            return mode
    if tool_name in (server.get("allow_in_background") or []):
        return "always"
    return "auto"


def fingerprint(user_id: str) -> str:
    """Cheap identity of a user's MCP config, for cache keys.

    Changes whenever a server is registered, refreshed, removed, or a tool mode
    is flipped -- all of which change the tool list the agent is built with.
    Returns "" on any failure so a Vault hiccup degrades to the old behaviour
    (a stale list for one cache window) rather than breaking tool building.
    """
    try:
        return ",".join(sorted(
            f"{s.get('label')}@{s.get('validated_at')}:{sorted((s.get('tool_modes') or {}).items())}"
            for s in list_servers(user_id)
        ))
    except Exception:
        return ""


def server_summary(server: Dict[str, Any]) -> Dict[str, Any]:
    """Credential-free view of a server, safe for API responses and the agent."""
    tools = server.get("tools") or []
    auth = server.get("auth") or {}
    return {
        "label": server.get("label", ""),
        "url": server.get("url", ""),
        "transport": server.get("transport", "streamable_http"),
        "authType": auth.get("type", "none"),
        "readOnly": bool(server.get("read_only", True)),
        "toolCount": len(tools),
        "tools": [
            {
                "name": t.get("name", ""),
                "write": not is_read_tool(t),
                # Lets the UI say "the server told us" rather than "we guessed".
                "declared": isinstance(t.get("annotations"), dict),
                "mode": tool_mode(server, t.get("name", "")),
            }
            for t in tools
        ],
        "validatedAt": server.get("validated_at"),
    }


def list_servers(user_id: str) -> List[Dict[str, Any]]:
    """All registered servers for the caller's org, or [] when none."""
    from utils.auth.token_management import get_token_data

    try:
        blob = get_token_data(user_id, PROVIDER) or {}
    except Exception:
        return []
    servers = blob.get("servers")
    if not isinstance(servers, list):
        return []
    return [
        s for s in servers
        if isinstance(s, dict) and s.get("label") and s.get("url")
    ]


def find_server(user_id: str, label: str) -> Optional[Dict[str, Any]]:
    """One server by label, or None."""
    target = slugify_label(label)
    return next((s for s in list_servers(user_id) if s.get("label") == target), None)


def save_servers(user_id: str, servers: List[Dict[str, Any]]) -> None:
    """Persist the server list, deleting the secret entirely when empty."""
    from utils.auth.token_management import store_tokens_in_db
    from utils.secrets.secret_ref_utils import delete_user_secret

    if not servers:
        delete_user_secret(user_id, PROVIDER)
        return
    store_tokens_in_db(user_id, {"servers": servers}, PROVIDER)


def upsert_server(user_id: str, server: Dict[str, Any]) -> Tuple[bool, str]:
    """Add or replace a server by label. Returns (ok, error_message)."""
    servers = list_servers(user_id)
    label = server.get("label", "")
    existing = next((i for i, s in enumerate(servers) if s.get("label") == label), None)
    if existing is None and len(servers) >= MAX_SERVERS:
        return False, f"At most {MAX_SERVERS} MCP servers can be registered"
    if existing is None:
        servers.append(server)
    else:
        servers[existing] = server
    save_servers(user_id, servers)
    return True, ""


def set_tool_mode(user_id: str, label: str, tool_name: str, mode: str) -> Tuple[bool, str]:
    """Persist one tool's override. Returns (ok, error_message).

    Targeted rather than a full upsert so changing a mode never re-probes the
    server or risks overwriting its cached tool list.
    """
    if mode not in TOOL_MODES:
        return False, f"mode must be one of: {', '.join(TOOL_MODES)}"

    target = slugify_label(label)
    servers = list_servers(user_id)
    server = next((s for s in servers if s.get("label") == target), None)
    if server is None:
        return False, "Unknown MCP server"
    if not any(t.get("name") == tool_name for t in server.get("tools") or []):
        return False, f"Server '{target}' exposes no tool named '{tool_name}'"

    modes = dict(server.get("tool_modes") or {})
    # Drop the legacy list once modes exist, so the two cannot disagree.
    for legacy in server.pop("allow_in_background", None) or []:
        modes.setdefault(legacy, "always")
    if mode == "auto":
        modes.pop(tool_name, None)
    else:
        modes[tool_name] = mode
    server["tool_modes"] = modes

    save_servers(user_id, servers)
    return True, ""


def remove_server(user_id: str, label: str) -> bool:
    """Remove one server by label. Returns False when the label was unknown."""
    target = slugify_label(label)
    servers = list_servers(user_id)
    remaining = [s for s in servers if s.get("label") != target]
    if len(remaining) == len(servers):
        return False
    save_servers(user_id, remaining)
    return True


def update_auth(user_id: str, label: str, auth: Dict[str, Any]) -> None:
    """Replace one server's stored credentials, leaving everything else intact.

    Used after an OAuth refresh. Re-reads the server list rather than taking a
    caller-held copy so a concurrent edit to a *different* server is not lost.

    ponytail: still a read-modify-write, so two parallel refreshes of the SAME
    server can clobber each other -- the loser's rotated refresh token is lost
    and that server needs reconnecting. Accepted: the window is one HTTP
    round-trip and the damage is recoverable from the UI. Upgrade path is a
    short Redis lock keyed on (org_id, label) around refresh-and-store.
    """
    target = slugify_label(label)
    servers = list_servers(user_id)
    found = False
    for server in servers:
        if server.get("label") == target:
            server["auth"] = auth
            found = True
            break
    if found:
        save_servers(user_id, servers)


def mcp_servers_section(user_id: str) -> str:
    """Render connected servers and their tools for the skill template.

    Lives here rather than beside the tool builder so ``SkillRegistry`` can
    import it without pulling in langchain.
    """
    servers = list_servers(user_id)
    if not servers:
        return "(no custom MCP servers registered)"

    lines: List[str] = []
    for server in servers:
        tools = server.get("tools") or []
        lines.append(f"- {server['label']} ({len(tools)} tools)")
        for tool in tools:
            name = tool.get("name", "")
            marker = "" if is_read_tool(tool) else "  [write — needs confirmation]"
            summary = (tool.get("description") or "").strip().splitlines()
            first_line = summary[0][:120] if summary else ""
            lines.append(
                f"    {qualified_tool_name(server['label'], name)}: {first_line}{marker}"
            )
    return "\n".join(lines)
