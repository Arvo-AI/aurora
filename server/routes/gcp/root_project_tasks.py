"""Async tasks for provisioning GCP root project dependencies."""

import logging

from celery_config import celery_app
from connectors.gcp_connector.auth import GCP_AUTH_TYPE_SA, get_gcp_auth_type
from connectors.gcp_connector.auth.oauth import get_credentials
from routes.gcp.root_project_service import RootProjectSetupManager
from utils.auth.stateless_auth import get_credentials_from_db, store_user_preference

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="routes.gcp.root_project.setup_root_project_async")
def setup_root_project_async(self, user_id: str, project_id: str) -> dict:
    """Provision service accounts for the specified root project asynchronously."""
    logger.info(
        "[RootProjectTask] Starting setup for user=%s project=%s", user_id, project_id
    )

    token_data = get_credentials_from_db(user_id, "gcp")
    if not token_data:
        error_message = f"No GCP credentials found for user {user_id}"
        logger.error(error_message)
        raise ValueError(error_message)

    # Service-account mode: Aurora never created a per-user SA to provision
    # against the root project. The uploaded SA is already the working
    # identity and has whatever IAM roles the user granted it directly in
    # GCP. Skip the provisioning step entirely and record a minimal
    # preference entry so the UI can still read "root project" state.
    if get_gcp_auth_type(token_data) == GCP_AUTH_TYPE_SA:
        # Guard against persisting a project the uploaded SA cannot actually
        # see. accessible_projects is populated at SA connect time via
        # cloudresourcemanager.projects.list; anything outside that set would
        # push the failure into later gcloud/kubectl flows with no signal.
        accessible = token_data.get("accessible_projects") or []
        accessible_ids = {
            p.get("project_id") for p in accessible if isinstance(p, dict)
        }
        if accessible_ids and project_id not in accessible_ids:
            logger.error(
                "[RootProjectTask] SA mode — rejecting project %s for user=%s: not in accessible_projects",
                project_id,
                user_id,
            )
            raise ValueError(
                "Selected root project is not accessible to the uploaded service account."
            )

        setup_result = {
            "root_project": project_id,
            "auth_type": GCP_AUTH_TYPE_SA,
            "service_account_email": token_data.get("client_email"),
        }
        store_user_preference(user_id, "gcp_service_accounts", setup_result)
        # Persist the root project explicitly so the task is self-contained
        # and doesn't rely on the calling route having already stored it.
        store_user_preference(user_id, "gcp_root_project", project_id)
        logger.info(
            "[RootProjectTask] SA mode — skipping Aurora SA provisioning for user=%s project=%s",
            user_id,
            project_id,
        )
        return setup_result

    credentials = get_credentials(token_data)
    manager = RootProjectSetupManager(credentials)

    setup_result = manager.setup_service_accounts(user_id, project_id)

    store_user_preference(user_id, "gcp_service_accounts", setup_result)
    logger.info(
        "[RootProjectTask] Completed setup for user=%s project=%s", user_id, project_id
    )

    return setup_result
