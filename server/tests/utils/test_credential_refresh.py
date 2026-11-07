"""Tests for utils.aws.credential_refresh -- proactive STS credential refresh."""

import os
import sys
import time
from unittest.mock import MagicMock, patch

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))


class TestCredentialRefresh:
    """Proactive refresh must target connections whose cached creds are expiring."""

    @patch("utils.auth.stateless_auth.set_rls_context")
    @patch("utils.db.connection_pool.db_pool")
    @patch("utils.aws.aws_sts_client.assume_workspace_role")
    def test_expiring_connection_is_refreshed(self, mock_assume, mock_db_pool, mock_set_rls):
        """A cached credential inside the refresh window must trigger a re-assume."""
        from utils.aws.aws_sts_client import _credential_cache
        from utils.aws.credential_refresh import refresh_aws_credentials

        role_arn = "arn:aws:iam::123456789012:role/AuroraRole"
        cache_key = f"42:{role_arn}:ext-abc:full"

        _credential_cache.clear()
        _credential_cache[cache_key] = {"expiration": int(time.time()) + 300}

        fake_cursor = MagicMock()
        fake_cursor.fetchall.side_effect = [
            [(42, 100)],  # users query
            [(42, role_arn, "us-east-1", "workspace-1", "ext-abc")],  # connections query
        ]
        fake_conn = MagicMock()
        fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
        mock_db_pool.get_admin_connection.return_value.__enter__.return_value = fake_conn

        result = refresh_aws_credentials()

        assert result["refreshed"] == 1
        assert result["skipped"] == 0
        mock_assume.assert_called_once_with(
            role_arn=role_arn,
            external_id="ext-abc",
            workspace_id="workspace-1",
            region="us-east-1",
            user_id=42,
        )

    @patch("utils.auth.stateless_auth.set_rls_context")
    @patch("utils.db.connection_pool.db_pool")
    @patch("utils.aws.aws_sts_client.assume_workspace_role")
    def test_prefix_does_not_match_longer_role(self, mock_assume, mock_db_pool, mock_set_rls):
        """The prefix for `Admin` must not accidentally match `AdminReadOnly`."""
        from utils.aws.aws_sts_client import _credential_cache
        from utils.aws.credential_refresh import refresh_aws_credentials

        admin = "arn:aws:iam::123456789012:role/Admin"
        admin_ro = "arn:aws:iam::123456789012:role/AdminReadOnly"

        _credential_cache.clear()
        _credential_cache[f"42:{admin}:ext-abc:full"] = {"expiration": int(time.time()) + 300}
        _credential_cache[f"42:{admin_ro}:ext-abc:full"] = {"expiration": int(time.time()) + 300}

        fake_cursor = MagicMock()
        fake_cursor.fetchall.side_effect = [
            [(42, 100)],
            [(42, admin, "us-east-1", "workspace-1", "ext-abc")],  # only Admin returned
        ]
        fake_conn = MagicMock()
        fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
        mock_db_pool.get_admin_connection.return_value.__enter__.return_value = fake_conn

        refresh_aws_credentials()

        mock_assume.assert_called_once_with(
            role_arn=admin,
            external_id="ext-abc",
            workspace_id="workspace-1",
            region="us-east-1",
            user_id=42,
        )