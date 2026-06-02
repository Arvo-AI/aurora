"""Tests the credential-reference helpers that every Vault-backed
connector goes through to look up "does user X have credentials for
provider Y?" Pins the org-scoping SQL fragment (so credential queries
never accidentally span tenants), provider-name canonicalization (so
``"AWS"``/``"aws"``/``"Aws"`` all resolve to the same Vault path), and
rejection of malformed provider strings (path-traversal-shaped inputs
must be refused before they reach the DB or Vault).
"""

from unittest.mock import MagicMock

import pytest

from utils.secrets import secret_ref_utils as sru
from utils.secrets.secret_ref_utils import (
    SUPPORTED_SECRET_PROVIDERS,
    SecretRefManager,
)
from utils.db.org_scope import org_read_predicate as _org_read_predicate, _validate_uuid

_UID = "00000000-0000-0000-0000-000000000001"
_OID = "00000000-0000-0000-0000-000000000007"


# ---------------------------------------------------------------------------
# _validate_uuid
# ---------------------------------------------------------------------------


class TestValidateUuid:
    """UUID sanitization gate that every SQL parameter passes through."""

    def test_valid_uuid_returned_unchanged(self):
        assert _validate_uuid(_UID, "user_id") == _UID

    def test_uppercase_uuid_normalised_to_lowercase(self):
        """Returns canonical (lowercase) form, not the original casing."""
        assert _validate_uuid(_UID.upper(), "user_id") == _UID

    @pytest.mark.parametrize("bad", [
        "",
        "not-a-uuid",
        "'; DROP TABLE user_tokens;--",
        "../etc/passwd",
        "00000000-0000-0000-0000-00000000000Z",
    ])
    def test_invalid_inputs_raise_value_error(self, bad):
        with pytest.raises(ValueError):
            _validate_uuid(bad, "user_id")


# ---------------------------------------------------------------------------
# _org_read_predicate
# ---------------------------------------------------------------------------


class TestOrgReadPredicate:
    """SQL predicate builder for all credential queries — reads, writes, and deletes."""

    def test_none_org_returns_user_id_only_predicate(self):
        clause, params = _org_read_predicate(_UID, None)
        assert clause == "user_id = %s"
        assert params == (_UID,)

    def test_concrete_org_returns_user_or_org_predicate(self):
        clause, params = _org_read_predicate(_UID, _OID)
        assert clause == "(user_id = %s OR org_id = %s)"
        assert params == (_UID, _OID)

    def test_params_is_tuple_not_list(self):
        _, params = _org_read_predicate(_UID, _OID)
        assert isinstance(params, tuple)

    def test_clause_is_plain_string(self):
        """Predicate is a plain SQL fragment — values go through %s params, not inline."""
        clause, _ = _org_read_predicate(_UID, _OID)
        assert isinstance(clause, str)
        assert clause.count("%s") == 2

    def test_malicious_user_id_raises_before_sql(self):
        """SQL injection attempt must be rejected at the UUID validation gate."""
        with pytest.raises(ValueError):
            _org_read_predicate("'; DROP TABLE user_tokens;--", _OID)

    def test_malicious_org_id_raises_before_sql(self):
        with pytest.raises(ValueError):
            _org_read_predicate(_UID, "'; DROP TABLE orgs;--")


# ---------------------------------------------------------------------------
# SUPPORTED_SECRET_PROVIDERS
# ---------------------------------------------------------------------------


class TestSupportedSecretProvidersShape:
    """The lookup is ``provider.lower().split('_')[0] in SUPPORTED_SECRET_PROVIDERS``."""

    def test_all_entries_are_lowercase(self):
        for provider in SUPPORTED_SECRET_PROVIDERS:
            assert provider == provider.lower(), (
                f"SUPPORTED_SECRET_PROVIDERS must be lowercase; offender: {provider!r}"
            )

    def test_uses_set_for_membership_lookup(self):
        """A list silently degrades to O(n) and tempts ``in`` substring confusion."""
        assert isinstance(SUPPORTED_SECRET_PROVIDERS, set)

    def test_set_is_non_empty(self):
        assert len(SUPPORTED_SECRET_PROVIDERS) > 0

    @pytest.mark.parametrize(
        "must_have",
        ["aws", "gcp", "azure", "github", "datadog", "google"],
    )
    def test_canonical_providers_present(self, must_have):
        """Anchor the spelling of headline providers; rename = visible CI break."""
        assert must_have in SUPPORTED_SECRET_PROVIDERS


# ---------------------------------------------------------------------------
# Case-insensitive provider lookup
# ---------------------------------------------------------------------------


@pytest.fixture
def manager_with_mocked_db(monkeypatch):
    """Mock the DB layer so an *accepted* provider exercises a real lookup."""
    cursor = MagicMock()
    cursor.fetchone.return_value = (1,)
    conn = MagicMock()
    conn.cursor.return_value = cursor

    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(sru, "connect_to_db_as_admin", connect)
    monkeypatch.setattr(sru, "set_rls_context", MagicMock(return_value=_OID))
    monkeypatch.setattr(sru, "resolve_org", MagicMock(return_value=_OID))

    return SecretRefManager(), connect, cursor


class TestProviderLookupCaseInsensitive:
    """``provider.lower().split('_')[0]`` must canonicalize before set membership."""

    @pytest.mark.parametrize("spelling", ["gcp", "GCP", "Gcp", "gCp"])
    def test_mixed_case_gcp_accepted(self, manager_with_mocked_db, spelling):
        manager, connect, _ = manager_with_mocked_db

        assert manager.has_user_credentials(_UID, spelling) is True
        connect.assert_called_once()

    @pytest.mark.parametrize("spelling", ["aws", "AWS", "Aws", "aWs"])
    def test_mixed_case_aws_accepted(self, manager_with_mocked_db, spelling):
        manager, _, _ = manager_with_mocked_db
        assert manager.has_user_credentials(_UID, spelling) is True

    @pytest.mark.parametrize(
        "compound",
        ["google_chat", "bitbucket_workspace_selection"],
    )
    def test_compound_provider_uses_first_underscore_segment(
        self, manager_with_mocked_db, compound,
    ):
        """Only the prefix before the first ``_`` is checked against the set."""
        manager, _, _ = manager_with_mocked_db
        assert manager.has_user_credentials(_UID, compound) is True

    def test_get_user_token_data_also_canonicalizes_case(
        self, manager_with_mocked_db, monkeypatch,
    ):
        """Same ``.lower().split('_')[0]`` rule on the read path."""
        manager, _, cursor = manager_with_mocked_db
        cursor.fetchone.return_value = ("vault:kv/data/aurora/users/x", None, None)
        monkeypatch.setattr(
            manager, "get_secret", MagicMock(return_value='{"token": "t"}'),
        )

        assert manager.get_user_token_data(_UID, "GCP") == {"token": "t"}


# ---------------------------------------------------------------------------
# Reference parser rejects malformed inputs
# ---------------------------------------------------------------------------


class TestProviderParserRejectsMalformed:
    """Bogus provider names must short-circuit before any DB or Vault call."""

    @pytest.fixture
    def db_explodes_if_called(self, monkeypatch):
        """DB and RLS hooks raise if rejection regresses."""
        connect = MagicMock(
            side_effect=AssertionError("DB must not run for rejected providers"),
        )
        monkeypatch.setattr(sru, "connect_to_db_as_admin", connect)
        monkeypatch.setattr(
            sru,
            "set_rls_context",
            MagicMock(side_effect=AssertionError("set_rls_context must not run")),
        )
        monkeypatch.setattr(sru, "resolve_org", MagicMock(return_value=None))
        return connect

    @pytest.mark.parametrize(
        "bad_provider",
        [
            "",
            "unknown",
            "wikipedia",
            "../etc/passwd",
            "..",
            "google/../wikipedia",
            "AWS;DROP TABLE user_tokens",
            " gcp",
            "gcp ",
            "aw",
        ],
    )
    def test_has_user_credentials_rejects_without_db_call(
        self, db_explodes_if_called, bad_provider,
    ):
        manager = SecretRefManager()

        assert manager.has_user_credentials(_UID, bad_provider) is False
        db_explodes_if_called.assert_not_called()

    @pytest.mark.parametrize(
        "bad_provider",
        [
            "",
            "unknown",
            "../etc/passwd",
            "google/../wikipedia",
            "AWS;DROP TABLE user_tokens",
        ],
    )
    def test_get_user_token_data_rejects_without_db_call(
        self, db_explodes_if_called, bad_provider,
    ):
        manager = SecretRefManager()

        assert manager.get_user_token_data(_UID, bad_provider) is None
        db_explodes_if_called.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_org returning None: org_read_predicate must be used (no NULL param bug)
# ---------------------------------------------------------------------------


class TestHasUserCredentialsNullOrgPath:
    """When resolve_org returns None the SQL must not pass None as a %s param
    for org_id — ``org_id = NULL`` is always false in PostgreSQL.  The fix is
    to route through ``org_read_predicate`` which falls back to user_id-only
    matching when org_id is None.
    """
    def test_db_still_queried_when_org_is_none(self, monkeypatch):
        """resolve_org=None must not short-circuit; row found by user_id alone."""
        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)
        conn = MagicMock()
        conn.cursor.return_value = cursor

        monkeypatch.setattr(sru, "connect_to_db_as_admin", MagicMock(return_value=conn))
        monkeypatch.setattr(sru, "set_rls_context", MagicMock(return_value=None))
        monkeypatch.setattr(sru, "resolve_org", MagicMock(return_value=None))

        manager = SecretRefManager()
        result = manager.has_user_credentials(_UID, "gcp")

        assert result is True
        cursor.execute.assert_called()

    def test_none_org_does_not_appear_as_sql_param(self, monkeypatch):
        """None must never be passed to psycopg2 as an org_id equality param."""
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value = cursor

        monkeypatch.setattr(sru, "connect_to_db_as_admin", MagicMock(return_value=conn))
        monkeypatch.setattr(sru, "set_rls_context", MagicMock(return_value=None))
        monkeypatch.setattr(sru, "resolve_org", MagicMock(return_value=None))

        manager = SecretRefManager()
        manager.has_user_credentials(_UID, "gcp")

        for call in cursor.execute.call_args_list:
            params = call.args[1] if len(call.args) > 1 else ()
            assert None not in params, (
                f"None passed as SQL param (would silently do nothing): {params}"
            )


# ---------------------------------------------------------------------------
# Provider canonicalization reaches the SQL query
# ---------------------------------------------------------------------------


class TestProviderCanonicalizedInSQL:
    """get_user_token_data and has_user_credentials must pass provider_base
    (lowercase, first segment) to the SQL query — not the raw caller string.
    If the DB stores 'gcp' and we query for 'GCP', zero rows come back.
    """

    def _make_db(self, monkeypatch, fetchone_val):
        cursor = MagicMock()
        cursor.fetchone.return_value = fetchone_val
        conn = MagicMock()
        conn.cursor.return_value = cursor
        monkeypatch.setattr(sru, "connect_to_db_as_admin", MagicMock(return_value=conn))
        monkeypatch.setattr(sru, "set_rls_context", MagicMock(return_value=_OID))
        monkeypatch.setattr(sru, "resolve_org", MagicMock(return_value=_OID))
        return cursor

    @pytest.mark.parametrize("raw_provider", ["GCP", "Gcp", "gCp", "GcP"])
    def test_has_user_credentials_passes_lowercase_to_sql(
        self, monkeypatch, raw_provider,
    ):
        cursor = self._make_db(monkeypatch, (1,))
        manager = SecretRefManager()
        manager.has_user_credentials(_UID, raw_provider)

        _, params = cursor.execute.call_args.args
        assert "gcp" in params, (
            f"Expected canonicalized 'gcp' in SQL params, got {params}"
        )
        assert raw_provider not in params, (
            f"Raw mixed-case provider {raw_provider!r} must not reach the DB"
        )

    @pytest.mark.parametrize("raw_provider", ["GCP", "AWS", "Azure"])
    def test_get_user_token_data_passes_lowercase_to_sql(
        self, monkeypatch, raw_provider,
    ):
        cursor = self._make_db(monkeypatch, None)
        manager = SecretRefManager()
        manager.get_user_token_data(_UID, raw_provider)

        for call in cursor.execute.call_args_list:
            params = call.args[1] if len(call.args) > 1 else ()
            assert raw_provider not in params, (
                f"Raw provider {raw_provider!r} found in SQL params {params}; "
                f"only lowercase provider_base should reach the query"
            )
