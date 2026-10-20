"""Org page: which role may use each connector, account, and credential.

Blueprint: connector_access_bp
Prefix: /api/org
"""

from __future__ import annotations

import logging
import re

from flask import Blueprint, jsonify, request

from utils.auth import VALID_ROLES
from utils.auth.connector_access import RESTRICTED_MARKER, Grant, inherits_general
from utils.auth.rbac_decorators import require_auth_only, require_permission
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


def _iam_role_name(arn: object) -> str:
    text = str(arn or "")
    marker = ":role/"
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].rsplit("/", 1)[-1]


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
        scope = _scope(account, account, creds, "aws")
        names = {
            "write": _iam_role_name(conn.get("role_arn")),
            "read": _iam_role_name(conn.get("read_only_role_arn")),
        }
        for cred in scope["credentials"]:
            role_name = names.get(cred["id"])
            # ARN has no role name — keep "Modify role" / "Read-only role"
            if not role_name:
                continue
            kind = "Modify" if cred["id"] == "write" else "Read-only"
            cred["label"] = f"{kind} · {role_name}"
        scopes.append(scope)
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


def _overlay(catalog: list, team_rows: list) -> list:
    """Show a team's rules where it has saved any, and everyone elsewhere."""
    by_connector: dict = {}
    for grant in team_rows:
        by_connector.setdefault(grant.connector, []).append(grant)
    for entry in catalog:
        rows = by_connector.get(entry["id"])
        # No team rows means this connector still follows everyone
        if not rows:
            entry["inherited"] = True
            continue
        entry["inherited"] = False
        entry["limited"] = True
        entry["grants"] = [
            {"scopeId": g.scope_id, "credential": g.credential, "role": g.role}
            for g in rows if g.role != RESTRICTED_MARKER
        ]
    return catalog


def _open_grants(connector: str, entry: dict, team_id: str) -> list:
    """Every role on the preferred credential. That is a fully open override."""
    grants = []
    for scope in entry["scopes"]:
        creds = [c["id"] for c in scope["credentials"]]
        preferred = "write" if "write" in creds else (creds[0] if creds else "")
        for role in VALID_ROLES:
            grants.append(Grant(connector, scope["id"], preferred, role, team_id))
    return grants


def _grants_from_raw(connector: str, raw: list, allowed: dict, team_id: str):
    """Parsed grants, or an error message when a cell is not a real account."""
    grants = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            return None, "Invalid grant"
        scope_id = str(item.get("scopeId") or "")
        credential = str(item.get("credential") or "")
        role = str(item.get("role") or "")
        if role not in VALID_ROLES:
            return None, "Unknown role"
        if scope_id not in allowed or credential not in allowed[scope_id]:
            return None, "Unknown account or credential"
        key = (scope_id, role)
        # One credential per role per account. A second one would be ambiguous.
        if key in seen:
            return None, "A role can have one credential per account"
        seen.add(key)
        grants.append(Grant(connector, scope_id, credential, role, team_id))
    return grants, None


def _plan_save(connector: str, entry: dict, general: list, limited: bool, grants: list, team_id: str):
    """(store, rows). store False deletes this audience's rows for the connector."""
    general_rows = [g for g in general if g.connector == connector]
    store = limited
    # Matching everyone means this team has no rule of its own
    if team_id and inherits_general(general_rows, limited, grants):
        store = False
    elif team_id and not limited:
        grants = _open_grants(connector, entry, team_id)
        store = True
    rows = grants if grants else [Grant(connector, "", "", RESTRICTED_MARKER, team_id)]
    return store, rows


def _write_plan(cur, org_id: str, user_id: str, team_id: str, connector: str, store: bool, rows: list) -> None:
    cur.execute(
        """DELETE FROM org_connector_access
            WHERE org_id = %s AND connector = %s AND team_id = %s""",
        (org_id, connector, team_id),
    )
    if not store:
        return
    for grant in rows:
        cur.execute(
            """INSERT INTO org_connector_access
                   (org_id, team_id, connector, scope_id, credential, role, updated_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (org_id, team_id, grant.connector, grant.scope_id,
             grant.credential, grant.role, user_id),
        )


def _fresh_catalog(user_id: str, team_id: str) -> list:
    from utils.auth.connector_access import load_grants, load_team_grants

    fresh = _catalog(user_id, load_grants(user_id))
    if team_id:
        fresh = _overlay(fresh, load_team_grants(user_id, team_id))
    return fresh


def _team_in_org(cur, org_id: str, team_id: str) -> bool:
    cur.execute(
        "SELECT 1 FROM org_teams WHERE org_id = %s AND id = %s",
        (org_id, team_id),
    )
    return cur.fetchone() is not None


@connector_access_bp.route("/viewer-run", methods=["GET"])
@require_auth_only
def get_viewer_run(user_id: str):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No org context"}), 400
    from utils.auth.viewer_run import org_allows_viewers

    return jsonify({"chat": org_allows_viewers(org_id)})


@connector_access_bp.route("/viewer-run", methods=["PUT"])
@require_permission("admin", "access")
def put_viewer_run(user_id: str):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No org context"}), 400
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("chat"), bool):
        return jsonify({"error": "`chat` must be a boolean"}), 400
    from utils.auth.viewer_run import save_viewer_run

    try:
        save_viewer_run(org_id, user_id, body["chat"])
    except Exception:
        logger.exception("Failed to save viewer run settings")
        return jsonify({"error": "Could not save"}), 500
    logger.info("Viewer chat setting updated by user %s", sanitize(user_id))
    return jsonify({"chat": body["chat"]})


@connector_access_bp.route("/connector-access", methods=["GET"])
@require_permission("admin", "access")
def list_access(user_id: str):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No org context"}), 400
    from utils.auth.connector_access import load_grants, load_team_grants

    team_id = str(request.args.get("teamId") or "")
    general = load_grants(user_id)
    catalog = _catalog(user_id, general)
    if not team_id:
        return jsonify({"connectors": catalog})
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[ConnectorAccess]")
                if not _team_in_org(cur, org_id, team_id):
                    return jsonify({"error": "Unknown team"}), 404
    except Exception:
        logger.exception("Failed to load team connector access")
        return jsonify({"error": "Could not load connector access"}), 500
    return jsonify({"connectors": _overlay(catalog, load_team_grants(user_id, team_id))})


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

    team_id = str(body.get("teamId") or "")
    general = load_grants(user_id)
    catalog = {c["id"]: c for c in _catalog(user_id, general)}
    entry = catalog.get(connector)
    if entry is None:
        return jsonify({"error": "Unknown connector"}), 404
    grants, error = _grants_from_raw(connector, raw, _known_scopes(entry), team_id)
    if error:
        return jsonify({"error": error}), 400
    store, rows = _plan_save(connector, entry, general, limited, grants, team_id)
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[ConnectorAccess]")
                if team_id and not _team_in_org(cur, org_id, team_id):
                    return jsonify({"error": "Unknown team"}), 404
                _write_plan(cur, org_id, user_id, team_id, connector, store, rows)
                conn.commit()
    except Exception:
        logger.exception("Failed to save connector access for %s", sanitize(connector))
        return jsonify({"error": "Could not save connector access"}), 500

    logger.info(
        "Connector access saved for %s by user %s",
        sanitize(connector), sanitize(user_id),
    )
    updated = {c["id"]: c for c in _fresh_catalog(user_id, team_id)}
    return jsonify(updated.get(connector))


@connector_access_bp.route("/connector-access", methods=["PUT"])
@require_permission("admin", "access")
def save_access_batch(user_id: str):
    """One transaction for a whole column, so a dropped request cannot stop halfway."""
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No org context"}), 400
    body = request.get_json(silent=True) or {}
    updates = body.get("updates")
    if not isinstance(updates, list) or not updates or len(updates) > 200:
        return jsonify({"error": "`updates` must be a list of connector changes"}), 400
    team_id = str(body.get("teamId") or "")

    from utils.auth.connector_access import load_grants

    general = load_grants(user_id)
    catalog = {c["id"]: c for c in _catalog(user_id, general)}
    plans = []
    seen = set()
    for item in updates:
        if not isinstance(item, dict):
            return jsonify({"error": "Invalid update"}), 400
        connector = str(item.get("connector") or "")
        if not _CONNECTOR_ID.match(connector) or connector in seen:
            return jsonify({"error": "Unknown connector"}), 400
        seen.add(connector)
        if not isinstance(item.get("limited"), bool):
            return jsonify({"error": "`limited` must be a boolean"}), 400
        raw = item.get("grants") or []
        if not isinstance(raw, list):
            return jsonify({"error": "`grants` must be a list"}), 400
        entry = catalog.get(connector)
        if entry is None:
            return jsonify({"error": "Unknown connector"}), 404
        grants, error = _grants_from_raw(connector, raw, _known_scopes(entry), team_id)
        if error:
            return jsonify({"error": error}), 400
        store, rows = _plan_save(connector, entry, general, item["limited"], grants, team_id)
        plans.append((connector, store, rows))

    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[ConnectorAccess]")
                if team_id and not _team_in_org(cur, org_id, team_id):
                    return jsonify({"error": "Unknown team"}), 404
                for connector, store, rows in plans:
                    _write_plan(cur, org_id, user_id, team_id, connector, store, rows)
                conn.commit()
    except Exception:
        logger.exception("Failed to save connector access batch")
        return jsonify({"error": "Could not save connector access"}), 500

    logger.info(
        "Connector access batch connectors=%d team=%s by user %s",
        len(plans), bool(team_id), sanitize(user_id),
    )
    return jsonify({"connectors": _fresh_catalog(user_id, team_id)})
