"""Business logic for provisioning GCP root-project resources."""

import logging
from typing import Dict

from connectors.gcp_connector.auth.service_accounts import (
    ensure_aurora_full_access,
    _get_user_sa_suffix,
)


class RootProjectSetupManager:
    """Handles service-account provisioning for a chosen root project."""

    def __init__(self, credentials):
        self._credentials = credentials
        self._logger = logging.getLogger(self.__class__.__name__)

    def setup_service_accounts(self, user_id: str, project_id: str) -> Dict[str, str]:
        ensure_aurora_full_access(
            self._credentials,
            user_id,
            root_project_id_override=project_id,
        )

        # Generate user-specific SA emails
        user_suffix_full = _get_user_sa_suffix(user_id, "full")
        user_suffix_readonly = _get_user_sa_suffix(user_id, "readonly")
        sa_email_full = (
            f"aurora-{user_suffix_full}@{project_id}.iam.gserviceaccount.com"
        )
        sa_email_readonly = (
            f"aurora-{user_suffix_readonly}@{project_id}.iam.gserviceaccount.com"
        )

        self._logger.info(
            "Provisioned service accounts for user %s in project %s",
            user_id,
            project_id,
        )

        return {
            "root_project": project_id,
            "full_access_sa": sa_email_full,
            "read_only_sa": sa_email_readonly,
        }
