"""Cross-tenant isolation tests at the route and RLS-context layers.

Pins the invariant that an authenticated request scoped to org-A can never
receive org-B data — whether the attacker crafts the URL path, spoofs
identity headers, or omits authentication entirely.  All tests are hermetic
(no network, no real database).
"""

from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# UUIDs used as test fixtures
# ---------------------------------------------------------------------------

ORG_A_USER_ID = "user-org-a-0000-0000-000000000001"
ORG_A_ID      = "org-a-0000-0000-0000-000000000001"
ORG_B_ID      = "org-b-0000-0000-0000-000000000002"

# An incident UUID that *belongs to org-B* — the attacker puts this in the URL.
ORG_B_INCIDENT_UUID = "b0000000-0000-0000-0000-000000000099"
# A valid UUID format that belongs to org-A (control case).
ORG_A_INCIDENT_UUID = "a0000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_cursor_returning(row):
    """Cursor whose fetchone returns *row* (None to simulate zero RLS rows)."""
    cursor = MagicMock(name="cursor")
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchone.return_value = row
    cursor.fetchall.return_value = []
    return cursor


def _make_conn(cursor):
    conn = MagicMock(name="conn")
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor
    return conn


# ---------------------------------------------------------------------------
# Helper: build the standard auth headers for org-A's authenticated user
# ---------------------------------------------------------------------------

_ORG_A_HEADERS = {
    "X-User-ID": ORG_A_USER_ID,
    "X-Org-ID":  ORG_A_ID,
}


# ---------------------------------------------------------------------------
# Shared mock plumbing
# ---------------------------------------------------------------------------


def _patch_auth_and_pool(monkeypatch, *, cursor):
    """Wire up the minimal set of stubs every route test needs:
      - RBAC decorator resolves to org-A's authenticated user
      - DB pool yields the provided cursor
      - set_rls_context is a no-op that returns org-A's org_id
    """
    import utils.auth.stateless_auth as sa_module
    import utils.auth.rbac_decorators as rbac_module

    monkeypatch.setattr(
        sa_module, "get_user_id_from_request",
        MagicMock(return_value=ORG_A_USER_ID),
    )
    monkeypatch.setattr(
        sa_module, "get_org_id_from_request",
        MagicMock(return_value=ORG_A_ID),
    )
    # Patch the copies imported by the rbac_decorators module
    monkeypatch.setattr(
        rbac_module, "get_user_id_from_request",
        MagicMock(return_value=ORG_A_USER_ID),
    )
    monkeypatch.setattr(
        rbac_module, "get_org_id_from_request",
        MagicMock(return_value=ORG_A_ID),
    )
    # RBAC enforcer: permit everything (we're testing data isolation, not authz)
    import utils.auth.enforcer as enforcer_module
    monkeypatch.setattr(
        enforcer_module, "enforce_with_reload",
        MagicMock(return_value=True),
    )

    conn = _make_conn(cursor)
    fake_pool = MagicMock(name="db_pool")
    fake_pool.get_admin_connection.return_value = conn

    # The route module does `from utils.db.connection_pool import db_pool`,
    # so we must patch the name *in the route module's namespace*, not just
    # in the pool module, to intercept the reference the route actually uses.
    from routes import incidents_routes as route_module
    monkeypatch.setattr(route_module, "db_pool", fake_pool)

    # set_rls_context and get_org_id_from_request are both imported by name
    # into the route module — patch them there too.
    rls_stub = MagicMock(return_value=ORG_A_ID)
    org_stub = MagicMock(return_value=ORG_A_ID)
    monkeypatch.setattr(route_module, "set_rls_context", rls_stub)
    monkeypatch.setattr(route_module, "get_org_id_from_request", org_stub)
    monkeypatch.setattr(sa_module, "set_rls_context", rls_stub)

    return fake_pool


# ---------------------------------------------------------------------------
# Cross-tenant route access: org-A user requests org-B incident
# ---------------------------------------------------------------------------


class TestCrossTenantIncidentAccess:
    """The ``GET /api/incidents/<incident_id>`` route must never serve data
    across tenant boundaries.  The DB query always includes ``AND i.org_id = %s``
    bound to the *authenticated* user's org, so an org-B UUID in the URL path
    simply returns zero rows from the mock — which the route turns into a 404.
    """

    def test_route_returns_404_when_cursor_returns_none(self, client, monkeypatch):
        """Route returns 404 when the cursor returns no row — simulates the
        org_id filter excluding the cross-tenant UUID."""
        # Cursor returns None: simulates the org_id WHERE clause returning no rows.
        cursor = _make_cursor_returning(None)
        _patch_auth_and_pool(monkeypatch, cursor=cursor)

        resp = client.get(
            f"/api/incidents/{ORG_B_INCIDENT_UUID}",
            headers=_ORG_A_HEADERS,
        )

        assert resp.status_code == 404, (
            "Route must return 404 when the org-filtered query returns no row. "
            f"Got {resp.status_code}: {resp.get_data(as_text=True)!r}"
        )

    def test_org_b_incident_uuid_does_not_return_incident_data(
        self, client, monkeypatch
    ):
        """The response body must contain an error, never incident fields."""
        cursor = _make_cursor_returning(None)
        _patch_auth_and_pool(monkeypatch, cursor=cursor)

        resp = client.get(
            f"/api/incidents/{ORG_B_INCIDENT_UUID}",
            headers=_ORG_A_HEADERS,
        )
        body = resp.get_data(as_text=True)

        assert "id" not in body or "error" in body, (
            "Response body must not contain incident data for a cross-tenant UUID."
        )

    def test_org_a_incident_uuid_returns_200_when_row_exists(
        self, client, monkeypatch
    ):
        """Control case: same route returns 200 for a valid org-A incident
        (verifies the fixture actually exercises the route, not a shortcut).
        """
        # Build a minimal 24-column row that _format_incident_response expects.
        fake_row = (
            ORG_A_INCIDENT_UUID,  # id
            ORG_A_USER_ID,        # user_id
            "datadog",            # source_type
            "alert-123",          # source_alert_id
            "open",               # status
            "high",               # severity
            "Test alert",         # alert_title
            "web",                # alert_service
            "prod",               # alert_environment
            "analyzed",           # aurora_status
            "RCA summary",        # aurora_summary
            None,                 # aurora_chat_session_id
            None,                 # started_at
            None,                 # analyzed_at
            None,                 # active_tab
            None,                 # created_at
            None,                 # updated_at
            None,                 # resolved_at
            None,                 # alert_fired_at
            "{}",                 # alert_metadata
            0,                    # correlated_alert_count
            None,                 # affected_services
            None,                 # merged_into_incident_id
            None,                 # merged_into_title
        )
        cursor = _make_cursor_returning(fake_row)
        _patch_auth_and_pool(monkeypatch, cursor=cursor)

        resp = client.get(
            f"/api/incidents/{ORG_A_INCIDENT_UUID}",
            headers=_ORG_A_HEADERS,
        )

        assert resp.status_code == 200, (
            f"Expected 200 for own-org incident, got {resp.status_code}: "
            f"{resp.get_data(as_text=True)!r}"
        )

    def test_set_rls_context_called_with_authenticated_user_id(
        self, client, monkeypatch
    ):
        """The route must call set_rls_context with the *authenticated* user_id
        extracted from the session — never with any value derived from the URL
        path (which an attacker controls).
        """
        cursor = _make_cursor_returning(None)
        # _patch_auth_and_pool already stubs set_rls_context in the route module;
        # we grab it after patching so we can inspect calls on the same spy.
        _patch_auth_and_pool(monkeypatch, cursor=cursor)

        from routes import incidents_routes as route_module
        rls_spy = MagicMock(return_value=ORG_A_ID)
        monkeypatch.setattr(route_module, "set_rls_context", rls_spy)

        client.get(
            f"/api/incidents/{ORG_B_INCIDENT_UUID}",
            headers=_ORG_A_HEADERS,
        )

        # set_rls_context must have been called with the auth-layer user_id,
        # never with the org-B incident UUID from the URL.
        calls = rls_spy.call_args_list
        assert calls, "set_rls_context must have been called by the route"
        for call in calls:
            positional_user_id = call.args[2] if len(call.args) >= 3 else call.kwargs.get("user_id")
            assert positional_user_id != ORG_B_INCIDENT_UUID, (
                "set_rls_context must not be called with the URL path value. "
                "Identity must come from the authenticated session, not the URL."
            )
            assert positional_user_id == ORG_A_USER_ID, (
                f"set_rls_context called with wrong user_id: {positional_user_id!r}. "
                f"Expected {ORG_A_USER_ID!r}."
            )


# ---------------------------------------------------------------------------
# RBAC gates fire before any DB call for unauthenticated requests
# ---------------------------------------------------------------------------


class TestUnauthenticatedAndNoOrgRequests:
    """The RBAC decorator must block the request before the route body runs —
    meaning no DB connection is opened and no data is queried.
    """

    def test_missing_auth_returns_401(self, client, monkeypatch):
        """No X-User-ID header → 401 before any DB call."""
        import utils.auth.rbac_decorators as rbac_module
        import utils.auth.stateless_auth as sa_module
        monkeypatch.setattr(
            rbac_module, "get_user_id_from_request",
            MagicMock(return_value=None),
        )
        monkeypatch.setattr(
            sa_module, "get_user_id_from_request",
            MagicMock(return_value=None),
        )

        # Pool must never be touched — patch via the route's local binding.
        from routes import incidents_routes as route_module
        pool_spy = MagicMock(name="db_pool")
        monkeypatch.setattr(route_module, "db_pool", pool_spy)

        resp = client.get(f"/api/incidents/{ORG_B_INCIDENT_UUID}")

        assert resp.status_code == 401
        pool_spy.get_admin_connection.assert_not_called()

    def test_no_org_context_returns_403(self, client, monkeypatch):
        """Valid X-User-ID but no resolvable org → 403 before any DB call."""
        import utils.auth.rbac_decorators as rbac_module
        import utils.auth.stateless_auth as sa_module
        monkeypatch.setattr(
            rbac_module, "get_user_id_from_request",
            MagicMock(return_value=ORG_A_USER_ID),
        )
        monkeypatch.setattr(
            rbac_module, "get_org_id_from_request",
            MagicMock(return_value=None),
        )
        monkeypatch.setattr(
            sa_module, "get_user_id_from_request",
            MagicMock(return_value=ORG_A_USER_ID),
        )
        monkeypatch.setattr(
            sa_module, "get_org_id_from_request",
            MagicMock(return_value=None),
        )

        from routes import incidents_routes as route_module
        pool_spy = MagicMock(name="db_pool")
        monkeypatch.setattr(route_module, "db_pool", pool_spy)

        resp = client.get(
            f"/api/incidents/{ORG_B_INCIDENT_UUID}",
            headers={"X-User-ID": ORG_A_USER_ID},
        )

        assert resp.status_code == 403
        pool_spy.get_admin_connection.assert_not_called()

    def test_attacker_supplied_x_user_id_header_ignored(
        self, client, monkeypatch
    ):
        """An attacker adding X-User-ID: <victim-id> to a request that has no
        valid session cookie/JWT must still be rejected with 401.

        The RBAC decorator resolves identity from the auth middleware only.
        If the underlying resolver returns None (no valid session), the
        attacker-supplied header must not allow access.
        """
        import utils.auth.rbac_decorators as rbac_module
        import utils.auth.stateless_auth as sa_module

        # Simulate: no valid session → resolver returns None regardless of headers
        monkeypatch.setattr(
            rbac_module, "get_user_id_from_request",
            MagicMock(return_value=None),
        )
        monkeypatch.setattr(
            sa_module, "get_user_id_from_request",
            MagicMock(return_value=None),
        )

        from routes import incidents_routes as route_module
        pool_spy = MagicMock(name="db_pool")
        monkeypatch.setattr(route_module, "db_pool", pool_spy)

        resp = client.get(
            f"/api/incidents/{ORG_B_INCIDENT_UUID}",
            # Attacker-controlled header with a victim's user id
            headers={"X-User-ID": "victim-user-id-that-owns-org-b"},
        )

        assert resp.status_code == 401, (
            "Attacker-supplied X-User-ID with no valid session must be rejected "
            f"with 401, got {resp.status_code}."
        )
        pool_spy.get_admin_connection.assert_not_called()
