"""Evidence-verified RCA replay routes.

Two endpoints, no persistence:

``GET  /api/incident-replay/bundles``   list the bundles this deployment ships
``POST /api/incident-replay/run``       run one replay and return the validated result

A replay deliberately does not create an incident, a citation row, or any other
durable record. The MVP answers a single question — "what does Aurora's
investigation look like on a real evidence bundle, with every claim checked?" —
and writes nothing that a real incident would later be judged against.
"""

from __future__ import annotations

import logging
import uuid

from flask import Blueprint, jsonify, request

from incident_replay.evidence import BUNDLE_NAMES, normalize_bundle
from incident_replay.investigator import DEFAULT_TIMEOUT_S, run_replay
from incident_replay.validator import validate
from utils.auth.rbac_decorators import require_permission
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

incident_replay_bp = Blueprint("incident_replay", __name__)


@incident_replay_bp.route("/api/incident-replay/bundles", methods=["GET"])
@require_permission("incident_replay", "read")
def list_bundles(user_id):
    """The bundles a caller may run. Names only — never paths."""
    return jsonify({"bundles": list(BUNDLE_NAMES)})


@incident_replay_bp.route("/api/incident-replay/run", methods=["POST"])
@require_permission("incident_replay", "write")
def run(user_id):
    """Run one replay and return the investigation plus its validation report."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "invalid request body"}), 400

    name = body.get("bundle")
    if not isinstance(name, str) or name not in BUNDLE_NAMES:
        # A path, an arbitrary name, or a nested value never reaches the loader.
        # The rejection is a constant message: echoing the supplied value would
        # turn the endpoint into a filesystem oracle and a log-injection sink.
        return jsonify({"error": "unknown bundle", "bundles": list(BUNDLE_NAMES)}), 400

    try:
        bundle = normalize_bundle(name)
    except ValueError:
        logger.warning("[replay] bundle rejected")
        return jsonify({"error": "unknown bundle", "bundles": list(BUNDLE_NAMES)}), 400

    timeout_s = body.get("timeout_s", DEFAULT_TIMEOUT_S)
    try:
        timeout_s = float(timeout_s)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    timeout_s = max(30.0, min(timeout_s, 600.0))

    replay_id = str(uuid.uuid4())
    outcome = run_replay(bundle, user_id=user_id, timeout_s=timeout_s)

    if "error" in outcome:
        return (
            jsonify(
                {
                    "replay_id": replay_id,
                    "bundle": name,
                    "error": outcome["error"],
                    "error_kind": outcome.get("error_kind"),
                    "model": outcome.get("model"),
                }
            ),
            502,
        )

    report = validate(bundle, outcome["result"])
    logger.info(
        "[replay] %s bundle=%s model=%s findings=%d invalid_ids=%d elapsed=%ss",
        replay_id,
        sanitize(name),
        sanitize(str(outcome.get("model"))),
        len(report.findings),
        len(report.missing_ids),
        outcome.get("elapsed_s"),
    )
    return jsonify(
        {
            "replay_id": replay_id,
            "bundle": name,
            "model": outcome["model"],
            "elapsed_s": outcome["elapsed_s"],
            "attempts": outcome["attempts"],
            "evidence": bundle.to_dict(),
            "investigation": outcome["result"],
            "validation": report.to_dict(),
        }
    )
