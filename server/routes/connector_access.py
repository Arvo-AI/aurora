"""Org page: which role may use each connector, account, and credential.

Blueprint: connector_access_bp
Prefix: /api/org
"""

from __future__ import annotations

import logging
import re

from flask import Blueprint, jsonify, request

from utils.auth import VALID_ROLES
from utils.auth.connector_access import RESTRICTED_MARKER, Grant
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

connector_access_bp = Blueprint("connector_access", __name__, url_prefix="/api/org")

_CONNECTOR_ID = re.compile(r"^[a-z0-9_:-]{1,128}$")

_NAMES = {
    "aws": "AWS",
    "gcp": "GCP",
    "azure": "Azure",
    "datadog": "Datadog",
    "grafana": "Grafana",
    "splunk": "Splunk",
    "elastic": "Elastic",
    "dynatrace": "Dynatrace",
    "newrelic": "New Relic",
    "sentry": "Sentry",
    "pagerduty": "PagerDuty",
    "opsgenie": "Opsgenie",
    "incidentio": "incident.io",
    "jira": "Jira",
    "confluence": "Confluence",
    "github": "GitHub",
    "gitlab": "GitLab",
    "bitbucket": "Bitbucket",
    "slack": "Slack",
    "teams": "Microsoft Teams",
    "google_chat": "Google Chat",
    "notion": "Notion",
    "kubectl": "Kubernetes",
    "onprem": "On-prem Kubernetes",
    "cloudflare": "Cloudflare",
    "coroot": "Coroot",
    "thousandeyes": "ThousandEyes",
    "netdata": "Netdata",
    "tailscale": "Tailscale",
    "jenkins": "Jenkins",
    "spinnaker": "Spinnaker",
    "cloudbees": "CloudBees",
    "ovh": "OVH",
    "scaleway": "Scaleway",
    "flyio": "Fly.io",
    "bigpanda": "BigPanda",
    "sharepoint": "SharePoint",
    "cloudwatch": "CloudWatch",
}


def _name(connector_id: str) -> str:
    if connector_id.startswith("mcp:"):
        return connector_id.split(":", 1)[1]
    return _NAMES.get(connector_id, connector_id.replace("_", " ").title())


def _cred(credential_id: str, connector_id: str) -> dict:
    if credential_id == "write":
        label = "Modify service account" if connector_id == "gcp" else "Modify role"
    elif credential_id == "read":
        label = "Read-only service account" if connector_id == "gcp" else "Read-only role"
    else:
        label = "Access"
    return {"id": credential_id, "label": label}


def _scope(scope_id: str, label: str, credentials: list, connector_id: str) -> dict:
    return {
        "id": scope_id,
        "label": label,
        "credentials": [_cred(c, connector_id) for c in credentials],
    }


def _aws_scopes(user_id: str) -> list:
    from utils.db.connection_utils import get_all_user_aws_connections

    scopes = []
    for conn in get_all_user_aws_connections(user_id):
        account = str(conn.get("account_id") or "")
        if not account:
            continue
        creds = []
        if conn.get("role_arn"):
            creds.append("write")
        if conn.get("read_only_role_arn"):
            creds.append("read")
        if not creds:
            continue
        scopes.append(_scope(account, account, creds, "aws"))
    return scopes


def _gcp_scopes(user_id: str) -> list:
    from utils.auth.stateless_auth import get_user_preference

    projects = get_user_preference(user_id, "gcp_connected_projects") or []
    if isinstance(projects, str):
        projects = [projects]
    if not isinstance(projects, list):
        projects = []
    ids = [str(p) for p in projects if isinstance(p, str) and p]
    creds = ["write", "read"]
    if not ids:
        return [_scope("", "All projects", creds, "gcp")]
    return [_scope(pid, pid, creds, "gcp") for pid in ids]


def _connection_scopes(user_id: str, provider: str) -> list:
    from utils.db.connection_utils import get_all_user_connections

    rows = get_all_user_connections(user_id, provider)
    accounts = []
    for row in rows:
        account = str(row.get("account_id") or "")
        if account and account not in accounts:
            accounts.append(account)
    if len(accounts) < 2:
        return []
    return [_scope(account, account, [""], provider) for account in accounts]


def _catalog(user_id: str, grants: list) -> list:
    from connectors.mcp_connector.store import list_servers
    from utils.auth.stateless_auth import get_connected_providers

    by_connector: dict = {}
    for grant in grants:
        by_connector.setdefault(grant.connector, []).append(grant)

    connectors = []
    seen = set()

    def add(connector_id: str, scopes: list):
        if not connector_id or connector_id in seen:
            return
        seen.add(connector_id)
        if not scopes:
            scopes = [_scope("", "", [""], connector_id)]
        rows = by_connector.get(connector_id, [])
        connectors.append({
            "id": connector_id,
            "name": _name(connector_id),
            "kind": "mcp" if connector_id.startswith("mcp:") else "connector",
            "limited": bool(rows),
            "scopes": scopes,
            "grants": [
                {"scopeId": g.scope_id, "credential": g.credential, "role": g.role}
                for g in rows if g.role != RESTRICTED_MARKER
            ],
        })

    for provider in get_connected_providers(user_id):
        if provider == "mcp":
            continue
        if provider == "aws":
            add("aws", _aws_scopes(user_id))
        elif provider == "gcp":
            add("gcp", _gcp_scopes(user_id))
        else:
            add(provider, _connection_scopes(user_id, provider))

    for server in list_servers(user_id):
        label = server.get("label")
        if label:
            add(f"mcp:{label}", [])

    connectors.sort(key=lambda c: (c["kind"] != "connector", c["name"].lower()))
    return connectors


def _known_scopes(entry: dict) -> dict:
    return {s["id"]: {c["id"] for c in s["credentials"]} for s in entry["scopes"]}


@connector_access_bp.route("/connector-access", methods=["GET"])
@require_permission("admin", "access")
def list_access(user_id: str):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No org context"}), 400
    from utils.auth.connector_access import load_grants

    return jsonify({"connectors": _catalog(user_id, load_grants(user_id))})


@connector_access_bp.route("/connector-access/<path:connector>", methods=["PUT"])
@require_permission("admin", "access")
def save_access(user_id: str, connector: str):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No org context"}), 400
    if not _CONNECTOR_ID.match(connector):
        return jsonify({"error": "Unknown connector"}), 400

    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("limited"), bool):
        return jsonify({"error": "`limited` must be a boolean"}), 400
    limited = body["limited"]
    raw = body.get("grants") or []
    if not isinstance(raw, list):
        return jsonify({"error": "`grants` must be a list"}), 400

    from utils.auth.connector_access import load_grants

    catalog = {c["id"]: c for c in _catalog(user_id, load_grants(user_id))}
    entry = catalog.get(connector)
    if entry is None:
        return jsonify({"error": "Unknown connector"}), 404
    allowed = _known_scopes(entry)

    grants = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            return jsonify({"error": "Invalid grant"}), 400
        scope_id = str(item.get("scopeId") or "")
        credential = str(item.get("credential") or "")
        role = str(item.get("role") or "")
        if role not in VALID_ROLES:
            return jsonify({"error": "Unknown role"}), 400
        if scope_id not in allowed or credential not in allowed[scope_id]:
            return jsonify({"error": "Unknown account or credential"}), 400
        key = (scope_id, role)
        # One credential per role per account. A second one would be ambiguous.
        if key in seen:
            return jsonify({"error": "A role can have one credential per account"}), 400
        seen.add(key)
        grants.append(Grant(connector, scope_id, credential, role))

    rows = grants if grants else [Grant(connector, "", "", RESTRICTED_MARKER)]
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[ConnectorAccess]")
                cur.execute(
                    "DELETE FROM org_connector_access WHERE org_id = %s AND connector = %s",
                    (org_id, connector),
                )
                if limited:
                    for grant in rows:
                        cur.execute(
                            """INSERT INTO org_connector_access
                                   (org_id, connector, scope_id, credential, role, updated_by)
                               VALUES (%s, %s, %s, %s, %s, %s)""",
                            (org_id, grant.connector, grant.scope_id, grant.credential,
                             grant.role, user_id),
                        )
                conn.commit()
    except Exception:
        logger.exception("Failed to save connector access for %s", sanitize(connector))
        return jsonify({"error": "Could not save connector access"}), 500

    logger.info(
        "Connector access for %s limited=%s grants=%d by user %s",
        sanitize(connector), limited, len(grants) if limited else 0, sanitize(user_id),
    )
    updated = {c["id"]: c for c in _catalog(user_id, load_grants(user_id))}
    return jsonify(updated.get(connector))
