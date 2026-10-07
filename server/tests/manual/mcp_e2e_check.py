"""End-to-end check: real MCP server + the connector client, in one process.

Starts a FastMCP Streamable HTTP server on a loopback port, then drives
``probe`` and ``call`` against it, plus the full LangChain tool build. Run
manually -- excluded from CI because it binds a port and needs the mcp SDK.

    MCP_ALLOW_PRIVATE_TARGETS=true python tests/manual/mcp_e2e_check.py
"""

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
os.environ["MCP_ALLOW_PRIVATE_TARGETS"] = "true"
os.environ.setdefault("POSTGRES_DB", "aurora_test")
os.environ.setdefault("POSTGRES_USER", "test_user")
os.environ.setdefault("POSTGRES_PASSWORD", "test_pw")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from connectors.mcp_connector import store  # noqa: E402
from connectors.mcp_connector.client import probe, call  # noqa: E402

PORT = 8931
URL = f"http://127.0.0.1:{PORT}/mcp"
AUTH = {"type": "none"}

server = FastMCP("fake-netbox", stateless_http=True)


@server.tool()
def list_devices(site: str = "all") -> str:
    """List network devices at a site."""
    return f"devices at {site}: router-01, switch-02"


@server.tool()
def restart_device(name: str) -> str:
    """Restart a device. Destructive."""
    return f"restarted {name}"


def serve() -> None:
    server.settings.host = "127.0.0.1"
    server.settings.port = PORT
    server.settings.log_level = "WARNING"
    server.run(transport="streamable-http")


def main() -> int:
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(4)

    tools, transport = asyncio.run(probe(URL, AUTH, "streamable_http"))
    names = sorted(t["name"] for t in tools)
    print(f"probe: transport={transport} tools={names}")
    assert names == ["list_devices", "restart_device"], names
    assert tools[0]["inputSchema"].get("properties"), "schema did not survive probe"

    out = asyncio.run(call(URL, AUTH, transport, "list_devices", {"site": "nyc"}))
    print(f"call:  {out}")
    assert "router-01" in out and "nyc" in out, out

    # Classification must split these two correctly.
    assert store.is_read_tool("list_devices")
    assert not store.is_read_tool("restart_device")

    # Full LangChain build off the cached schemas, with writes withheld in RCA.
    from chat.backend.agent.tools import custom_mcp_tools as cmt
    from chat.backend.agent.tools.custom_mcp_tools import get_custom_mcp_tools

    saved = {
        "label": "netbox", "url": URL, "transport": transport, "auth": AUTH,
        "allow_in_background": [], "tools": tools,
    }
    # Patch the name as custom_mcp_tools imported it, not store's own binding.
    cmt.list_servers = lambda uid: [saved]  # noqa: E731

    chat_tools = {t.name for t in get_custom_mcp_tools("u1")}
    rca_tools = {t.name for t in get_custom_mcp_tools("u1", is_background=True)}
    print(f"chat tools: {sorted(chat_tools)}")
    print(f"rca tools:  {sorted(rca_tools)}")
    assert chat_tools == {"mcp_netbox_list_devices", "mcp_netbox_restart_device"}
    assert rca_tools == {"mcp_netbox_list_devices"}, "RCA must not expose the write"

    # Invoke the generated read tool exactly as the agent would.
    read_tool = next(t for t in get_custom_mcp_tools("u1") if "list_devices" in t.name)
    result = read_tool.invoke({"site": "sfo"})
    print(f"invoke: {result}")
    assert "sfo" in result, result

    # A bad URL must surface as a clean, explanatory error -- not a traceback and
    # not the SDK's opaque "unhandled errors in a TaskGroup".
    broken = dict(saved, url="http://127.0.0.1:1/mcp")
    cmt.list_servers = lambda uid: [broken]  # noqa: E731
    err_tool = next(t for t in get_custom_mcp_tools("u1") if "list_devices" in t.name)
    err = err_tool.invoke({"site": "x"})
    print(f"unreachable: {err[:90]}")
    assert "error" in err.lower(), err
    assert "TaskGroup" not in err, f"opaque SDK error leaked to the user: {err}"
    assert "onnect" in err, f"error does not explain the cause: {err}"

    print("\nALL E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
