"""Unified connector status endpoint.

Returns the live connection status for every provider in a single response,
so the frontend never has to scatter status calls across a dozen endpoints.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Optional

import requests
from flask import Blueprint, jsonify

from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.auth.token_management import get_token_data
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

connector_status_bp = Blueprint("connector_status", __name__)

LIVE_CHECK_TIMEOUT = 10
HTTP_TIMEOUT = 8


def _check_grafana(creds: Dict[str, Any]) -> Dict[str, Any]:
    api_token = creds.get("api_token")
    base_url = creds.get("base_url")
    if not api_token or not base_url:
        return {"connected": False}
    try:
        r = requests.get(
            f"{base_url}/api/org",
            headers={"Authorization": f"Bearer {api_token}", "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return {"connected": True, "baseUrl": base_url}
    except Exception:
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


def _check_netdata(creds: Dict[str, Any]) -> Dict[str, Any]:
    api_token = creds.get("api_token")
    if not api_token:
        return {"connected": False}
    return {"connected": True, "spaceName": creds.get("space_name")}


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
            verify=False,
        )
        r.raise_for_status()
        return {"connected": True, "baseUrl": base_url}
    except Exception:
        return {"connected": False}


def _check_pagerduty(creds: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "connected": True,
        "displayName": creds.get("display_name", "PagerDuty"),
        "authType": creds.get("auth_type", "api_token"),
    }


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


def _check_credentials_only(creds: Dict[str, Any]) -> Dict[str, Any]:
    """For providers where having stored credentials is sufficient."""
    return {"connected": True}


PROVIDER_CHECKERS = {
    "grafana": _check_grafana,
    "datadog": _check_datadog,
    "jenkins": _check_ci_provider,
    "cloudbees": _check_ci_provider,
    "netdata": _check_netdata,
    "splunk": _check_splunk,
    "pagerduty": _check_pagerduty,
    "coroot": _check_coroot,
    "gcp": _check_credentials_only,
    "aws": _check_credentials_only,
    "azure": _check_credentials_only,
    "tailscale": _check_credentials_only,
    "scaleway": _check_credentials_only,
    "ovh": _check_credentials_only,
    "confluence": _check_credentials_only,
    "dynatrace": _check_credentials_only,
    "thousandeyes": _check_credentials_only,
    "bigpanda": _check_credentials_only,
    "slack": _check_credentials_only,
    "bitbucket": _check_credentials_only,
}


@connector_status_bp.route("/api/connectors/status", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def all_connector_status(user_id):
    org_id = get_org_id_from_request() or ""

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
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

    results: Dict[str, Dict[str, Any]] = {}

    def _run_check(provider: str, token_owner_id: str) -> tuple:
        if provider == "kubectl":
            return provider, _check_kubectl(org_id)
        creds = get_token_data(token_owner_id, provider)
        if not creds:
            return provider, {"connected": False}
        checker = PROVIDER_CHECKERS.get(provider, _check_credentials_only)
        try:
            return provider, checker(creds)
        except Exception as exc:
            logger.warning("[STATUS] %s check raised: %s", provider, exc)
            return provider, {"connected": False}

    providers.setdefault("kubectl", user_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
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

    if "aws" not in results or not results["aws"].get("connected"):
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT 1 FROM user_connections WHERE user_id = %s AND (org_id = %s OR org_id IS NULL) AND status = 'active' LIMIT 1",
                        (user_id, org_id),
                    )
                    if cursor.fetchone():
                        results["aws"] = {"connected": True}
        except Exception:
            pass

    return jsonify({"connectors": results})


def _check_kubectl(org_id: str) -> Dict[str, Any]:
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM active_kubectl_connections WHERE org_id = %s",
                    (org_id,),
                )
                count = cursor.fetchone()[0]
        return {"connected": count > 0}
    except Exception:
        return {"connected": False}
