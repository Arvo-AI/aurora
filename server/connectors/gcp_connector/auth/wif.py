"""
Workload Identity Federation (WIF) authentication for customer GCP projects.

Aurora presents its own identity (via GKE pod token, SA key, or OIDC) to
Google's STS endpoint, which exchanges it for a short-lived federated token
scoped to the customer's project. The customer pre-configures a WIF pool
that trusts Aurora's identity.

This is the GCP equivalent of AWS STS AssumeRole.
"""

import json
import logging
import os
import tempfile
from typing import Dict, List, Optional

import google.auth.transport.requests
from google.oauth2 import service_account as google_service_account

logger = logging.getLogger(__name__)

GCP_AUTH_TYPE_WIF = "wif"

# Aurora's own identity configuration (set via environment)
_CREDENTIAL_SOURCE = os.getenv("AURORA_WIF_CREDENTIAL_SOURCE", "json_file")
_SA_KEY_PATH = os.getenv("AURORA_WIF_SA_KEY_PATH", "")
_SA_EMAIL = os.getenv("AURORA_WIF_SA_EMAIL", "")

_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _get_aurora_source_credentials() -> google_service_account.Credentials:
    """Build credentials representing Aurora's own identity.

    In production GKE with Workload Identity, the pod's projected SA token
    is consumed automatically by google-auth's default credential chain.
    For other deployments we load an explicit SA key file.
    """
    source = _CREDENTIAL_SOURCE.lower().strip()

    if source == "gke_metadata":
        import google.auth
        creds, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
        return creds

    if source == "json_file":
        if not _SA_KEY_PATH or not os.path.isfile(_SA_KEY_PATH):
            raise RuntimeError(
                "AURORA_WIF_SA_KEY_PATH is not set or file does not exist. "
                "Required when AURORA_WIF_CREDENTIAL_SOURCE=json_file."
            )
        return google_service_account.Credentials.from_service_account_file(
            _SA_KEY_PATH, scopes=[_CLOUD_PLATFORM_SCOPE]
        )

    raise ValueError(f"Unknown AURORA_WIF_CREDENTIAL_SOURCE: {source!r}")


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
    """Generate a short-lived access token via WIF impersonation.

    Uses Aurora's own credentials to call iamcredentials.generateAccessToken
    on the customer's SA (which the WIF pool authorises).

    Returns the same dict shape as generate_sa_access_token for drop-in
    compatibility: {access_token, expire_time, project_id, service_account_email, auth_type}.
    """
    wif_config = token_data.get("wif_config")
    if not wif_config:
        raise ValueError("Token data missing wif_config")

    target_sa = _resolve_sa_email(wif_config, mode)
    target_project = selected_project_id or wif_config.get("project_id")

    if selected_project_id:
        accessible = {
            p.get("project_id")
            for p in (token_data.get("accessible_projects") or [])
            if isinstance(p, dict)
        }
        if accessible and selected_project_id not in accessible:
            logger.info(
                "WIF: selected project %s not in accessible list; using default",
                selected_project_id,
            )
            target_project = wif_config.get("project_id")

    scopes = scopes or [_CLOUD_PLATFORM_SCOPE]

    source_creds = _get_aurora_source_credentials()
    if not source_creds.valid:
        source_creds.refresh(google.auth.transport.requests.Request())

    from googleapiclient.discovery import build

    iamcred = build("iamcredentials", "v1", credentials=source_creds)
    resp = iamcred.projects().serviceAccounts().generateAccessToken(
        name=f"projects/-/serviceAccounts/{target_sa}",
        body={"scope": scopes, "lifetime": "3600s"},
    ).execute()

    return {
        "access_token": resp["accessToken"],
        "expire_time": resp["expireTime"],
        "project_id": target_project,
        "service_account_email": target_sa,
        "auth_type": GCP_AUTH_TYPE_WIF,
    }


def verify_wif_access(wif_config: Dict) -> Dict:
    """Verify Aurora can federate into the customer's project.

    Attempts a token exchange then calls projects.get to confirm access.
    If org_id is present, enumerates all org projects instead of checking
    individual project IDs.
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
    """Write a Google external_account credential config JSON file.

    This file can be pointed at by GOOGLE_APPLICATION_CREDENTIALS and is
    natively understood by gcloud, gsutil, Terraform, and all Google client
    libraries. It tells the SDK to perform the STS token exchange on demand.

    For simplicity we use the 'file-sourced' credential type with a
    temporary file containing Aurora's own SA key, so the google-auth
    library can self-refresh without Aurora acting as a token broker.
    """
    wif_config = token_data.get("wif_config")
    if not wif_config:
        raise ValueError("Token data missing wif_config")

    target_sa = _resolve_sa_email(wif_config, mode)
    project_number = wif_config["project_number"]
    pool_id = wif_config.get("pool_id", "aurora-wif-pool")
    provider_id = wif_config.get("provider_id", "aurora-provider")

    audience = (
        f"//iam.googleapis.com/projects/{project_number}"
        f"/locations/global/workloadIdentityPools/{pool_id}"
        f"/providers/{provider_id}"
    )

    source = _CREDENTIAL_SOURCE.lower().strip()

    if source == "json_file":
        credential_source = {
            "file": _SA_KEY_PATH,
            "format": {"type": "json", "subject_token_field_name": "access_token"},
        }
    elif source == "gke_metadata":
        credential_source = {
            "url": "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token",
            "headers": {"Metadata-Flavor": "Google"},
            "format": {"type": "json", "subject_token_field_name": "access_token"},
        }
    else:
        raise ValueError(f"Cannot build credential config for source: {source!r}")

    config = {
        "type": "external_account",
        "audience": audience,
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": "https://sts.googleapis.com/v1/token",
        "service_account_impersonation_url": (
            f"https://iamcredentials.googleapis.com/v1/projects/-"
            f"/serviceAccounts/{target_sa}:generateAccessToken"
        ),
        "credential_source": credential_source,
    }

    kwargs = {"suffix": ".json", "prefix": "gcp_wif_cred_", "mode": "w", "delete": False}
    if target_dir:
        kwargs["dir"] = target_dir

    with tempfile.NamedTemporaryFile(**kwargs) as f:
        json.dump(config, f)
        return f.name
