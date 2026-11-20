from flask import Blueprint

detection_bp = Blueprint("detection", __name__)
detection_bp.strict_slashes = False

from . import detection_routes  # noqa: E402, F401
