from flask import Blueprint

bp = Blueprint('aws', __name__)

from . import aws_routes, auth, onboarding
from .cloudwatch_routes import cloudwatch_bp
from . import cloudwatch_tasks  # noqa: F401 — registers Celery task

bp.register_blueprint(aws_routes.aws_bp)
bp.register_blueprint(auth.auth_bp)
bp.register_blueprint(onboarding.onboarding_bp)
bp.register_blueprint(cloudwatch_bp)
