"""Fly.io API routes."""

from flask import Blueprint

flyio_bp = Blueprint('flyio', __name__)

from . import flyio_routes  # noqa: E402, F401
