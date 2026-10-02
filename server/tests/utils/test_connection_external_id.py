"""ExternalId must come from the workspace that registered the AWS connection.

Regression tests for the org-shared connector bug: connectors are visible
org-wide but ExternalIds are per-workspace, so resolving one from the *caller's*
workspace produced AccessDenied on every account for any org member who hadn't
run onboarding. Worse, the old path called get_or_create_workspace, which minted
a fresh random ExternidId as a side effect of a read.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from utils.workspace.workspace_utils import resolve_connection_external_id

OWNER_WORKSPACE = "11111111-1111-1111-1111-111111111111"
OWNER_EXTERNAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CALLER_USER = "uid-other-org-member"


def _connection(workspace_id: str | None = OWNER_WORKSPACE) -> dict:
    return {
        "account_id": "111122223333",
        "role_arn": "arn:aws:iam::111122223333:role/AuroraReadOnlyRole",
        "workspace_id": workspace_id,
    }


def test_resolves_from_connections_own_workspace():
    """The ExternalId follows the connection, not whoever is asking."""
    with patch(
        "utils.workspace.workspace_utils.get_workspace_by_id",
        return_value={"id": OWNER_WORKSPACE, "aws_external_id": OWNER_EXTERNAL_ID},
    ) as get_ws:
        result = resolve_connection_external_id(_connection(), CALLER_USER)

    assert result == OWNER_EXTERNAL_ID
    # Looked up the owning workspace, not the caller's default
    get_ws.assert_called_once_with(OWNER_WORKSPACE, user_id=CALLER_USER)


def test_never_mints_a_new_external_id():
    """A read path must not create a workspace; that masked the bug as AccessDenied."""
    with patch(
        "utils.workspace.workspace_utils.get_or_create_workspace"
    ) as get_or_create, patch(
        "utils.workspace.workspace_utils.get_workspace_by_id",
        return_value={"id": OWNER_WORKSPACE, "aws_external_id": OWNER_EXTERNAL_ID},
    ):
        resolve_connection_external_id(_connection(), CALLER_USER)

    get_or_create.assert_not_called()


@pytest.mark.parametrize(
    "workspace_id,workspace_row",
    [
        # Legacy row registered before workspace_id was recorded
        (None, None),
        # workspace_id points at a row that no longer exists
        (OWNER_WORKSPACE, None),
        # Workspace exists but onboarding never stored an ExternalId
        (OWNER_WORKSPACE, {"id": OWNER_WORKSPACE, "aws_external_id": None}),
    ],
)
def test_returns_none_rather_than_a_wrong_external_id(workspace_id, workspace_row):
    """Fail closed: signing STS with a guessed ExternalId is the bug being fixed."""
    with patch(
        "utils.workspace.workspace_utils.get_workspace_by_id",
        return_value=workspace_row,
    ):
        assert resolve_connection_external_id(_connection(workspace_id), CALLER_USER) is None
