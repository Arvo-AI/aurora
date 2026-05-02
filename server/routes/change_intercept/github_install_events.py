"""GitHub App installation lifecycle webhook handler.

Maintains the ``github_app_installations`` table as customers install,
uninstall, or change repo selections for the Aurora GitHub App.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from flask import jsonify, request as flask_request

logger = logging.getLogger(__name__)


def handle_install_event(request: Any):
    """Dispatch installation / installation_repositories events."""
    event_type = request.headers.get("X-GitHub-Event", "")
    payload: dict[str, Any] = request.get_json(silent=True) or {}
    action = payload.get("action", "")

    if event_type == "installation":
        if action == "created":
            return _on_install_created(payload)
        if action == "deleted":
            return _on_install_deleted(payload)
        if action == "suspend":
            return _on_install_suspended(payload, suspended=True)
        if action == "unsuspend":
            return _on_install_suspended(payload, suspended=False)

    if event_type == "installation_repositories":
        return _on_repos_changed(payload)

    return jsonify({"status": "ignored"}), 200


# ------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------

def _on_install_created(payload: dict[str, Any]):
    installation = payload.get("installation", {})
    installation_id = installation.get("id")
    account_login = installation.get("account", {}).get("login", "")
    repos = [
        r.get("full_name") for r in payload.get("repositories", [])
    ]

    if not installation_id:
        return jsonify({"error": "missing installation_id"}), 400

    _upsert_installation(installation_id, account_login, repos)
    logger.info(
        "[ChangeIntercept:GitHub] App installed: %s (installation %s, %d repos)",
        account_login, installation_id, len(repos),
    )
    return jsonify({"status": "installed"}), 200


def _on_install_deleted(payload: dict[str, Any]):
    installation_id = payload.get("installation", {}).get("id")
    if installation_id:
        _delete_installation(installation_id)
        logger.info(
            "[ChangeIntercept:GitHub] App uninstalled: installation %s",
            installation_id,
        )
    return jsonify({"status": "uninstalled"}), 200


def _on_install_suspended(payload: dict[str, Any], *, suspended: bool):
    installation_id = payload.get("installation", {}).get("id")
    if installation_id:
        _set_suspended(installation_id, suspended)
        logger.info(
            "[ChangeIntercept:GitHub] App %s: installation %s",
            "suspended" if suspended else "unsuspended", installation_id,
        )
    return jsonify({"status": "updated"}), 200


def _on_repos_changed(payload: dict[str, Any]):
    installation_id = payload.get("installation", {}).get("id")
    added = [r.get("full_name") for r in payload.get("repositories_added", [])]
    removed = [r.get("full_name") for r in payload.get("repositories_removed", [])]

    if installation_id:
        _update_repos(installation_id, added, removed)
        logger.info(
            "[ChangeIntercept:GitHub] Repos changed for installation %s: "
            "+%d -%d",
            installation_id, len(added), len(removed),
        )
    return jsonify({"status": "repos_updated"}), 200


# ------------------------------------------------------------------
# DB operations
# ------------------------------------------------------------------

def _upsert_installation(
    installation_id: int, account_login: str, repos: list[str],
) -> None:
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO github_app_installations
                    (installation_id, org_id, github_account_login, repos)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (installation_id) DO UPDATE SET
                    github_account_login = EXCLUDED.github_account_login,
                    repos = EXCLUDED.repos,
                    suspended_at = NULL
                """,
                (installation_id, "", account_login, json.dumps(repos)),
            )
        conn.commit()


def _delete_installation(installation_id: int) -> None:
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM github_app_installations WHERE installation_id = %s",
                (installation_id,),
            )
        conn.commit()


def _set_suspended(installation_id: int, suspended: bool) -> None:
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            if suspended:
                cur.execute(
                    "UPDATE github_app_installations SET suspended_at = now() "
                    "WHERE installation_id = %s",
                    (installation_id,),
                )
            else:
                cur.execute(
                    "UPDATE github_app_installations SET suspended_at = NULL "
                    "WHERE installation_id = %s",
                    (installation_id,),
                )
        conn.commit()


def _update_repos(
    installation_id: int, added: list[str], removed: list[str],
) -> None:
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT repos FROM github_app_installations "
                "WHERE installation_id = %s",
                (installation_id,),
            )
            row = cur.fetchone()
            current: list[str] = json.loads(row[0]) if row and row[0] else []

            updated = list(set(current + added) - set(removed))

            cur.execute(
                "UPDATE github_app_installations SET repos = %s "
                "WHERE installation_id = %s",
                (json.dumps(updated), installation_id),
            )
        conn.commit()
