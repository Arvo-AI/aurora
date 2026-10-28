"""A command the guardrail allows still dies when this caller cannot use the connector."""

import json

from chat.backend.agent.tools.cloud_exec_tool import connector_call_denied


def test_closed_connector_is_refused_before_login(monkeypatch):
    monkeypatch.setattr(
        "utils.auth.connector_access.may_use",
        lambda user_id, connector, scope_id="": False,
    )
    body = json.loads(connector_call_denied("uid-viewer", "azure", None, "resource list"))
    assert body["code"] == "CONNECTOR_DENIED"
    assert body["success"] is False
    assert "Access tab" in body["error"]


def test_azure_fanout_keeps_only_granted_subscriptions(monkeypatch):
    class _Access:
        def may_use(self, connector, scope_id=""):
            return scope_id == "sub-a"

    monkeypatch.setattr("utils.auth.connector_access.access_for", lambda user_id: _Access())
    from chat.backend.agent.tools.cloud_exec_tool import _granted_azure_connections

    kept = _granted_azure_connections("uid", [{"account_id": "sub-a"}, {"account_id": "sub-b"}])
    assert [row["account_id"] for row in kept] == ["sub-a"]


def test_resolved_azure_subscription_is_refused_when_not_granted(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "utils.auth.connector_access.may_use",
        lambda user_id, connector, scope_id="": scope_id == "sub-a",
    )
    from chat.backend.agent.tools.cloud_exec_tool import _refuse_resolved_azure

    config = tmp_path / "az-config"
    config.mkdir()
    body = json.loads(_refuse_resolved_azure("uid", "sub-b", "group list", {"AZURE_CONFIG_DIR": str(config)}))
    assert body["code"] == "CONNECTOR_DENIED"
    assert not config.exists()
    assert _refuse_resolved_azure("uid", "sub-a", "group list", {"AZURE_CONFIG_DIR": str(config)}) is None


def test_command_subscription_is_refused_when_not_granted(monkeypatch):
    class _Access:
        team_ids = set()

        def credentials(self, connector, scope_id=""):
            return frozenset({"write"})

        def may_use(self, connector, scope_id=""):
            return scope_id == "sub-a"

        def checked_as(self, team_names=None):
            return "viewer"

    monkeypatch.setattr("utils.auth.connector_access.access_for", lambda user_id: _Access())
    from chat.backend.agent.tools.cloud_exec_tool import _explicit_azure_subscriptions, _refuse_named_azure

    connections = [{"account_id": "sub-a"}]
    command = "az group list --subscription sub-b"
    assert _explicit_azure_subscriptions(command) == ["sub-b"]
    assert _explicit_azure_subscriptions("az resource show --ids /subscriptions/sub-b/resourceGroups/rg") == ["sub-b"]
    assert _explicit_azure_subscriptions("az graph query --subscriptions sub-a,sub-b") == ["sub-a", "sub-b"]
    body = json.loads(_refuse_named_azure("uid", command, connections))
    assert body["code"] == "CONNECTOR_DENIED"
    assert _refuse_named_azure("uid", "az graph query --subscriptions sub-a,sub-b", connections) is not None
    assert _refuse_named_azure("uid", "az group list --subscription sub-a", connections) is None
    assert _refuse_named_azure("uid", "az group list -s SUB-A", connections) is None
    assert _refuse_named_azure("uid", "az graph query -q Resources", connections) is not None


def test_gcp_project_flag_is_refused_when_not_granted(monkeypatch):
    class _Access:
        grants = []
        roles = ["viewer"]
        team_rules = []
        team_ids = set()

        def credentials(self, connector, scope_id=""):
            if scope_id in ("", "proj-a"):
                return frozenset({"read"})
            return frozenset()

        def may_use(self, connector, scope_id=""):
            return scope_id in ("", "proj-a")

        def checked_as(self, team_names=None):
            return "viewer"

    monkeypatch.setattr("utils.auth.connector_access.access_for", lambda user_id: _Access())
    from chat.backend.agent.tools.cloud_exec_tool import _explicit_gcp_projects, gcp_login_project

    assert _explicit_gcp_projects("gcloud compute instances list --project=proj-b") == ["proj-b"]
    assert _explicit_gcp_projects("bq query --project_id proj-a") == ["proj-a"]
    project, denied = gcp_login_project("uid", "gcloud compute instances list --project=proj-b")
    assert project is None
    assert json.loads(denied)["code"] == "CONNECTOR_DENIED"
    project, denied = gcp_login_project("uid", "gcloud compute instances list --project=proj-a")
    assert (project, denied) == ("proj-a", None)

    class _Grant:
        def __init__(self, scope_id):
            self.connector = "gcp"
            self.scope_id = scope_id
            self.role = "viewer"

    # No flag and more than one project: don't mint a root-project token.
    _Access.grants = [_Grant("proj-a"), _Grant("proj-b")]
    project, denied = gcp_login_project("uid", "gcloud compute instances list")
    body = json.loads(denied)
    assert project is None
    assert body["code"] == "CONNECTOR_DENIED"
    assert "proj-a" in body["error"] and "proj-b" in body["error"]


def test_gcp_single_grant_is_the_login_project_when_the_command_names_none(monkeypatch):
    class _Grant:
        def __init__(self, scope_id):
            self.connector = "gcp"
            self.scope_id = scope_id
            self.role = "viewer"

    class _Access:
        grants = [_Grant("proj-a")]
        roles = ["viewer"]
        team_rules = []
        team_ids = set()

        def credentials(self, connector, scope_id=""):
            return frozenset({"read"})

        def may_use(self, connector, scope_id=""):
            return scope_id in ("", "proj-a")

        def checked_as(self, team_names=None):
            return "viewer"

    monkeypatch.setattr("utils.auth.connector_access.access_for", lambda user_id: _Access())
    from chat.backend.agent.tools.cloud_exec_tool import gcp_login_project

    assert gcp_login_project("uid", "gcloud compute instances list") == ("proj-a", None)
    assert gcp_login_project("uid", "gcloud compute instances list", "proj-b")[0] is None
    # A missing value or an unparseable command must not fall through to the grant.
    assert gcp_login_project("uid", "gcloud compute instances list --project")[0] is None
    assert gcp_login_project("uid", 'gcloud compute instances list "')[0] is None


def test_open_connector_is_not_refused(monkeypatch):
    monkeypatch.setattr(
        "utils.auth.connector_access.may_use",
        lambda user_id, connector, scope_id="": True,
    )
    assert connector_call_denied("uid-viewer", "aws", None, "rds describe-db-instances") is None
