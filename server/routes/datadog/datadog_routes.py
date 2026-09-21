import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import requests
from flask import Blueprint, jsonify, request

from routes.datadog.tasks import process_datadog_event
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize, hash_for_log
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, resolve_org_id, set_rls_context
from utils.secrets.secret_ref_utils import delete_user_secret
logger = logging.getLogger(__name__)

datadog_bp = Blueprint("datadog", __name__)

DATADOG_TIMEOUT = 20

_SITE_BASE_URLS: Dict[str, str] = {
    "datadoghq.com": "https://api.datadoghq.com",
    "us": "https://api.datadoghq.com",
    "us1": "https://api.datadoghq.com",
    "us1.datadoghq.com": "https://api.datadoghq.com",
    "datadoghq.eu": "https://api.datadoghq.eu",
    "eu": "https://api.datadoghq.eu",
    "us3": "https://api.us3.datadoghq.com",
    "us3.datadoghq.com": "https://api.us3.datadoghq.com",
    "us5": "https://api.us5.datadoghq.com",
    "us5.datadoghq.com": "https://api.us5.datadoghq.com",
    "ap1": "https://api.ap1.datadoghq.com",
    "ap1.datadoghq.com": "https://api.ap1.datadoghq.com",
    "ap2": "https://api.ap2.datadoghq.com",
    "ap2.datadoghq.com": "https://api.ap2.datadoghq.com",
    "gov": "https://api.ddog-gov.com",
    "ddog-gov.com": "https://api.ddog-gov.com",
}


class DatadogAPIError(Exception):
    """Custom error for Datadog API interactions."""


def _normalize_site(site: Optional[str]) -> Tuple[str, str]:
    """Normalize a user-supplied site string to a canonical site + base URL."""
    if not site:
        return "datadoghq.com", _SITE_BASE_URLS["datadoghq.com"]

    candidate = site.strip().lower()
    if not candidate:
        return "datadoghq.com", _SITE_BASE_URLS["datadoghq.com"]

    # Allow users to paste full URLs (https://api.datadoghq.com)
    if candidate.startswith("http://") or candidate.startswith("https://"):
        base_url = candidate.rstrip("/")
        return base_url.replace("https://", "").replace("http://", ""), base_url

    if candidate in _SITE_BASE_URLS:
        return candidate, _SITE_BASE_URLS[candidate]

    # Handle shorthand like "us5.datadoghq.com" → map to known host
    host_candidate = candidate.replace("https://", "").replace("http://", "")
    if host_candidate in _SITE_BASE_URLS:
        return host_candidate, _SITE_BASE_URLS[host_candidate]

    logger.warning("[DATADOG] Unknown site provided, defaulting to datadoghq.com")
    return candidate, _SITE_BASE_URLS["datadoghq.com"]


def _to_rfc3339(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class DatadogClient:
    def __init__(self, api_key: str, app_key: str, site: Optional[str] = None):
        self.api_key = api_key
        self.app_key = app_key
        self.site, self.base_url = _normalize_site(site)

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "DD-API-KEY": self.api_key,
            "DD-APPLICATION-KEY": self.app_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(method, url, headers=self.headers, timeout=DATADOG_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            logger.error("[DATADOG] %s %s network error: %s", method, url, exc)
            raise DatadogAPIError("Unable to reach Datadog") from exc

        if response.status_code == 429:
            logger.warning("[DATADOG] Rate limited on %s %s", method, path)
            raise DatadogAPIError("Datadog API rate limit reached. Please retry later.")

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            logger.error("[DATADOG] %s %s failed (%s): %s", method, url, response.status_code, response.text)
            raise DatadogAPIError(response.text or str(exc)) from exc

        return response

    def validate_credentials(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v1/validate").json()

    def _org_payload(self, path: str) -> Dict[str, Any]:
        """GET a JSON object, or {} if the call fails. Org lookups are best-effort:
        an unnamed org is recoverable, a failed /connect is not."""
        try:
            payload = self._request("GET", path).json()
        except DatadogAPIError:
            logger.debug("[DATADOG] Unable to fetch %s", path, exc_info=True)
            return {}
        return payload if isinstance(payload, dict) else {}

    def _current_org_from_v2(self) -> Optional[Dict[str, Any]]:
        """The org these keys belong to, per v2's current_org relationship.

        v2 is JSON:API: current_org points at a UUID and the matching entry in
        `included` carries the name and public_id.
        """
        payload = self._org_payload("/api/v2/org")
        relationships = (payload.get("data") or {}).get("relationships") or {}
        current_id = ((relationships.get("current_org") or {}).get("data") or {}).get("id")
        if not current_id:
            return None
        return next(
            (e.get("attributes") for e in payload.get("included") or []
             if isinstance(e, dict) and e.get("id") == current_id),
            None,
        )

    def get_org(self) -> Optional[Dict[str, Any]]:
        """Name and id of the org these keys belong to, or None if unprovable.

        GET /api/v1/org answers `{"orgs": [...]}` -- every org the key manages --
        while GET /api/v1/org/{public_id} answers `{"org": {...}}`. One entry is
        unambiguous. A parent key returns several and v1 never says which is
        current, so ask v2, whose current_org relationship names it. The label
        decides which org the agent queries, so guessing here is not an option,
        and without an id /connect cannot tell a key rotation from a new org.

        Returns name and id only. Both raw payloads carry more (v1 includes
        billing details and SAML config) and /status hands this to the browser.
        """
        payload = self._org_payload("/api/v1/org")
        org = payload.get("org") if isinstance(payload.get("org"), dict) else None
        if org is None:
            orgs = [o for o in payload.get("orgs") or [] if isinstance(o, dict)]
            org = orgs[0] if len(orgs) == 1 else (self._current_org_from_v2() if orgs else None)
        if not org:
            return None
        return {"name": org.get("name"), "id": org.get("public_id") or org.get("id")}

    def search_logs(self, query: str, start: str, end: str, limit: int, cursor: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "filter": {
                "query": query or "*",
                "from": start,
                "to": end,
            },
            "page": {
                "limit": max(1, min(limit, 500)),
            },
            "sort": "desc",
        }
        if cursor:
            payload["page"]["cursor"] = cursor

        return self._request("POST", "/api/v2/logs/events/search", json=payload).json()

    def query_metrics(self, query: str, start_ms: int, end_ms: int, interval: Optional[int] = None) -> Dict[str, Any]:
        attributes: Dict[str, Any] = {
            "formulas": [{"formula": "a"}],
            "queries": [
                {
                    "data_source": "metrics",
                    "name": "a",
                    "query": query,
                }
            ],
            "from": start_ms,
            "to": end_ms,
        }
        if interval:
            attributes["interval"] = interval

        body = {
            "data": {
                "type": "timeseries_request",
                "attributes": attributes,
            }
        }
        return self._request("POST", "/api/v2/query/timeseries", json=body).json()

    def list_events(self, start_ts: int, end_ts: int, params: Dict[str, Any]) -> Dict[str, Any]:
        query_params = {
            "start": start_ts,
            "end": end_ts,
        }
        if params.get("priority"):
            query_params["priority"] = params["priority"]
        if params.get("sources"):
            query_params["sources"] = params["sources"]
        if params.get("tags"):
            query_params["tags"] = params["tags"]

        return self._request("GET", "/api/v1/events", params=query_params).json()

    def list_monitors(self, params: Dict[str, Any]) -> Any:
        try:
            page = int(params.get("page", 0))
        except (TypeError, ValueError):
            page = 0

        try:
            page_size = int(params.get("page_size", 25))
        except (TypeError, ValueError):
            page_size = 25

        query_params = {
            "page": max(page, 0),
            "page_size": min(max(page_size, 1), 100),
            "group_states": params.get("group_states", "all"),
            "with_downtimes": str(params.get("with_downtimes", "true")).lower(),
        }
        if params.get("name"):
            query_params["name"] = params["name"]
        if params.get("tags"):
            query_params["monitor_tags"] = params["tags"]
        if params.get("status"):
            query_params["status"] = params["status"]

        return self._request("GET", "/api/v1/monitor", params=query_params).json()

    def search_traces(self, query: str, start: str, end: str, limit: int) -> Dict[str, Any]:
        """Search APM spans/traces via POST /api/v2/spans/events/search."""
        payload = {
            "data": {
                "attributes": {
                    "filter": {
                        "query": query or "*",
                        "from": start,
                        "to": end,
                    },
                    "page": {"limit": max(1, min(limit, 500))},
                    "sort": "desc",
                },
                "type": "search_request",
            }
        }
        return self._request("POST", "/api/v2/spans/events/search", json=payload).json()

    def list_hosts(self, query: Optional[str] = None, count: int = 100, from_ts: Optional[int] = None) -> Dict[str, Any]:
        """List infrastructure hosts via GET /api/v1/hosts."""
        params: Dict[str, Any] = {
            "count": max(0, min(count, 1000)),
            "include_muted_hosts_data": True,
            "include_hosts_metadata": True,
        }
        if query:
            params["filter"] = query
        if from_ts is not None:
            params["from"] = from_ts
        return self._request("GET", "/api/v1/hosts", params=params).json()

    def list_incidents(self, page_size: int = 25, page_offset: int = 0) -> Dict[str, Any]:
        """List Datadog incidents via GET /api/v2/incidents."""
        params: Dict[str, Any] = {
            "page[size]": max(1, min(page_size, 100)),
            "page[offset]": max(0, page_offset),
        }
        return self._request("GET", "/api/v2/incidents", params=params).json()


def _get_stored_datadog_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        data = get_token_data(user_id, "datadog")
        if data:
            return data

        # resolve_org_id() works in and out of request context (DB fallback);
        # get_org_id_from_request() raises "working outside of application
        # context" when called from Celery tasks.
        org_id = resolve_org_id(user_id)
        if not org_id:
            return None

        from utils.db.db_utils import connect_to_db_as_admin
        conn = connect_to_db_as_admin()
        cursor = conn.cursor()
        set_rls_context(cursor, conn, user_id, log_prefix="[Datadog:_get_stored_datadog_credentials]")
        cursor.execute(
            "SELECT user_id FROM user_tokens WHERE org_id = %s AND provider = 'datadog' AND is_active = TRUE AND secret_ref IS NOT NULL LIMIT 1",
            (org_id,)
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row:
            data = get_token_data(row[0], "datadog")
            return data or None

        return None
    except Exception as exc:
        logger.error("[DATADOG] Failed to retrieve credentials for user %s: %s", user_id, exc)
        return None


def _build_client_from_creds(creds: Dict[str, Any]) -> Optional[DatadogClient]:
    api_key = creds.get("api_key")
    app_key = creds.get("app_key")
    site = creds.get("site")
    if not api_key or not app_key:
        return None
    return DatadogClient(api_key=api_key, app_key=app_key, site=site)


def account_label(account: Dict[str, Any]) -> str:
    """Stable, user-facing name for one Datadog connection.

    Falls back to what Datadog itself reports, so a label is almost never needed:
    org_name is the friendly name, and org_id (public_id) is unique per org, which
    keeps two unlabelled orgs from colliding. Site is deliberately NOT in the
    chain -- every org on datadoghq.com would collide, and "datadoghq.com" reads
    like a deliberate label rather than a missing one.
    """
    label = account.get("label") or account.get("org_name") or account.get("org_id") or "default"
    return str(label).strip() or "default"


def _account_summary(account: Dict[str, Any]) -> Dict[str, Any]:
    """Credential-free view of one account, safe for API responses and the agent."""
    return {
        "label": account_label(account),
        "site": account.get("site"),
        "orgName": account.get("org_name"),
        "serviceAccountName": account.get("service_account_name"),
        "validatedAt": account.get("validated_at"),
    }


def list_datadog_accounts(user_id: str) -> list:
    """Every connected Datadog org, primary first.

    Each org needs its own API + application key pair, so unlike AWS (one role ARN
    per account, assumed on demand) and Azure (one service principal spanning
    subscriptions) there is no single credential covering them all, and therefore
    no use for user_connections rows.

    ponytail: all orgs live in one Vault blob under "accounts", with the primary
    also mirrored at the top level so store_tokens_in_db's datadog branch keeps
    finding org_name/site for its user_tokens display columns, the skill's
    connection_check keeps finding api_key, and the webhook's truthiness check
    keeps working. Ceiling: no per-org key rotation or RBAC, and the blob grows
    with org count. Upgrade path is user_connections rows plus one Vault secret
    per account.
    """
    creds = _get_stored_datadog_credentials(user_id)
    if not creds:
        return []
    accounts = creds.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        accounts = [creds]  # pre-multi-account blob: one bare credential dict
    return [
        a for a in accounts
        if isinstance(a, dict) and a.get("api_key") and a.get("app_key")
    ]


def select_datadog_account(accounts: list, selector: Optional[str]) -> Optional[Dict[str, Any]]:
    """Pick one account by label. No selector means the primary (first)."""
    if not accounts:
        return None
    if not selector or not str(selector).strip():
        return accounts[0]
    wanted = str(selector).strip().lower()
    return next((a for a in accounts if account_label(a).lower() == wanted), None)


def _resolve_client(user_id: str, selector: Optional[str] = None):
    """Resolve a client for one account, or a ready-to-return Flask error tuple."""
    accounts = list_datadog_accounts(user_id)
    if not accounts:
        return None, (jsonify({"error": "Datadog is not connected"}), 400)

    account = select_datadog_account(accounts, selector)
    if not account:
        known = ", ".join(account_label(a) for a in accounts)
        return None, (jsonify({"error": f"Unknown Datadog account. Connected: {known}"}), 400)

    client = _build_client_from_creds(account)
    if not client:
        return None, (jsonify({"error": "Stored Datadog credentials are incomplete"}), 400)
    return client, None


def _validate_account(account: Dict[str, Any]) -> Dict[str, Any]:
    """Summary of one account plus a live validity check of its keys."""
    summary = _account_summary(account)
    client = _build_client_from_creds(account)
    if not client:
        return {**summary, "valid": False, "error": "Stored Datadog credentials are incomplete"}
    try:
        valid = bool(client.validate_credentials().get("valid"))
    except DatadogAPIError as exc:
        logger.warning("[DATADOG] Validation failed for account %s: %s",
                       sanitize(summary["label"]), exc)
        return {**summary, "valid": False, "error": "Failed to validate stored Datadog credentials"}
    if not valid:
        return {**summary, "valid": False, "error": "Stored Datadog keys are no longer valid"}
    return {**summary, "valid": True}


def _store_accounts(user_id: str, accounts: list) -> None:
    """Persist the account list, mirroring the primary at the top level.

    The mirror is load-bearing: store_tokens_in_db's datadog branch reads
    org_name/org_id/site/service_account_name off the top level for its
    user_tokens display columns, and several callers still read api_key there.
    """
    stored = dict(accounts[0])
    stored["accounts"] = accounts
    store_tokens_in_db(user_id, stored, "datadog")


@datadog_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id):
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    api_key = payload.get("apiKey")
    app_key = payload.get("appKey")
    raw_site = payload.get("site")
    service_account = payload.get("serviceAccountName")
    label = payload.get("label")

    if not api_key or not isinstance(api_key, str):
        return jsonify({"error": "Datadog API key is required"}), 400
    if not app_key or not isinstance(app_key, str):
        return jsonify({"error": "Datadog application key is required"}), 400
    if label is not None and not isinstance(label, str):
        return jsonify({"error": "Datadog label must be a string"}), 400

    site, base_url = _normalize_site(raw_site)

    logger.info("[DATADOG] Connecting user %s to site=%s api_hash=%s app_hash=%s", sanitize(user_id), sanitize(site), hash_for_log(api_key), hash_for_log(app_key))

    client = DatadogClient(api_key=api_key, app_key=app_key, site=site)

    try:
        validation = client.validate_credentials()
        if not validation.get("valid"):
            logger.warning("[DATADOG] Validation failed for user %s: %s", sanitize(user_id), validation)
            return jsonify({"error": "Unable to validate Datadog credentials"}), 400
    except DatadogAPIError as exc:
        logger.error("[DATADOG] Credential validation failed for user %s: %s", sanitize(user_id), exc)
        return jsonify({"error": "Failed to validate Datadog credentials"}), 502

    org_data = None
    try:
        org_data = client.get_org()
    except DatadogAPIError as exc:
        logger.debug("[DATADOG] Org lookup failed for user %s: %s", sanitize(user_id), exc)

    token_payload = {
        "api_key": api_key,
        "app_key": app_key,
        "site": site,
        "base_url": base_url,
        "org_name": org_data.get("name") if org_data else None,
        "org_id": str(org_data.get("id")) if org_data and org_data.get("id") is not None else None,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "service_account_name": service_account,
    }
    if label and label.strip():
        token_payload["label"] = label.strip()

    # Upsert into the account list rather than replacing it. An org that alerts
    # from both a dev and a prod Datadog needs both reachable, and a second
    # /connect used to silently overwrite the first.
    existing = list_datadog_accounts(user_id)
    new_label = account_label(token_payload).lower()
    clash = next((a for a in existing if account_label(a).lower() == new_label), None)

    # A label collision is either a re-connect of the same org (rotating keys,
    # which should overwrite) or a different org that happens to resolve to the
    # same label (which must not). Labels are unreliable here: get_org() returns
    # None whenever the org cannot be proven, so org_name is absent and unrelated
    # orgs all fall through to "default". Datadog's own org id is the only
    # trustworthy identity, so compare on that and refuse whenever sameness cannot
    # be proven -- guessing wrong destroys the stored credentials of a working org.
    if clash is not None:
        incoming_org = token_payload.get("org_id")
        existing_org = clash.get("org_id")
        if not incoming_org or not existing_org or incoming_org != existing_org:
            return jsonify({
                "error": (
                    f"A different Datadog organization is already connected as "
                    f"'{account_label(clash)}'. Set a label to tell them apart."
                ),
                "conflictingLabel": account_label(clash),
                "accounts": [_account_summary(a) for a in existing],
            }), 409

    if clash is not None:
        # Replace in place. Appending would move a re-connected org to the end of
        # the list, silently promoting a different org to primary -- so rotating
        # prod's keys would quietly redirect every unqualified query to dev.
        accounts = [token_payload if a is clash else a for a in existing]
    else:
        accounts = [*existing, token_payload]
    replaced = clash is not None

    try:
        _store_accounts(user_id, accounts)
        logger.info(
            "[DATADOG] Stored credentials for user %s (site=%s, label=%s, accounts=%d, replaced=%s)",
            sanitize(user_id), sanitize(site), sanitize(account_label(token_payload)),
            len(accounts), replaced,
        )
    except Exception as exc:
        logger.exception("[DATADOG] Failed to store credentials: %s", exc)
        return jsonify({"error": "Failed to store Datadog credentials"}), 500

    response = {
        "success": True,
        "site": site,
        "baseUrl": base_url,
        "org": org_data,
        "serviceAccountName": service_account,
        "label": account_label(token_payload),
        "replaced": replaced,
        "accounts": [_account_summary(a) for a in accounts],
        "validated": True,
    }
    return jsonify(response)


@datadog_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    accounts = list_datadog_accounts(user_id)
    if not accounts:
        return jsonify({"connected": False, "accounts": []})

    # Validate every account, not just the primary: a revoked dev key must be
    # visible rather than hidden behind a healthy prod org.
    summaries = [_validate_account(account) for account in accounts]
    primary, primary_summary = accounts[0], summaries[0]

    # "connected" means any account works, so one dead org cannot make Datadog
    # read as disconnected and hide the others from the agent.
    any_valid = any(s["valid"] for s in summaries)

    response = {
        "connected": any_valid,
        "site": primary.get("site"),
        "baseUrl": primary.get("base_url"),
        "serviceAccountName": primary.get("service_account_name"),
        "validatedAt": primary.get("validated_at"),
        "label": primary_summary["label"],
        "accounts": summaries,
    }

    if not any_valid:
        response["error"] = primary_summary.get("error") or "Stored Datadog keys are no longer valid"
        return jsonify(response)

    if primary_summary["valid"]:
        client = _build_client_from_creds(primary)
        try:
            response["org"] = client.get_org() if client else None
        except DatadogAPIError:
            logger.debug("[DATADOG] Org lookup failed during status", exc_info=True)
            response["org"] = None

    return jsonify(response)


@datadog_bp.route("/disconnect", methods=["DELETE", "POST"])
@require_permission("connectors", "write")
def disconnect(user_id):
    # ?account=<label> removes one org. No selector keeps the original
    # remove-everything behaviour, so existing callers are unaffected.
    selector = request.args.get("account")
    try:
        if selector and selector.strip():
            accounts = list_datadog_accounts(user_id)
            if not select_datadog_account(accounts, selector):
                known = ", ".join(account_label(a) for a in accounts) or "none"
                return jsonify({"error": f"Unknown Datadog account. Connected: {known}"}), 404

            wanted = selector.strip().lower()
            remaining = [a for a in accounts if account_label(a).lower() != wanted]
            if remaining:
                _store_accounts(user_id, remaining)
                logger.info("[DATADOG] Removed account %s for user %s (%d remaining)",
                            sanitize(selector), sanitize(user_id), len(remaining))
                # Ingested events are not attributable to an account (datadog_events
                # has no such column), so they are kept while any org remains.
                return jsonify({
                    "success": True,
                    "message": "Datadog account removed",
                    "accounts": [_account_summary(a) for a in remaining],
                })
            # Last remaining account: fall through to the full teardown below.

        success, token_rows = delete_user_secret(user_id, "datadog")
        if not success:
            logger.warning("[DATADOG] Failed to clean up secrets during disconnect")

        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[DATADOG:disconnect]")
            cursor.execute(
                "DELETE FROM datadog_events WHERE user_id = %s",
                (user_id,)
            )
            event_rows = cursor.rowcount
            conn.commit()

        logger.info("[DATADOG] Disconnected provider (tokens=%s, events=%s)", token_rows, event_rows)
        return jsonify({
            "success": True,
            "message": "Datadog disconnected successfully",
            "tokensDeleted": token_rows,
            "eventsDeleted": event_rows,
            "accounts": [],
        })
    except Exception as exc:
        logger.exception("[DATADOG] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect Datadog"}), 500


@datadog_bp.route("/logs/search", methods=["POST"])
@require_permission("connectors", "read")
def search_logs(user_id):
    client, error = _resolve_client(user_id, request.args.get("account"))
    if error:
        return error

    body = request.get_json(force=True, silent=True) or {}
    query = body.get("query", "*")
    limit = int(body.get("limit") or 50)
    cursor = body.get("cursor")

    now = datetime.now(timezone.utc)
    default_from = now - timedelta(minutes=15)

    start_str = body.get("from") or _to_rfc3339(default_from)
    end_str = body.get("to") or _to_rfc3339(now)

    try:
        data = client.search_logs(query=query, start=start_str, end=end_str, limit=limit, cursor=cursor)
        return jsonify(data)
    except DatadogAPIError as exc:
        logger.error("[DATADOG] Log search failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to search Datadog logs"}), 502


@datadog_bp.route("/metrics/query", methods=["POST"])
@require_permission("connectors", "read")
def query_metrics(user_id):
    client, error = _resolve_client(user_id, request.args.get("account"))
    if error:
        return error

    body = request.get_json(force=True, silent=True) or {}
    query = body.get("query")
    if not query:
        return jsonify({"error": "query is required"}), 400

    to_ms = int(body.get("toMs") or int(datetime.now(timezone.utc).timestamp() * 1000))
    from_ms = int(body.get("fromMs") or (to_ms - 15 * 60 * 1000))
    interval = body.get("interval")
    if interval is not None:
        try:
            interval = int(interval)
        except (ValueError, TypeError):
            interval = None

    try:
        data = client.query_metrics(query=query, start_ms=from_ms, end_ms=to_ms, interval=interval)
        return jsonify(data)
    except DatadogAPIError as exc:
        logger.error("[DATADOG] Metrics query failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to query Datadog metrics"}), 502


@datadog_bp.route("/events", methods=["GET"])
@require_permission("connectors", "read")
def list_events(user_id):
    client, error = _resolve_client(user_id, request.args.get("account"))
    if error:
        return error

    args = request.args
    try:
        end = int(args.get("end", int(datetime.now(timezone.utc).timestamp())))
    except ValueError:
        end = int(datetime.now(timezone.utc).timestamp())
    try:
        start = int(args.get("start", end - 3600))
    except ValueError:
        start = end - 3600

    params = {
        "priority": args.get("priority"),
        "sources": args.get("sources"),
        "tags": args.get("tags"),
    }

    try:
        data = client.list_events(start_ts=start, end_ts=end, params=params)
        return jsonify(data)
    except DatadogAPIError as exc:
        logger.error("[DATADOG] List events failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to list Datadog events"}), 502


@datadog_bp.route("/monitors", methods=["GET"])
@require_permission("connectors", "read")
def list_monitors(user_id):
    client, error = _resolve_client(user_id, request.args.get("account"))
    if error:
        return error

    raw_params = {
        "page": request.args.get("page"),
        "page_size": request.args.get("page_size"),
        "group_states": request.args.get("group_states"),
        "with_downtimes": request.args.get("with_downtimes"),
        "name": request.args.get("name"),
        "tags": request.args.get("tags"),
        "status": request.args.get("status"),
    }

    try:
        data = client.list_monitors(params=raw_params)
        return jsonify(data)
    except DatadogAPIError as exc:
        logger.error("[DATADOG] List monitors failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to list Datadog monitors"}), 502


@datadog_bp.route("/events/ingested", methods=["GET"])
@require_permission("connectors", "read")
def list_ingested_events(user_id):
    org_id = get_org_id_from_request()
    limit = request.args.get("limit", default=50, type=int)
    offset = request.args.get("offset", default=0, type=int)
    status_filter = request.args.get("status")
    type_filter = request.args.get("event_type")

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[Datadog]")

            base_query = """
                SELECT id, event_type, event_title, status, scope, payload, received_at, created_at
                FROM datadog_events
                WHERE org_id = %s
            """
            params = [org_id]
            if status_filter:
                base_query += " AND status = %s"
                params.append(status_filter)
            if type_filter:
                base_query += " AND event_type = %s"
                params.append(type_filter)

            base_query += " ORDER BY received_at DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])

            cursor.execute(base_query, params)
            rows = cursor.fetchall()

            count_query = "SELECT COUNT(*) FROM datadog_events WHERE org_id = %s"
            count_params = [org_id]
            if status_filter:
                count_query += " AND status = %s"
                count_params.append(status_filter)
            if type_filter:
                count_query += " AND event_type = %s"
                count_params.append(type_filter)

            cursor.execute(count_query, count_params)
            total = cursor.fetchone()[0]

        events = []
        for row in rows:
            events.append({
                "id": row[0],
                "eventType": row[1],
                "title": row[2],
                "status": row[3],
                "scope": row[4],
                "payload": row[5],
                "receivedAt": row[6].isoformat() if row[6] else None,
                "createdAt": row[7].isoformat() if row[7] else None,
            })

        return jsonify({
            "events": events,
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    except Exception as exc:
        logger.exception("[DATADOG] Failed to list ingested events for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to load Datadog webhook events"}), 500


@datadog_bp.route("/webhook/<user_id>", methods=["POST"])
def webhook(user_id: str):
    if not user_id:
        logger.warning("[DATADOG] Webhook received without user_id")
        return jsonify({"error": "user_id is required"}), 400

    # Check if user has Datadog connected
    creds = get_token_data(user_id, "datadog")
    if not creds:
        logger.warning("[DATADOG] Webhook received for user %s with no Datadog connection", sanitize(user_id))
        return jsonify({"error": "Datadog not connected for this user"}), 404

    payload_text = request.get_data(as_text=True) or ""
    # Webhook signature verification removed for OSS version

    payload = request.get_json(silent=True) or {}
    metadata = {
        "headers": dict(request.headers),
        "remote_addr": request.remote_addr,
    }
    logger.info("[DATADOG] Received webhook for user %s type=%s", sanitize(user_id), sanitize(payload.get("event_type")))

    process_datadog_event.delay(payload, metadata, user_id)
    return jsonify({"received": True})


@datadog_bp.route("/webhook-url", methods=["GET"])
@require_permission("connectors", "read")
def webhook_url(user_id):
    # Use ngrok URL for development if available, otherwise use backend URL
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")

    # For development, prefer ngrok URL if available
    if ngrok_url and backend_url.startswith("http://localhost"):
        base_url = ngrok_url
    else:
        base_url = backend_url

    url = f"{base_url}/datadog/webhook/{user_id}"

    # The URL is per Aurora user, not per Datadog org, so a user with both a dev
    # and a prod Datadog connected must create this same webhook in each of them.
    instructions = [
        "1. Navigate to Integrations → Webhooks in Datadog.",
        "2. Create a new webhook with the URL above.",
        "3. (Optional) Configure a custom payload to include monitor or event context.",
        "4. Add the webhook to your monitors or event rules.",
        "5. Save and test the webhook to verify connectivity.",
        "6. Repeat these steps in every Datadog organization you connect to Aurora - this URL is shared across all of them.",
    ]

    return jsonify({
        "webhookUrl": url,
        "instructions": instructions,
        "perOrganization": True,
    })
