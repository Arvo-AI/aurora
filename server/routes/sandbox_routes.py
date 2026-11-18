"""Public sample-only routes, reached through the Next.js internal proxy.

No user or organization identity is accepted. No customer data is queried.
"""

import asyncio
import hmac
import logging
import os
import threading

from flask import Blueprint, jsonify, request
from pydantic import ValidationError

from services.sandbox.evidence import list_cases
from services.sandbox.models import ChallengeRequest, InvestigateRequest
from services.sandbox.receipts import InvalidRun, RunExpired, issue_receipt, verify_receipt
from services.sandbox.runner import SandboxUnavailable, challenge, investigate

logger = logging.getLogger(__name__)
sandbox_bp = Blueprint("sandbox", __name__, url_prefix="/sandbox")
# Limit in-flight work per process as well as the shared deployment rate limit.
_slots = threading.BoundedSemaphore(3)


@sandbox_bp.before_request
def require_internal_proxy():
    secret = os.getenv("INTERNAL_API_SECRET", "")
    if not secret:
        return jsonify({"error": "Sandbox server configuration is incomplete."}), 503
    supplied = request.headers.get("X-Internal-Secret", "")
    if not supplied or not hmac.compare_digest(supplied.encode(), secret.encode()):
        return jsonify({"error": "Forbidden"}), 403


@sandbox_bp.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@sandbox_bp.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": "Request is too large."}), 413


@sandbox_bp.get("/cases")
def cases():
    return jsonify({"cases": list_cases()})


@sandbox_bp.post("/investigate")
def investigate_case():
    # This endpoint accepts a case ID only, not prompts, paths, model choices or credentials.
    # Read at most one byte past the accepted limit: Werkzeug can truncate a
    # chunked stream at max_content_length instead of raising on get_data().
    error = check_body(512)
    if error:
        return error
    try:
        payload = InvestigateRequest.model_validate(request.get_json(silent=True))
    except ValidationError:
        return jsonify({"error": "Choose case-a, case-b or case-c. No other fields are accepted."}), 400

    async def run():
        result = await investigate(payload.case_id)
        result["run_token"] = issue_receipt(result)
        return result

    return execute(run)


def check_body(limit):
    request.max_content_length = limit + 1
    if request.content_length is not None and request.content_length > limit:
        return jsonify({"error": "Request is too large."}), 413
    if not request.is_json:
        return jsonify({"error": "Send a JSON object."}), 415
    if len(request.get_data()) > limit:
        return jsonify({"error": "Request is too large."}), 413


@sandbox_bp.post("/challenge")
def challenge_diagnosis():
    error = check_body(65536)
    if error:
        return error
    try:
        payload = ChallengeRequest.model_validate(request.get_json(silent=True))
    except ValidationError:
        return jsonify({"error": "Choose a preset question for a completed investigation. No other fields are accepted."}), 400
    try:
        receipt = verify_receipt(payload.run_token)
    except RunExpired as exc:
        return jsonify({"error": str(exc)}), 410
    except InvalidRun as exc:
        return jsonify({"error": str(exc)}), 400

    return execute(lambda: challenge(receipt, payload.question_id))


def execute(operation):
    if not _slots.acquire(blocking=False):
        return jsonify({"error": "All investigation slots are busy. Try again shortly."}), 429
    try:
        return jsonify(asyncio.run(operation()))
    except RunExpired as exc:
        return jsonify({"error": str(exc)}), 410
    except SandboxUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    except TimeoutError:
        return jsonify({"error": "This request reached its time limit. No answer was substituted. You can retry it independently."}), 504
    except Exception as exc:
        # Log only the exception class: provider messages can include HTTP bodies.
        logger.warning("Sample investigation failed (%s)", type(exc).__name__)
        return jsonify({"error": "This request could not finish. You can retry it independently."}), 502
    finally:
        _slots.release()
