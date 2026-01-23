"""Project selection strategies for Aurora's GCP workflows."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Dict, Optional

from connectors.gcp_connector.gcp.projects import (
    select_best_project,
    check_billing_enabled,
)
from googleapiclient.discovery import build


class ProjectSelectionStrategy(ABC):
    """Abstract strategy that resolves the root project for a user."""

    @abstractmethod
    def determine(
        self,
        credentials,
        projects: List[Dict],
        user_id: Optional[str] = None,
    ) -> str:
        """Return the project ID that should act as the root project."""


class DefaultProjectSelectionStrategy(ProjectSelectionStrategy):
    """Uses the existing heuristic (preference > billing enabled > first)."""

    def determine(self, credentials, projects: List[Dict], user_id: Optional[str] = None) -> str:
        return select_best_project(credentials, projects, user_id)


class StaticProjectSelectionStrategy(ProjectSelectionStrategy):
    """Always returns the provided project, validating access first."""

    def __init__(self, project_id: str):
        if not project_id:
            raise ValueError("project_id is required for StaticProjectSelectionStrategy")
        self._project_id = project_id

    def determine(self, credentials, projects: List[Dict], user_id: Optional[str] = None) -> str:
        project_ids = {proj.get("projectId") for proj in projects if proj.get("projectId")}
        if self._project_id not in project_ids:
            raise ValueError(
                f"Project {self._project_id} is not accessible for the authenticated user."
            )

        if not check_billing_enabled(credentials, self._project_id):
            raise ValueError(
                f"Project {self._project_id} does not have billing enabled."
            )

        crm_service = build("cloudresourcemanager", "v1", credentials=credentials)
        crm_service.projects().getIamPolicy(resource=self._project_id, body={}).execute()
        return self._project_id
