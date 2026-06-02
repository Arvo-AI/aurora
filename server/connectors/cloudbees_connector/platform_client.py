"""
CloudBees Platform orchestration layer.

Combines Operations Center and Feature Management clients to provide
unified context for RCA investigations.
"""

import logging
from typing import Any, Dict, List, Optional

from connectors.cloudbees_connector.oc_client import CloudBeesOCClient
from connectors.cloudbees_connector.fm_client import CloudBeesFMClient

logger = logging.getLogger(__name__)


class CloudBeesPlatformClient:
    """Orchestration layer combining CloudBees OC and FM clients."""

    def __init__(
        self,
        oc_client: Optional[CloudBeesOCClient] = None,
        fm_client: Optional[CloudBeesFMClient] = None,
    ):
        self.oc_client = oc_client
        self.fm_client = fm_client

    def get_deployment_context(
        self, service: Optional[str] = None, time_window_hours: int = 24
    ) -> Dict[str, Any]:
        """Query OC for cross-controller recent builds.

        Returns deployment context including builds from all managed controllers.
        Gracefully degrades if OC client is not configured.
        """
        if self.oc_client is None:
            return {
                "status": "not_configured",
                "message": "Operations Center is not connected. Connect it in Connectors -> CloudBees to enable cross-controller queries.",
            }

        try:
            success, builds, error = self.oc_client.query_recent_builds_across_controllers(
                service=service, time_window_hours=time_window_hours
            )

            if not success:
                return {
                    "status": "error",
                    "message": error or "Failed to query Operations Center.",
                    "builds": [],
                }

            return {
                "status": "ok",
                "builds": builds,
                "build_count": len(builds),
                "time_window_hours": time_window_hours,
                "service_filter": service,
                "warnings": error,  # May contain partial errors from individual controllers
            }

        except Exception as e:
            logger.error("Failed to get deployment context from OC: %s", e)
            return {
                "status": "error",
                "message": "Unexpected error querying Operations Center.",
                "builds": [],
            }

    def get_flag_change_context(
        self, app_id: Optional[str] = None, time_window_hours: int = 24
    ) -> Dict[str, Any]:
        """Query FM for recent flag changes.

        Returns feature flag change context for incident correlation.
        Gracefully degrades if FM client is not configured.
        """
        if self.fm_client is None:
            return {
                "status": "not_configured",
                "message": "Feature Management is not connected. Connect it in Connectors -> CloudBees to enable flag change queries.",
            }

        try:
            if not app_id:
                # List all apps and get changes from each
                success, apps, error = self.fm_client.list_applications()
                if not success:
                    return {
                        "status": "error",
                        "message": error or "Failed to list Feature Management applications.",
                        "changes": [],
                    }

                all_changes: List[Dict] = []
                for app in apps[:10]:  # Cap at 10 apps
                    aid = app.get("id") or app.get("_id") or app.get("appId")
                    if not aid:
                        continue
                    ok, changes, _ = self.fm_client.get_recent_flag_changes(
                        aid, since_hours=time_window_hours
                    )
                    if ok and changes:
                        for change in changes:
                            change["_application"] = app.get("name") or aid
                        all_changes.extend(changes)

                return {
                    "status": "ok",
                    "changes": all_changes,
                    "change_count": len(all_changes),
                    "time_window_hours": time_window_hours,
                    "applications_queried": len(apps),
                }

            # Single app query
            success, changes, error = self.fm_client.get_recent_flag_changes(
                app_id, since_hours=time_window_hours
            )
            if not success:
                return {
                    "status": "error",
                    "message": error or "Failed to query flag changes.",
                    "changes": [],
                }

            return {
                "status": "ok",
                "changes": changes,
                "change_count": len(changes),
                "time_window_hours": time_window_hours,
                "app_id": app_id,
            }

        except Exception as e:
            logger.error("Failed to get flag change context from FM: %s", e)
            return {
                "status": "error",
                "message": "Unexpected error querying Feature Management.",
                "changes": [],
            }

    def get_full_rca_context(
        self, service: Optional[str] = None, time_window_hours: int = 24
    ) -> Dict[str, Any]:
        """Combine OC deployment context and FM flag change context for full RCA.

        Both sources are queried independently; failures in one do not block the other.
        """
        deployment_context = self.get_deployment_context(
            service=service, time_window_hours=time_window_hours
        )
        flag_context = self.get_flag_change_context(
            time_window_hours=time_window_hours
        )

        return {
            "deployments": deployment_context,
            "flag_changes": flag_context,
            "time_window_hours": time_window_hours,
            "service_filter": service,
        }
