"""RBAC decorators for Flask route handlers with org (domain) support.

``@require_permission(resource, action)``
    Checks authentication **and** Casbin authorisation (domain-aware).
    Returns 401 if the request has no valid user, 403 if the user lacks
    the required permission in their org.  Injects ``user_id`` as the
    first positional argument of the wrapped function.

``@require_auth_only``
    Authentication-only check (no permission evaluation).  Useful for routes
    that every logged-in user may access.  Also injects ``user_id``.
"""

import logging
from functools import wraps

from flask import jsonify, request

from utils.auth.stateless_auth import get_user_id_from_request, get_org_id_from_request
from utils.auth.enforcer import get_enforcer

logger = logging.getLogger(__name__)


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
                return fn(None, *args, **kwargs)

            user_id = get_user_id_from_request()
            if not user_id:
                return jsonify({"error": "Unauthorized"}), 401

            org_id = get_org_id_from_request()

            enforcer = get_enforcer()
            if org_id and not enforcer.enforce(user_id, org_id, resource, action):
                logger.warning(
                    "RBAC denied: user=%s org=%s resource=%s action=%s endpoint=%s",
                    user_id, org_id, resource, action, fn.__name__,
                )
                return jsonify({"error": "Forbidden"}), 403

            try:
                return fn(user_id, *args, **kwargs)
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
            return fn(None, *args, **kwargs)

        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401
        try:
            return fn(user_id, *args, **kwargs)
        except Exception as exc:
            logger.error("Unhandled error in %s: %s", fn.__name__, exc, exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
    return wrapper
