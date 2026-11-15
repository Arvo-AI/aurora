"""Google Chat sender resolution must not guess a tenant.

The sender lookup is case-insensitive since Google sends whatever case is on the
Workspace profile. But case-variant duplicate rows can span two orgs, and there's
no password here to prove ownership, so an ambiguous match must fail closed
rather than run the event in the wrong tenant.
"""

from unittest.mock import MagicMock

import pytest


_TOKEN_SQL_MARKER = "user_tokens"


class _FakeCursor:
    """Implements just enough of the queries the helper issues."""

    def __init__(self, user_rows, token_user_id="owner-uid"):
        self._user_rows = user_rows
        self._token_user_id = token_user_id
        self._result: list = []

    def execute(self, query, params=None):
        # Second query: resolve the org's google_chat connector owner.
        if _TOKEN_SQL_MARKER in query:
            self._result = [(self._token_user_id,)] if self._token_user_id else []
            return

        # RLS context statement — no result set.
        if "set_config" in query or "SET " in query.upper():
            self._result = []
            return

        # org_id lookup by user id (set_rls_context -> get_org_id_for_user).
        # Handled explicitly so a real, unpatched RLS helper surfaces loudly
        # instead of tripping the sender-lookup assertion below and having the
        # AssertionError swallowed upstream.
        if "org_id" in query and "WHERE id = %s" in query:
            uid = params[0]
            match = next((r for r in self._user_rows if r[0] == uid), None)
            self._result = [(match[1],)] if match else []
            return

        # First query: the sender lookup under test.
        assert "LOWER(email) = LOWER(%s)" in query, query
        sender = params[0]
        matches = [r for r in self._user_rows if r[2].lower() == sender.lower()]
        # Mirrors ORDER BY (email = %s) DESC, created_at ASC LIMIT 2.
        self._result = sorted(matches, key=lambda r: (r[2] != sender, r[0]))[:2]

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def resolve(monkeypatch):
    """Return a callable invoking the helper against fabricated user rows."""
    from routes.google_chat import google_chat_events_helpers as helpers

    # The helper imports set_rls_context *inside* the function, so the patch has
    # to land on the defining module — patching the helper module would be a
    # no-op and let the real RLS lookup run against the fake cursor.
    import utils.auth.stateless_auth as stateless_auth

    rls = MagicMock(return_value="org")
    monkeypatch.setattr(stateless_auth, "set_rls_context", rls)

    def _run(user_rows, sender_email, token_user_id="owner-uid"):
        cursor = _FakeCursor(user_rows, token_user_id)
        conn = MagicMock()
        conn.cursor.return_value = cursor
        conn.__enter__ = lambda _s: conn
        conn.__exit__ = lambda *_a: False
        monkeypatch.setattr(helpers.db_pool, "get_admin_connection", lambda: conn)

        return helpers.get_org_google_chat_credentials(sender_email)

    _run.rls = rls
    return _run


# Rows are (id, org_id, email) — matching the helper's SELECT.
_EXACT = ("uid-a", "org-a", "sender@example.com")
_VARIANT = ("uid-b", "org-b", "Sender@example.com")


def test_exact_case_match_resolves(resolve):
    """The unambiguous happy path still works."""
    result = resolve([_EXACT], "sender@example.com")
    assert result is not None
    assert result[1] == "org-a"
    assert result[2] == "uid-a"


def test_rls_context_is_set_for_the_resolved_sender(resolve):
    """Guards the mock: proves the patched RLS helper is the one being called."""
    assert resolve([_EXACT], "sender@example.com") is not None
    resolve.rls.assert_called_once()
    assert resolve.rls.call_args.args[2] == "uid-a"


def test_single_case_variant_row_still_resolves(resolve):
    """The lockout fix: one row, event carries different casing."""
    result = resolve([_VARIANT], "sender@example.com")
    assert result is not None
    assert result[1] == "org-b"
    assert result[2] == "uid-b"


def test_ambiguous_cross_org_match_is_refused(resolve):
    """Two orgs, third casing — must NOT silently choose a tenant."""
    result = resolve([_EXACT, _VARIANT], "SENDER@EXAMPLE.COM")
    assert result is None


def test_exact_case_wins_over_a_case_variant_duplicate(resolve):
    """An exact hit is unambiguous (users.email is UNIQUE), so it's honoured."""
    result = resolve([_EXACT, _VARIANT], "sender@example.com")
    assert result is not None
    assert result[1] == "org-a"
    assert result[2] == "uid-a"

    result = resolve([_EXACT, _VARIANT], "Sender@example.com")
    assert result is not None
    assert result[1] == "org-b"
    assert result[2] == "uid-b"


def test_unknown_sender_is_refused(resolve):
    assert resolve([_EXACT], "nobody@example.com") is None


def test_user_without_an_org_is_refused(resolve):
    assert resolve([("uid-c", None, "orphan@example.com")], "orphan@example.com") is None


@pytest.mark.parametrize("bad", ["", None, "not-an-email"])
def test_malformed_sender_is_refused(resolve, bad):
    assert resolve([_EXACT], bad) is None
