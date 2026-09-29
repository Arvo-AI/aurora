"""Login must resolve an account by normalized email, and by *password*.

Login matched email case-sensitively while registration stored a lowercased
copy, so users who typed any capitalization got "Invalid credentials" and could
register a duplicate. Those duplicate rows exist in production, so a lookup
using fetchone() would be non-deterministic about which row it picks and could
take login away from whoever can authenticate today. These tests pin the
required behaviour: every candidate is authenticated and the password decides.

Fixtures use fictional ``example.com`` addresses that mirror the *shape* of the
production duplicates. Never put real user emails, names or org names in tests.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import bcrypt
import pytest


# Column order must match the SELECT in routes.auth_routes.login().
_COLS = (
    "id", "email", "name", "password_hash", "role", "org_id", "org_name",
    "must_change_password", "email_verified", "is_github",
)

# Mirrors the shape of the duplicate pair seen in production — an
# admin-created lowercase row and a self-registered capitalized row — using a
# fictional address on the reserved example.com domain and throwaway passwords.
_ADMIN_PW = "AdminChosenPw1"
_SELF_PW = "OwnerChosenPw2"
_EMAIL_LOWER = "first.last@example.com"
_EMAIL_MIXED = "First.Last@example.com"


def _row(user_id, email, password, created_at, org_id):
    """Build one users-table row in the shape login() unpacks."""
    return (
        user_id,
        email,
        "Test User",
        bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
        "admin",
        org_id,
        org_id,
        False,
        True,
        False,
    )


@pytest.fixture(scope="module")
def duplicate_pair():
    """The admin-created row (older) and the self-registered row (newer)."""
    return [
        _row("uid-admin", _EMAIL_LOWER, _ADMIN_PW, 1, "acme"),
        _row("uid-self", _EMAIL_MIXED, _SELF_PW, 2, "acme-test"),
    ]


class _FakeCursor:
    """Stands in for a psycopg2 cursor, implementing just the login SELECT.

    Replicates the server-side semantics the route depends on:
    ``WHERE LOWER(u.email) = %s ORDER BY (u.email = %s) DESC, u.created_at ASC``
    so the test exercises the real candidate ordering rather than a hardcoded
    list.
    """

    def __init__(self, rows):
        self._rows = rows
        self._result: list = []

    def execute(self, query, params=None):
        assert "LOWER(u.email) = %s" in query, "login must match on normalized email"
        normalized, raw = params
        matches = [r for r in self._rows if r[1].lower() == normalized]
        # Exact-case row first, then oldest — mirrors the ORDER BY clause.
        self._result = sorted(matches, key=lambda r: (r[1] != raw, r[_COLS.index("id")]))

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def login_client(monkeypatch, duplicate_pair):
    """Flask test client for auth_bp with the users table faked out."""
    for heavy in ("celery_config", "celery", "routes.audit_routes"):
        if heavy not in sys.modules:
            sys.modules[heavy] = MagicMock()
    sys.modules["routes.audit_routes"].record_audit_event = MagicMock()

    from flask import Flask

    from routes import auth_routes

    fake_conn = MagicMock()
    fake_conn.cursor.return_value = _FakeCursor(duplicate_pair)
    monkeypatch.setattr(auth_routes, "connect_to_db_as_user", lambda: fake_conn)
    monkeypatch.setattr(auth_routes, "record_audit_event", MagicMock())

    application = Flask(__name__)  # NOSONAR — test-local app
    application.register_blueprint(auth_routes.auth_bp)
    return application.test_client()


def _login(client, email, password):
    resp = client.post("/api/auth/login", json={"email": email, "password": password})
    body = resp.get_json() or {}
    return resp.status_code, body.get("id")


# ---------------------------------------------------------------------------
# Candidate selection: the password decides the account, not row ordering.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("typed_email", "password", "expected_status", "expected_uid"),
    [
        # The self-registered owner's password reaches their own account,
        # regardless of how they capitalize the address.
        (_EMAIL_MIXED, _SELF_PW, 200, "uid-self"),
        (_EMAIL_LOWER, _SELF_PW, 200, "uid-self"),
        (_EMAIL_MIXED.upper(), _SELF_PW, 200, "uid-self"),
        # The admin-created account stays reachable with the admin's password.
        (_EMAIL_LOWER, _ADMIN_PW, 200, "uid-admin"),
        (_EMAIL_MIXED, _ADMIN_PW, 200, "uid-admin"),
        (_EMAIL_MIXED.upper(), _ADMIN_PW, 200, "uid-admin"),
        # Neither candidate's password matches.
        (_EMAIL_LOWER, "WrongPw", 401, None),
        # No candidate at all.
        ("nobody@nowhere.com", _SELF_PW, 401, None),
    ],
)
def test_password_decides_which_duplicate_account_logs_in(
    login_client, typed_email, password, expected_status, expected_uid
):
    status, uid = _login(login_client, typed_email, password)
    assert status == expected_status
    assert uid == expected_uid


def test_surrounding_whitespace_is_stripped(login_client):
    """Copy-paste and mobile autocomplete routinely add a trailing space."""
    assert _login(login_client, f"  {_EMAIL_MIXED} ", _SELF_PW) == (200, "uid-self")


def test_lowercase_only_account_is_reachable_when_typed_capitalized(monkeypatch):
    """The original lockout: one lowercase row, user types it capitalized."""
    for heavy in ("celery_config", "celery", "routes.audit_routes"):
        if heavy not in sys.modules:
            sys.modules[heavy] = MagicMock()
    sys.modules["routes.audit_routes"].record_audit_event = MagicMock()

    from flask import Flask

    from routes import auth_routes

    only_row = [_row("uid-admin", _EMAIL_LOWER, _ADMIN_PW, 1, "acme")]
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = _FakeCursor(only_row)
    monkeypatch.setattr(auth_routes, "connect_to_db_as_user", lambda: fake_conn)
    monkeypatch.setattr(auth_routes, "record_audit_event", MagicMock())

    application = Flask(__name__)  # NOSONAR — test-local app
    application.register_blueprint(auth_routes.auth_bp)
    client = application.test_client()

    assert _login(client, _EMAIL_MIXED, _ADMIN_PW) == (200, "uid-admin")
    assert _login(client, _EMAIL_MIXED.upper(), _ADMIN_PW) == (200, "uid-admin")


def test_failed_login_still_records_one_audit_event(login_client, monkeypatch):
    """Timing invariant: success and failure both do exactly one audit INSERT."""
    from routes import auth_routes

    recorder = MagicMock()
    monkeypatch.setattr(auth_routes, "record_audit_event", recorder)

    _login(login_client, _EMAIL_LOWER, "WrongPw")
    assert recorder.call_count == 1
    assert recorder.call_args[0][2] == "login_failed"
    assert recorder.call_args[0][5]["reason"] == "invalid_password"

    recorder.reset_mock()
    _login(login_client, "nobody@nowhere.com", "WrongPw")
    assert recorder.call_count == 1
    detail = recorder.call_args[0][5]
    assert detail["reason"] == "unknown_email"
    # Unknown emails are hashed, never stored in the clear.
    assert "email" not in detail
    assert len(detail["email_sha256"]) == 64

    recorder.reset_mock()
    _login(login_client, _EMAIL_LOWER, _ADMIN_PW)
    assert recorder.call_count == 1
    assert recorder.call_args[0][2] == "login"


# ---------------------------------------------------------------------------
# Malformed bodies must 400/401, never 500.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "body",
    [
        {"email": _EMAIL_LOWER},                       # no password
        {"password": _ADMIN_PW},                       # no email
        {"email": 12345, "password": _ADMIN_PW},       # non-string email
        {"email": None, "password": _ADMIN_PW},
        {"email": "   ", "password": _ADMIN_PW},       # whitespace-only
        {},
    ],
)
def test_malformed_login_body_is_a_400(login_client, body):
    resp = login_client.post("/api/auth/login", json=body)
    assert resp.status_code == 400


@pytest.mark.parametrize("password", [12345, None, {"a": 1}, [1, 2], True])
def test_non_string_password_does_not_500(login_client, password):
    """A hand-crafted password type must fall through to 400/401, not blow up."""
    resp = login_client.post(
        "/api/auth/login", json={"email": _EMAIL_LOWER, "password": password}
    )
    assert resp.status_code in (400, 401)


def test_non_dict_json_body_is_a_400(login_client):
    """A JSON list/scalar body: .get() on it would raise AttributeError."""
    for raw in ("[1,2,3]", '"just-a-string"', "42"):
        resp = login_client.post(
            "/api/auth/login", data=raw, content_type="application/json"
        )
        assert resp.status_code == 400, raw


def test_corrupt_hash_on_first_candidate_does_not_block_the_second(monkeypatch):
    """One unusable hash must not abort the loop before the valid account."""
    for heavy in ("celery_config", "celery", "routes.audit_routes"):
        if heavy not in sys.modules:
            sys.modules[heavy] = MagicMock()
    sys.modules["routes.audit_routes"].record_audit_event = MagicMock()

    from flask import Flask

    from routes import auth_routes

    # Exact-case row is ordered first but carries a truncated hash.
    corrupt = list(_row("uid-corrupt", _EMAIL_MIXED, _ADMIN_PW, 1, "orgA"))
    corrupt[3] = corrupt[3][:20]
    rows = [tuple(corrupt), _row("uid-good", _EMAIL_LOWER, _SELF_PW, 2, "orgB")]

    fake_conn = MagicMock()
    fake_conn.cursor.return_value = _FakeCursor(rows)
    monkeypatch.setattr(auth_routes, "connect_to_db_as_user", lambda: fake_conn)
    monkeypatch.setattr(auth_routes, "record_audit_event", MagicMock())

    application = Flask(__name__)  # NOSONAR — test-local app
    application.register_blueprint(auth_routes.auth_bp)
    client = application.test_client()

    assert _login(client, _EMAIL_MIXED, _SELF_PW) == (200, "uid-good")


# ---------------------------------------------------------------------------
# _password_matches: must return a bool for any input, never raise.
# ---------------------------------------------------------------------------

_GOOD_HASH = bcrypt.hashpw(b"correct-horse", bcrypt.gensalt()).decode()


@pytest.mark.parametrize(
    ("password", "password_hash", "expected"),
    [
        ("correct-horse", _GOOD_HASH, True),
        ("wrong-horse", _GOOD_HASH, False),
        # Non-string password from a hand-crafted JSON body. .encode() would
        # raise AttributeError, which is NOT in the (ValueError, TypeError)
        # tuple — hence the explicit isinstance guard.
        (12345, _GOOD_HASH, False),
        ({"a": 1}, _GOOD_HASH, False),
        ([1, 2], _GOOD_HASH, False),
        (None, _GOOD_HASH, False),
        # Corrupt / truncated / missing hashes must not abort the candidate loop.
        ("correct-horse", "not-a-bcrypt-hash", False),
        ("correct-horse", _GOOD_HASH[:20], False),
        ("correct-horse", "", False),
        ("correct-horse", None, False),
        ("correct-horse", 12345, False),
        # bcrypt raises above 72 bytes on some builds; must be a clean False.
        ("x" * 200, _GOOD_HASH, False),
    ],
)
def test_password_matches_never_raises(password, password_hash, expected):
    from routes.auth_routes import _password_matches

    result = _password_matches(password, password_hash)
    assert isinstance(result, bool)
    assert result is expected


def test_over_long_correct_password_is_not_a_500():
    """A 200-char password against its own hash must resolve, not explode."""
    from routes.auth_routes import _password_matches

    long_pw = "y" * 200
    long_hash = bcrypt.hashpw(long_pw.encode()[:72], bcrypt.gensalt()).decode()
    assert isinstance(_password_matches(long_pw, long_hash), bool)


# ---------------------------------------------------------------------------
# normalize_email
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Foo@Bar.COM", "foo@bar.com"),
        ("  foo@bar.com  ", "foo@bar.com"),
        ("FOO@BAR.COM", "foo@bar.com"),
        ("foo@bar.com", "foo@bar.com"),
        ("", ""),
        (None, ""),
        (12345, ""),
        ({"a": 1}, ""),
    ],
)
def test_normalize_email(raw, expected):
    from utils.auth import normalize_email

    assert normalize_email(raw) == expected
