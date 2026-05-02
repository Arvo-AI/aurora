"""Webhook endpoint for the change-intercept pipeline.

Single route: POST /webhook/<vendor>
Dispatches to the appropriate adapter from the registry.
"""

from __future__ import annotations

import logging

from flask import jsonify, request

from routes.change_intercept import bp
from services.change_intercept.adapters.registry import get_adapter

logger = logging.getLogger(__name__)


@bp.route("/webhook/<vendor>", methods=["POST"])
def handle_webhook(vendor: str):
    try:
        adapter = get_adapter(vendor)
    except ValueError:
        return jsonify({"error": f"unsupported vendor: {vendor}"}), 404

    if not adapter.verify_signature(request):
        logger.warning("[ChangeIntercept] Invalid signature from %s", vendor)
        return jsonify({"error": "invalid signature"}), 401

    # GitHub lifecycle events are handled separately.
    gh_event = request.headers.get("X-GitHub-Event", "")
    if gh_event in ("installation", "installation_repositories"):
        from routes.change_intercept.github_install_events import (
            handle_install_event,
        )
        return handle_install_event(request)

    # Check for reply-to-Aurora before normal parse.
    reply = adapter.is_reply_to_us(request)
    if reply:
        from routes.change_intercept.tasks import launch_followup_investigation
        org_id = _resolve_org_from_payload(vendor, request)
        if org_id:
            launch_followup_investigation.delay(
                reply.original_event_dedup_key,
                reply.reply_body,
                org_id,
            )
        return jsonify({"status": "follow-up queued"}), 202

    event = adapter.parse(request)
    if event is None:
        return jsonify({"status": "ignored"}), 200

    from routes.change_intercept.tasks import process_change_event
    process_change_event.delay(vendor, request.get_json(), event.org_id)
    return jsonify({"status": "queued"}), 202


def _resolve_org_from_payload(vendor: str, req) -> str | None:
    """Best-effort org_id resolution from a comment webhook payload."""
    if vendor == "github":
        payload = req.get_json(silent=True) or {}
        installation_id = payload.get("installation", {}).get("id")
        if installation_id:
            from services.change_intercept.adapters.github import GitHubAdapter
            return GitHubAdapter._resolve_org_id(installation_id)
    return None
