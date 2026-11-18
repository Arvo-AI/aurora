"""Authenticated evidence-only incident replay endpoint."""
import logging
import os
from flask import Blueprint, request, jsonify
from utils.auth.rbac_decorators import require_permission
from utils.web.limiter_ext import limiter
from services.incident_replay import investigate

logger = logging.getLogger(__name__)
incident_replay_bp = Blueprint('incident_replay', __name__, url_prefix='/api/incident-replay')

@incident_replay_bp.route('', methods=['POST'])
@limiter.limit('3 per minute')
@require_permission('chat', 'write')
def replay_incident(user_id):
    if os.getenv('INCIDENT_REPLAY_ENABLED') != 'true':
        return jsonify({'error':'Incident replay is disabled.'}), 404
    # Reject oversized payloads before decoding JSON, independent of global Flask 1GB limit.
    if request.content_length is not None and request.content_length > 16000:
        return jsonify({'error':'Payload too large.'}), 413
    raw = request.get_data(cache=True)
    if len(raw) > 16000:
        return jsonify({'error':'Payload too large.'}), 413
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'error':'Invalid JSON.'}), 400
    try:
        result = investigate(payload.get('alert'), payload.get('logs'))
        return jsonify(result), 200
    except ValueError as exc:
        if str(exc) in ('Alert and logs are required.', 'Input exceeds maximum size.', 'Too many log lines.'):
            return jsonify({'error': str(exc)}), 400
        logger.warning('Replay model response could not be validated')
        return jsonify({'error':'Investigation could not be completed.'}), 502
    except Exception:
        logger.exception('Incident replay inference failed')
        return jsonify({'error':'Investigation service unavailable.'}), 502
