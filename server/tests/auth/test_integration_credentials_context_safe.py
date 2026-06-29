"""Regression guard: integration credential helpers must resolve org context
in a request-context-safe way.

The ``_get_stored_*_credentials`` helpers for Opsgenie / Sentry / New Relic /
Datadog are invoked from Celery tasks (event processing, RCA correlation) as
well as from HTTP routes. They used to call ``get_org_id_from_request()``,
which touches ``flask.request`` / ``flask.g`` and raises

    RuntimeError: Working outside of application context.

when there is no Flask request context — i.e. on every Celery invocation. The
helper caught it and logged
``[<PROVIDER>] Failed to retrieve credentials for user <id>: Working outside
of application context``, so org-shared integration credentials could not be
loaded in background work.

The fix is to resolve org context with ``resolve_org_id(user_id)``, which is
documented as safe to call inside OR outside a request context (it falls back
to a direct DB lookup). This test parses the helper source with ``ast`` so it
needs none of the heavy runtime deps (Redis, casbin, ...) the route modules
import at module load.
"""

from __future__ import annotations

import ast
import os

import pytest

_SERVER_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)

# (module path relative to server/, credential-helper function name)
_CREDENTIAL_HELPERS = [
    ("routes/opsgenie/opsgenie_routes.py", "_get_stored_opsgenie_credentials"),
    ("routes/sentry/sentry_routes.py", "_get_stored_sentry_credentials"),
    ("routes/newrelic/newrelic_routes.py", "_get_stored_newrelic_credentials"),
    ("routes/datadog/datadog_routes.py", "_get_stored_datadog_credentials"),
]


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _called_names(func: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


@pytest.mark.parametrize("rel_path,func_name", _CREDENTIAL_HELPERS)
def test_credential_helper_is_request_context_safe(rel_path, func_name):
    source = open(os.path.join(_SERVER_DIR, rel_path)).read()
    func = _find_function(ast.parse(source), func_name)
    called = _called_names(func)

    assert "get_org_id_from_request" not in called, (
        f"{rel_path}:{func_name} calls get_org_id_from_request(), which raises "
        "'Working outside of application context' from Celery tasks. Use "
        "resolve_org_id(user_id) instead."
    )
    assert "resolve_org_id" in called, (
        f"{rel_path}:{func_name} must resolve org context via "
        "resolve_org_id(user_id) so it works outside a Flask request context."
    )
