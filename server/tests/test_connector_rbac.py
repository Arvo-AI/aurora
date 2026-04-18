"""Verify every connector route has RBAC decorators.

This test statically inspects all Python files under ``server/routes/`` that
define Flask connector blueprints and asserts that every route function is
wrapped with ``@require_permission`` (or ``@require_auth_only`` for routes
that intentionally skip permission checks, like event webhooks).

If a new connector is added without RBAC decorators, this test fails in CI
before code review even starts.
"""

import ast
import os
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

ROUTES_DIR = Path(__file__).resolve().parent.parent / "routes"

# Directories that contain connector route files.  Non-connector route
# files (auth_routes, admin_routes, health_routes, etc.) are excluded.
CONNECTOR_DIRS: Set[str] = {
    "aws",
    "atlassian",
    "azure",
    "bigpanda",
    "bitbucket",
    "cloudbees",
    "cloudflare",
    "confluence",
    "coroot",
    "datadog",
    "dynatrace",
    "gcp",
    "github",
    "google_chat",
    "grafana",
    "jenkins",
    "jira",
    "netdata",
    "newrelic",
    "notion",
    "opsgenie",
    "ovh",
    "pagerduty",
    "scaleway",
    "sharepoint",
    "slack",
    "spinnaker",
    "splunk",
    "tailscale",
    "terraform",
    "thousandeyes",
}

# Files that are legitimately exempt from RBAC (webhook receivers that
# validate via HMAC/signing secret, internal task modules, helpers, etc.).
EXEMPT_FILES: Set[str] = {
    "slack_events.py",
    "slack_events_helpers.py",
    "google_chat_events.py",
    "google_chat_events_helpers.py",
    "tasks.py",
    "config.py",
    "helpers.py",
    "oauth_utils.py",
    "oauth2_auth_code_flow.py",
    "pagerduty_helpers.py",
    "runbook_utils.py",
    "root_project_service.py",
    "root_project_tasks.py",
}

RBAC_DECORATORS = {"require_permission", "require_auth_only"}

# Known RBAC violations tracked for follow-up.  Each entry is
# "relative/path.py:func_name".  Remove entries as they're fixed.
KNOWN_VIOLATIONS: Set[str] = {
    "routes/sharepoint/sharepoint_routes.py:connect",
    "routes/sharepoint/sharepoint_routes.py:status",
    "routes/sharepoint/sharepoint_routes.py:disconnect",
    "routes/sharepoint/sharepoint_routes.py:search",
    "routes/sharepoint/sharepoint_routes.py:fetch_page",
    "routes/sharepoint/sharepoint_routes.py:fetch_document",
    "routes/sharepoint/sharepoint_routes.py:create_page",
    "routes/sharepoint/sharepoint_routes.py:list_sites",
}

# Function names that are legitimately exempt from RBAC:
#  - OAuth callbacks: called by provider redirects, auth is via state param
#  - Webhooks: called by external services, auth is via HMAC/signing secret
#  - Setup scripts: static shell scripts served without auth
EXEMPT_FUNCTIONS: Set[str] = {
    # OAuth callbacks (provider redirect — state param validates user)
    "callback",
    "oauth_callback",
    "github_callback",
    "bitbucket_callback",
    "slack_callback",
    "google_chat_callback",
    "azure_callback_route",
    # Webhooks (external service push — HMAC/secret validates sender)
    "webhook",
    "alert_webhook",
    "deployment_webhook",
    # Setup scripts (static content, no user data)
    "home",
    "aws_setup_script",
    "aws_setup_role_script",
    "aws_setup_script_ps1",
    "aws_setup_role_script_ps1",
    "azure_setup_script",
    "azure_setup_script_ps1",
}


def _find_route_functions(filepath: Path) -> List[Tuple[str, int, bool]]:
    """Parse a Python file and return (func_name, lineno, has_rbac) for each
    function decorated with ``@<bp>.route(...)``."""
    try:
        tree = ast.parse(filepath.read_text(), filename=str(filepath))
    except SyntaxError:
        return []

    results: List[Tuple[str, int, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        has_route = False
        has_rbac = False
        for dec in node.decorator_list:
            dec_name = _decorator_name(dec)
            if dec_name and "route" in dec_name:
                has_route = True
            if dec_name and dec_name in RBAC_DECORATORS:
                has_rbac = True
        if has_route:
            results.append((node.name, node.lineno, has_rbac))
    return results


def _decorator_name(node: ast.expr) -> str:
    """Extract the callable name from a decorator AST node."""
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _collect_violations() -> List[str]:
    """Walk connector route files and collect RBAC violations."""
    violations: List[str] = []

    for dirname in sorted(CONNECTOR_DIRS):
        dirpath = ROUTES_DIR / dirname
        if not dirpath.is_dir():
            continue
        for filepath in sorted(dirpath.glob("*.py")):
            if filepath.name.startswith("_"):
                continue
            if filepath.name in EXEMPT_FILES:
                continue
            routes = _find_route_functions(filepath)
            for func_name, lineno, has_rbac in routes:
                if not has_rbac and func_name not in EXEMPT_FUNCTIONS:
                    rel = filepath.relative_to(ROUTES_DIR.parent)
                    key = f"{rel}:{func_name}"
                    if key in KNOWN_VIOLATIONS:
                        continue
                    violations.append(
                        f"{rel}:{lineno} — {func_name}() is missing "
                        f"@require_permission or @require_auth_only"
                    )
    return violations


def test_all_connector_routes_have_rbac():
    """Every connector route function must be wrapped with an RBAC decorator."""
    violations = _collect_violations()
    if violations:
        msg = (
            "Connector routes missing RBAC decorators:\n\n"
            + "\n".join(f"  • {v}" for v in violations)
            + "\n\nSee CLAUDE.md § 'New Connector Checklist' for requirements."
        )
        pytest.fail(msg)
