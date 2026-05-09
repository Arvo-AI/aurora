from flask import Blueprint

actions_bp = Blueprint("actions", __name__)
actions_bp.strict_slashes = False

from . import actions_routes  # noqa: E402, F401
