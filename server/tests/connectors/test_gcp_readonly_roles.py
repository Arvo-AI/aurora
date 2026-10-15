"""Ask-mode GCP identity includes BigQuery read, and existing OAuth connects sync once."""

import sys
import types

from connectors.gcp_connector.auth import service_accounts as sa


def test_read_only_roles_include_bigquery_query_access():
    assert "roles/bigquery.dataViewer" in sa.READ_ONLY_PROJECT_ROLES
    assert "roles/bigquery.jobUser" in sa.READ_ONLY_PROJECT_ROLES
    assert sa.GCP_READONLY_ROLES_REVISION >= 1


def test_sync_skips_uploaded_service_account(monkeypatch):
    monkeypatch.setattr(sa, "_readonly_roles_current", lambda user_id: False)
    monkeypatch.setattr(
        "utils.auth.token_management.get_token_data",
        lambda user_id, provider: {"auth_type": "service_account"},
    )
    granted = []
    monkeypatch.setattr(
        "connectors.gcp_connector.gcp.iam.set_project_bindings",
        lambda *args, **kwargs: granted.append(args),
    )
    remembered = []
    monkeypatch.setattr(sa, "_remember_readonly_roles_revision", remembered.append)

    sa.sync_read_only_roles("user-1")

    assert granted == []
    assert remembered == ["user-1"]


def _stub_celery(monkeypatch, app):
    # maybe_schedule imports celery_config lazily. The real module dials Redis.
    module = types.ModuleType("celery_config")
    module.celery_app = app
    monkeypatch.setitem(sys.modules, "celery_config", module)


def test_schedule_sends_task_when_revision_is_behind(monkeypatch):
    monkeypatch.setattr(sa, "_readonly_roles_current", lambda user_id: False)
    sent = {}

    class _App:
        def send_task(self, name, args):
            sent["name"] = name
            sent["args"] = args

    _stub_celery(monkeypatch, _App())

    sa.maybe_schedule_readonly_role_sync("user-1")

    assert sent == {
        "name": sa.SYNC_GCP_READONLY_ROLES_TASK,
        "args": ["user-1"],
    }


def test_schedule_skips_when_revision_is_current(monkeypatch):
    monkeypatch.setattr(sa, "_readonly_roles_current", lambda user_id: True)

    class _App:
        def send_task(self, name, args):
            raise AssertionError("sync must not be enqueued")

    _stub_celery(monkeypatch, _App())

    sa.maybe_schedule_readonly_role_sync("user-1")
