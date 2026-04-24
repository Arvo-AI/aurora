"""
AWS API Routes - Handles AWS-specific API endpoints
"""
import logging
from flask import Blueprint, jsonify
import flask
import os

aws_bp = Blueprint("aws_bp", __name__)

@aws_bp.route("/setup-script", methods=["GET"])
def aws_setup_script():
    """Legacy endpoint - redirects to role-based setup"""
    # Redirect to new role-based script
    return flask.redirect("/aws/setup-role", code=301)

@aws_bp.route("/aws/setup-role", methods=["GET"])
def aws_setup_role_script():
    """Serve the new role-based setup script"""
    try:
        script_path = os.path.join(os.path.dirname(__file__), "..", "..", "connectors", "aws_connector", "setup-aurora-role.sh")
        if os.path.exists(script_path):
            with open(script_path, 'r', encoding='utf-8') as f:
                content = f.read()
            resp = flask.Response(content, mimetype='text/plain')
            resp.headers['Content-Disposition'] = 'inline; filename=setup-aurora-role.sh'
            return resp
        return jsonify({"error": "Role setup script not found"}), 404
    except Exception as e:
        logging.error("Error serving AWS role setup script", exc_info=e)
        return jsonify({"error": "Failed to serve setup script"}), 500


@aws_bp.route("/setup-script-ps1", methods=["GET"])
def aws_setup_script_ps1():
    """Legacy endpoint - redirects to role-based setup"""
    # Redirect to new role-based script
    return flask.redirect("/aws/setup-role-ps1", code=301)

@aws_bp.route("/aws/setup-role-ps1", methods=["GET"])
def aws_setup_role_script_ps1():
    """Serve the new role-based PowerShell setup script"""
    try:
        script_path = os.path.join(os.path.dirname(__file__), "..", "..", "connectors", "aws_connector", "setup-aurora-role.ps1")
        if os.path.exists(script_path):
            with open(script_path, 'r', encoding='utf-8') as f:
                content = f.read()
            resp = flask.Response(content, mimetype='text/plain')
            resp.headers['Content-Disposition'] = 'inline; filename=setup-aurora-role.ps1'
            return resp
        return jsonify({"error": "PowerShell role setup script not found"}), 404
    except Exception as e:
        logging.error("Error serving AWS PS1 role setup script", exc_info=e)
        return jsonify({"error": "Failed to serve PowerShell setup script"}), 500