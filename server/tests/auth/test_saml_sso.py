"""Account resolution rules for SAML SSO (utils/auth/saml_sso.py).

The org a login lands in comes from the ACS URL whose certificate verified the
assertion; these tests pin what happens to the *user* once that's established.
"""

from __future__ import annotations

import psycopg2.errors
import pytest

from utils.auth import saml_sso as sso


class FakeCursor:
    """Just enough of a cursor for resolve_user / org_requires_sso / discovery."""

    def __init__(self, users=None, domains=None, configs=None):
        self.users = users or []        # dicts: id, email, org_id, sso_subject
        self.domains = domains or []    # dicts: org_id, domain, verified
        self.configs = configs or {}    # org_id -> dict(enabled, require_sso)
        self.race_winner = None
        self._result = []

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        if sql.startswith("SELECT 1 FROM org_sso_domains"):
            org_id, domain = params
            self._result = [(1,)] if any(
                d["org_id"] == org_id and d["domain"] == domain and d["verified"] for d in self.domains
            ) else []
        elif sql.startswith("SELECT id FROM users WHERE org_id"):
            org_id, subject = params
            self._result = [(u["id"],) for u in self.users if u["org_id"] == org_id and u["sso_subject"] == subject]
        elif sql.startswith("SELECT id, org_id, sso_subject FROM users"):
            (email,) = params
            self._result = [(u["id"], u["org_id"], u["sso_subject"]) for u in self.users if u["email"].lower() == email]
        elif sql.startswith("UPDATE users SET sso_subject"):
            subject, user_id = params
            user = next(u for u in self.users if u["id"] == user_id)
            user["sso_subject"] = subject
            if "must_change_password = FALSE" in sql:
                user["must_change_password"] = False
            self._result = []
        elif sql.startswith(("SAVEPOINT", "ROLLBACK TO SAVEPOINT")):
            self._result = []
        elif sql.startswith("INSERT INTO users"):
            email, _pw, _name, _role, org_id, subject = params
            # Simulates a concurrent login committing the same user first.
            if self.race_winner:
                self.users.append(self.race_winner)
                self.race_winner = None
                raise psycopg2.errors.UniqueViolation()
            new_id = f"uid-{len(self.users) + 1}"
            self.users.append({"id": new_id, "email": email, "org_id": org_id, "sso_subject": subject})
            self._result = [(new_id,)]
        elif sql.startswith("SELECT 1 FROM org_sso_configs c"):
            org_id, domain = params
            cfg = self.configs.get(org_id) or {}
            ok = cfg.get("enabled") and cfg.get("require_sso") and any(
                d["org_id"] == org_id and d["domain"] == domain and d["verified"] for d in self.domains
            )
            self._result = [(1,)] if ok else []
        elif sql.startswith("SELECT d.org_id FROM org_sso_domains d"):
            (domain,) = params
            self._result = [
                (d["org_id"],) for d in self.domains
                if d["domain"] == domain and d["verified"] and (self.configs.get(d["org_id"]) or {}).get("enabled")
            ]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


def _identity(subject="sub-1", email="first.last@example.com", name="First Last"):
    return {"subject": subject, "email": email, "name": name}


ACME_DOMAIN = {"org_id": "org-acme", "domain": "example.com", "verified": True}


class TestResolveUser:
    def test_first_login_provisions_into_org(self):
        cur = FakeCursor(domains=[ACME_DOMAIN])
        user_id, created = sso.resolve_user(cur, "org-acme", _identity(), "viewer")
        assert created is True
        assert cur.users == [{"id": user_id, "email": "first.last@example.com", "org_id": "org-acme", "sso_subject": "sub-1"}]

    def test_concurrent_first_login_resolves_to_winner(self):
        cur = FakeCursor(domains=[ACME_DOMAIN])
        cur.race_winner = {"id": "uid-tab1", "email": "first.last@example.com", "org_id": "org-acme", "sso_subject": "sub-1"}
        assert sso.resolve_user(cur, "org-acme", _identity(), "viewer") == ("uid-tab1", False)

    def test_returning_user_matched_by_subject_not_email(self):
        # The IdP renamed the user's address; the immutable subject still matches.
        cur = FakeCursor(
            domains=[ACME_DOMAIN],
            users=[{"id": "uid-a", "email": "old.name@example.com", "org_id": "org-acme", "sso_subject": "sub-1"}],
        )
        assert sso.resolve_user(cur, "org-acme", _identity(email="new.name@example.com"), "viewer") == ("uid-a", False)

    def test_existing_password_member_gets_linked(self):
        cur = FakeCursor(
            domains=[ACME_DOMAIN],
            users=[{"id": "uid-a", "email": "First.Last@example.com", "org_id": "org-acme",
                    "sso_subject": None, "must_change_password": True}],
        )
        assert sso.resolve_user(cur, "org-acme", _identity(), "viewer") == ("uid-a", False)
        assert cur.users[0]["sso_subject"] == "sub-1"
        # Invited with a temp password; SSO now owns sign-in.
        assert cur.users[0]["must_change_password"] is False

    def test_member_linked_to_other_subject_is_refused(self):
        cur = FakeCursor(
            domains=[ACME_DOMAIN],
            users=[{"id": "uid-a", "email": "first.last@example.com", "org_id": "org-acme", "sso_subject": "sub-OLD"}],
        )
        identity = _identity(subject="sub-NEW")
        with pytest.raises(sso.SsoError) as exc:
            sso.resolve_user(cur, "org-acme", identity, "viewer")
        assert exc.value.code == "identity_mismatch"

    def test_account_in_another_org_is_never_moved(self):
        cur = FakeCursor(
            domains=[ACME_DOMAIN],
            users=[{"id": "uid-b", "email": "first.last@example.com", "org_id": "org-other", "sso_subject": None}],
        )
        identity = _identity()
        with pytest.raises(sso.SsoError) as exc:
            sso.resolve_user(cur, "org-acme", identity, "viewer")
        assert exc.value.code == "account_in_other_org"
        assert cur.users[0]["org_id"] == "org-other"

    @pytest.mark.parametrize("domains", [
        [],                                                               # never claimed
        [{"org_id": "org-acme", "domain": "example.com", "verified": False}],  # claimed, unverified
        [{"org_id": "org-other", "domain": "example.com", "verified": True}],  # someone else's
    ])
    def test_unverified_domain_is_refused(self, domains):
        # Otherwise any org's IdP could assert any victim's address.
        cur = FakeCursor(domains=domains)
        identity = _identity()
        with pytest.raises(sso.SsoError) as exc:
            sso.resolve_user(cur, "org-acme", identity, "viewer")
        assert exc.value.code == "domain_not_allowed"
        assert cur.users == []

    @pytest.mark.parametrize("field,code", [("subject", "missing_subject"), ("email", "missing_email")])
    def test_missing_identity_fields(self, field, code):
        identity = _identity()
        identity[field] = ""
        cur = FakeCursor(domains=[ACME_DOMAIN])
        with pytest.raises(sso.SsoError) as exc:
            sso.resolve_user(cur, "org-acme", identity, "viewer")
        assert exc.value.code == code


class TestRequireSso:
    def test_blocks_only_enabled_required_verified_domain(self):
        cur = FakeCursor(domains=[ACME_DOMAIN], configs={"org-acme": {"enabled": True, "require_sso": True}})
        assert sso.org_requires_sso(cur, "org-acme", "First.Last@example.com") is True
        assert sso.org_requires_sso(cur, "org-acme", "contractor@example.org") is False
        assert sso.org_requires_sso(cur, None, "first.last@example.com") is False

    def test_not_required_when_toggle_off(self):
        cur = FakeCursor(domains=[ACME_DOMAIN], configs={"org-acme": {"enabled": True, "require_sso": False}})
        assert sso.org_requires_sso(cur, "org-acme", "first.last@example.com") is False


class TestDiscovery:
    def test_routes_by_verified_domain_of_enabled_org(self):
        cur = FakeCursor(domains=[ACME_DOMAIN], configs={"org-acme": {"enabled": True}})
        assert sso.find_org_for_email(cur, "First.Last@Example.com") == "org-acme"

    def test_disabled_or_unknown_domain_has_no_route(self):
        cur = FakeCursor(domains=[ACME_DOMAIN], configs={"org-acme": {"enabled": False}})
        assert sso.find_org_for_email(cur, "first.last@example.com") is None
        assert sso.find_org_for_email(cur, "not-an-email") is None


class TestPasswordLoginWhenSsoRequired:
    @pytest.fixture
    def login(self, monkeypatch):
        import sys
        from unittest.mock import MagicMock

        import bcrypt

        # auth_routes builds its rate limiter at import; memory:// avoids Redis.
        monkeypatch.setenv("REDIS_URL", "memory://")
        for heavy in ("celery_config", "celery", "routes.audit_routes"):
            sys.modules.setdefault(heavy, MagicMock())
        from flask import Flask
        from routes import auth_routes

        def _client(role, requires_sso):
            row = ("uid-1", "first.last@example.com", "First Last",
                   bcrypt.hashpw(b"pw-123456", bcrypt.gensalt()).decode(),
                   role, "org-acme", "acme", False, True, False)
            cur = MagicMock()
            cur.fetchall.return_value = [row]
            conn = MagicMock()
            conn.cursor.return_value.__enter__.return_value = cur
            monkeypatch.setattr(auth_routes, "connect_to_db_as_user", lambda: conn)
            monkeypatch.setattr(auth_routes, "record_audit_event", MagicMock())
            monkeypatch.setattr(auth_routes, "org_requires_sso", lambda *_a: requires_sso)
            app = Flask(__name__)  # NOSONAR
            app.register_blueprint(auth_routes.auth_bp)
            return app.test_client().post(
                "/api/auth/login", json={"email": "first.last@example.com", "password": "pw-123456"},
            )
        return _client

    def test_member_is_sent_to_sso(self, login):
        resp = login("viewer", True)
        assert resp.status_code == 403
        assert resp.get_json() == {"error": "sso_required"}

    def test_admin_keeps_break_glass_password_login(self, login):
        assert login("admin", True).status_code == 200

    def test_unaffected_when_not_required(self, login):
        assert login("viewer", False).status_code == 200


class TestSplitCerts:
    def test_bundle_and_junk(self):
        a = "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----"
        b = "-----BEGIN CERTIFICATE-----\nBBB\n-----END CERTIFICATE-----"
        assert sso.split_certs(f"junk\n{a}\n\n{b}\ntrailing") == [a, b]
        assert sso.split_certs("") == []

    def test_repeated_begin_markers_stay_linear(self):
        # The admin-supplied field must not trigger regex backtracking.
        hostile = "-----BEGIN CERTIFICATE-----" * 50_000
        assert sso.split_certs(hostile) == []


class TestDomainNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("Example.COM", "example.com"),
        ("  sub.example.com. ", "sub.example.com"),
        ("http://example.com", ""),
        ("example", ""),
        ("-bad.example.com", ""),
        ("10.0.0.1", ""),
        (None, ""),
    ])
    def test_normalize(self, raw, expected):
        assert sso.normalize_domain(raw) == expected
