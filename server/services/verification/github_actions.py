"""GitHub Actions reads needed to pick up a verification run.

Extends the change-gating PR adapter rather than starting a second client, so
token minting, the shared HTTP session, pagination and error logging all stay
in one place (``services/change_gating/github_adapter.py``).

Needs the GitHub App's **Actions: read** permission, which the documented App
setup already grants.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from services.change_gating.github_adapter import GitHubPRAdapter

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30
_CHUNK_BYTES = 64 * 1024


class GitHubActionsClient(GitHubPRAdapter):
    """PR adapter plus the workflow-run and artifact endpoints."""

    def list_run_artifacts(self, run_id: int) -> List[Dict[str, Any]]:
        """GET the artifacts a workflow run uploaded (first 100; runs have a handful)."""
        path = f"/actions/runs/{int(run_id)}/artifacts"
        response = self._session.get(
            self._url(path),
            headers=self._headers(),
            params={"per_page": 100},
            timeout=_TIMEOUT_SECONDS,
        )
        self._raise_for_status(response, path)
        data = response.json()
        artifacts = data.get("artifacts") if isinstance(data, dict) else None
        return [a for a in artifacts or [] if isinstance(a, dict)]

    def download_artifact(self, artifact_id: int, max_bytes: int) -> Optional[bytes]:
        """GET an artifact's zip archive, or ``None`` if it exceeds ``max_bytes``.

        GitHub answers with a redirect to short-lived blob storage. ``requests``
        drops the ``Authorization`` header when a redirect changes host, so the
        installation token is not forwarded there.

        The body is streamed and abandoned at the cap: the size GitHub reports
        for an artifact is checked by the caller first, but that is a claim
        about the bytes, not a limit on them.
        """
        path = f"/actions/artifacts/{int(artifact_id)}/zip"
        response = self._session.get(
            self._url(path),
            headers=self._headers(),
            timeout=_TIMEOUT_SECONDS,
            stream=True,
        )
        try:
            self._raise_for_status(response, path)
            chunks: List[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                total += len(chunk)
                if total > max_bytes:
                    logger.warning(
                        "[Verification] artifact exceeds size cap: path=%s cap=%s",
                        path, max_bytes,
                    )
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            response.close()

    def list_pulls_for_commit(self, head_sha: str) -> List[Dict[str, Any]]:
        """GET the pull requests a commit belongs to.

        Fallback for pull requests from forks: GitHub leaves the
        ``pull_requests`` array of a ``workflow_run`` event empty for those.
        """
        return self._get_paginated(f"/commits/{head_sha}/pulls")
