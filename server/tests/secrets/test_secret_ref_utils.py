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
    _org_clause,
)


# ---------------------------------------------------------------------------
# _org_clause
# ---------------------------------------------------------------------------


class TestOrgClause:
    """Pure SQL fragment builder concatenated into every credential WHERE clause."""

    def test_none_org_returns_empty_fragment_and_empty_params(self):
        assert _org_clause(None) == ("", ())

    def test_empty_string_org_returns_empty_fragment(self):
        """Falsy values follow the same path as ``None``."""
        assert _org_clause("") == ("", ())

    def test_concrete_org_returns_and_clause_and_param_tuple(self):
        clause, params = _org_clause("org-7")

        assert clause == "AND (org_id = %s OR org_id IS NULL)"
        assert params == ("org-7",)

    def test_clause_starts_with_and_so_it_appends_to_existing_where(self):
        """Callers concatenate this onto a WHERE; missing AND is a SyntaxError."""
        clause, _ = _org_clause("org-7")
        assert clause.startswith("AND ")

    def test_clause_includes_org_id_is_null_for_legacy_rows(self):
        """Pre-multi-tenancy rows have NULL org_id; they must remain visible."""
        clause, _ = _org_clause("org-7")
        assert "org_id IS NULL" in clause

    def test_params_is_tuple_not_list(self):
        _, params = _org_clause("org-7")
        assert isinstance(params, tuple)

    def test_clause_uses_parameter_placeholder_not_inlined_value(self):
        """SQL injection guard: org_id must go through %s, not f-string interpolation."""
        clause, params = _org_clause("'; DROP TABLE user_tokens;--")

        assert "%s" in clause
        assert "DROP TABLE" not in clause
        assert params == ("'; DROP TABLE user_tokens;--",)


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


@pytest.fixture()
def manager_with_mocked_db(monkeypatch):
    """Mock the DB layer so an *accepted* provider exercises a real lookup."""
    cursor = MagicMock()
    cursor.fetchone.return_value = (1,)
    conn = MagicMock()
    conn.cursor.return_value = cursor

    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(sru, "connect_to_db_as_admin", connect)
    monkeypatch.setattr(sru, "set_rls_context", MagicMock(return_value="org-7"))
    monkeypatch.setattr(sru, "_resolve_org", MagicMock(return_value="org-7"))

    return SecretRefManager(), connect, cursor


class TestProviderLookupCaseInsensitive:
    """``provider.lower().split('_')[0]`` must canonicalize before set membership."""

    @pytest.mark.parametrize("spelling", ["gcp", "GCP", "Gcp", "gCp"])
    def test_mixed_case_gcp_accepted(self, manager_with_mocked_db, spelling):
        manager, connect, _ = manager_with_mocked_db

        assert manager.has_user_credentials("u-1", spelling) is True
        connect.assert_called_once()

    @pytest.mark.parametrize("spelling", ["aws", "AWS", "Aws", "aWs"])
    def test_mixed_case_aws_accepted(self, manager_with_mocked_db, spelling):
        manager, _, _ = manager_with_mocked_db
        assert manager.has_user_credentials("u-1", spelling) is True

    @pytest.mark.parametrize("spelling", ["azure", "AZURE", "Azure"])
    def test_mixed_case_azure_accepted(self, manager_with_mocked_db, spelling):
        manager, connect, _ = manager_with_mocked_db
        assert manager.has_user_credentials("u-1", spelling) is True
        connect.assert_called_once()

    @pytest.mark.parametrize(
        "compound",
        ["google_chat", "bitbucket_workspace_selection"],
    )
    def test_compound_provider_uses_first_underscore_segment(
        self, manager_with_mocked_db, compound,
    ):
        """Only the prefix before the first ``_`` is checked against the set."""
        manager, _, _ = manager_with_mocked_db
        assert manager.has_user_credentials("u-1", compound) is True

    def test_get_user_token_data_also_canonicalizes_case(
        self, manager_with_mocked_db, monkeypatch,
    ):
        """Same ``.lower().split('_')[0]`` rule on the read path."""
        manager, _, cursor = manager_with_mocked_db
        cursor.fetchone.return_value = ("vault:kv/data/aurora/users/x", None, None)
        monkeypatch.setattr(
            manager, "get_secret", MagicMock(return_value='{"token": "t"}'),
        )

        assert manager.get_user_token_data("u-1", "GCP") == {"token": "t"}


# ---------------------------------------------------------------------------
# Reference parser rejects malformed inputs
# ---------------------------------------------------------------------------


class TestProviderParserRejectsMalformed:
    """Bogus provider names must short-circuit before any DB or Vault call."""

    @pytest.fixture()
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
        monkeypatch.setattr(sru, "_resolve_org", MagicMock(return_value=None))
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

        assert manager.has_user_credentials("u-1", bad_provider) is False
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

        assert manager.get_user_token_data("u-1", bad_provider) is None
        db_explodes_if_called.assert_not_called()
