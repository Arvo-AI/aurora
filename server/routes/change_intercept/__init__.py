from flask import Blueprint

bp = Blueprint("change_intercept", __name__)

# Import routes so they register on the blueprint.
import routes.change_intercept.webhook  # noqa: F401, E402
import routes.change_intercept.github_install_events  # noqa: F401, E402
