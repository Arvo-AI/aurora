"""The Jira write endpoints honour the org's mode.

These are the routes MCP's jira_create_issue / jira_add_comment dispatch to, so
the gate has to live here as well as in the agent tool.
"""

from __future__ import annotations

import pytest

from connectors.jira_connector import settings


@pytest.fixture
def routes():
    return pytest.importorskip("routes.jira.jira_routes")


@pytest.fixture
def app():
    from flask import Flask

    # jsonify needs an app context; the blueprint itself isn't under test here.
    return Flask(__name__)


def _block(app, routes, mode, **kwargs):
    import unittest.mock as mock

    with app.app_context(), mock.patch.object(routes, "get_jira_mode", lambda uid: mode):
        return routes._write_blocked("uid-1", "commenting", **kwargs)


def test_read_only_blocks_commenting(app, routes):
    blocked = _block(app, routes, settings.READ_ONLY)
    assert blocked is not None
    response, status = blocked
    assert status == 403
    assert response.get_json()["jiraMode"] == settings.READ_ONLY


def test_comment_only_allows_commenting(app, routes):
    assert _block(app, routes, settings.COMMENT_ONLY) is None


def test_comment_only_blocks_create_update_and_link(app, routes):
    blocked = _block(app, routes, settings.COMMENT_ONLY, require_full=True)
    assert blocked is not None
    assert blocked[1] == 403


def test_full_allows_everything(app, routes):
    assert _block(app, routes, settings.FULL) is None
    assert _block(app, routes, settings.FULL, require_full=True) is None


def test_error_names_the_setting_that_unblocks_it(app, routes):
    """A bare 403 leaves the caller with nothing to act on."""
    response, _ = _block(app, routes, settings.READ_ONLY)
    assert "RCA permissions" in response.get_json()["error"]


def test_valid_modes_mirror_the_connector_settings(routes):
    """The PUT validator must accept exactly the modes the resolver knows."""
    assert tuple(routes.VALID_MODES) == settings.VALID_MODES


def test_every_write_route_calls_the_gate():
    """A new Jira write route that forgets _write_blocked silently reopens this.

    Checked by source inspection rather than per-route fixtures: the routes need
    live credentials, so a request-level test would prove nothing about the ones
    nobody added a fixture for.
    """
    import ast
    import inspect
    import textwrap

    import routes.jira.jira_routes as mod

    source = textwrap.dedent(inspect.getsource(mod))
    tree = ast.parse(source)

    def _is_connector_write(node):
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "require_permission":
                continue
            args = [a.value for a in dec.args if isinstance(a, ast.Constant)]
            if args[:2] == ["connectors", "write"]:
                return True
        return False

    # Changing the mode and disconnecting are the user's own actions, not
    # Aurora writing into their Jira, so they are not gated on the mode.
    exempt = {"update_settings", "disconnect"}

    ungated = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and _is_connector_write(node)
        and node.name not in exempt
        and "_write_blocked" not in ast.unparse(node)
    ]
    assert ungated == [], f"Jira write routes missing the mode gate: {ungated}"
