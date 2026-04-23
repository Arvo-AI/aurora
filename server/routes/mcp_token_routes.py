import logging
import secrets
from datetime import datetime, timedelta, timezone
from flask import Blueprint, jsonify, request
from psycopg2.extras import RealDictCursor
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.db_adapters import connect_to_db_as_user
from utils.log_sanitizer import sanitize
from utils.web.limiter_ext import limiter

logger = logging.getLogger(__name__)
mcp_token_bp = Blueprint('mcp_token', __name__)
_LOG_PREFIX = "[MCPToken]"


def _generate_token():
    # Tokens are stored in plaintext (consistent with kubectl_agent_tokens).
    # Future hardening: store SHA-256 hash, compare hashes on lookup.
    return f"aurora_mcp_{secrets.token_urlsafe(48)}"


def _to_iso(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


@mcp_token_bp.route('/api/mcp/tokens', methods=['POST'])
@require_permission("connectors", "write")
@limiter.limit("5 per minute;20 per hour")
def create_mcp_token(user_id):
    try:
        data = request.get_json() or {}
        name = data.get('name', 'Unnamed Token')[:100]
        expires_days = data.get('expires_days')
        token = _generate_token()
        if expires_days is not None:
            try:
                expires_days = int(expires_days)
            except (ValueError, TypeError):
                return jsonify({'error': 'expires_days must be a positive integer'}), 400
            if expires_days < 1:
                return jsonify({'error': 'expires_days must be a positive integer'}), 400
            expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)
        else:
            expires_at = None
        org_id = get_org_id_from_request()
        if not org_id:
            return jsonify({'error': 'Organization context required'}), 400

        with connect_to_db_as_user() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                cursor.execute("""
                    INSERT INTO mcp_tokens (token, user_id, org_id, name, expires_at, status)
                    VALUES (%s, %s, %s, %s, %s, 'active')
                    RETURNING id, token, name, created_at, expires_at
                """, (token, user_id, org_id, name, expires_at))
                result = cursor.fetchone()
                conn.commit()

        logger.info(f"Created MCP token for user {sanitize(user_id)}, name: {sanitize(name)}")
        return jsonify({
            'success': True,
            'token': result['token'],
            'id': result['id'],
            'name': result['name'],
            'created_at': _to_iso(result['created_at']),
            'expires_at': _to_iso(result['expires_at']),
            'message': 'Token created. Save this token - it will only be shown once!'
        }), 201
    except Exception as e:
        logger.error(f"Error creating MCP token: {e}", exc_info=True)
        return jsonify({'error': 'Failed to create token'}), 500


@mcp_token_bp.route('/api/mcp/tokens', methods=['GET'])
@require_permission("connectors", "read")
@limiter.limit("30 per minute")
def list_mcp_tokens(user_id):
    try:
        org_id = get_org_id_from_request()
        if not org_id:
            return jsonify({'error': 'Organization context required'}), 400

        with connect_to_db_as_user() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                cursor.execute("""
                    SELECT id, name, created_at, last_used_at, expires_at, status,
                           CONCAT(SUBSTRING(token, 1, 20), '...') as token_preview
                    FROM mcp_tokens WHERE user_id = %s AND org_id = %s ORDER BY created_at DESC
                """, (user_id, org_id))
                tokens = cursor.fetchall()

        for t in tokens:
            t['created_at'] = _to_iso(t['created_at'])
            t['last_used_at'] = _to_iso(t['last_used_at'])
            t['expires_at'] = _to_iso(t['expires_at'])

        return jsonify({'tokens': tokens}), 200
    except Exception as e:
        logger.error(f"Error listing MCP tokens: {e}", exc_info=True)
        return jsonify({'error': 'Failed to list tokens'}), 500


@mcp_token_bp.route('/api/mcp/tokens/<int:token_id>', methods=['DELETE'])
@require_permission("connectors", "write")
@limiter.limit("10 per minute;30 per hour")
def revoke_mcp_token(user_id, token_id):
    try:
        org_id = get_org_id_from_request()
        if not org_id:
            return jsonify({'error': 'Organization context required'}), 400

        with connect_to_db_as_user() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                cursor.execute(
                    "UPDATE mcp_tokens SET status = 'revoked' WHERE id = %s AND user_id = %s AND org_id = %s RETURNING id",
                    (token_id, user_id, org_id)
                )
                result = cursor.fetchone()
                conn.commit()

        if not result:
            return jsonify({'error': 'Token not found or unauthorized'}), 404

        logger.info(f"Revoked MCP token {token_id} for user {user_id}")
        return jsonify({'success': True, 'message': 'Token revoked'}), 200
    except Exception as e:
        logger.error(f"Error revoking MCP token: {e}", exc_info=True)
        return jsonify({'error': 'Failed to revoke token'}), 500
