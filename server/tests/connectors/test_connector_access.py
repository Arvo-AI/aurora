"""Connector access: a role gets a connector, and optionally one credential
on one account. Nothing limited stays open.
"""

from utils.auth.connector_access import (
    CREDENTIAL_READ,
    CREDENTIAL_WRITE,
    Grant,
    credentials_for,
    gcp_token_mode,
    may_use_roles,
    select_aws_role,
)


def _g(connector, role, scope="", credential="", team_id=""):
    return Grant(connector, scope, credential, role, team_id)


def test_unlimited_connector_is_open_to_every_role():
    grants = [_g("datadog", "admin")]
    assert credentials_for(grants, "splunk", ["viewer"]) is None
    assert may_use_roles(grants, "splunk", ["viewer"]) is True


def test_roles_are_independent():
    grants = [_g("datadog", "admin"), _g("datadog", "editor")]
    assert may_use_roles(grants, "datadog", ["viewer"]) is False
    assert may_use_roles(grants, "datadog", ["admin"]) is True


def test_account_grant_does_not_open_another_account():
    grants = [
        _g("aws", "admin", "111", CREDENTIAL_WRITE),
        _g("aws", "viewer", "222", CREDENTIAL_READ),
    ]
    assert credentials_for(grants, "aws", ["admin"], "111") == frozenset({CREDENTIAL_WRITE})
    assert credentials_for(grants, "aws", ["admin"], "222") == frozenset()
    assert credentials_for(grants, "aws", ["viewer"], "222") == frozenset({CREDENTIAL_READ})
    # Any granted account is enough for the connector's tools to be offered.
    assert may_use_roles(grants, "aws", ["viewer"]) is True


def test_viewer_gets_the_read_only_role_even_in_agent_mode():
    conn = {"role_arn": "arn:write", "read_only_role_arn": "arn:read"}
    arn, session = select_aws_role(conn, frozenset({CREDENTIAL_READ}), ask_mode=False)
    assert (arn, session) == ("arn:read", False)


def test_admin_gets_the_modify_role_in_agent_mode():
    conn = {"role_arn": "arn:write", "read_only_role_arn": "arn:read"}
    arn, session = select_aws_role(conn, frozenset({CREDENTIAL_WRITE}), ask_mode=False)
    assert (arn, session) == ("arn:write", False)


def test_ask_mode_still_prefers_the_read_only_role():
    conn = {"role_arn": "arn:write", "read_only_role_arn": "arn:read"}
    arn, session = select_aws_role(conn, frozenset({CREDENTIAL_WRITE}), ask_mode=True)
    assert (arn, session) == ("arn:read", False)


def test_ungranted_account_is_skipped():
    conn = {"role_arn": "arn:write", "read_only_role_arn": "arn:read"}
    assert select_aws_role(conn, frozenset(), ask_mode=False) == (None, False)


def test_unlimited_aws_keeps_the_old_ask_mode_behavior():
    conn = {"role_arn": "arn:write", "read_only_role_arn": "arn:read"}
    assert select_aws_role(conn, None, ask_mode=True) == ("arn:read", False)
    assert select_aws_role(conn, None, ask_mode=False) == ("arn:write", False)


def test_gcp_read_grant_forces_the_read_only_service_account():
    assert gcp_token_mode(frozenset({CREDENTIAL_READ}), "agent") == "ask"
    assert gcp_token_mode(frozenset({CREDENTIAL_WRITE}), "agent") == "agent"
    assert gcp_token_mode(frozenset(), "agent") == "deny"
    assert gcp_token_mode(None, "agent") is None


def test_team_without_its_own_rules_follows_everyone():
    from utils.auth.connector_access import Access

    general = [_g("aws", "viewer", "net", CREDENTIAL_READ)]
    access = Access(general, ["viewer"], [], ["db"])
    assert access.credentials("aws", "net") == frozenset({CREDENTIAL_READ})


def test_team_rules_override_everyone():
    from utils.auth.connector_access import Access

    general = [_g("aws", "viewer", "net", CREDENTIAL_WRITE)]
    team = [_g("aws", "editor", "db-account", CREDENTIAL_READ, "db")]
    member = Access(general, ["editor"], team, ["db"])
    assert member.credentials("aws", "db-account") == frozenset({CREDENTIAL_READ})
    assert member.credentials("aws", "net") == frozenset()
    outsider = Access(general, ["viewer"], team, [])
    assert outsider.credentials("aws", "net") == frozenset({CREDENTIAL_WRITE})


def test_matching_team_save_inherits_everyone():
    from utils.auth.connector_access import inherits_general

    general = [_g("datadog", "admin")]
    assert inherits_general(general, True, general) is True
    assert inherits_general([], False, []) is True
    assert inherits_general(general, False, []) is False
    assert inherits_general([], True, []) is False


def test_denial_names_the_general_role_or_the_team_override():
    from utils.auth.connector_access import Access, denial_message

    general = Access([_g("azure", "admin"), _g("azure", "editor")], ["viewer"])
    assert general.checked_as() == "general viewer"
    assert denial_message("azure", "general viewer") == (
        "You don't have access to azure as a general viewer. "
        "Ask an admin to change this permission in org Settings, on the Access tab."
    )

    # No Azure rows for Database. They still follow everyone, but they are a Database viewer.
    team = Access(
        [_g("azure", "admin"), _g("azure", "editor")],
        ["viewer"],
        [],
        ["db"],
    )
    assert team.checked_as({"db": "Database"}) == "Database viewer"


def test_a_failed_grant_read_denies_every_connector():
    from utils.auth.connector_access import Access

    denied = Access([], ["admin"], unavailable=True)
    assert denied.may_use("aws") is False
    assert denied.credentials("aws") == frozenset()
    assert denied.revision() == "unavailable"
