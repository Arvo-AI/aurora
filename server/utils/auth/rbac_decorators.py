"""RBAC decorators for Flask route handlers with org (domain) support.

``@require_permission(resource, action)``
    Checks authentication **and** Casbin authorisation (domain-aware).
    Returns 401 if the request has no valid user, 403 if the user lacks
    the required permission in their org or if no org context is available.
    Injects ``user_id`` as the first positional argument of the wrapped
    function.

``@require_auth_only``
    Authentication-only check (no permission evaluation).  Useful for routes
    that every logged-in user may access.  Also injects ``user_id``.
"""

import logging
from functools import wraps

from flask import jsonify, request
from werkzeug.exceptions import HTTPException

from utils.auth.stateless_auth import get_user_id_from_request, get_org_id_from_request
from utils.auth.enforcer import get_enforcer, reload_policies
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)


def _audit_auth_failure(user_id, org_id, action, detail):
    """Best-effort audit log for auth/RBAC failures."""
    try:
        from routes.audit_routes import record_audit_event
        from flask import request as _req
        record_audit_event(org_id or "", user_id or "", action, "auth", None, detail, _req)
    except Exception:
        logger.debug("Could not record auth audit event", exc_info=True)


def require_permission(resource: str, action: str):
    """Decorator that enforces Casbin domain-based RBAC on a Flask route.

    OPTIONS (CORS preflight) requests are passed through without auth so
    that browser preflight checks succeed.

    Usage::

        @bp.route("/things", methods=["POST"])
        @require_permission("things", "write")
        def create_thing(user_id):
            ...
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if request.method == "OPTIONS":
                from utils.web.cors_utils import create_cors_response
                return create_cors_response()

            user_id = get_user_id_from_request()
            if not user_id:
                _audit_auth_failure(None, None, "auth_failed", {"endpoint": fn.__name__, "reason": "no_user_id"})
                return jsonify({"error": "Unauthorized"}), 401

            org_id = get_org_id_from_request()
            if not org_id:
                logger.warning(
                    "RBAC denied: no org context for user=%s endpoint=%s",
                    sanitize(user_id), fn.__name__,
                )
                _audit_auth_failure(user_id, None, "rbac_denied", {"endpoint": fn.__name__, "reason": "no_org_context"})
                return jsonify({"error": "Forbidden - no organization context"}), 403

            enforcer = get_enforcer()
            if not enforcer.enforce(user_id, org_id, resource, action):
                reload_policies()
                if not enforcer.enforce(user_id, org_id, resource, action):
                    logger.warning(
                        "RBAC denied: user=%s org=%s resource=%s action=%s endpoint=%s",
                        sanitize(user_id), sanitize(org_id), resource, action, fn.__name__,
                    )
                    _audit_auth_failure(user_id, org_id, "rbac_denied", {
                        "endpoint": fn.__name__, "resource": resource, "action": action,
                    })
                    return jsonify({"error": "Forbidden"}), 403

            try:
                return fn(user_id, *args, **kwargs)
            except HTTPException:
                raise
            except Exception as exc:
                logger.error("Unhandled error in %s: %s", fn.__name__, exc, exc_info=True)
                return jsonify({"error": "Internal server error"}), 500
        return wrapper
    return decorator


def require_auth_only(fn):
    """Decorator that checks authentication but skips permission checks.

    OPTIONS (CORS preflight) requests are passed through without auth.

    Usage::

        @bp.route("/profile")
        @require_auth_only
        def get_profile(user_id):
            ...
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if request.method == "OPTIONS":
            from utils.web.cors_utils import create_cors_response
            return create_cors_response()

        user_id = get_user_id_from_request()
        if not user_id:
            _audit_auth_failure(None, None, "auth_failed", {"endpoint": fn.__name__, "reason": "no_user_id"})
            return jsonify({"error": "Unauthorized"}), 401
        try:
            return fn(user_id, *args, **kwargs)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Unhandled error in %s: %s", fn.__name__, exc, exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
    return wrapper
