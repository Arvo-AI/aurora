#!/usr/bin/env python3
"""Migrate legacy GCP credentials from ``user_tokens`` to ``user_connections``.

Idempotent: re-running on already-migrated rows is a no-op (the upsert just
refreshes ``last_verified_at`` + ``accessible_project_ids``). Legacy rows are
left intact so the system stays bilingual during the transition.

# TODO(2026-Q3): remove user_tokens.gcp legacy storage after one release.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# Make ``server/`` importable when run via `python server/scripts/...`
_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.dirname(_HERE)
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

from google.oauth2 import service_account as google_sa  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

from utils.db.connection_utils import save_connection_metadata  # noqa: E402
from utils.db.db_utils import connect_to_db_as_admin  # noqa: E402
from utils.log_sanitizer import hash_for_log  # noqa: E402
from utils.secrets.secret_ref_utils import (  # noqa: E402
    secret_manager,
    store_connection_secret,
)

logger = logging.getLogger("migrate_gcp_tokens")

READ_ONLY_SCOPE = "https://www.googleapis.com/auth/cloud-platform.read-only"


def _iter_gcp_token_rows() -> List[Tuple[str, str]]:
    """Return ``(user_id, secret_ref)`` for every active GCP user_tokens row."""
    sql = (
        "SELECT user_id, secret_ref FROM user_tokens "
        "WHERE provider = 'gcp' AND secret_ref IS NOT NULL AND is_active = TRUE"
    )
    rows: List[Tuple[str, str]] = []
    conn = None
    try:
        conn = connect_to_db_as_admin()
        with conn.cursor() as cur:
            cur.execute(sql)
            for user_id, secret_ref in cur.fetchall():
                if user_id and secret_ref:
                    rows.append((user_id, secret_ref))
    finally:
        if conn:
            conn.close()
    return rows


def _decode_secret(secret_ref: str) -> Optional[Dict[str, Any]]:
    try:
        raw = secret_manager.get_secret(secret_ref)
    except Exception as exc:
        logger.warning("Failed to read secret %s: %s", hash_for_log(secret_ref), type(exc).__name__)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _extract_sa_info(token_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pull out the SA JSON blob from the legacy token payload.

    Aurora has stored SA creds in several shapes over time — at the top level,
    nested under ``service_account``, or nested under ``sa_info``. Try each.
    """
    if not isinstance(token_data, dict):
        return None

    candidates = (
        token_data,
        token_data.get("service_account"),
        token_data.get("sa_info"),
        token_data.get("credentials"),
    )
    for cand in candidates:
        if isinstance(cand, dict) and cand.get("client_email") and cand.get("project_id"):
            return cand
    return None


def _enumerate_projects(sa_info: Dict[str, Any], project_id: str) -> List[str]:
    try:
        creds = google_sa.Credentials.from_service_account_info(
            sa_info, scopes=[READ_ONLY_SCOPE]
        )
        crm = build("cloudresourcemanager", "v1", credentials=creds, cache_discovery=False)
        resp = crm.projects().list().execute()
    except Exception as exc:
        logger.info(
            "projects.list failed for project=%s (%s) — using home project only",
            hash_for_log(project_id),
            type(exc).__name__,
        )
        return [project_id]

    accessible: List[str] = []
    for proj in resp.get("projects") or []:
        pid = proj.get("projectId")
        if not pid:
            continue
        if (proj.get("lifecycleState") or "ACTIVE") != "ACTIVE":
            continue
        accessible.append(pid)
    if project_id not in accessible:
        accessible.append(project_id)
    return accessible


def migrate(dry_run: bool) -> Tuple[int, int, int]:
    """Returns ``(processed, migrated, skipped)``."""
    rows = _iter_gcp_token_rows()
    logger.info("Found %d GCP user_tokens rows", len(rows))

    processed = migrated = skipped = 0
    for user_id, secret_ref in rows:
        processed += 1
        token_data = _decode_secret(secret_ref)
        if token_data is None:
            logger.info(
                "Skipping user=%s — secret unavailable",
                hash_for_log(user_id),
            )
            skipped += 1
            continue

        sa_info = _extract_sa_info(token_data)
        if not sa_info:
            logger.info(
                "Skipping user=%s — no SA JSON in payload (likely OAuth row)",
                hash_for_log(user_id),
            )
            skipped += 1
            continue

        client_email = sa_info["client_email"]
        project_id = sa_info["project_id"]

        if dry_run:
            logger.info(
                "[DRY-RUN] Would migrate user=%s account=%s project=%s",
                hash_for_log(user_id),
                hash_for_log(client_email),
                hash_for_log(project_id),
            )
            migrated += 1
            continue

        accessible = _enumerate_projects(sa_info, project_id)
        new_ref = store_connection_secret(user_id, "gcp", client_email, sa_info)
        if not new_ref:
            logger.warning(
                "Failed to store secret for user=%s account=%s",
                hash_for_log(user_id),
                hash_for_log(client_email),
            )
            skipped += 1
            continue

        ok = save_connection_metadata(
            user_id,
            "gcp",
            client_email,
            project_id=project_id,
            accessible_project_ids=accessible,
            visibility="private",
            secret_ref=new_ref,
            connection_method="service_account",
            status="active",
        )
        if not ok:
            logger.warning(
                "Failed to save metadata for user=%s account=%s",
                hash_for_log(user_id),
                hash_for_log(client_email),
            )
            skipped += 1
            continue

        logger.info(
            "Migrated user=%s account=%s project=%s accessible=%d",
            hash_for_log(user_id),
            hash_for_log(client_email),
            hash_for_log(project_id),
            len(accessible),
        )
        migrated += 1

    return processed, migrated, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be migrated without writing to Vault or the DB.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable DEBUG logging."
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    processed, migrated, skipped = migrate(args.dry_run)
    logger.info(
        "Done — processed=%d migrated=%d skipped=%d dry_run=%s",
        processed,
        migrated,
        skipped,
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
