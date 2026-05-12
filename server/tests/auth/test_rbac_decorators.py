"""Tests the ``@require_permission`` and ``@require_auth_only`` decorators
that every authenticated Aurora route flows through. Pins the
fail-closed paths -- missing user_id, unresolved org, or a Casbin
"deny" verdict must all return 401/403 instead of leaking through (any
of those would be a cross-tenant privilege escalation) -- and the
deliberate fail-open path for ``OPTIONS`` preflight requests, which
must skip the auth check or browser CORS would break the entire
frontend while the backend looks healthy.

Also covers escalation-guard scenarios: bogus resource/action strings
passed to the decorator, a misbehaving outer decorator that injects
``user_id=None`` as a positional arg, and OPTIONS requests carrying
a body that the wrapped function would otherwise act on.
"""

import sys
from unittest.mock import MagicMock

import pytest

from flask import Flask, jsonify
from werkzeug.exceptions import NotFound

from utils.auth import rbac_decorators as rbac_module
from utils.auth.rbac_decorators import (
    require_auth_only,
    require_permission,
)


# ---------------------------------------------------------------------------
# Module-scoped stub for ``routes.audit_routes`` -- the decorator lazily
# imports this module in ``_audit_auth_failure``. Using a fixture with
# monkeypatch ensures the real module (if loaded elsewhere) is restored
# after this test module finishes.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True, scope="module")
def _stub_audit_routes():
    audit_stub = MagicMock(name="routes.audit_routes")
    audit_stub.record_audit_event = MagicMock(name="record_audit_event")

    orig_routes = sys.modules.get("routes")
    orig_audit = sys.modules.get("routes.audit_routes")

    routes_pkg = sys.modules.setdefault("routes", MagicMock(name="routes"))
    routes_pkg.audit_routes = audit_stub
    sys.modules["routes.audit_routes"] = audit_stub

    yield audit_stub

    if orig_routes is None:
        sys.modules.pop("routes", None)
    else:
        sys.modules["routes"] = orig_routes

    if orig_audit is None:
        sys.modules.pop("routes.audit_routes", None)
    else:
        sys.modules["routes.audit_routes"] = orig_audit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def flask_app():
    return Flask(__name__)


@pytest.fixture()
def patch_auth(monkeypatch):
    """Stub the decorator's external dependencies; ``_audit_auth_failure``
    is silenced so the auth path is the only thing under test."""
    user_id_lookup = MagicMock(return_value="u-1")
    org_id_lookup = MagicMock(return_value="org-7")
    enforce = MagicMock(return_value=True)
    audit = MagicMock(return_value=None)

    monkeypatch.setattr(rbac_module, "get_user_id_from_request", user_id_lookup)
    monkeypatch.setattr(rbac_module, "get_org_id_from_request", org_id_lookup)
    monkeypatch.setattr(rbac_module, "enforce_with_reload", enforce)
    monkeypatch.setattr(rbac_module, "_audit_auth_failure", audit)

    return MagicMock(
        get_user_id_from_request=user_id_lookup,
        get_org_id_from_request=org_id_lookup,
        enforce_with_reload=enforce,
        audit=audit,
    )


def _build_view():
    observed: dict = {}

    @require_permission("incidents", "read")
    def view(user_id):
        observed["user_id"] = user_id
        observed["called"] = True
        return jsonify(ok=True), 200

    return view, observed


# ---------------------------------------------------------------------------
# OPTIONS preflight -- bypasses auth so browsers can negotiate CORS
# ---------------------------------------------------------------------------


class TestOptionsPreflight:
    """OPTIONS must short-circuit to a CORS response without auth checks."""

    def test_options_returns_cors_response_without_auth(
        self, flask_app, patch_auth, monkeypatch,
    ):
        cors = MagicMock(return_value=("cors", 200))
        monkeypatch.setattr("utils.web.cors_utils.create_cors_response", cors)
        view, observed = _build_view()

        with flask_app.test_request_context("/api/x", method="OPTIONS"):
            response = view()

        assert response == ("cors", 200)
        cors.assert_called_once()
        patch_auth.get_user_id_from_request.assert_not_called()
        patch_auth.get_org_id_from_request.assert_not_called()
        patch_auth.enforce_with_reload.assert_not_called()
        assert "called" not in observed


# ---------------------------------------------------------------------------
# 401: no user identity on the request
# ---------------------------------------------------------------------------


class TestMissingUserId:
    """No user_id -> 401, regardless of any other state."""

    def test_returns_401_when_no_user_id(self, flask_app, patch_auth):
        patch_auth.get_user_id_from_request.return_value = None
        view, observed = _build_view()

        with flask_app.test_request_context("/api/x"):
            response, status = view()

        assert status == 401
        assert response.get_json() == {"error": "Unauthorized"}
        assert "called" not in observed

    def test_does_not_check_org_or_enforce_when_user_missing(
        self, flask_app, patch_auth,
    ):
        """Auth must short-circuit -- no org lookup, no Casbin call."""
        patch_auth.get_user_id_from_request.return_value = None
        view, _ = _build_view()

        with flask_app.test_request_context("/api/x"):
            view()

        patch_auth.get_org_id_from_request.assert_not_called()
        patch_auth.enforce_with_reload.assert_not_called()

    def test_audit_event_recorded_for_missing_user(self, flask_app, patch_auth):
        patch_auth.get_user_id_from_request.return_value = None
        view, _ = _build_view()

        with flask_app.test_request_context("/api/x"):
            view()

        patch_auth.audit.assert_called_once()
        args = patch_auth.audit.call_args.args
        assert args[0] is None
        assert args[1] is None
        assert args[2] == "auth_failed"
        assert args[3]["reason"] == "no_user_id"


# ---------------------------------------------------------------------------
# 403: user authenticated but no org context
# ---------------------------------------------------------------------------


class TestMissingOrgId:
    """User without org -> 403; never call the enforcer (no domain to scope)."""

    def test_returns_403_when_no_org_id(self, flask_app, patch_auth):
        patch_auth.get_org_id_from_request.return_value = None
        view, observed = _build_view()

        with flask_app.test_request_context("/api/x"):
            response, status = view()

        assert status == 403
        assert response.get_json() == {
            "error": "Forbidden - no organization context",
        }
        assert "called" not in observed

    def test_does_not_invoke_enforcer_without_org(self, flask_app, patch_auth):
        patch_auth.get_org_id_from_request.return_value = None
        view, _ = _build_view()

        with flask_app.test_request_context("/api/x"):
            view()

        patch_auth.enforce_with_reload.assert_not_called()

    def test_audit_event_recorded_with_user_and_no_org_reason(
        self, flask_app, patch_auth,
    ):
        patch_auth.get_org_id_from_request.return_value = None
        view, _ = _build_view()

        with flask_app.test_request_context("/api/x"):
            view()

        patch_auth.audit.assert_called_once()
        args = patch_auth.audit.call_args.args
        assert args[0] == "u-1"
        assert args[1] is None
        assert args[2] == "rbac_denied"
        assert args[3]["reason"] == "no_org_context"


# ---------------------------------------------------------------------------
# 403: enforcer denies
# ---------------------------------------------------------------------------


class TestEnforcerDenies:
    """Casbin says no -> 403; wrapped function never runs."""

    def test_returns_403_when_enforcer_denies(self, flask_app, patch_auth):
        patch_auth.enforce_with_reload.return_value = False
        view, observed = _build_view()

        with flask_app.test_request_context("/api/x"):
            response, status = view()

        assert status == 403
        assert response.get_json() == {"error": "Forbidden"}
        assert "called" not in observed

    def test_audit_event_records_resource_and_action(self, flask_app, patch_auth):
        patch_auth.enforce_with_reload.return_value = False
        view, _ = _build_view()

        with flask_app.test_request_context("/api/x"):
            view()

        patch_auth.audit.assert_called_once()
        args = patch_auth.audit.call_args.args
        assert args[0] == "u-1"
        assert args[1] == "org-7"
        assert args[2] == "rbac_denied"
        assert args[3]["resource"] == "incidents"
        assert args[3]["action"] == "read"


# ---------------------------------------------------------------------------
# Happy path: enforcer allows -> wrapped fn called with user_id injected
# ---------------------------------------------------------------------------


class TestEnforcerAllows:
    """Allowed: wrapped fn runs, ``user_id`` injected as first arg."""

    def test_wrapped_fn_called_when_enforcer_allows(self, flask_app, patch_auth):
        view, observed = _build_view()

        with flask_app.test_request_context("/api/x"):
            response, status = view()

        assert status == 200
        assert response.get_json() == {"ok": True}
        assert observed == {"user_id": "u-1", "called": True}

    def test_enforcer_called_with_user_org_resource_action(
        self, flask_app, patch_auth,
    ):
        """``(user, org, resource, action)`` must be passed through verbatim."""
        view, _ = _build_view()

        with flask_app.test_request_context("/api/x"):
            view()

        patch_auth.enforce_with_reload.assert_called_once_with(
            "u-1", "org-7", "incidents", "read",
        )

    def test_user_id_is_first_positional_arg(self, flask_app, patch_auth):
        """``user_id`` is inserted ahead of any caller args/kwargs."""
        seen: dict = {}

        @require_permission("incidents", "read")
        def handler(user_id, *args, **kwargs):
            seen["user_id"] = user_id
            seen["args"] = args
            seen["kwargs"] = kwargs
            return jsonify(ok=True), 200

        with flask_app.test_request_context("/api/x"):
            handler("path-arg", extra="kw")

        assert seen["user_id"] == "u-1"
        assert seen["args"] == ("path-arg",)
        assert seen["kwargs"] == {"extra": "kw"}

    def test_no_audit_event_on_success(self, flask_app, patch_auth):
        view, _ = _build_view()

        with flask_app.test_request_context("/api/x"):
            view()

        patch_auth.audit.assert_not_called()


# ---------------------------------------------------------------------------
# Enforcer errors: exception in enforce_with_reload must fail closed (500)
# ---------------------------------------------------------------------------


class TestEnforcerErrors:
    """enforce_with_reload raising must fail closed (500), never fail open."""

    def test_enforcer_exception_returns_500(self, flask_app, patch_auth):
        """An unexpected Casbin error is NOT a pass — it must be 500, not 200."""
        patch_auth.enforce_with_reload.side_effect = RuntimeError("casbin exploded")
        view, observed = _build_view()

        with flask_app.test_request_context("/api/x"):
            response, status = view()

        assert status == 500
        assert response.get_json() == {"error": "Internal server error"}
        assert "called" not in observed

    def test_enforcer_exception_does_not_leak_200(self, flask_app, patch_auth):
        """Double-check the fail-open case: status must not be 200."""
        patch_auth.enforce_with_reload.side_effect = Exception("enforcer down")
        view, _ = _build_view()

        with flask_app.test_request_context("/api/x"):
            _, status = view()

        assert status != 200


# ---------------------------------------------------------------------------
# Wrapped fn errors: HTTPException propagates, everything else -> 500
# ---------------------------------------------------------------------------


class TestWrappedFunctionErrors:
    """Exceptions in the wrapped fn are sanitised; HTTPException is preserved."""

    def test_unhandled_exception_returns_500(self, flask_app, patch_auth):
        @require_permission("incidents", "read")
        def boom(user_id):
            raise RuntimeError("unexpected")

        with flask_app.test_request_context("/api/x"):
            response, status = boom()

        assert status == 500
        assert response.get_json() == {"error": "Internal server error"}

    def test_exception_message_does_not_appear_in_500_body(self, flask_app, patch_auth):
        """The 500 body must be the canned string, never ``repr(exc)``.

        Downstream libraries (psycopg2, requests) can embed passwords or tokens
        in exception text; that must not reach the HTTP response.
        """
        @require_permission("incidents", "read")
        def leaky(user_id):
            raise RuntimeError("password=db_password_DO_NOT_LEAK_XYZ wrong host")

        with flask_app.test_request_context("/api/x"):
            response, status = leaky()

        assert status == 500
        assert "db_password_DO_NOT_LEAK_XYZ" not in response.get_data(as_text=True)

    def test_httpexception_is_reraised(self, flask_app, patch_auth):
        """Flask error handlers rely on HTTPException reaching them unwrapped."""

        @require_permission("incidents", "read")
        def missing(user_id):
            raise NotFound("nope")

        with flask_app.test_request_context("/api/x"):
            with pytest.raises(NotFound):
                missing()


# ---------------------------------------------------------------------------
# Audit logging is best-effort and must never block the auth response
# ---------------------------------------------------------------------------


class TestAuditFailureIsNonFatal:
    """A broken audit pipeline must not turn a 401 into a 500."""

    def test_audit_exception_does_not_break_401(
        self, flask_app, monkeypatch, _stub_audit_routes,
    ):
        monkeypatch.setattr(
            rbac_module,
            "get_user_id_from_request",
            MagicMock(return_value=None),
        )
        boom = MagicMock(side_effect=RuntimeError("audit pipe down"))
        monkeypatch.setattr(_stub_audit_routes, "record_audit_event", boom)
        view, _ = _build_view()

        with flask_app.test_request_context("/api/x"):
            response, status = view()

        assert status == 401
        assert response.get_json() == {"error": "Unauthorized"}


# ---------------------------------------------------------------------------
# require_auth_only -- same shape, but never calls the enforcer
# ---------------------------------------------------------------------------


class TestRequireAuthOnly:
    """``require_auth_only`` shares the auth path; never calls the enforcer."""

    def test_calls_wrapped_fn_when_user_id_present(self, flask_app, patch_auth):
        seen: dict = {}

        @require_auth_only
        def me(user_id):
            seen["user_id"] = user_id
            return jsonify(ok=True), 200

        with flask_app.test_request_context("/api/x"):
            _, status = me()

        assert status == 200
        assert seen == {"user_id": "u-1"}
        patch_auth.enforce_with_reload.assert_not_called()
        patch_auth.get_org_id_from_request.assert_not_called()

    def test_returns_401_without_user_id(self, flask_app, patch_auth):
        patch_auth.get_user_id_from_request.return_value = None

        @require_auth_only
        def me(user_id):
            return jsonify(ok=True), 200

        with flask_app.test_request_context("/api/x"):
            response, status = me()

        assert status == 401
        assert response.get_json() == {"error": "Unauthorized"}

    def test_options_bypasses_auth(self, flask_app, patch_auth, monkeypatch):
        cors = MagicMock(return_value=("cors", 200))
        monkeypatch.setattr("utils.web.cors_utils.create_cors_response", cors)

        @require_auth_only
        def me(user_id):
            return jsonify(ok=True), 200

        with flask_app.test_request_context("/api/x", method="OPTIONS"):
            response = me()

        assert response == ("cors", 200)
        cors.assert_called_once()
        patch_auth.get_user_id_from_request.assert_not_called()

    def test_unhandled_exception_returns_500(self, flask_app, patch_auth):
        @require_auth_only
        def boom(user_id):
            raise RuntimeError("kaboom")

        with flask_app.test_request_context("/api/x"):
            response, status = boom()

        assert status == 500
        assert response.get_json() == {"error": "Internal server error"}

    def test_httpexception_propagates(self, flask_app, patch_auth):
        @require_auth_only
        def missing(user_id):
            raise NotFound("nope")

        with flask_app.test_request_context("/api/x"):
            with pytest.raises(NotFound):
                missing()


# ---------------------------------------------------------------------------
# Escalation guards: decorator misuse must not weaken the auth gate
# ---------------------------------------------------------------------------


class TestRBACEscalationGuards:
    """@require_permission cannot be silently weakened by misuse at the
    call site.  Empty / None resource+action strings must still 401 on
    missing identity; an outer decorator that corrupts the user_id
    positional arg must not bypass the wrapper's own identity resolution.
    """

    @pytest.mark.parametrize("resource,action", [
        ("", ""),
        (None, None),
        ("", "read"),
        ("incidents", ""),
    ])
    def test_empty_or_none_resource_action_still_401_without_user(
        self, flask_app, patch_auth, monkeypatch, resource, action,
    ):
        """Bogus resource/action strings must not create a permissive fast-path.

        The wrapper resolves identity before it ever reaches the enforcer, so
        an empty or None permission spec is irrelevant -- a missing user_id
        must still 401.
        """
        patch_auth.get_user_id_from_request.return_value = None
        observed: dict = {}

        @require_permission(resource, action)
        def view(user_id):
            observed["called"] = True
            return jsonify(ok=True), 200

        with flask_app.test_request_context("/api/x"):
            response, status = view()

        assert status == 401
        assert response.get_json() == {"error": "Unauthorized"}
        assert "called" not in observed

    @pytest.mark.parametrize("resource,action", [
        ("", ""),
        (None, None),
    ])
    def test_empty_or_none_resource_action_enforcer_not_called_without_user(
        self, flask_app, patch_auth, monkeypatch, resource, action,
    ):
        """Identity check must short-circuit before the enforcer is invoked."""
        patch_auth.get_user_id_from_request.return_value = None

        @require_permission(resource, action)
        def view(user_id):
            return jsonify(ok=True), 200

        with flask_app.test_request_context("/api/x"):
            view()

        patch_auth.enforce_with_reload.assert_not_called()

    def test_outer_decorator_injecting_none_user_id_still_401s(
        self, flask_app, patch_auth,
    ):
        """An outer decorator that passes user_id=None positionally must not
        slip through the wrapper's own identity resolution.

        The wrapper calls get_user_id_from_request() independently; it never
        trusts a user_id that arrives via *args or **kwargs from the caller.
        """
        patch_auth.get_user_id_from_request.return_value = None
        observed: dict = {}

        def poisoning_outer(fn):
            """Simulates a misbehaving decorator that pre-populates user_id=None."""
            from functools import wraps

            @wraps(fn)
            def wrapper(*args, **kwargs):
                return fn(None, *args, **kwargs)
            return wrapper

        @poisoning_outer
        @require_permission("incidents", "read")
        def view(user_id):
            observed["user_id"] = user_id
            observed["called"] = True
            return jsonify(ok=True), 200

        with flask_app.test_request_context("/api/x"):
            response, status = view()

        assert status == 401
        assert response.get_json() == {"error": "Unauthorized"}
        assert "called" not in observed

    def test_outer_decorator_injecting_victim_user_id_does_not_bypass(
        self, flask_app, patch_auth,
    ):
        """An outer decorator injecting a real-looking user_id into *args must
        not satisfy the wrapper's auth check -- it always resolves identity
        from the request, not from caller-supplied arguments.
        """
        patch_auth.get_user_id_from_request.return_value = None
        observed: dict = {}

        def impersonating_outer(fn):
            from functools import wraps

            @wraps(fn)
            def wrapper(*args, **kwargs):
                return fn("victim-user-id", *args, **kwargs)
            return wrapper

        @impersonating_outer
        @require_permission("incidents", "read")
        def view(user_id):
            observed["user_id"] = user_id
            observed["called"] = True
            return jsonify(ok=True), 200

        with flask_app.test_request_context("/api/x"):
            _, status = view()

        assert status == 401
        assert "called" not in observed

