"""
CloudBees standalone controller fleet client.

For clients running multiple standalone CloudBees CI / Jenkins controllers
WITHOUT an Operations Center (CJOC). Operations Center is normally the
discovery and federation layer; without it, each controller is an autonomous
instance with its own URL and credentials (Jenkins API tokens are
controller-local and cannot be shared across instances).

This client takes a manually-registered list of controllers — each with its
own ``base_url``, ``username`` and ``api_token`` — and exposes the same
cross-controller surface that ``CloudBeesOCClient`` provides for OC users:
controller discovery and recent-build queries spanning all controllers.

Unlike ``CloudBeesOCClient`` (one shared credential, controllers discovered
live from OC), here the controller list IS the stored config and each
controller carries its own credentials.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from connectors.jenkins_connector.api_client import JenkinsClient

logger = logging.getLogger(__name__)

MAX_CONTROLLERS = 20
MAX_JOBS_PER_CONTROLLER = 50
MAX_BUILDS_PER_CONTROLLER = 5


class CloudBeesFleetClient:
    """Client for a manually-registered fleet of standalone controllers."""

    def __init__(self, controllers: List[Dict[str, Any]]):
        # Each controller dict: {id, name, base_url, username, api_token, status, last_error}
        self.controllers = controllers or []

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_controllers(self, reping: bool = False) -> Tuple[bool, List[Dict], Optional[str]]:
        """Return the stored controllers as {name, url, status, last_error}.

        When ``reping`` is True, each controller is re-validated and its status
        refreshed; otherwise the stored status is returned as-is (cheap).
        """
        result: List[Dict] = []
        for ctrl in self.controllers[:MAX_CONTROLLERS]:
            status = ctrl.get("status", "unknown")
            last_error = ctrl.get("last_error")
            if reping:
                ok, _, error = self.validate_controller(ctrl)
                status = "online" if ok else "offline"
                last_error = None if ok else error
            result.append({
                "id": ctrl.get("id"),
                "name": ctrl.get("name") or ctrl.get("base_url", "unknown"),
                "url": ctrl.get("base_url", ""),
                "status": status,
                "last_error": last_error,
            })
        return True, result, None

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def validate_controller(ctrl: Dict[str, Any]) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """Validate a single controller config by fetching its server info."""
        base_url = ctrl.get("base_url")
        username = ctrl.get("username")
        api_token = ctrl.get("api_token")
        if not base_url or not username or not api_token:
            return False, None, "Controller is missing base_url, username, or api_token."
        client = JenkinsClient(base_url=base_url, username=username, api_token=api_token)
        return client.get_server_info()

    # ------------------------------------------------------------------
    # Per-controller client resolution
    # ------------------------------------------------------------------

    def get_controller_client(self, controller_url: str) -> JenkinsClient:
        """Return a JenkinsClient for a controller identified by URL.

        The URL must match one of the registered controllers exactly (after
        normalising the trailing slash). This is the SSRF guard for fleet mode:
        we only ever talk to controllers the user explicitly registered.
        """
        target = (controller_url or "").rstrip("/")
        for ctrl in self.controllers:
            if (ctrl.get("base_url") or "").rstrip("/") == target:
                return JenkinsClient(
                    base_url=ctrl["base_url"].rstrip("/"),
                    username=ctrl.get("username", ""),
                    api_token=ctrl.get("api_token", ""),
                )
        raise ValueError(
            f"Controller URL '{controller_url}' is not a registered controller in this fleet."
        )

    # ------------------------------------------------------------------
    # Cross-controller queries
    # ------------------------------------------------------------------

    def query_recent_builds_across_controllers(
        self, service: Optional[str] = None, time_window_hours: int = 24
    ) -> Tuple[bool, List[Dict], Optional[str]]:
        """Query recent builds across all registered controllers."""
        import time as _time

        if not self.controllers:
            return True, [], None

        cutoff_ms = int((_time.time() - time_window_hours * 3600) * 1000)

        all_builds: List[Dict] = []
        errors: List[str] = []

        for controller in self.controllers[:MAX_CONTROLLERS]:
            ctrl_builds, ctrl_error = self._query_single_controller(
                controller, service, cutoff_ms
            )
            all_builds.extend(ctrl_builds)
            if ctrl_error:
                errors.append(ctrl_error)

        all_builds.sort(key=lambda b: b.get("timestamp", 0), reverse=True)
        return True, all_builds, "; ".join(errors) if errors else None

    def _query_single_controller(
        self, controller: Dict, service: Optional[str], cutoff_ms: int
    ) -> Tuple[List[Dict], Optional[str]]:
        """Query builds from a single controller. Returns (builds, error_msg)."""
        controller_url = controller.get("base_url")
        controller_name = controller.get("name") or controller_url or "unknown"
        if not controller_url:
            return [], None

        try:
            client = JenkinsClient(
                base_url=controller_url.rstrip("/"),
                username=controller.get("username", ""),
                api_token=controller.get("api_token", ""),
            )
            ok, jobs, _ = client.list_jobs()
            if not ok:
                return [], f"{controller_name}: Failed to query controller"

            if service:
                jobs = [j for j in jobs if service.lower() in (j.get("name", "") or "").lower()]

            builds: List[Dict] = []
            for job in jobs[:MAX_JOBS_PER_CONTROLLER]:
                builds.extend(
                    self._collect_job_builds(client, job, controller_name, controller_url.rstrip("/"), cutoff_ms)
                )
            return builds, None

        except Exception as e:
            logger.warning("Failed to query fleet controller %s: %s", controller_name, e)
            return [], f"{controller_name}: Failed to query controller"

    @staticmethod
    def _collect_job_builds(
        client: JenkinsClient, job: Dict, controller_name: str, controller_url: str, cutoff_ms: int
    ) -> List[Dict]:
        """Return recent builds for a single job, annotated with controller/job context."""
        job_name = job.get("name") or job.get("fullName")
        if not job_name:
            return []

        ok, job_builds, _ = client.list_builds(job_name, limit=MAX_BUILDS_PER_CONTROLLER)
        if not ok or not job_builds:
            return []

        collected = []
        for build in job_builds:
            if build.get("timestamp", 0) < cutoff_ms:
                continue
            build["_controller"] = controller_name
            build["_controller_url"] = controller_url
            build["_job"] = job_name
            collected.append(build)
        return collected
