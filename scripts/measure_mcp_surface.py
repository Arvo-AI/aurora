"""Measure the MCP token surface — does the Tier-1/Tier-2/Tier-3 design
actually hit the token-budget targets called out in the design doc?

Targets:
  (a) Tier-1 only:                                  < 2k tokens
  (b) Tier-1 + Tier-2 (all connectors hypothetically connected): < 8k tokens
  (c) search_tools response for a representative query:           < 2k tokens

Approximation: 1 token ≈ 4 characters of JSON. Good enough for budget checks.

Run inside the aurora-server container so the package imports cleanly:

    docker exec aurora-server python /app/scripts/measure_mcp_surface.py
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict


def _est_tokens(s: str) -> int:
    return max(1, len(s) // 4)


def _tool_to_schema(tool) -> Dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description or "",
        "parameters": getattr(tool, "parameters", {}) or {},
    }


def measure() -> Dict[str, Any]:
    # Import after path setup is verified.
    try:
        import mcp_server  # noqa: F401  — registers all tools on the FastMCP instance
        from mcp_server import mcp
    except Exception as e:
        print(f"FATAL: could not import mcp_server: {e}", file=sys.stderr)
        sys.exit(1)

    from aurora_mcp import registry

    all_tools = list(mcp._tool_manager._tools.values())  # noqa: SLF001
    tier1_names = {
        "list_incidents", "get_incident",
        "incident_findings", "incident_finding_detail", "incident_list_alerts",
        "get_infrastructure_context", "list_services", "service_impact",
        "list_actions", "get_action", "list_action_runs",
        "trigger_rca", "knowledge_base_search", "search_runbooks",
        "search_tools", "call_tool",
    }
    tier1 = [t for t in all_tools if t.name in tier1_names]
    tier2 = [t for t in all_tools if t.name not in tier1_names]

    tier1_schemas = [_tool_to_schema(t) for t in tier1]
    tier2_schemas = [_tool_to_schema(t) for t in tier2]

    tier1_bytes = json.dumps(tier1_schemas, default=str)
    combined_bytes = json.dumps(tier1_schemas + tier2_schemas, default=str)

    # Simulated search_tools result for a representative query.
    sample = [
        {
            "name": e.name,
            "description": e.description,
            "category": e.category,
            "callable_now": True,
            "enabling_skills": list(e.enabling_skills),
            "args": (
                [{"name": a, "in": "path", "required": True} for a in e.path_args]
                + [{"name": a, "in": "body", "required": False} for a in e.body_keys]
            ),
        }
        for e in registry.search_dispatch_entries(query="jira", user_id=None, limit=10)
    ]
    search_bytes = json.dumps({"tools": sample, "total_matches": len(sample)})

    report = {
        "tier1_tools": len(tier1),
        "tier1_tokens_est": _est_tokens(tier1_bytes),
        "tier1_plus_tier2_tools": len(tier1) + len(tier2),
        "tier1_plus_tier2_tokens_est": _est_tokens(combined_bytes),
        "search_tools_response_tokens_est": _est_tokens(search_bytes),
        "allowlist_size": len(registry.DISPATCH_ALLOWLIST),
        "tier2_tool_count": len(registry.TIER2_TOOLS),
        "budget_targets": {
            "tier1_only_under": 2000,
            "tier1_plus_tier2_under": 8000,
            "search_response_under": 2000,
        },
    }

    def _verdict(actual: int, ceiling: int) -> str:
        return "PASS" if actual < ceiling else "FAIL"

    report["verdicts"] = {
        "tier1_only": _verdict(report["tier1_tokens_est"], 2000),
        "tier1_plus_tier2": _verdict(report["tier1_plus_tier2_tokens_est"], 8000),
        "search_response": _verdict(report["search_tools_response_tokens_est"], 2000),
    }
    return report


if __name__ == "__main__":
    report = measure()
    print(json.dumps(report, indent=2))
    if any(v == "FAIL" for v in report["verdicts"].values()):
        sys.exit(2)
