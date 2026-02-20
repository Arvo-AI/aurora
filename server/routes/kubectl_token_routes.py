import logging
import secrets
from datetime import datetime, timedelta, timezone
from flask import Blueprint, jsonify, request
from functools import wraps
from psycopg2.extras import RealDictCursor
from utils.auth.stateless_auth import get_user_id_from_request
from utils.db.db_adapters import connect_to_db_as_user
from utils.web.limiter_ext import limiter

logger = logging.getLogger(__name__)
kubectl_token_bp = Blueprint('kubectl_token', __name__)

def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({'error': 'Authentication required'}), 401
        return f(user_id, *args, **kwargs)
    return decorated_function

def generate_token():
    return f"aurora_kubectl_{secrets.token_urlsafe(48)}"

def _to_iso(dt):
    if not dt:
        return None
    # If datetime is naive (no timezone), assume it's UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

@kubectl_token_bp.route('/api/kubectl/tokens', methods=['POST'])
@require_auth
@limiter.limit("5 per minute;20 per hour")
def create_token(user_id):
    try:
        data = request.get_json() or {}
        cluster_name = data.get('cluster_name', 'Unnamed Cluster')
        notes = data.get('notes', '')
        expires_days = data.get('expires_days')
        token = generate_token()
        expires_at = datetime.now() + timedelta(days=expires_days) if expires_days else None
        conn = connect_to_db_as_user()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("""
                INSERT INTO kubectl_agent_tokens (token, user_id, cluster_name, notes, expires_at, status)
                VALUES (%s, %s, %s, %s, %s, 'active')
                RETURNING id, token, cluster_name, created_at, expires_at
            """, (token, user_id, cluster_name, notes, expires_at))
            result = cursor.fetchone()
            conn.commit()
            cursor.close()
        finally:
            conn.close()
        logger.info(f"Created kubectl token for user {user_id}, cluster: {cluster_name}")
        return jsonify({
            'success': True,
            'token': result['token'],
            'cluster_name': result['cluster_name'],
            'created_at': _to_iso(result['created_at']),
            'expires_at': _to_iso(result['expires_at']),
            'message': 'Token created successfully. Save this token - it will only be shown once!'
        }), 201
    except Exception as e:
        logger.error(f"Error creating kubectl token: {e}", exc_info=True)
        return jsonify({'error': 'Failed to create token'}), 500

@kubectl_token_bp.route('/api/kubectl/tokens', methods=['GET'])
@require_auth
@limiter.limit("30 per minute")
def list_tokens(user_id):
    try:
        conn = connect_to_db_as_user()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("""
                SELECT id, cluster_name, cluster_id, created_at, last_connected_at, expires_at, status, notes,
                       CONCAT(SUBSTRING(token, 1, 20), '...') as token_preview
                FROM kubectl_agent_tokens WHERE user_id = %s ORDER BY created_at DESC
            """, (user_id,))
            tokens = cursor.fetchall()
            cursor.close()
        finally:
            conn.close()
        for token in tokens:
            token['created_at'] = _to_iso(token['created_at'])
            token['last_connected_at'] = _to_iso(token['last_connected_at'])
            token['expires_at'] = _to_iso(token['expires_at'])
        return jsonify({'tokens': tokens}), 200
    except Exception as e:
        logger.error(f"Error listing kubectl tokens: {e}", exc_info=True)
        return jsonify({'error': 'Failed to list tokens'}), 500

@kubectl_token_bp.route('/api/kubectl/connections', methods=['GET'])
@require_auth
@limiter.limit("30 per minute")
def list_connections(user_id):
    try:
        token_filter = request.args.get('token')
        conn = connect_to_db_as_user()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("UPDATE active_kubectl_connections SET status = 'stale' WHERE last_heartbeat < NOW() - INTERVAL '3 minutes'")
            cursor.execute("UPDATE active_kubectl_connections SET status = 'active' WHERE last_heartbeat >= NOW() - INTERVAL '3 minutes'")
            conn.commit()
            query = """SELECT c.cluster_id, c.connected_at, c.last_heartbeat, c.agent_version, c.status, t.cluster_name
                       FROM active_kubectl_connections c JOIN kubectl_agent_tokens t ON c.token = t.token
                       WHERE t.user_id = %s""" + (" AND c.token = %s" if token_filter else "") + " ORDER BY c.connected_at DESC"
            cursor.execute(query, (user_id, token_filter) if token_filter else (user_id,))
            connections = cursor.fetchall()
            cursor.close()
        finally:
            conn.close()
        for conn_data in connections:
            conn_data['connected_at'] = _to_iso(conn_data['connected_at'])
            conn_data['last_heartbeat'] = _to_iso(conn_data['last_heartbeat'])
        return jsonify({'connections': connections}), 200
    except Exception as e:
        logger.error(f"Error listing kubectl connections: {e}", exc_info=True)
        return jsonify({'error': 'Failed to list connections'}), 500

@kubectl_token_bp.route('/api/kubectl/connections/<cluster_id>', methods=['DELETE'])
@require_auth
@limiter.limit("10 per minute;30 per hour")
def disconnect_cluster(user_id, cluster_id):
    try:
        conn = connect_to_db_as_user()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("""
                SELECT c.token, t.cluster_name FROM active_kubectl_connections c
                JOIN kubectl_agent_tokens t ON c.token = t.token
                WHERE c.cluster_id = %s AND t.user_id = %s
            """, (cluster_id, user_id))
            
            result = cursor.fetchone()
            if not result:
                cursor.close()
                conn.close()
                return jsonify({'error': 'Cluster not found or unauthorized'}), 403
            
            token = result['token']
            cluster_name = result['cluster_name']
            
            # Revoke the token so it can't reconnect
            cursor.execute("UPDATE kubectl_agent_tokens SET status = 'revoked' WHERE token = %s", (token,))
            # Delete the active connection
            cursor.execute("DELETE FROM active_kubectl_connections WHERE cluster_id = %s", (cluster_id,))
            conn.commit()
            cursor.close()
        finally:
            conn.close()
        logger.info(f"Disconnected kubectl cluster {cluster_id} (revoked token) for user {user_id}")
        
        # Return command to delete agent from cluster (Helm deployment)
        delete_command = "helm uninstall aurora-kubectl-agent -n <your-namespace>"
        
        return jsonify({
            'cluster_name': cluster_name,
            'delete_command': delete_command,
            'message': 'Token revoked successfully'
        }), 200
    except Exception as e:
        logger.error(f"Error disconnecting kubectl cluster: {e}", exc_info=True)
        return jsonify({'error': 'Failed to disconnect cluster'}), 500
