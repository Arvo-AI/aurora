"""Forgot-password flow: /forgot-password and /reset-password in auth_routes.

Pins the security contract of an unauthenticated password reset:

- /forgot-password answers with one constant 200 body for every outcome
  (account found, no account, ambiguous match, SMTP down, internal error) so it
  can't be used to enumerate which emails have Aurora accounts.
- Only a SHA-256 digest of the code is stored, so a database read can't be
  replayed as a valid code.
- A reset is refused when SMTP is unconfigured — there would be no emailed code
  to prove inbox ownership, so auto-approving would be account takeover.
- /reset-password rejects wrong, expired, never-issued, and over-attempted
  codes with one constant message, increments the attempt counter only on a
  wrong code, and clears the code on success so it can't be replayed.

Emails are fictional addresses on the reserved example.com domain. DB and SMTP
are mocked — no I/O.
"""

from __future__ import annotations

import hashlib
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import bcrypt
import pytest

_EMAIL = "first.last@example.com"
_UID = "uid-reset"
_NEW_PW = "BrandNewPw123"


class _FakeCursor:
    """Cursor stub implementing just the statements these two routes issue.

    Keeps real per-statement semantics (exact vs. LOWER() email match, the
    reset-code SELECT, the attempt-counter UPDATE) so the tests exercise the
    route's actual branching rather than a hardcoded fetch sequence.
    """

    def __init__(self, users, reset_state):
        # users: list of (id, email) rows. reset_state: dict keyed by user id.
        self.users = users
        self.reset_state = reset_state
        self.executed: list[tuple[str, tuple]] = []
        self._result: list = []

    def execute(self, query, params=None):
        params = params or ()
        self.executed.append((query, params))
        normalized = " ".join(query.split())

        # Exact-case email lookup — the preferred match.
        if "WHERE email = %s" in normalized:
            self._result = [u for u in self.users if u[1] == params[0]]
        # Case-insensitive fallback, only used when the exact match missed.
        elif "LOWER(email) = LOWER(%s)" in normalized:
            self._result = [u for u in self.users if u[1].lower() == params[0].lower()]
        # Reset-code freshness probe (per-account cooldown).
        elif "SELECT password_reset_code_expires_at" in normalized:
            state = self.reset_state.get(params[-1])
            self._result = [(state[1],)] if state else []
        # Reset-code state for a resolved user id.
        elif "SELECT password_reset_code" in normalized:
            self._result = [self.reset_state.get(params[-1])]
            self._result = [r for r in self._result if r is not None]
        # Wrong-code path: bump the attempt counter.
        elif "password_reset_attempts = COALESCE" in normalized:
            uid = params[-1]
            prev = self.reset_state[uid]
            self.reset_state[uid] = (prev[0], prev[1], prev[2] + 1)
            self._result = []
        # Issuance: new code + expiry. Mirrors the real UPDATE, which carries
        # attempts over unless the previous code lapsed outside the budget
        # window. Both SQL shapes are honoured so a regression to the old
        # unconditional `= 0` fails on the attempt count, not on unpacking.
        elif "SET password_reset_code = %s" in normalized:
            code_hash, expires, uid = params[0], params[1], params[-1]
            prev = self.reset_state.get(uid)
            spent = prev[2] if prev else 0
            prev_expiry = prev[1] if prev else None
            carries_over = "password_reset_attempts = CASE" in normalized
            if not carries_over or prev_expiry is None:
                carried = 0
            else:
                lapsed_before = params[2]
                carried = 0 if prev_expiry < lapsed_before else spent
            self.reset_state[uid] = (code_hash, expires, carried)
            self._result = []
        # Success path: new password written, code burned.
        elif "SET password_hash = %s" in normalized:
            uid = params[-1]
            self.reset_state[uid] = (None, None, 0)
            self._result = []
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def sql_for(self, fragment: str):
        """Return the (query, params) of the first executed statement matching."""
        return next(
            (
                (q, p) for q, p in self.executed
                if fragment in " ".join(q.split())
            ),
            None,
        )


@pytest.fixture
def reset_env(monkeypatch):
    """auth_bp test client with the users table, pool, and SMTP faked out.

    Yields ``(client, cursor, email_svc)`` so tests can assert on both the HTTP
    response and what was written/sent.
    """
    for heavy in ("celery_config", "celery", "routes.audit_routes"):
        if heavy not in sys.modules:
            sys.modules[heavy] = MagicMock()
    sys.modules["routes.audit_routes"].record_audit_event = MagicMock()

    from flask import Flask

    from routes import auth_routes

    # One live reset code, unexpired, no attempts used yet.
    cursor = _FakeCursor(
        users=[(_UID, _EMAIL)],
        reset_state={},
    )

    fake_conn = MagicMock()
    fake_conn.cursor.return_value = cursor
    fake_conn.__enter__ = MagicMock(return_value=fake_conn)
    fake_conn.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _admin_conn():
        yield fake_conn

    fake_pool = MagicMock()
    fake_pool.get_admin_connection = _admin_conn

    monkeypatch.setattr(auth_routes, "connect_to_db_as_user", lambda: fake_conn)
    monkeypatch.setattr(auth_routes, "db_pool", fake_pool)
    monkeypatch.setattr(auth_routes, "record_audit_event", MagicMock())
    monkeypatch.setattr(
        "utils.auth.stateless_auth.resolve_org_id", lambda _uid: "acme"
    )

    email_svc = MagicMock()
    email_svc.send_password_reset_email.return_value = True
    monkeypatch.setattr(
        "utils.notifications.email_service.get_email_service", lambda: email_svc
    )

    application = Flask(__name__)  # NOSONAR — test-local app
    application.register_blueprint(auth_routes.auth_bp)
    return application.test_client(), cursor, email_svc


def _issue_code(cursor, code="123456", *, expires_in_min=15, attempts=0, uid=_UID):
    """Seed a reset code for ``uid`` exactly as send_password_reset_email would."""
    cursor.reset_state[uid] = (
        hashlib.sha256(code.encode()).hexdigest(),
        datetime.now() + timedelta(minutes=expires_in_min),
        attempts,
    )
    return code


def _forgot(client, email):
    resp = client.post("/api/auth/forgot-password", json={"email": email})
    return resp.status_code, (resp.get_json() or {})


def _reset(client, email, code, password=_NEW_PW):
    resp = client.post(
        "/api/auth/reset-password",
        json={"email": email, "code": code, "newPassword": password},
    )
    return resp.status_code, (resp.get_json() or {})


# ---------------------------------------------------------------------------
# /forgot-password: one constant response, never an enumeration oracle.
# ---------------------------------------------------------------------------

class TestForgotPasswordIsNotAnOracle:
    def test_known_email_sends_code(self, reset_env):
        client, cursor, email_svc = reset_env
        status, body = _forgot(client, _EMAIL)
        assert status == 200
        email_svc.send_password_reset_email.assert_called_once()
        sent_to, sent_code = email_svc.send_password_reset_email.call_args.args
        assert sent_to == _EMAIL
        assert len(sent_code) == 6
        assert sent_code.isdigit()

    def test_only_the_hash_is_stored(self, reset_env):
        # A DB read must not yield a usable code.
        client, cursor, email_svc = reset_env
        _forgot(client, _EMAIL)
        _, sent_code = email_svc.send_password_reset_email.call_args.args
        _, params = cursor.sql_for("SET password_reset_code = %s")
        stored = params[0]
        assert stored == hashlib.sha256(sent_code.encode()).hexdigest()
        assert sent_code not in stored

    @pytest.mark.parametrize(
        "email",
        [
            "nobody@example.com",   # no such account
            "",                     # empty
            "   ",                  # whitespace only
            "a" * 250 + "@example.com",  # over the 254-char cap
        ],
    )
    def test_unknown_or_invalid_email_returns_same_body(self, reset_env, email):
        client, cursor, email_svc = reset_env
        known_status, known_body = _forgot(client, _EMAIL)
        other_status, other_body = _forgot(client, email)
        assert (other_status, other_body) == (known_status, known_body)

    def test_non_string_email_returns_same_body(self, reset_env):
        client, _cursor, _svc = reset_env
        known_status, known_body = _forgot(client, _EMAIL)
        resp = client.post("/api/auth/forgot-password", json={"email": {"a": 1}})
        assert (resp.status_code, resp.get_json()) == (known_status, known_body)

    def test_missing_body_returns_same_body(self, reset_env):
        client, _cursor, _svc = reset_env
        known_status, known_body = _forgot(client, _EMAIL)
        resp = client.post("/api/auth/forgot-password")
        assert (resp.status_code, resp.get_json()) == (known_status, known_body)

    def test_unknown_email_sends_nothing(self, reset_env):
        client, _cursor, email_svc = reset_env
        _forgot(client, "nobody@example.com")
        email_svc.send_password_reset_email.assert_not_called()

    def test_response_mentions_spam_folder(self, reset_env):
        # The code email is the single most spam-filtered thing we send, so the
        # hint has to be in the UI copy too — not only in the email body.
        client, _cursor, _svc = reset_env
        _status, body = _forgot(client, _EMAIL)
        assert "spam" in body["message"].lower()


class TestForgotPasswordAmbiguityAndFailures:
    def test_ambiguous_case_variants_send_nothing(self, reset_env):
        # Legacy duplicate rows differing only by case: mailing a code for the
        # wrong row is worse than asking for the exact address.
        client, cursor, email_svc = reset_env
        cursor.users = [("uid-a", "dupe@example.com"), ("uid-b", "Dupe@example.com")]
        status, body = _forgot(client, "DUPE@example.com")
        assert status == 200
        email_svc.send_password_reset_email.assert_not_called()

    def test_exact_case_match_wins_over_ambiguity(self, reset_env):
        # An exact hit is unambiguous even when case-variants exist.
        client, cursor, email_svc = reset_env
        cursor.users = [("uid-a", "dupe@example.com"), ("uid-b", "Dupe@example.com")]
        _forgot(client, "Dupe@example.com")
        sent_to, _code = email_svc.send_password_reset_email.call_args.args
        assert sent_to == "Dupe@example.com"

    def test_single_case_variant_still_resolves(self, reset_env):
        # Only one row matches case-insensitively — safe to use it.
        client, cursor, email_svc = reset_env
        cursor.users = [(_UID, _EMAIL)]
        _forgot(client, _EMAIL.upper())
        sent_to, _code = email_svc.send_password_reset_email.call_args.args
        assert sent_to == _EMAIL

    def test_smtp_unconfigured_refuses_without_leaking(self, monkeypatch, reset_env):
        # No email means no proof of inbox ownership. Must NOT auto-approve,
        # and must not become an oracle by answering differently.
        client, cursor, _svc = reset_env
        monkeypatch.setattr(
            "utils.notifications.email_service.get_email_service",
            MagicMock(side_effect=ValueError("SMTP not configured")),
        )
        status, body = _forgot(client, _EMAIL)
        assert status == 200
        assert "spam" in body["message"].lower()
        # Crucially: no password was touched and no code was left behind.
        assert cursor.sql_for("SET password_hash = %s") is None

    def test_send_failure_returns_same_body(self, reset_env):
        client, _cursor, email_svc = reset_env
        email_svc.send_password_reset_email.return_value = False
        status, body = _forgot(client, _EMAIL)
        assert status == 200
        assert "spam" in body["message"].lower()

    def test_internal_error_returns_same_body(self, monkeypatch, reset_env):
        client, _cursor, _svc = reset_env
        known_status, known_body = _forgot(client, _EMAIL)
        monkeypatch.setattr(
            "routes.auth_routes.connect_to_db_as_user",
            MagicMock(side_effect=RuntimeError("db down")),
        )
        resp = client.post("/api/auth/forgot-password", json={"email": _EMAIL})
        assert (resp.status_code, resp.get_json()) == (known_status, known_body)


# ---------------------------------------------------------------------------
# /reset-password: the code is the only credential, so guessing must be costly
# and every rejection must look alike.
# ---------------------------------------------------------------------------

_INVALID = "Invalid or expired reset code"


class TestResetPasswordSuccess:
    def test_correct_code_sets_new_password(self, reset_env):
        client, cursor, _svc = reset_env
        code = _issue_code(cursor)
        status, body = _reset(client, _EMAIL, code)
        assert status == 200
        assert body["message"] == "Password reset successfully"

    def test_new_password_is_bcrypt_hashed(self, reset_env):
        client, cursor, _svc = reset_env
        code = _issue_code(cursor)
        _reset(client, _EMAIL, code)
        _q, params = cursor.sql_for("SET password_hash = %s")
        stored_hash = params[0]
        assert stored_hash.startswith("$2")
        assert stored_hash != _NEW_PW
        assert bcrypt.checkpw(_NEW_PW.encode(), stored_hash.encode())

    def test_code_is_burned_so_it_cannot_be_replayed(self, reset_env):
        client, cursor, _svc = reset_env
        code = _issue_code(cursor)
        assert _reset(client, _EMAIL, code)[0] == 200
        # Same code again must fail — the row was cleared.
        status, body = _reset(client, _EMAIL, code)
        assert status == 400
        assert body["error"] == _INVALID

    def test_success_clears_must_change_and_verifies_email(self, reset_env):
        # The user just proved inbox ownership and chose this password, so
        # neither forced-change nor re-verification should greet them.
        client, cursor, _svc = reset_env
        code = _issue_code(cursor)
        _reset(client, _EMAIL, code)
        query, _params = cursor.sql_for("SET password_hash = %s")
        flat = " ".join(query.split())
        assert "must_change_password = FALSE" in flat
        assert "email_verified = TRUE" in flat
        assert "password_reset_code = NULL" in flat

    def test_case_insensitive_email_still_resets(self, reset_env):
        client, cursor, _svc = reset_env
        code = _issue_code(cursor)
        assert _reset(client, _EMAIL.upper(), code)[0] == 200


class TestResetPasswordRejections:
    def test_wrong_code_rejected(self, reset_env):
        client, cursor, _svc = reset_env
        _issue_code(cursor, "123456")
        status, body = _reset(client, _EMAIL, "654321")
        assert status == 400
        assert body["error"] == _INVALID

    def test_wrong_code_does_not_change_password(self, reset_env):
        client, cursor, _svc = reset_env
        _issue_code(cursor, "123456")
        _reset(client, _EMAIL, "654321")
        assert cursor.sql_for("SET password_hash = %s") is None

    def test_wrong_code_increments_attempts(self, reset_env):
        client, cursor, _svc = reset_env
        _issue_code(cursor, "123456")
        _reset(client, _EMAIL, "654321")
        assert cursor.reset_state[_UID][2] == 1

    def test_correct_code_does_not_increment_attempts(self, reset_env):
        client, cursor, _svc = reset_env
        code = _issue_code(cursor)
        _reset(client, _EMAIL, code)
        assert cursor.sql_for("password_reset_attempts = COALESCE") is None

    def test_expired_code_rejected(self, reset_env):
        client, cursor, _svc = reset_env
        code = _issue_code(cursor, expires_in_min=-1)
        status, body = _reset(client, _EMAIL, code)
        assert status == 400
        assert body["error"] == _INVALID
        assert cursor.sql_for("SET password_hash = %s") is None

    def test_never_issued_code_rejected(self, reset_env):
        # No reset was ever requested for this account.
        client, cursor, _svc = reset_env
        cursor.reset_state[_UID] = (None, None, 0)
        status, body = _reset(client, _EMAIL, "123456")
        assert status == 400
        assert body["error"] == _INVALID

    def test_attempt_cap_locks_out_even_with_correct_code(self, reset_env):
        # Brute-force protection must not be bypassable by eventually guessing
        # right — the cap is checked before the comparison.
        client, cursor, _svc = reset_env
        code = _issue_code(cursor, attempts=5)
        status, body = _reset(client, _EMAIL, code)
        assert status == 429
        assert "Too many attempts" in body["error"]
        assert cursor.sql_for("SET password_hash = %s") is None

    def test_unknown_email_rejected_generically(self, reset_env):
        client, cursor, _svc = reset_env
        _issue_code(cursor)
        status, body = _reset(client, "nobody@example.com", "123456")
        assert status == 400
        assert body["error"] == _INVALID


class TestResetPasswordInputValidation:
    @pytest.mark.parametrize(
        "payload",
        [
            {"code": "123456", "newPassword": _NEW_PW},               # no email
            {"email": _EMAIL, "newPassword": _NEW_PW},                # no code
            {"email": "", "code": "123456", "newPassword": _NEW_PW},  # empty email
        ],
    )
    def test_missing_fields_are_400(self, reset_env, payload):
        client, cursor, _svc = reset_env
        _issue_code(cursor)
        resp = client.post("/api/auth/reset-password", json=payload)
        assert resp.status_code == 400
        assert cursor.sql_for("SET password_hash = %s") is None

    @pytest.mark.parametrize("code", ["12345", "1234567", "12345a", "abcdef", "  "])
    def test_malformed_code_never_reaches_the_db(self, reset_env, code):
        client, cursor, _svc = reset_env
        _issue_code(cursor)
        resp = client.post(
            "/api/auth/reset-password",
            json={"email": _EMAIL, "code": code, "newPassword": _NEW_PW},
        )
        assert resp.status_code == 400
        assert cursor.sql_for("SELECT password_reset_code") is None

    @pytest.mark.parametrize("password", ["short", "", "1234567"])
    def test_short_password_rejected(self, reset_env, password):
        client, cursor, _svc = reset_env
        code = _issue_code(cursor)
        status, body = _reset(client, _EMAIL, code, password)
        assert status == 400
        assert "8 characters" in body["error"]
        assert cursor.sql_for("SET password_hash = %s") is None

    @pytest.mark.parametrize("password", [None, 12345678, {"a": 1}, ["x"]])
    def test_non_string_password_is_400_not_500(self, reset_env, password):
        # len()/.encode() on a non-string would raise and surface as a 500.
        client, cursor, _svc = reset_env
        code = _issue_code(cursor)
        resp = client.post(
            "/api/auth/reset-password",
            json={"email": _EMAIL, "code": code, "newPassword": password},
        )
        assert resp.status_code == 400
        assert cursor.sql_for("SET password_hash = %s") is None

    def test_missing_body_is_400_not_500(self, reset_env):
        client, _cursor, _svc = reset_env
        resp = client.post("/api/auth/reset-password")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Email bodies: every code email carries the spam-folder notice.
# ---------------------------------------------------------------------------

@pytest.fixture
def email_service(monkeypatch):
    """EmailService with SMTP config satisfied and _send_email captured."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "aurora@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "unused-in-test")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "aurora@example.com")
    monkeypatch.setenv("SMTP_FROM_NAME", "Aurora")
    monkeypatch.setenv("FRONTEND_URL", "https://aurora.example.com")

    from utils.notifications.email_service import EmailService

    svc = EmailService()
    sent: list[dict] = []
    monkeypatch.setattr(
        svc,
        "_send_email",
        lambda to, subject, html_body, text_body: sent.append({
            "to": to, "subject": subject, "html": html_body, "text": text_body,
        }) or True,
    )
    return svc, sent


class TestCodeEmails:
    @pytest.mark.parametrize(
        "method",
        [
            "send_account_verification_email",
            "send_password_reset_email",
            "send_verification_code_email",
        ],
    )
    def test_every_code_email_mentions_spam_folder(self, email_service, method):
        # Users who can't find the code read it as a broken product, so the
        # hint belongs in all three code emails, in both MIME parts.
        svc, sent = email_service
        getattr(svc, method)("user@example.com", "123456")
        body = sent[0]
        assert "spam" in body["text"].lower()
        assert "spam" in body["html"].lower()

    @pytest.mark.parametrize(
        "method",
        [
            "send_account_verification_email",
            "send_password_reset_email",
            "send_verification_code_email",
        ],
    )
    def test_code_appears_in_both_mime_parts(self, email_service, method):
        svc, sent = email_service
        getattr(svc, method)("user@example.com", "246813")
        assert "246813" in sent[0]["text"]
        assert "246813" in sent[0]["html"]

    def test_reset_email_says_password_was_not_changed(self, email_service):
        # An unsolicited reset email should reassure, not alarm.
        svc, sent = email_service
        svc.send_password_reset_email("user@example.com", "123456")
        assert "has not been changed" in sent[0]["text"]

    def test_reset_and_verification_emails_are_distinguishable(self, email_service):
        # Same builder, different copy — a user must be able to tell a takeover
        # attempt from their own signup.
        svc, sent = email_service
        svc.send_password_reset_email("user@example.com", "123456")
        svc.send_account_verification_email("user@example.com", "123456")
        reset, verify = sent
        assert reset["subject"] != verify["subject"]
        assert "Reset Your Password" in reset["subject"]

    def test_reset_text_part_does_not_call_the_code_a_verification_code(
        self, email_service
    ):
        # The shared builder used to hardcode "Your verification code is:" in the
        # text part, so the reset email contradicted its own HTML.
        svc, sent = email_service
        svc.send_password_reset_email("user@example.com", "123456")
        assert "verification code" not in sent[0]["text"].lower()

    @pytest.mark.parametrize(
        "method",
        [
            "send_account_verification_email",
            "send_password_reset_email",
            "send_verification_code_email",
        ],
    )
    def test_both_mime_parts_use_the_same_code_label(self, email_service, method):
        svc, sent = email_service
        getattr(svc, method)("user@example.com", "123456")
        body = sent[0]
        # The label sits directly above the code box in the HTML; the text part
        # must introduce the code the same way.
        label = "Enter this code in Aurora to choose a new password:"
        if method != "send_password_reset_email":
            label = "Enter this verification code in Aurora:"
        assert label in body["text"]
        assert label in body["html"]


class TestAttemptBudgetSurvivesReissue:
    """The 5-guess cap has to be per account, not per code.

    Reissuing used to zero password_reset_attempts, so an attacker who can
    request codes (20/hour) got a fresh 5 guesses each time — ~100/hour, with no
    cumulative ceiling. The budget now carries across reissues and lapses only
    once the account has been quiet.
    """

    def test_reissue_keeps_the_attempts_already_spent(self, reset_env):
        client, cursor, email_svc = reset_env
        # 4 guesses burned, and the code is old enough to be reissued but well
        # inside the budget window.
        _issue_code(cursor, expires_in_min=13, attempts=4)
        _forgot(client, _EMAIL)
        email_svc.send_password_reset_email.assert_called_once()
        assert cursor.reset_state[_UID][2] == 4

    def test_reissue_cannot_buy_a_sixth_guess(self, reset_env):
        client, cursor, email_svc = reset_env
        _issue_code(cursor, expires_in_min=13, attempts=5)
        _forgot(client, _EMAIL)
        # A new code was mailed, but the spent budget came with it.
        new_code = email_svc.send_password_reset_email.call_args.args[1]
        status, body = _reset(client, _EMAIL, new_code)
        assert status == 429
        assert "Too many attempts" in body["error"]
        assert cursor.sql_for("SET password_hash = %s") is None

    def test_429_does_not_tell_the_user_to_request_a_new_code(self, reset_env):
        # Requesting one no longer clears the counter, so advising it would
        # just send the user in a loop.
        client, cursor, _svc = reset_env
        _issue_code(cursor, attempts=5)
        _status, body = _reset(client, _EMAIL, "123456")
        assert "new code" not in body["error"].lower()

    def test_budget_lapses_once_the_account_goes_quiet(self, reset_env):
        # Code expired longer ago than the lapse window — a legitimate user who
        # burned their guesses must not be locked out forever.
        from routes import auth_routes

        client, cursor, email_svc = reset_env
        _issue_code(
            cursor,
            expires_in_min=-(auth_routes.PASSWORD_RESET_ATTEMPT_BUDGET_LAPSE_MINUTES + 5),
            attempts=5,
        )
        _forgot(client, _EMAIL)
        email_svc.send_password_reset_email.assert_called_once()
        assert cursor.reset_state[_UID][2] == 0

        new_code = email_svc.send_password_reset_email.call_args.args[1]
        status, _body = _reset(client, _EMAIL, new_code)
        assert status == 200


class TestPerAccountCooldown:
    """Rate limiting bounds the request rate; this cooldown bounds the mail. Even
    with a per-email limiter key, 5 requests/min would still mean 5 emails/min to
    one mailbox, so the cooldown is what actually stops a flood."""

    def test_second_request_within_cooldown_sends_nothing(self, reset_env):
        client, cursor, email_svc = reset_env
        # A code was mailed seconds ago (full 15 min still to run).
        _issue_code(cursor, expires_in_min=15)
        _forgot(client, _EMAIL)
        email_svc.send_password_reset_email.assert_not_called()

    def test_suppressed_request_still_returns_the_same_body(self, reset_env):
        # Suppression must not be observable — a 429 here would be the oracle
        # the constant body exists to avoid.
        client, cursor, _svc = reset_env
        _status, fresh_body = _forgot(client, _EMAIL)
        _issue_code(cursor, expires_in_min=15)
        status, body = _forgot(client, _EMAIL)
        assert (status, body) == (200, fresh_body)

    def test_request_after_cooldown_sends_again(self, reset_env):
        client, cursor, email_svc = reset_env
        # Issued ~2 min ago: 13 min of a 15 min lifetime left.
        _issue_code(cursor, expires_in_min=13)
        _forgot(client, _EMAIL)
        email_svc.send_password_reset_email.assert_called_once()

    def test_expired_code_does_not_block_a_new_request(self, reset_env):
        client, cursor, email_svc = reset_env
        _issue_code(cursor, expires_in_min=-5)
        _forgot(client, _EMAIL)
        email_svc.send_password_reset_email.assert_called_once()

    def test_freshness_lookup_failure_fails_open(self, monkeypatch, reset_env):
        # A read error must not lock a user out of resetting their password, so
        # the helper reports "not fresh" rather than propagating.
        from routes import auth_routes

        broken_pool = MagicMock()
        broken_pool.get_admin_connection = MagicMock(
            side_effect=RuntimeError("db blip")
        )
        monkeypatch.setattr(auth_routes, "db_pool", broken_pool)
        assert auth_routes._reset_code_is_fresh(_UID) is False

    def test_freshness_failure_still_sends_the_code(self, monkeypatch, reset_env):
        client, cursor, email_svc = reset_env
        _issue_code(cursor, expires_in_min=15)
        monkeypatch.setattr(
            "routes.auth_routes._reset_code_is_fresh", lambda _uid: False
        )
        _forgot(client, _EMAIL)
        email_svc.send_password_reset_email.assert_called_once()


class TestResetCodeRowLock:
    """The verify → increment → write sequence must serialize, or two concurrent
    requests can both read the same attempt count and slip past the cap."""

    def test_code_lookup_locks_the_row(self, reset_env):
        client, cursor, _svc = reset_env
        code = _issue_code(cursor)
        _reset(client, _EMAIL, code)
        query, _params = cursor.sql_for("SELECT password_reset_code,")
        assert "FOR UPDATE" in " ".join(query.split())

    def test_lock_is_taken_before_the_password_write(self, reset_env):
        # Both statements must land inside one transaction, the SELECT first.
        client, cursor, _svc = reset_env
        code = _issue_code(cursor)
        _reset(client, _EMAIL, code)
        statements = [" ".join(q.split()) for q, _p in cursor.executed]
        locked = [i for i, q in enumerate(statements) if "FOR UPDATE" in q]
        wrote = [
            i for i, q in enumerate(statements) if "SET password_hash = %s" in q
        ]
        assert len(locked) == 1
        assert len(wrote) == 1
        assert locked[0] < wrote[0]


class TestPublicAuthRateLimitKey:
    """These routes carry no X-User-ID, so the default key would bucket every
    caller under the frontend proxy's address — one global allowance."""

    @staticmethod
    def _key(app, body):
        from utils.web.limiter_ext import get_public_auth_rate_limit_key

        with app.test_request_context("/api/auth/forgot-password", json=body):
            return get_public_auth_rate_limit_key()

    @pytest.fixture
    def app(self):
        from flask import Flask

        return Flask(__name__)  # NOSONAR — test-local app

    def test_distinct_emails_get_distinct_buckets(self, app):
        assert self._key(app, {"email": "a@example.com"}) != self._key(
            app, {"email": "b@example.com"}
        )

    def test_same_email_is_stable_across_requests(self, app):
        # Compared against an independently computed digest rather than a second
        # call, so this asserts the actual key format, not just self-consistency.
        expected = hashlib.sha256(b"a@example.com").hexdigest()
        assert self._key(app, {"email": "a@example.com"}) == f"email:{expected}"

    def test_case_and_whitespace_share_one_bucket(self, app):
        # Otherwise re-casing the address resets the allowance.
        assert self._key(app, {"email": "  User@Example.com "}) == self._key(
            app, {"email": "user@example.com"}
        )

    def test_key_does_not_contain_the_raw_email(self, app):
        # The key becomes a Redis key; addresses shouldn't be written into it.
        key = self._key(app, {"email": "user@example.com"})
        assert "user@example.com" not in key
        assert key.startswith("email:")

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"email": ""},
            {"email": "   "},
            {"email": None},
            {"email": 12345},
            {"email": "a" * 250 + "@example.com"},
        ],
        ids=["missing", "empty", "blank", "null", "non-string", "too-long"],
    )
    def test_unusable_email_falls_back_to_the_ip_bucket(self, app, body):
        # Must not mint an unlimited per-request key for junk input.
        assert self._key(app, body).startswith("ip:")

    def test_non_dict_body_falls_back_to_the_ip_bucket(self, app):
        from utils.web.limiter_ext import get_public_auth_rate_limit_key

        with app.test_request_context(
            "/api/auth/forgot-password", json=["not", "a", "dict"]
        ):
            assert get_public_auth_rate_limit_key().startswith("ip:")

    def test_malformed_json_does_not_raise(self, app):
        from utils.web.limiter_ext import get_public_auth_rate_limit_key

        with app.test_request_context(
            "/api/auth/forgot-password",
            data="{not json",
            content_type="application/json",
        ):
            assert get_public_auth_rate_limit_key().startswith("ip:")

    def test_reading_the_body_leaves_it_available_to_the_route(self, reset_env):
        # The limiter calls the key function before the view runs, so the body is
        # read twice per request. Flask caches the parsed JSON, but the limiter is
        # not initialized on this test app, so the first read has to be staged
        # explicitly — otherwise this passes without the double read ever happening.
        from utils.web.limiter_ext import get_public_auth_rate_limit_key

        client, cursor, email_svc = reset_env
        keys: list[str] = []

        @client.application.before_request
        def _read_key() -> None:
            keys.append(get_public_auth_rate_limit_key())

        _issue_code(cursor, expires_in_min=-5)
        status, _body = _forgot(client, _EMAIL)

        # The key really was derived from the body, ahead of the view.
        assert keys == [f"email:{hashlib.sha256(_EMAIL.encode()).hexdigest()}"]
        # And the view still read the same body rather than an empty one.
        assert status == 200
        email_svc.send_password_reset_email.assert_called_once()
        assert email_svc.send_password_reset_email.call_args.args[0] == _EMAIL


# ---------------------------------------------------------------------------
# SMTP availability: /forgot-password answers 200 even when it can't send, so
# the UI needs a separate way to know a reset is impossible.
# ---------------------------------------------------------------------------

_SMTP_ENV = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")


class TestIsEmailConfigured:
    def test_true_when_all_required_vars_are_set(self, monkeypatch):
        for var in _SMTP_ENV:
            monkeypatch.setenv(var, "set")

        from utils.notifications.email_service import is_email_configured

        assert is_email_configured() is True

    @pytest.mark.parametrize("missing", _SMTP_ENV)
    def test_false_when_any_required_var_is_missing(self, monkeypatch, missing):
        for var in _SMTP_ENV:
            monkeypatch.setenv(var, "set")
        monkeypatch.delenv(missing)

        from utils.notifications.email_service import is_email_configured

        assert is_email_configured() is False

    @pytest.mark.parametrize("missing", _SMTP_ENV)
    def test_false_when_a_required_var_is_empty(self, monkeypatch, missing):
        # .env.example ships these as empty strings, which is the common case.
        for var in _SMTP_ENV:
            monkeypatch.setenv(var, "set")
        monkeypatch.setenv(missing, "")

        from utils.notifications.email_service import is_email_configured

        assert is_email_configured() is False

    @pytest.mark.parametrize("missing", _SMTP_ENV)
    def test_agrees_with_whether_the_service_constructs(self, monkeypatch, missing):
        # The whole point is to predict get_email_service() without calling it,
        # so the two must not drift apart.
        for var in _SMTP_ENV:
            monkeypatch.setenv(var, "set")
        monkeypatch.delenv(missing)

        from utils.notifications.email_service import (
            EmailService,
            is_email_configured,
        )

        assert is_email_configured() is False
        with pytest.raises(ValueError):
            EmailService()


class TestPasswordResetAvailableEndpoint:
    def test_reports_true_when_smtp_is_configured(self, monkeypatch, reset_env):
        client, _cursor, _svc = reset_env
        monkeypatch.setattr(
            "utils.notifications.email_service.is_email_configured", lambda: True
        )
        resp = client.get("/api/auth/password-reset-available")
        assert resp.status_code == 200
        assert resp.get_json() == {"available": True}

    def test_reports_false_when_smtp_is_unconfigured(self, monkeypatch, reset_env):
        # Without this the sign-in page promises a code that can never arrive.
        client, _cursor, _svc = reset_env
        monkeypatch.setattr(
            "utils.notifications.email_service.is_email_configured", lambda: False
        )
        resp = client.get("/api/auth/password-reset-available")
        assert resp.status_code == 200
        assert resp.get_json() == {"available": False}

    def test_needs_no_authentication(self, monkeypatch, reset_env):
        # The caller is locked out by definition, so there's no session to send.
        client, _cursor, _svc = reset_env
        monkeypatch.setattr(
            "utils.notifications.email_service.is_email_configured", lambda: True
        )
        resp = client.get("/api/auth/password-reset-available")
        assert resp.status_code == 200

    def test_says_nothing_about_any_account(self, monkeypatch, reset_env):
        # Deployment-level fact only: it must stay independent of the users table,
        # or it becomes the enumeration oracle the POST route avoids being.
        client, cursor, _svc = reset_env
        monkeypatch.setattr(
            "utils.notifications.email_service.is_email_configured", lambda: False
        )
        before = len(cursor.executed)
        body = client.get("/api/auth/password-reset-available").get_json()
        assert set(body) == {"available"}
        assert len(cursor.executed) == before
