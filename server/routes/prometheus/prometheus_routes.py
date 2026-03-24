"""Prometheus + Alertmanager integration routes.

Handles connection management, PromQL queries, alert retrieval,
target health, and Alertmanager alert/silence proxying.
"""

import logging

from flask import Blueprint, jsonify, request

from connectors.prometheus_connector.api_client import (
    AlertmanagerAPIError,
    AlertmanagerClient,
    PrometheusAPIError,
    PrometheusClient,
)
from utils.auth.rbac_decorators import require_permission
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)

prometheus_bp = Blueprint("prometheus", __name__)


def _build_clients(user_id: str):
    """Return (PrometheusClient, AlertmanagerClient | None) from stored creds."""
    creds = get_token_data(user_id, "prometheus")
    if not creds or not creds.get("prometheus_url"):
        return None, None

    prom = PrometheusClient(
        base_url=creds["prometheus_url"],
        bearer_token=creds.get("bearer_token"),
        username=creds.get("username"),
        password=creds.get("password"),
    )
    am = None
    if creds.get("alertmanager_url"):
        am = AlertmanagerClient(
            base_url=creds["alertmanager_url"],
            bearer_token=creds.get("bearer_token"),
            username=creds.get("username"),
            password=creds.get("password"),
        )
    return prom, am


# ── Connection management ──────────────────────────────────────────────────


@prometheus_bp.route("/connect", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def connect(user_id):
    data = request.get_json(force=True, silent=True) or {}
    prometheus_url = (data.get("prometheusUrl") or "").strip().rstrip("/")

    if not prometheus_url:
        return jsonify({"error": "prometheusUrl is required"}), 400

    alertmanager_url = (data.get("alertmanagerUrl") or "").strip().rstrip("/") or None
    bearer_token = data.get("bearerToken") or None
    username = data.get("username") or None
    password = data.get("password") or None

    logger.info("[PROMETHEUS] Connecting user %s to %s", user_id, prometheus_url)

    client = PrometheusClient(
        base_url=prometheus_url,
        bearer_token=bearer_token,
        username=username,
        password=password,
    )
    try:
        info = client.validate_connection()
    except PrometheusAPIError as exc:
        logger.error("[PROMETHEUS] Connection validation failed for user %s: %s", user_id, exc)
        return jsonify({"error": f"Failed to connect to Prometheus: {exc}"}), 502

    am_connected = False
    if alertmanager_url:
        am_client = AlertmanagerClient(
            base_url=alertmanager_url,
            bearer_token=bearer_token,
            username=username,
            password=password,
        )
        try:
            am_client.validate_connection()
            am_connected = True
        except AlertmanagerAPIError as exc:
            logger.warning("[PROMETHEUS] Alertmanager validation failed for user %s: %s", user_id, exc)

    token_data = {"prometheus_url": prometheus_url}
    if alertmanager_url:
        token_data["alertmanager_url"] = alertmanager_url
    if bearer_token:
        token_data["bearer_token"] = bearer_token
    if username:
        token_data["username"] = username
    if password:
        token_data["password"] = password

    try:
        store_tokens_in_db(user_id, token_data, "prometheus")
    except Exception as exc:
        logger.exception("[PROMETHEUS] Failed to store credentials for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to store Prometheus credentials"}), 500

    return jsonify({
        "success": True,
        "connected": True,
        "version": info.get("version"),
        "alertmanagerConnected": am_connected,
    })


@prometheus_bp.route("/status", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def status(user_id):
    try:
        creds = get_token_data(user_id, "prometheus")
    except Exception:
        logger.exception("Failed to retrieve Prometheus credentials for user %s", user_id)
        return jsonify({"connected": False})

    if not creds or not creds.get("prometheus_url"):
        return jsonify({"connected": False})

    result: dict = {"connected": True}

    prom = PrometheusClient(
        base_url=creds["prometheus_url"],
        bearer_token=creds.get("bearer_token"),
        username=creds.get("username"),
        password=creds.get("password"),
    )
    try:
        info = prom.validate_connection()
        result["version"] = info.get("version")
    except PrometheusAPIError:
        return jsonify({"connected": False})

    if creds.get("alertmanager_url"):
        am = AlertmanagerClient(
            base_url=creds["alertmanager_url"],
            bearer_token=creds.get("bearer_token"),
            username=creds.get("username"),
            password=creds.get("password"),
        )
        try:
            am.validate_connection()
            result["alertmanagerConnected"] = True
        except AlertmanagerAPIError:
            result["alertmanagerConnected"] = False

    return jsonify(result)


@prometheus_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
@require_permission("connectors", "write")
def disconnect(user_id):
    try:
        success, deleted = delete_user_secret(user_id, "prometheus")
        if not success:
            logger.warning("[PROMETHEUS] Failed to clean up secrets during disconnect")
            return jsonify({"success": False, "error": "Failed to delete stored credentials"}), 500

        logger.info("[PROMETHEUS] Disconnected provider (deleted %d token rows)", deleted)
        return jsonify({"success": True, "message": "Prometheus disconnected successfully", "deleted": deleted})
    except Exception:
        logger.exception("[PROMETHEUS] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect Prometheus"}), 500


# ── PromQL queries ─────────────────────────────────────────────────────────


@prometheus_bp.route("/query", methods=["POST", "OPTIONS"])
@require_permission("connectors", "read")
def instant_query(user_id):
    prom, _ = _build_clients(user_id)
    if prom is None:
        return jsonify({"error": "Prometheus not connected"}), 404

    data = request.get_json(force=True, silent=True) or {}
    promql = data.get("query")
    if not promql:
        return jsonify({"error": "query is required"}), 400

    try:
        result = prom.query(promql, time=data.get("time"))
        return jsonify(result)
    except PrometheusAPIError as exc:
        return jsonify({"error": str(exc)}), 502


@prometheus_bp.route("/query-range", methods=["POST", "OPTIONS"])
@require_permission("connectors", "read")
def range_query(user_id):
    prom, _ = _build_clients(user_id)
    if prom is None:
        return jsonify({"error": "Prometheus not connected"}), 404

    data = request.get_json(force=True, silent=True) or {}
    promql = data.get("query")
    start = data.get("start")
    end = data.get("end")
    step = data.get("step")
    if not all([promql, start, end, step]):
        return jsonify({"error": "query, start, end, and step are required"}), 400

    try:
        result = prom.query_range(promql, start, end, step)
        return jsonify(result)
    except PrometheusAPIError as exc:
        return jsonify({"error": str(exc)}), 502


# ── Prometheus alerts, targets, rules, metadata ────────────────────────────


@prometheus_bp.route("/alerts", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def alerts(user_id):
    prom, _ = _build_clients(user_id)
    if prom is None:
        return jsonify({"error": "Prometheus not connected"}), 404
    try:
        return jsonify({"alerts": prom.get_alerts()})
    except PrometheusAPIError as exc:
        return jsonify({"error": str(exc)}), 502


@prometheus_bp.route("/targets", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def targets(user_id):
    prom, _ = _build_clients(user_id)
    if prom is None:
        return jsonify({"error": "Prometheus not connected"}), 404
    try:
        return jsonify(prom.get_targets())
    except PrometheusAPIError as exc:
        return jsonify({"error": str(exc)}), 502


@prometheus_bp.route("/rules", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def rules(user_id):
    prom, _ = _build_clients(user_id)
    if prom is None:
        return jsonify({"error": "Prometheus not connected"}), 404
    try:
        return jsonify(prom.get_rules())
    except PrometheusAPIError as exc:
        return jsonify({"error": str(exc)}), 502


@prometheus_bp.route("/metadata", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def metadata(user_id):
    prom, _ = _build_clients(user_id)
    if prom is None:
        return jsonify({"error": "Prometheus not connected"}), 404
    metric = request.args.get("metric")
    try:
        return jsonify(prom.get_metadata(metric=metric))
    except PrometheusAPIError as exc:
        return jsonify({"error": str(exc)}), 502


# ── Alertmanager alerts & silences ─────────────────────────────────────────


@prometheus_bp.route("/alertmanager/alerts", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def alertmanager_alerts(user_id):
    _, am = _build_clients(user_id)
    if am is None:
        return jsonify({"error": "Alertmanager not configured"}), 404
    try:
        silenced = request.args.get("silenced")
        inhibited = request.args.get("inhibited")
        active = request.args.get("active")
        return jsonify({
            "alerts": am.get_alerts(
                silenced=silenced == "true" if silenced else None,
                inhibited=inhibited == "true" if inhibited else None,
                active=active == "true" if active else None,
            ),
        })
    except AlertmanagerAPIError as exc:
        return jsonify({"error": str(exc)}), 502


@prometheus_bp.route("/alertmanager/silences", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def alertmanager_silences(user_id):
    _, am = _build_clients(user_id)
    if am is None:
        return jsonify({"error": "Alertmanager not configured"}), 404
    try:
        return jsonify({"silences": am.get_silences()})
    except AlertmanagerAPIError as exc:
        return jsonify({"error": str(exc)}), 502
