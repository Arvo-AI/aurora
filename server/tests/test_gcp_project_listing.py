"""Test parallelized GCP project IAM checks.

These tests import _check_project_iam and _oauth_mode_project_list directly
by loading only the projects module (not the full routes package) to avoid
pulling in the entire server import chain.
"""
import importlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_credentials():
    return MagicMock()


@pytest.fixture
def fake_projects():
    return [
        {"projectId": f"project-{i}", "name": f"Project {i}"}
        for i in range(25)
    ]


def _load_projects_module():
    """Load routes.gcp.projects in isolation, stubbing heavy dependencies."""
    # Stub modules that would pull in celery, db, etc.
    stubs = [
        "utils.auth.stateless_auth",
        "utils.auth.rbac_decorators",
        "utils.auth.token_refresh",
        "connectors.gcp_connector.auth.oauth",
        "utils.auth.token_management",
        "utils.log_sanitizer",
        "connectors.gcp_connector.gcp.projects",
        "connectors.gcp_connector.auth.service_accounts",
        "connectors.gcp_connector.billing",
        "utils.secrets.secret_ref_utils",
        "flask",
    ]
    mocks = {}
    for mod_name in stubs:
        if mod_name not in sys.modules:
            mocks[mod_name] = MagicMock()
            sys.modules[mod_name] = mocks[mod_name]

    # Provide flask.Blueprint and flask decorators
    flask_mock = sys.modules["flask"]
    flask_mock.Blueprint.return_value = MagicMock()
    flask_mock.request = MagicMock()
    flask_mock.jsonify = MagicMock()

    # Provide decorator stubs
    rbac_mock = sys.modules["utils.auth.rbac_decorators"]
    rbac_mock.require_permission = lambda *a, **kw: (lambda f: f)

    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        "routes.gcp.projects",
        os.path.join(os.path.dirname(__file__), "..", "routes", "gcp", "projects.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["routes.gcp.projects"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def projects_module():
    return _load_projects_module()


def test_oauth_mode_parallel_is_faster_than_sequential(mock_credentials, fake_projects, projects_module):
    """Verify parallelization actually runs IAM checks concurrently."""
    call_count = {"n": 0}

    def slow_execute():
        call_count["n"] += 1
        time.sleep(0.1)
        return {"bindings": []}

    mock_crm_service = MagicMock()
    mock_crm_service.projects.return_value.getIamPolicy.return_value.execute = slow_execute

    with patch.object(projects_module, "get_project_list", return_value=fake_projects), \
         patch.object(projects_module, "build", return_value=mock_crm_service):

        start = time.monotonic()
        result = projects_module._oauth_mode_project_list(
            mock_credentials, "sa@project.iam.gserviceaccount.com", "project-0"
        )
        elapsed = time.monotonic() - start

    assert len(result) == 25
    assert call_count["n"] == 25
    # Sequential would take 25 * 0.1s = 2.5s. Parallel with 10 workers ~0.3s.
    assert elapsed < 1.5, f"Took {elapsed:.1f}s — not running in parallel"


def test_oauth_mode_handles_iam_errors_gracefully(mock_credentials, fake_projects, projects_module):
    """Projects with IAM errors should still appear (enabled=False)."""
    mock_crm_service = MagicMock()
    mock_crm_service.projects.return_value.getIamPolicy.return_value.execute.side_effect = Exception("Permission denied")

    with patch.object(projects_module, "get_project_list", return_value=fake_projects), \
         patch.object(projects_module, "build", return_value=mock_crm_service):

        result = projects_module._oauth_mode_project_list(
            mock_credentials, "sa@project.iam.gserviceaccount.com", "project-0"
        )

    assert len(result) == 25
    for item in result:
        assert item["enabled"] is False
        assert item["hasPermission"] is False


def test_oauth_mode_detects_sa_binding(mock_credentials, projects_module):
    """When SA has a binding, project shows enabled=True."""
    projects = [{"projectId": "my-project", "name": "My Project"}]
    sa_email = "aurora@my-project.iam.gserviceaccount.com"

    policy = {
        "bindings": [
            {"role": "roles/viewer", "members": [f"serviceAccount:{sa_email}"]},
        ]
    }
    mock_crm_service = MagicMock()
    mock_crm_service.projects.return_value.getIamPolicy.return_value.execute.return_value = policy

    with patch.object(projects_module, "get_project_list", return_value=projects), \
         patch.object(projects_module, "build", return_value=mock_crm_service):

        result = projects_module._oauth_mode_project_list(mock_credentials, sa_email, "my-project")

    assert len(result) == 1
    assert result[0]["enabled"] is True
    assert result[0]["hasPermission"] is True
    assert result[0]["isRootProject"] is True


def test_oauth_mode_result_sorted_by_name(mock_credentials, projects_module):
    """Results should be alphabetically sorted by project name."""
    projects = [
        {"projectId": "z-project", "name": "Zebra"},
        {"projectId": "a-project", "name": "Alpha"},
        {"projectId": "m-project", "name": "Middle"},
    ]

    mock_crm_service = MagicMock()
    mock_crm_service.projects.return_value.getIamPolicy.return_value.execute.return_value = {"bindings": []}

    with patch.object(projects_module, "get_project_list", return_value=projects), \
         patch.object(projects_module, "build", return_value=mock_crm_service):

        result = projects_module._oauth_mode_project_list(mock_credentials, "sa@x.iam.gserviceaccount.com", None)

    names = [r["name"] for r in result]
    assert names == ["Alpha", "Middle", "Zebra"]
