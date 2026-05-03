"""
Workload Identity Federation (WIF) authentication for customer GCP projects.

Flow:
  1. Aurora obtains a Google-signed ID token for its own SA, targeting the
     customer's WIF pool as the audience.
  2. The ID token is exchanged at Google's STS endpoint for a short-lived
     federated access token (the WIF pool validates the OIDC token and
     enforces the attribute_condition).
  3. The federated token impersonates the customer's SA via
     iamcredentials.generateAccessToken.

This is the GCP equivalent of AWS STS AssumeRole.
"""

import json
import logging
import os
import tempfile
from typing import Dict, List, Optional

import google.auth.transport.requests
import requests as http_requests
from google.oauth2 import service_account as google_service_account

logger = logging.getLogger(__name__)

GCP_AUTH_TYPE_WIF = "wif"

_CREDENTIAL_SOURCE = os.getenv("AURORA_WIF_CREDENTIAL_SOURCE", "json_file")
_SA_KEY_PATH = os.getenv("AURORA_WIF_SA_KEY_PATH", "")
_SA_EMAIL = os.getenv("AURORA_WIF_SA_EMAIL", "")

_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_STS_ENDPOINT = "https://sts.googleapis.com/v1/token"


def _wif_audience(wif_config: Dict) -> str:
    """Build the full WIF pool provider audience URI."""
    return (
        f"//iam.googleapis.com/projects/{wif_config['project_number']}"
        f"/locations/global/workloadIdentityPools/{wif_config.get('pool_id', 'aurora-wif-pool')}"
        f"/providers/{wif_config.get('provider_id', 'aurora-provider')}"
    )


def _get_aurora_id_token(audience: str) -> str:
    """Obtain a Google-signed OIDC ID token for Aurora's SA.

    On GKE with Workload Identity the metadata server provides one directly.
    With a SA key file we call iamcredentials.generateIdToken.
    """
    source = _CREDENTIAL_SOURCE.lower().strip()

    if source == "gke_metadata":
        from google.auth.transport.requests import Request
        from google.auth import compute_engine
        id_creds = compute_engine.IDTokenCredentials(
            request=Request(), target_audience=audience,
            use_metadata_identity_endpoint=True,
        )
        id_creds.refresh(Request())
        return id_creds.token

    if source == "json_file":
        if not _SA_KEY_PATH or not os.path.isfile(_SA_KEY_PATH):
            raise RuntimeError(
                "AURORA_WIF_SA_KEY_PATH is not set or file does not exist. "
                "Required when AURORA_WIF_CREDENTIAL_SOURCE=json_file."
            )
        sa_creds = google_service_account.Credentials.from_service_account_file(
            _SA_KEY_PATH, scopes=[_CLOUD_PLATFORM_SCOPE],
        )
        sa_creds.refresh(google.auth.transport.requests.Request())

        from googleapiclient.discovery import build
        iam = build("iamcredentials", "v1", credentials=sa_creds)
        resp = iam.projects().serviceAccounts().generateIdToken(
            name=f"projects/-/serviceAccounts/{_SA_EMAIL}",
            body={"audience": audience, "includeEmail": True},
        ).execute()
        return resp["token"]

    raise ValueError(f"Unknown AURORA_WIF_CREDENTIAL_SOURCE: {source!r}")


def _sts_exchange(id_token: str, audience: str) -> str:
    """Exchange a Google-signed ID token for a federated access token via STS."""
    resp = http_requests.post(_STS_ENDPOINT, json={
        "grantType": "urn:ietf:params:oauth:grant-type:token-exchange",
        "audience": audience,
        "scope": _CLOUD_PLATFORM_SCOPE,
        "requestedTokenType": "urn:ietf:params:oauth:token-type:access_token",
        "subjectTokenType": "urn:ietf:params:oauth:token-type:id_token",
        "subjectToken": id_token,
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def _impersonate_sa(federated_token: str, target_sa: str, scopes: List[str]) -> Dict:
    """Use a federated token to generate a short-lived access token for a customer SA."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    fed_creds = Credentials(token=federated_token)
    iam = build("iamcredentials", "v1", credentials=fed_creds)
    return iam.projects().serviceAccounts().generateAccessToken(
        name=f"projects/-/serviceAccounts/{target_sa}",
        body={"scope": scopes, "lifetime": "3600s"},
    ).execute()


def _resolve_sa_email(wif_config: Dict, mode: str = "agent") -> str:
    """Pick the target SA email based on mode (agent vs ask/viewer)."""
    if mode == "ask":
        viewer = wif_config.get("viewer_sa_email")
        if viewer:
            return viewer
        logger.warning("WIF: no viewer_sa_email configured; falling back to agent SA")
    return wif_config["sa_email"]


def get_wif_access_token(
    token_data: Dict,
    scopes: Optional[List[str]] = None,
    selected_project_id: Optional[str] = None,
    mode: str = "agent",
) -> Dict:
    """Generate a short-lived access token via the full WIF flow.

    1. Obtain a Google-signed ID token for Aurora targeting the customer's WIF pool
    2. Exchange it at Google STS for a federated access token
    3. Use the federated token to impersonate the customer's SA

    Returns: {access_token, expire_time, project_id, service_account_email, auth_type}
    """
    wif_config = token_data.get("wif_config")
    if not wif_config:
        raise ValueError("Token data missing wif_config")

    target_sa = _resolve_sa_email(wif_config, mode)
    target_project = selected_project_id or wif_config.get("project_id")
    scopes = scopes or [_CLOUD_PLATFORM_SCOPE]
    audience = _wif_audience(wif_config)

    id_token = _get_aurora_id_token(audience)
    federated_token = _sts_exchange(id_token, audience)
    resp = _impersonate_sa(federated_token, target_sa, scopes)

    return {
        "access_token": resp["accessToken"],
        "expire_time": resp["expireTime"],
        "project_id": target_project,
        "service_account_email": target_sa,
        "auth_type": GCP_AUTH_TYPE_WIF,
    }


def verify_wif_access(wif_config: Dict) -> Dict:
    """Verify Aurora can federate into the customer's project.

    Runs the full WIF flow then calls CRM to confirm access.
    Returns {ok: bool, error?: str, projects?: list}.
    """
    try:
        token_data = {"wif_config": wif_config}
        result = get_wif_access_token(token_data, mode="agent")
        access_token = result["access_token"]

        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials

        creds = Credentials(token=access_token)
        crm = build("cloudresourcemanager", "v1", credentials=creds)

        org_id = wif_config.get("org_id")
        if org_id:
            verified_projects = _enumerate_org_projects(crm, org_id)
        else:
            verified_projects = _verify_explicit_projects(crm, wif_config)

        return {"ok": True, "projects": verified_projects}
    except Exception as e:
        logger.error("WIF verification failed: %s", e)
        return {"ok": False, "error": str(e)}


def _enumerate_org_projects(crm, org_id: str) -> list:
    """List all ACTIVE projects in an organization."""
    projects = []
    req = crm.projects().list(filter=f"parent.id:{org_id} lifecycleState:ACTIVE")
    while req:
        resp = req.execute()
        for p in resp.get("projects", []):
            projects.append({
                "project_id": p.get("projectId"),
                "name": p.get("name"),
            })
        req = crm.projects().list_next(req, resp)
    return projects


def _verify_explicit_projects(crm, wif_config: Dict) -> list:
    """Verify access to explicitly listed project IDs."""
    project_id = wif_config["project_id"]
    project_info = crm.projects().get(projectId=project_id).execute()

    verified = [{
        "project_id": project_info.get("projectId"),
        "name": project_info.get("name"),
    }]

    for pid in (wif_config.get("additional_project_ids") or []):
        try:
            info = crm.projects().get(projectId=pid).execute()
            verified.append({
                "project_id": info.get("projectId"),
                "name": info.get("name"),
            })
        except Exception as e:
            logger.warning("WIF: cannot access additional project %s: %s", pid, e)

    return verified


def write_credential_config_file(token_data: Dict, target_dir: Optional[str] = None, mode: str = "agent") -> str:
    """Write a credential file usable by GOOGLE_APPLICATION_CREDENTIALS.

    Fetches a short-lived access token via the full WIF flow and writes it
    as a simple JSON file. The primary auth path for gcloud/gsutil is the
    CLOUDSDK_AUTH_ACCESS_TOKEN env var (set by the caller); this file
    serves as a fallback for tools that only read GOOGLE_APPLICATION_CREDENTIALS.
    """
    wif_config = token_data.get("wif_config")
    if not wif_config:
        raise ValueError("Token data missing wif_config")

    result = get_wif_access_token(token_data, mode=mode)

    cred_path = os.path.join(
        target_dir or tempfile.gettempdir(),
        f"gcp_wif_cred_{mode}.json",
    )
    payload = {
        "type": "access_token",
        "access_token": result["access_token"],
        "project_id": result.get("project_id", wif_config.get("project_id", "")),
    }
    fd = os.open(cred_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    return cred_path


def get_aurora_sa_email() -> str:
    """Return Aurora's WIF SA email (for display in setup instructions)."""
    return _SA_EMAIL
