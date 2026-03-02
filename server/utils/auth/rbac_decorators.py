"""RBAC decorators for Flask route handlers.

``@require_permission(resource, action)``
    Checks authentication **and** Casbin authorisation.  Returns 401 if the
    request has no valid user, 403 if the user lacks the required permission.
    Injects ``user_id`` as the first positional argument of the wrapped
    function (same convention as the legacy ``@require_auth``).

``@require_auth_only``
    Authentication-only check (no permission evaluation).  Useful for routes
    that every logged-in user may access.  Also injects ``user_id``.
"""

import logging
from functools import wraps

from flask import jsonify

from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.enforcer import get_enforcer

logger = logging.getLogger(__name__)


def require_permission(resource: str, action: str):
    """Decorator that enforces Casbin RBAC on a Flask route.

    Usage::

        @bp.route("/things", methods=["POST"])
        @require_permission("things", "write")
        def create_thing(user_id):
            ...
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user_id = get_user_id_from_request()
            if not user_id:
                return jsonify({"error": "Unauthorized"}), 401

            enforcer = get_enforcer()
            if not enforcer.enforce(user_id, resource, action):
                logger.warning(
                    "RBAC denied: user=%s resource=%s action=%s endpoint=%s",
                    user_id, resource, action, fn.__name__,
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

    Usage::

        @bp.route("/profile")
        @require_auth_only
        def get_profile(user_id):
            ...
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401
        try:
            return fn(user_id, *args, **kwargs)
        except Exception as exc:
            logger.error("Unhandled error in %s: %s", fn.__name__, exc, exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
    return wrapper
