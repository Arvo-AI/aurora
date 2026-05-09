"""Verifies that X-User-ID / X-Org-ID headers cannot be spoofed to gain access.

Requests lacking a valid session must receive 401 regardless of header content.
Also documents that ``get_user_id_from_request`` / ``get_org_id_from_request``
are transparent header readers — identity validation is the RBAC decorator's
responsibility, not the functions themselves.
"""

import sys
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Shared helper: minimal throwaway Flask app for request-context tests
# ---------------------------------------------------------------------------


def _make_test_app():
    """Return a fresh Flask app suitable for test_request_context use."""
    sys.modules.pop("utils.auth.stateless_auth", None)
    from flask import Flask as _Flask  # fresh import after eviction
    return _Flask(__name__)


# ---------------------------------------------------------------------------
# Fixed identities used across tests
# ---------------------------------------------------------------------------

VICTIM_USER_ID  = "victim-0000-0000-0000-000000000001"
VICTIM_ORG_ID   = "victim-org-0000-0000-000000000001"
ATTACKER_VALUE  = "attacker-supplied-identity"


# ---------------------------------------------------------------------------
# Tests: RBAC decorator rejects spoofed identity headers
# ---------------------------------------------------------------------------


class TestSpoofedIdentityHeaderRejectedAtRoute:
    """An attacker who sets X-User-ID / X-Org-ID without a valid session must
    receive 401 — the RBAC decorator's ``get_user_id_from_request`` call must
    return None (simulating: no valid session) regardless of what the header says.
    """

    def _stub_no_session(self, monkeypatch):
        """Configure the auth resolver to return None — no valid session exists."""
        import utils.auth.rbac_decorators as rbac_module
        import utils.auth.stateless_auth as sa_module

        for mod in (rbac_module, sa_module):
            monkeypatch.setattr(mod, "get_user_id_from_request", MagicMock(return_value=None))
            monkeypatch.setattr(mod, "get_org_id_from_request", MagicMock(return_value=None))

        from routes import incidents_routes as route_module
        pool_spy = MagicMock(name="db_pool")
        monkeypatch.setattr(route_module, "db_pool", pool_spy)
        return pool_spy

    def test_spoofed_x_user_id_returns_401(self, client, monkeypatch):
        """Attacker sets X-User-ID to a victim's id with no real session → 401."""
        pool_spy = self._stub_no_session(monkeypatch)

        resp = client.get(
            "/api/incidents/some-incident-uuid",
            headers={"X-User-ID": VICTIM_USER_ID},
        )

        assert resp.status_code == 401, (
            f"Spoofed X-User-ID with no valid session must return 401, got {resp.status_code}."
        )
        pool_spy.get_admin_connection.assert_not_called()

    def test_spoofed_x_org_id_returns_401(self, client, monkeypatch):
        """Attacker sets both X-User-ID and X-Org-ID with no real session → 401."""
        pool_spy = self._stub_no_session(monkeypatch)

        resp = client.get(
            "/api/incidents/some-incident-uuid",
            headers={"X-User-ID": VICTIM_USER_ID, "X-Org-ID": VICTIM_ORG_ID},
        )

        assert resp.status_code == 401, (
            f"Spoofed X-User-ID + X-Org-ID with no valid session must return 401, "
            f"got {resp.status_code}."
        )
        pool_spy.get_admin_connection.assert_not_called()

    def test_arbitrary_header_value_does_not_resolve_identity(self, client, monkeypatch):
        """Arbitrary strings in X-User-ID (not a real user) with no session → 401."""
        pool_spy = self._stub_no_session(monkeypatch)

        resp = client.get(
            "/api/incidents/some-incident-uuid",
            headers={"X-User-ID": ATTACKER_VALUE},
        )

        assert resp.status_code == 401, (
            f"Arbitrary X-User-ID value with no session must return 401, got {resp.status_code}."
        )
        pool_spy.get_admin_connection.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: get_user_id_from_request returns what is in the header
# (documents that the trust boundary is the Next.js proxy, not this function)
# ---------------------------------------------------------------------------


class TestGetUserIdFromRequestHeaderBehaviour:
    """``get_user_id_from_request`` reads the X-User-ID header as-is.

    This is intentional: the security boundary is the Next.js auth middleware
    that sets the header — not this function.  These tests document that
    the function does not add a second layer of validation (so we don't
    accidentally strip legitimate IDs), and also that it returns None when
    the header is absent (so callers that do their own validation work correctly).
    """

    def _call_within_app_ctx(self, headers: dict):
        """Run ``get_user_id_from_request`` inside a minimal Flask request context.

        Evicts and re-imports ``utils.auth.stateless_auth`` each time so that
        Flask's ``request`` proxy inside the module is bound to the fresh
        app created here, not to a stale app from a previous test file.
        """
        tmp_app = _make_test_app()
        with tmp_app.test_request_context("/", headers=headers):
            import utils.auth.stateless_auth as sa_module_fresh
            return sa_module_fresh.get_user_id_from_request()

    def test_returns_none_when_header_absent(self):
        """No X-User-ID header → None (callers treat this as unauthenticated)."""
        result = self._call_within_app_ctx(headers={})
        assert result is None

    def test_returns_none_for_empty_header_value(self):
        """Empty X-User-ID header → None (equivalent to absent)."""
        tmp_app = _make_test_app()
        with tmp_app.test_request_context("/", headers={"X-User-ID": ""}):
            import utils.auth.stateless_auth as sa_module_fresh
            result = sa_module_fresh.get_user_id_from_request()
        assert result is None

    def test_returns_header_value_when_present(self):
        """When the header is present, returns its exact value.

        Trust validation is the caller's responsibility (RBAC decorator,
        Next.js proxy).  This function is intentionally transparent.
        """
        result = self._call_within_app_ctx(headers={"X-User-ID": VICTIM_USER_ID})
        assert result == VICTIM_USER_ID

    def test_does_not_resolve_identity_from_attacker_string(self):
        """An attacker-style string in the header is returned verbatim, not
        resolved to a real user.  The caller (RBAC decorator + auth middleware)
        is responsible for deciding whether the value is trustworthy.
        """
        result = self._call_within_app_ctx(headers={"X-User-ID": ATTACKER_VALUE})
        assert result == ATTACKER_VALUE


# ---------------------------------------------------------------------------
# Tests: get_org_id_from_request header behaviour
# ---------------------------------------------------------------------------


class TestGetOrgIdFromRequestHeaderBehaviour:
    """``get_org_id_from_request`` reads X-Org-ID and falls back to a DB lookup.

    These tests pin the fail-safe: when neither the header nor the DB
    provides an org, the function returns None so the RBAC decorator can
    issue a 403.
    """

    def _fresh_app_and_module(self):
        """Return a minimal Flask app for test_request_context use.

        Evicts the stale module so the ``request`` proxy inside it is rebound
        to the new Flask app's context, not a dead one from a prior test file.
        """
        return _make_test_app()

    def test_returns_org_from_header_when_present(self):
        """X-Org-ID header is trusted and returned directly."""
        tmp_app = self._fresh_app_and_module()
        with tmp_app.test_request_context(
            "/", headers={"X-User-ID": VICTIM_USER_ID, "X-Org-ID": VICTIM_ORG_ID}
        ):
            import utils.auth.stateless_auth as sa_module_fresh
            result = sa_module_fresh.get_org_id_from_request()
        assert result == VICTIM_ORG_ID

    def test_returns_none_when_no_header_and_no_db(self, monkeypatch):
        """No X-Org-ID and no resolvable DB org → None.

        Pins the fail-closed path so a missing org never silently becomes
        an empty string that passes a truthy check.
        """
        tmp_app = self._fresh_app_and_module()
        with tmp_app.test_request_context("/", headers={"X-User-ID": VICTIM_USER_ID}):
            import utils.auth.stateless_auth as sa_module_fresh

            fake_pool = MagicMock(name="db_pool")
            fake_conn = MagicMock()
            fake_cursor = MagicMock()
            fake_cursor.fetchone.return_value = None
            fake_conn.__enter__ = MagicMock(return_value=fake_conn)
            fake_conn.__exit__ = MagicMock(return_value=False)
            fake_conn.cursor.return_value.__enter__ = MagicMock(return_value=fake_cursor)
            fake_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            fake_pool.get_admin_connection.return_value = fake_conn

            import utils.db.connection_pool as pool_module
            monkeypatch.setattr(pool_module, "db_pool", fake_pool)

            result = sa_module_fresh.get_org_id_from_request()

        assert result is None, (
            "When neither header nor DB provides an org_id, "
            f"get_org_id_from_request must return None. Got: {result!r}"
        )


# ---------------------------------------------------------------------------
# Tests: RBAC decorator uses its own resolver, not raw header values
# ---------------------------------------------------------------------------


class TestRBACDecoratorIdentitySource:
    """The RBAC decorator must obtain ``user_id`` from ``get_user_id_from_request``,
    not from ``request.headers`` directly.  Patching the function to return None
    must cause a 401 even when attacker headers are present.
    """

    def test_rbac_uses_resolver_not_raw_headers(self, client, monkeypatch):
        """Confirm the decorator calls ``get_user_id_from_request`` and honours
        its return value, never bypassing it by reading the header a second time.
        """
        import utils.auth.rbac_decorators as rbac_module
        import utils.auth.stateless_auth as sa_module

        resolver_spy = MagicMock(return_value=None)
        monkeypatch.setattr(rbac_module, "get_user_id_from_request", resolver_spy)
        monkeypatch.setattr(sa_module, "get_user_id_from_request", resolver_spy)

        from routes import incidents_routes as route_module
        monkeypatch.setattr(route_module, "db_pool", MagicMock(name="db_pool"))

        resp = client.get(
            "/api/incidents/some-incident-uuid",
            # Attacker provides a victim header, but the resolver is returning None
            headers={"X-User-ID": VICTIM_USER_ID},
        )

        resolver_spy.assert_called()
        assert resp.status_code == 401, (
            "Decorator must honour the resolver's None and return 401, "
            f"not the raw header. Got {resp.status_code}."
        )
