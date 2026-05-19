from flask import Blueprint

bp = Blueprint('aws', __name__)

from . import aws_routes, auth, onboarding, securityhub_routes

bp.register_blueprint(aws_routes.aws_bp)
bp.register_blueprint(auth.auth_bp)
bp.register_blueprint(onboarding.onboarding_bp)
bp.register_blueprint(securityhub_routes.securityhub_bp, url_prefix='/securityhub')
