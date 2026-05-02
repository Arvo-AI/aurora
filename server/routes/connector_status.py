"""Unified connector status endpoint.

Returns the live connection status for every provider in a single response,
so the frontend never has to scatter status calls across a dozen endpoints.

Every checker here mirrors the validation logic of its dedicated /status
route.  If a dedicated route makes a live API call, so does the checker
here.  This is the single source of truth for "is this provider actually
connected right now?"
"""

import base64
import logging
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict

import requests
from flask import Blueprint, jsonify

from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)
_LOG_PREFIX = "[ConnectorStatus]"

from utils.splunk_config import SPLUNK_SSL_VERIFY

connector_status_bp = Blueprint("connector_status", __name__)

LIVE_CHECK_TIMEOUT = 10
HTTP_TIMEOUT = (3.5, 5)


# ── Providers with live API validation ──────────────────────────────


def _check_grafana(user_id: str, org_id: str) -> Dict[str, Any]:
    """Grafana is webhook-based — check is_active directly (no secret_ref needed)."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                cursor.execute(
                    """SELECT 1 FROM user_tokens
                       WHERE (user_id = %s OR org_id = %s)
                         AND provider = 'grafana'
                         AND is_active = TRUE
                       LIMIT 1""",
                    (user_id, org_id),
                )
                return {"connected": cursor.fetchone() is not None}
    except Exception as exc:
        logger.warning("[STATUS] grafana check failed: %s", exc)
        return {"connected": False}


def _check_datadog(creds: Dict[str, Any]) -> Dict[str, Any]:
    api_key = creds.get("api_key")
    app_key = creds.get("app_key")
    if not api_key or not app_key:
        return {"connected": False}
    site = creds.get("site", "datadoghq.com")
    base_url = creds.get("base_url", "https://api.datadoghq.com")
    try:
        r = requests.get(
            f"{base_url}/api/v1/validate",
            headers={"DD-API-KEY": api_key, "DD-APPLICATION-KEY": app_key},
            timeout=HTTP_TIMEOUT,
        )
        data = r.json()
        if data.get("valid"):
            return {"connected": True, "site": site}
        return {"connected": False}
    except Exception:
        return {"connected": False}


def _check_ci_provider(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Jenkins and CloudBees share the same check."""
    base_url = creds.get("base_url")
    username = creds.get("username")
    api_token = creds.get("api_token")
    if not base_url or not username or not api_token:
        return {"connected": False}
    try:
        r = requests.get(
            f"{base_url}/api/json",
            auth=(username, api_token),
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return {
            "connected": True,
            "baseUrl": base_url,
            "username": username,
            "server": {
                "version": creds.get("version"),
                "mode": data.get("mode"),
            },
        }
    except Exception:
        return {"connected": False}


def _check_splunk(creds: Dict[str, Any]) -> Dict[str, Any]:
    api_token = creds.get("api_token")
    base_url = creds.get("base_url")
    if not api_token or not base_url:
        return {"connected": False}
    try:
        r = requests.get(
            f"{base_url}/services/server/info?output_mode=json",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=HTTP_TIMEOUT,
            verify=SPLUNK_SSL_VERIFY,
        )
        r.raise_for_status()
        return {"connected": True, "baseUrl": base_url}
    except Exception:
        return {"connected": False}


def _check_coroot(creds: Dict[str, Any]) -> Dict[str, Any]:
    url = creds.get("url")
    email = creds.get("email")
    password = creds.get("password")
    if not url:
        return {"connected": False}
    try:
        session = requests.Session()
        if email and password:
            session.post(
                f"{url}/api/login",
                json={"email": email, "password": password},
                timeout=HTTP_TIMEOUT,
            )
        r = session.get(f"{url}/api/projects", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return {"connected": True, "url": url}
    except Exception:
        return {"connected": False}


def _check_confluence(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors /confluence/status — validates via Atlassian API + OAuth refresh."""
    from connectors.confluence_connector.client import ConfluenceClient
    from connectors.confluence_connector.auth import refresh_access_token

    auth_type = (creds.get("auth_type") or "oauth").lower()
    base_url = creds.get("base_url")
    token = creds.get("pat_token") if auth_type == "pat" else creds.get("access_token")
    if not base_url or not token:
        return {"connected": False}

    cloud_id = creds.get("cloud_id") if auth_type == "oauth" else None
    try:
        client = ConfluenceClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)
        client.get_current_user()
        return {"connected": True, "baseUrl": base_url}
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 401 and auth_type == "oauth":
            refresh_tok = creds.get("refresh_token")
            if not refresh_tok:
                return {"connected": False}
            try:
                token_data = refresh_access_token(refresh_tok)
                new_access = token_data.get("access_token")
                if not new_access:
                    return {"connected": False}
                updated = dict(creds)
                updated["access_token"] = new_access
                if token_data.get("refresh_token"):
                    updated["refresh_token"] = token_data["refresh_token"]
                expires_in = token_data.get("expires_in")
                if expires_in:
                    updated["expires_at"] = int(_time.time()) + int(expires_in)
                uid = creds.get("_user_id")
                if uid:
                    store_tokens_in_db(uid, updated, "confluence")
                client = ConfluenceClient(base_url, new_access, auth_type=auth_type, cloud_id=cloud_id)
                client.get_current_user()
                return {"connected": True, "baseUrl": base_url}
            except Exception:
                return {"connected": False}
        return {"connected": False}
    except Exception:
        return {"connected": False}


def _check_jira(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors /atlassian/status for Jira — validates via Jira API + OAuth refresh."""
    from connectors.jira_connector.client import JiraClient
    from connectors.atlassian_auth.auth import refresh_access_token

    auth_type = (creds.get("auth_type") or "oauth").lower()
    base_url = creds.get("base_url")
    token = creds.get("pat_token") if auth_type == "pat" else creds.get("access_token")
    if not base_url or not token:
        return {"connected": False}

    cloud_id = creds.get("cloud_id") if auth_type == "oauth" else None
    try:
        client = JiraClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)
        client.get_myself()
        return {"connected": True, "baseUrl": base_url}
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 401 and auth_type == "oauth":
            refresh_tok = creds.get("refresh_token")
            if not refresh_tok:
                return {"connected": False}
            try:
                token_data = refresh_access_token(refresh_tok)
                new_access = token_data.get("access_token")
                if not new_access:
                    return {"connected": False}
                updated = dict(creds)
                updated["access_token"] = new_access
                if token_data.get("refresh_token"):
                    updated["refresh_token"] = token_data["refresh_token"]
                expires_in = token_data.get("expires_in")
                if expires_in:
                    updated["expires_at"] = int(_time.time()) + int(expires_in)
                uid = creds.get("_user_id")
                if uid:
                    store_tokens_in_db(uid, updated, "jira")
                client = JiraClient(base_url, new_access, auth_type=auth_type, cloud_id=cloud_id)
                client.get_myself()
                return {"connected": True, "baseUrl": base_url}
            except Exception:
                return {"connected": False}
        return {"connected": False}
    except Exception:
        return {"connected": False}


def _check_slack(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors GET /slack/ — validates via Slack auth.test API."""
    access_token = creds.get("access_token")
    if not access_token:
        return {"connected": False}
    try:
        r = requests.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 200 and r.json().get("ok"):
            return {"connected": True}
        return {"connected": False}
    except Exception:
        return {"connected": False}


def _check_google_chat(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Validates Google Chat connection via service account."""
    from connectors.google_chat_connector.client import get_chat_app_client

    if not creds.get("incidents_space_name"):
        return {"connected": False}
    try:
        return {"connected": get_chat_app_client() is not None}
    except Exception as e:
        logger.debug("Google Chat status check failed: %s", e)
        return {"connected": False}


def _check_github(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors /github/status — validates via GitHub user API."""
    access_token = creds.get("access_token")
    if not access_token:
        return {"connected": False}
    try:
        r = requests.get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {access_token}"},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            return {"connected": True}
        return {"connected": False}
    except Exception:
        return {"connected": False}


def _check_bitbucket(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors /bitbucket/status — validates via Bitbucket API + OAuth refresh."""
    access_token = creds.get("access_token")
    auth_type = creds.get("auth_type", "oauth")
    if not access_token:
        return {"connected": False}

    if auth_type == "oauth":
        try:
            from connectors.bitbucket_connector.oauth_utils import refresh_token_if_needed
            refreshed = refresh_token_if_needed(creds)
            if refreshed.get("access_token") != access_token:
                access_token = refreshed["access_token"]
                uid = creds.get("_user_id")
                if uid:
                    store_tokens_in_db(uid, refreshed, "bitbucket")
        except Exception as exc:
            logger.debug("[CONNECTOR_STATUS] Bitbucket token refresh failed, using existing token: %s", exc)

    try:
        from connectors.bitbucket_connector.api_client import BitbucketAPIClient
        client = BitbucketAPIClient(
            access_token=access_token,
            auth_type=auth_type,
            email=creds.get("email"),
        )
        user_data = client.get_current_user()
        if not user_data or user_data.get("error"):
            return {"connected": False}
        return {"connected": True}
    except Exception:
        return {"connected": False}


def _check_thousandeyes(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors /thousandeyes/status — validates via ThousandEyes API."""
    api_token = creds.get("api_token")
    if not api_token:
        return {"connected": False}
    try:
        from connectors.thousandeyes_connector.client import (
            ThousandEyesAPIError,
            get_thousandeyes_client,
        )
        uid = creds.get("_user_id") or "batch-check"
        account_group_id = creds.get("account_group_id")
        client = get_thousandeyes_client(
            uid, api_token=api_token, account_group_id=account_group_id,
        )
        client.get_account_status()
        return {"connected": True}
    except Exception:
        return {"connected": False}


def _check_scaleway(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors /scaleway/status — validates via Scaleway API."""
    secret_key = creds.get("secret_key")
    if not secret_key:
        return {"connected": False}
    try:
        from connectors.scaleway_connector.auth import get_account_info
        success, _, error = get_account_info(secret_key)
        if not success:
            return {"connected": False}
        return {"connected": True}
    except Exception:
        return {"connected": False}


def _check_ovh(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors /ovh/status — validates via OVH cloud/project API with OAuth refresh."""
    uid = creds.get("_user_id")
    if uid:
        try:
            from routes.ovh.oauth2_auth_code_flow import get_valid_access_token
            token_data = get_valid_access_token(uid)
            if token_data:
                access_token = token_data.get("access_token")
                endpoint = token_data.get("endpoint")
            else:
                return {"connected": False}
        except Exception:
            access_token = creds.get("access_token")
            endpoint = creds.get("endpoint")
    else:
        access_token = creds.get("access_token")
        endpoint = creds.get("endpoint")

    if not access_token or not endpoint:
        return {"connected": False}

    ovh_api_endpoints = {
        "ovh-eu": "https://eu.api.ovh.com/1.0",
        "ovh-us": "https://api.us.ovhcloud.com/1.0",
        "ovh-ca": "https://ca.api.ovh.com/1.0",
    }
    api_base_url = ovh_api_endpoints.get(endpoint)
    if not api_base_url:
        return {"connected": False}

    try:
        r = requests.get(
            f"{api_base_url}/cloud/project",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code in (401, 403):
            return {"connected": False}
        return {"connected": True, "endpoint": endpoint}
    except Exception:
        return {"connected": False}


def _check_sharepoint(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors /sharepoint/status — validates via Microsoft Graph API + OAuth refresh."""
    from connectors.sharepoint_connector.client import SharePointClient
    from connectors.sharepoint_connector.auth import refresh_access_token

    access_token = creds.get("access_token")
    if not access_token:
        return {"connected": False}

    try:
        client = SharePointClient(access_token)
        client.get_current_user()
        return {"connected": True}
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 401:
            refresh_tok = creds.get("refresh_token")
            if not refresh_tok:
                return {"connected": False}
            try:
                token_data = refresh_access_token(refresh_tok)
                new_access = token_data.get("access_token")
                if not new_access:
                    return {"connected": False}
                updated = dict(creds)
                updated["access_token"] = new_access
                if token_data.get("refresh_token"):
                    updated["refresh_token"] = token_data["refresh_token"]
                expires_in = token_data.get("expires_in")
                if expires_in:
                    updated["expires_at"] = int(_time.time()) + int(expires_in)
                uid = creds.get("_user_id")
                if uid:
                    store_tokens_in_db(uid, updated, "sharepoint")
                client = SharePointClient(new_access)
                client.get_current_user()
                return {"connected": True}
            except Exception:
                return {"connected": False}
        return {"connected": False}
    except Exception:
        return {"connected": False}


def _check_notion(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Validates Notion connection via GET /v1/users/me; handles OAuth refresh."""
    from connectors.notion_connector.client import NotionClient, NotionAuthExpiredError
    uid = creds.get("_user_id")
    if not uid:
        return {"connected": False}
    try:
        NotionClient(uid).get_self()
        return {
            "connected": True,
            "workspaceName": creds.get("workspace_name"),
            "authType": creds.get("type", "oauth"),
        }
    except NotionAuthExpiredError:
        return {"connected": False}
    except Exception:
        return {"connected": False}


def _check_spinnaker(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors /spinnaker/status — validates via Spinnaker Gate API."""
    try:
        from connectors.spinnaker_connector.client import get_spinnaker_client_for_user
        uid = creds.get("_user_id")
        if not uid:
            return {"connected": False}
        client = get_spinnaker_client_for_user(uid)
        if not client:
            return {"connected": False}
        client.get_credentials()
        return {"connected": True}
    except Exception:
        return {"connected": False}


def _check_pagerduty(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors /pagerduty status — validates with live API call + OAuth refresh."""
    from routes.pagerduty.pagerduty_helpers import PagerDutyClient, PagerDutyAPIError, validate_token

    auth_type = creds.get("auth_type", "api_token")
    if auth_type == "oauth":
        from routes.pagerduty.oauth_utils import refresh_token_if_needed
        success, refreshed = refresh_token_if_needed(creds)
        if success and refreshed:
            creds = {**creds, **refreshed}
            uid = creds.get("_user_id")
            if uid:
                try:
                    store_tokens_in_db(uid, creds, "pagerduty")
                except Exception as exc:
                    logger.debug("[CONNECTOR_STATUS] PagerDuty token persist failed (non-fatal): %s", exc)
        access_token = creds.get("access_token")
        if not access_token:
            return {"connected": False}
        try:
            validate_token(PagerDutyClient(oauth_token=access_token))
            return {"connected": True, "authType": "oauth"}
        except (PagerDutyAPIError, Exception):
            return {"connected": False}
    else:
        api_token = creds.get("api_token")
        if not api_token:
            return {"connected": False}
        try:
            validate_token(PagerDutyClient(api_token=api_token))
            return {"connected": True, "authType": "api_token"}
        except (PagerDutyAPIError, Exception):
            return {"connected": False}


def _check_opsgenie(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Validate OpsGenie / JSM Operations credentials."""
    auth_type = creds.get("auth_type", "opsgenie")

    # ── JSM Operations (Basic auth: email + api_token) ──────────────
    if auth_type == "jsm_basic":
        cloud_id = creds.get("cloud_id")
        email = creds.get("email")
        api_token = creds.get("api_token")
        if not cloud_id or not email or not api_token:
            return {"connected": False}

        url = f"https://api.atlassian.com/jsm/ops/api/{cloud_id}/v1/alerts"
        cred_str = f"{email}:{api_token}"
        encoded = base64.b64encode(cred_str.encode()).decode()
        headers = {"Authorization": f"Basic {encoded}", "Accept": "application/json"}

        try:
            r = requests.get(url, headers=headers, params={"limit": 1}, timeout=HTTP_TIMEOUT)
            if r.ok:
                return {
                    "connected": True,
                    "authType": auth_type,
                    "siteUrl": creds.get("site_url"),
                }
            return {"connected": False}
        except Exception:
            return {"connected": False}

    # ── OpsGenie GenieKey (unchanged) ────────────────────────────────
    api_key = creds.get("api_key")
    if not api_key:
        return {"connected": False}
    region = creds.get("region", "us")
    base_url = "https://api.eu.opsgenie.com" if region == "eu" else "https://api.opsgenie.com"
    try:
        r = requests.get(
            f"{base_url}/v2/account",
            headers={"Authorization": f"GenieKey {api_key}"},
            timeout=HTTP_TIMEOUT,
        )
        if r.ok:
            data = r.json().get("data", {})
            return {
                "connected": True,
                "region": region,
                "accountName": data.get("name"),
                "plan": data.get("plan", {}).get("name") if isinstance(data.get("plan"), dict) else None,
                "authType": auth_type,
            }
        return {"connected": False}
    except Exception:
        return {"connected": False}


def _check_dynatrace(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors /dynatrace/connect validation — live API call via token lookup."""
    api_token = creds.get("api_token")
    environment_url = creds.get("environment_url")
    if not api_token or not environment_url:
        return {"connected": False}
    try:
        from routes.dynatrace.dynatrace_routes import DynatraceClient
        client = DynatraceClient(environment_url, api_token)
        client.validate_connection()
        return {"connected": True, "environmentUrl": environment_url}
    except Exception:
        return {"connected": False}


def _check_bigpanda(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors /bigpanda/connect validation — live API call."""
    api_token = creds.get("api_token")
    if not api_token:
        return {"connected": False}
    try:
        from connectors.bigpanda_connector.api_client import BigPandaClient
        client = BigPandaClient(api_token)
        client.validate_token()
        return {"connected": True}
    except Exception:
        return {"connected": False}


def _check_tailscale(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Mirrors /tailscale/connect validation — live API call with token refresh."""
    client_id = creds.get("client_id")
    client_secret = creds.get("client_secret")
    if not client_id or not client_secret:
        return {"connected": False}
    try:
        from connectors.tailscale_connector.auth import get_valid_access_token
        success, access_token, error = get_valid_access_token(
            client_id, client_secret, creds.get("token_data"),
        )
        if not success or not access_token:
            return {"connected": False}
        return {"connected": True, "tailnet": creds.get("tailnet")}
    except Exception:
        return {"connected": False}


# ── Providers where credential-existence is sufficient ──────────────


def _check_credentials_only(creds: Dict[str, Any]) -> Dict[str, Any]:
    """For providers where having stored credentials is sufficient."""
    return {"connected": True}


def _check_gcp_credentials(creds: Dict[str, Any]) -> Dict[str, Any]:
    """GCP credential-existence check that also surfaces the auth mode
    (OAuth vs service-account vs WIF) so the frontend can label the connection.
    """
    from connectors.gcp_connector.auth import GCP_AUTH_TYPE_SA, GCP_AUTH_TYPE_WIF, get_gcp_auth_type

    auth_type = get_gcp_auth_type(creds)
    response: Dict[str, Any] = {"connected": True, "authType": auth_type}
    if auth_type == GCP_AUTH_TYPE_SA:
        if creds.get("client_email"):
            response["clientEmail"] = creds["client_email"]
        if creds.get("default_project_id"):
            response["defaultProjectId"] = creds["default_project_id"]
    elif auth_type == GCP_AUTH_TYPE_WIF:
        wif_config = creds.get("wif_config") or {}
        response["clientEmail"] = wif_config.get("sa_email", creds.get("email", ""))
        response["defaultProjectId"] = wif_config.get("project_id", creds.get("default_project_id", ""))
    return response


def _check_newrelic(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Validate New Relic credentials via NerdGraph user query."""
    api_key = creds.get("api_key")
    account_id = creds.get("account_id")
    if not api_key or not account_id:
        return {"connected": False}
    region = creds.get("region", "us")
    endpoint = "https://api.eu.newrelic.com/graphql" if region == "eu" else "https://api.newrelic.com/graphql"
    try:
        r = requests.post(
            endpoint,
            json={"query": "{ actor { user { email } } }"},
            headers={"Content-Type": "application/json", "API-Key": api_key},
            timeout=HTTP_TIMEOUT,
        )
        data = r.json()
        email = data.get("data", {}).get("actor", {}).get("user", {}).get("email")
        if email:
            return {"connected": True, "accountId": account_id, "region": region}
        return {"connected": False}
    except Exception:
        return {"connected": False}


def _check_netdata(creds: Dict[str, Any]) -> Dict[str, Any]:
    api_token = creds.get("api_token")
    if not api_token:
        return {"connected": False}
    return {"connected": True, "spaceName": creds.get("space_name")}


# ── Provider checker registry ──────────────────────────────────────


PROVIDER_CHECKERS = {
    # Live API validation
    "grafana": _check_grafana,
    "datadog": _check_datadog,
    "jenkins": _check_ci_provider,
    "cloudbees": _check_ci_provider,
    "splunk": _check_splunk,
    "coroot": _check_coroot,
    "confluence": _check_confluence,
    "jira": _check_jira,
    "slack": _check_slack,
    "google_chat": _check_google_chat,
    "github": _check_github,
    "bitbucket": _check_bitbucket,
    "thousandeyes": _check_thousandeyes,
    "scaleway": _check_scaleway,
    "ovh": _check_ovh,
    "sharepoint": _check_sharepoint,
    "notion": _check_notion,
    "spinnaker": _check_spinnaker,
    "pagerduty": _check_pagerduty,
    "opsgenie":      _check_opsgenie,
    "dynatrace": _check_dynatrace,
    "bigpanda": _check_bigpanda,
    "tailscale": _check_tailscale,
    # Credential-existence checks (no live API endpoint to validate against)
    "netdata": _check_netdata,
    "newrelic": _check_newrelic,
    "gcp": _check_gcp_credentials,
    "aws": _check_credentials_only,
    "azure": _check_credentials_only,
}


# ── Route + batch logic ─────────────────────────────────────────────


@connector_status_bp.route("/api/connectors/status", methods=["GET"])
@require_permission("connectors", "read")
def all_connector_status(user_id):
    org_id = get_org_id_from_request() or ""
    results = _check_all_connectors(user_id, org_id)
    return jsonify({"connectors": results})


def get_connected_count(user_id: str, org_id: str) -> int:
    """Return the number of connectors with a live connection."""
    results = _check_all_connectors(user_id, org_id)
    return sum(1 for c in results.values() if c.get("connected"))


def _check_all_connectors(user_id: str, org_id: str) -> Dict[str, Dict[str, Any]]:

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
            cursor.execute(
                """
                SELECT DISTINCT ON (provider) provider, user_id
                FROM user_tokens
                WHERE (user_id = %s OR org_id = %s)
                  AND secret_ref IS NOT NULL
                  AND is_active = TRUE
                ORDER BY provider, CASE WHEN user_id = %s THEN 0 ELSE 1 END
                """,
                (user_id, org_id, user_id),
            )
            providers = {row[0]: row[1] for row in cursor.fetchall()}

            cursor.execute(
                """
                SELECT DISTINCT ON (provider) provider
                FROM user_connections
                WHERE (user_id = %s OR org_id = %s)
                  AND status = 'active'
                ORDER BY provider, CASE WHEN user_id = %s THEN 0 ELSE 1 END
                """,
                (user_id, org_id, user_id),
            )
            for (prov,) in cursor.fetchall():
                if prov not in providers:
                    providers[prov] = user_id

    results: Dict[str, Dict[str, Any]] = {}

    def _run_check(provider: str, token_owner_id: str) -> tuple:
        if provider == "onprem":
            return provider, _check_onprem(user_id, org_id)
        if provider == "kubectl":
            return provider, _check_kubectl(user_id, org_id)
        if provider == "grafana":
            return provider, _check_grafana(user_id, org_id)
        creds = get_token_data(token_owner_id, provider)
        if not creds:
            with db_pool.get_admin_connection() as fallback_conn:
                with fallback_conn.cursor() as cur:
                    set_rls_context(cur, fallback_conn, user_id, log_prefix=_LOG_PREFIX)
                    cur.execute(
                        "SELECT 1 FROM user_connections WHERE (user_id = %s OR org_id = %s) AND provider = %s AND status = 'active' LIMIT 1",
                        (user_id, org_id, provider),
                    )
                    if cur.fetchone():
                        return provider, {"connected": True}
            return provider, {"connected": False}
        creds["_user_id"] = token_owner_id
        checker = PROVIDER_CHECKERS.get(provider, _check_credentials_only)
        try:
            return provider, checker(creds)
        except Exception as exc:
            logger.warning("[STATUS] %s check raised: %s", provider, exc)
            return provider, {"connected": False}

    providers.setdefault("onprem", user_id)
    providers.setdefault("kubectl", user_id)
    providers.setdefault("grafana", user_id)

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {
            pool.submit(_run_check, prov, owner): prov
            for prov, owner in providers.items()
        }
        for future in as_completed(futures):
            try:
                prov, status = future.result(timeout=LIVE_CHECK_TIMEOUT)
                results[prov] = status
            except Exception as exc:
                prov = futures[future]
                logger.warning("[STATUS] %s check timed out: %s", prov, exc)
                results[prov] = {"connected": False}

    return results


def _check_onprem(user_id: str, org_id: str) -> Dict[str, Any]:
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                cursor.execute(
                    """SELECT COUNT(*) FROM user_manual_vms
                       WHERE (user_id = %s OR org_id = %s)
                         AND connection_verified = TRUE""",
                    (user_id, org_id),
                )
                count = cursor.fetchone()[0]
        return {"connected": count > 0}
    except Exception as e:
        logger.warning("[STATUS] onprem check failed (user=%s, org=%s): %s", user_id, org_id, e, exc_info=True)
        return {"connected": False}


def _check_kubectl(user_id: str, org_id: str) -> Dict[str, Any]:
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                cursor.execute(
                    """SELECT COUNT(*) FROM active_kubectl_connections ac
                       JOIN kubectl_agent_tokens kat ON ac.token = kat.token
                       WHERE (kat.user_id = %s OR kat.org_id = %s) AND ac.status = 'active'""",
                    (user_id, org_id),
                )
                count = cursor.fetchone()[0]
        return {"connected": count > 0}
    except Exception as e:
        logger.warning("[STATUS] kubectl check failed (user=%s, org=%s): %s", user_id, org_id, e, exc_info=True)
        return {"connected": False}
