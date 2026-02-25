"""
Bitbucket Cloud browsing routes.
Provides endpoints for listing workspaces, projects, repos, branches, PRs, and issues.
"""
import logging

from flask import Blueprint, request, jsonify

from utils.auth.stateless_auth import get_user_id_from_request, get_credentials_from_db
from utils.web.cors_utils import create_cors_response

bitbucket_browsing_bp = Blueprint("bitbucket_browsing", __name__)
logger = logging.getLogger(__name__)


def _get_bb_client(user_id):
    """
    Build a BitbucketAPIClient for the given user, auto-refreshing OAuth tokens.

    Returns:
        A ``BitbucketAPIClient`` instance, or ``None`` if credentials are missing.
    """
    bb_creds = get_credentials_from_db(user_id, "bitbucket")
    if not bb_creds or not bb_creds.get("access_token"):
        return None

    auth_type = bb_creds.get("auth_type", "oauth")

    # Auto-refresh OAuth tokens
    if auth_type == "oauth":
        from connectors.bitbucket_connector.oauth_utils import refresh_token_if_needed

        old_access_token = bb_creds.get("access_token")
        bb_creds = refresh_token_if_needed(bb_creds)

        if bb_creds.get("access_token") != old_access_token:
            try:
                from utils.auth.token_management import store_tokens_in_db
                store_tokens_in_db(user_id, bb_creds, "bitbucket")
            except Exception as e:
                logger.warning(f"Failed to persist refreshed Bitbucket token: {e}")

    from connectors.bitbucket_connector.api_client import BitbucketAPIClient

    return BitbucketAPIClient(
        access_token=bb_creds["access_token"],
        auth_type=auth_type,
        email=bb_creds.get("email"),
    )


@bitbucket_browsing_bp.route("/workspaces", methods=["GET", "OPTIONS"])
def list_workspaces():
    """List Bitbucket workspaces for the authenticated user."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "User ID required"}), 400

        client = _get_bb_client(user_id)
        if not client:
            return jsonify({"error": "Bitbucket not connected"}), 401

        workspaces = client.get_workspaces()
        return jsonify({"workspaces": workspaces})

    except Exception as e:
        logger.error(f"Error listing Bitbucket workspaces: {e}", exc_info=True)
        return jsonify({"error": "Failed to list workspaces"}), 500


@bitbucket_browsing_bp.route("/projects/<workspace>", methods=["GET", "OPTIONS"])
def list_projects(workspace):
    """List projects in a Bitbucket workspace."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "User ID required"}), 400

        client = _get_bb_client(user_id)
        if not client:
            return jsonify({"error": "Bitbucket not connected"}), 401

        projects = client.get_projects(workspace)
        return jsonify({"projects": projects})

    except Exception as e:
        logger.error(f"Error listing Bitbucket projects: {e}", exc_info=True)
        return jsonify({"error": "Failed to list projects"}), 500


@bitbucket_browsing_bp.route("/repos/<workspace>", methods=["GET", "OPTIONS"])
def list_repos(workspace):
    """List repositories in a Bitbucket workspace, optionally filtered by project."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "User ID required"}), 400

        client = _get_bb_client(user_id)
        if not client:
            return jsonify({"error": "Bitbucket not connected"}), 401

        repos = client.get_repositories(workspace)

        # Optional project filter
        project_key = request.args.get("project")
        if project_key:
            repos = [
                r for r in repos
                if r.get("project", {}).get("key") == project_key
            ]

        return jsonify({"repositories": repos})

    except Exception as e:
        logger.error(f"Error listing Bitbucket repos: {e}", exc_info=True)
        return jsonify({"error": "Failed to list repositories"}), 500


@bitbucket_browsing_bp.route("/branches/<workspace>/<repo_slug>", methods=["GET", "OPTIONS"])
def list_branches(workspace, repo_slug):
    """List branches for a Bitbucket repository."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "User ID required"}), 400

        client = _get_bb_client(user_id)
        if not client:
            return jsonify({"error": "Bitbucket not connected"}), 401

        branches = client.get_branches(workspace, repo_slug)
        return jsonify({"branches": branches})

    except Exception as e:
        logger.error(f"Error listing Bitbucket branches: {e}", exc_info=True)
        return jsonify({"error": "Failed to list branches"}), 500


@bitbucket_browsing_bp.route("/pull-requests/<workspace>/<repo_slug>", methods=["GET", "OPTIONS"])
def list_pull_requests(workspace, repo_slug):
    """List pull requests for a Bitbucket repository."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "User ID required"}), 400

        client = _get_bb_client(user_id)
        if not client:
            return jsonify({"error": "Bitbucket not connected"}), 401

        state = request.args.get("state")
        pull_requests = client.get_pull_requests(workspace, repo_slug, state=state)
        return jsonify({"pull_requests": pull_requests})

    except Exception as e:
        logger.error(f"Error listing Bitbucket pull requests: {e}", exc_info=True)
        return jsonify({"error": "Failed to list pull requests"}), 500


@bitbucket_browsing_bp.route("/issues/<workspace>/<repo_slug>", methods=["GET", "OPTIONS"])
def list_issues(workspace, repo_slug):
    """List issues for a Bitbucket repository."""
    if request.method == "OPTIONS":
        return create_cors_response()

    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "User ID required"}), 400

        client = _get_bb_client(user_id)
        if not client:
            return jsonify({"error": "Bitbucket not connected"}), 401

        issues = client.get_issues(workspace, repo_slug)
        return jsonify({"issues": issues})

    except Exception as e:
        logger.error(f"Error listing Bitbucket issues: {e}", exc_info=True)
        return jsonify({"error": "Failed to list issues"}), 500
