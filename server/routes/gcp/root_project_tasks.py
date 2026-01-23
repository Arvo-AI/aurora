"""Async tasks for provisioning GCP root project dependencies."""

import logging

from celery_config import celery_app
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

    credentials = get_credentials(token_data)
    manager = RootProjectSetupManager(credentials)

    setup_result = manager.setup_service_accounts(user_id, project_id)

    store_user_preference(user_id, "gcp_service_accounts", setup_result)
    logger.info(
        "[RootProjectTask] Completed setup for user=%s project=%s", user_id, project_id
    )

    return setup_result
