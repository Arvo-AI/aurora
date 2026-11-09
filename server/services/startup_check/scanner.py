"""Startup Check scanner: repo files -> manifest index -> findings.

The GitHub fetch layer is deliberately separate from :func:`run_scan`:
``run_scan`` takes a plain ``{path: content}`` mapping so tests (and any
future source — a git clone, a tarball — day-1 no-install doors) drive
the exact same rule pipeline as the GitHub path.

Fetch discipline:
- One recursive tree call, then bounded file fetches (cap on count AND
  total bytes) so a huge repo can't turn the check into a crawl.
- Helm templates (``{{ ... }}``) are skipped: they aren't plain manifests
  and parsing them would produce garbage findings.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests
import yaml

from services.startup_check.context import UserContext
from services.startup_check.model import (
    K8sObject,
    RepoIndex,
    best_effort_line,
    make_finding,
)

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"

# Fetch budget — keeps a scan inside ~10-30s and inside rate limits.
_MAX_FILES = 60
_MAX_FILE_BYTES = 200 * 1024
_MAX_TOTAL_BYTES = 2 * 1024 * 1024

# Directories where Kubernetes manifests live, best first. When a repo has
# more YAML than the fetch budget, these paths win the budget — otherwise
# CI workflows (which sort before `k8s/` alphabetically) crowd the cap and
# the real manifests are never fetched (silent false-green).
_MANIFEST_DIR_HINTS = (
    "k8s/", "kubernetes/", "manifests/", "deploy/", "deploys/",
    "charts/", "overlays/", "kube/", "helm/", "kustomize/",
)
# YAML that can never be a K8s manifest — never spends budget on it.
_NEVER_MANIFEST_DIRS = (
    ".github/workflows/", ".gitlab/", "docs/", "doc/", "node_modules/",
)
_NEVER_MANIFEST_FILES = (
    "mkdocs.yml", "docker-compose.yml", "docker-compose.yaml",
    "docker-compose.override.yml", "docker-compose.override.yaml",
    "renovate.yml", "renovate.json5", ".yamllint", "readthedocs.yml",
)
_HELM_TEMPLATE_MARKERS = ("{{", "}}")


class StartupCheckError(RuntimeError):
    """Scan could not complete (auth, repo, network, rate limit)."""


def is_manifest_path(path: str) -> bool:
    """YAML manifests only — rules are Kubernetes-shaped."""
    lowered = path.lower()
    return lowered.endswith((".yaml", ".yml"))


def _manifest_priority(path: str) -> int:
    """Sort key: 0 = manifest-likely, 1 = unknown, excluded = never fetched.

    Called on already-YAML paths only; the never-manifest checks live here
    so list_yaml_files stays a single filter + sort.
    """
    lowered = path.lower()
    if lowered.startswith(_NEVER_MANIFEST_DIRS):
        return 2
    base = lowered.rsplit("/", 1)[-1]
    if base in _NEVER_MANIFEST_FILES:
        return 2
    if any(lowered.startswith(hint) or f"/{hint}" in lowered for hint in _MANIFEST_DIR_HINTS):
        return 0
    return 1


def looks_like_helm_template(content: str) -> bool:
    return all(marker in content for marker in _HELM_TEMPLATE_MARKERS)


def build_index(files: Dict[str, str]) -> RepoIndex:
    """Parse every YAML file into the manifest index.

    Unparseable files are counted and skipped — a broken YAML in a repo is
    a deploy-time problem, not something the check-engine light itself
    should crash on.
    """
    index = RepoIndex()
    for path, content in sorted(files.items()):
        if looks_like_helm_template(content):
            index.skipped_files.append(path)
            continue
        try:
            docs = list(yaml.safe_load_all(content))
        except yaml.YAMLError:
            index.skipped_files.append(path)
            continue
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            metadata = doc.get("metadata") or {}
            name = metadata.get("name")
            if not isinstance(kind, str) or not isinstance(name, str):
                continue
            obj = K8sObject(
                kind=kind,
                name=name,
                namespace=metadata.get("namespace"),
                manifest=doc,
                file_path=path,
            )
            obj.line = best_effort_line(content, obj)
            index.objects.append(obj)
    return index


def summarize(
    findings: List[Dict[str, Any]], index: RepoIndex, fetched_files: int
) -> Dict[str, Any]:
    """Assemble the final scan result (change-gating verdict shape)."""
    active = [f for f in findings if not f.get("dismissed")]
    high = sum(1 for f in active if f.get("severity") == "HIGH")
    medium = sum(1 for f in active if f.get("severity") == "MEDIUM")
    low = sum(1 for f in active if f.get("severity") == "LOW")
    if not index.objects:
        # Honest verdict: 0 manifests parsed means the rules never ran.
        # "SAFE" must mean *checked and clean*, not *nothing to check* —
        # otherwise this reads as a green pass on a car with no engine.
        summary = (
            f"No Kubernetes manifests found among the {fetched_files} YAML files "
            f"fetched — nothing to check. If the repo deploys to Kubernetes, "
            f"the manifests may live in a directory this scan didn't reach "
            f"(path prioritization fetches k8s/, manifests/, deploy/ first)."
        )
        verdict = "NO_MANIFESTS"
    elif not active:
        summary = (
            f"Check complete: {len(index.objects)} manifests scanned, no known incident "
            f"preconditions found. The engine light stays off."
        )
        verdict = "SAFE"
    else:
        top = active[0]
        summary = (
            f"{high} high, {medium} medium, {low} low findings across "
            f"{len(index.objects)} manifests scanned. Highest-severity: {top['title']}. "
            f"Each finding names the incident it could cause — dismiss any that are "
            f"created out-of-band to clear the code."
        )
        verdict = "RISKY"
    return {
        "verdict": verdict,
        "summary": summary,
        "counts": {"high": high, "medium": medium, "low": low},
        "scanned_manifests": len(index.objects),
        "skipped_files": len(index.skipped_files),
        "findings": findings,
    }


def run_scan(
    files: Dict[str, str], context: UserContext
) -> Dict[str, Any]:
    """Pure pipeline: file contents -> index -> rules -> verdict."""
    from services.startup_check.rules import run_all_rules

    index = build_index(files)
    findings = run_all_rules(index, context)
    result = summarize(findings, index, fetched_files=len(files))
    result["context_applied"] = not context.is_empty
    return result


# ---------------------------------------------------------------------------
# GitHub fetch layer
# ---------------------------------------------------------------------------


class GitHubFileFetcher:
    """Read-only GitHub REST access scoped to one user + repo.

    Uses the shared auth router (App install first, OAuth fallback) — no
    new credential plumbing. The token is never logged.
    """

    def __init__(self, user_id: str, repo_full_name: str):
        from utils.auth.github_auth_router import (
            NoGitHubAuthError,
            get_auth_for_user_repo,
            make_auth_header,
        )

        try:
            auth = get_auth_for_user_repo(user_id, repo_full_name)
        except NoGitHubAuthError as exc:
            raise StartupCheckError(
                "No GitHub credential available — connect GitHub first "
                "(OAuth is enough; the check is read-only)."
            ) from exc
        except Exception as exc:
            raise StartupCheckError(f"GitHub auth failed: {exc}") from exc
        self.repo_full_name = repo_full_name
        self._session = requests.Session()
        self._session.headers.update(make_auth_header(auth))

    def close(self) -> None:
        self._session.close()

    def _get(self, path: str, **kwargs) -> requests.Response:
        resp = self._session.get(f"{GITHUB_API_BASE}{path}", timeout=30, **kwargs)
        if resp.status_code == 404:
            raise StartupCheckError(f"Repository '{self.repo_full_name}' not found (or no access).")
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            raise StartupCheckError("GitHub rate limit reached — retry in a few minutes.")
        if resp.status_code in (401, 403):
            raise StartupCheckError("GitHub rejected the credential — reconnect GitHub.")
        resp.raise_for_status()
        return resp

    def default_branch(self) -> str:
        resp = self._get(f"/repos/{self.repo_full_name}")
        return resp.json().get("default_branch") or "main"

    def list_yaml_files(self) -> List[Dict[str, Any]]:
        """Recursive tree walk, YAML files only, size- and count-capped."""
        branch = self.default_branch()
        resp = self._get(
            f"/repos/{self.repo_full_name}/git/trees/{branch}", params={"recursive": "1"}
        )
        tree = resp.json().get("tree") or []
        truncated = bool(resp.json().get("truncated"))
        if truncated:
            logger.warning(
                "[STARTUP-CHECK] tree listing truncated for %s; scanning a subset",
                self.repo_full_name,
            )
        candidates = [
            entry
            for entry in tree
            if entry.get("type") == "blob"
            and is_manifest_path(entry.get("path", ""))
            and entry.get("size", 0) <= _MAX_FILE_BYTES
            and _manifest_priority(entry.get("path", "")) < 2
        ]
        # Manifest-likely paths first, then unknowns; stable within tiers.
        candidates.sort(key=lambda e: _manifest_priority(e.get("path", "")))
        return candidates[:_MAX_FILES]

    def fetch_contents(self, paths: List[str], branch: str) -> Dict[str, str]:
        """Fetch file bodies (raw), respecting the total-byte budget."""
        files: Dict[str, str] = {}
        total = 0
        for path in paths:
            try:
                resp = self._get(
                    f"/repos/{self.repo_full_name}/contents/{path}",
                    params={"ref": branch},
                    headers={"Accept": "application/vnd.github.raw"},
                )
            except StartupCheckError:
                # contents API rejects files > 1MB even with raw Accept; skip
                logger.info("[STARTUP-CHECK] skipping un-fetchable file %s", path)
                continue
            body = resp.text
            if len(body) > _MAX_FILE_BYTES or total + len(body) > _MAX_TOTAL_BYTES:
                logger.info("[STARTUP-CHECK] byte budget reached at %s", path)
                continue
            total += len(body)
            files[path] = body
        return files


def scan_github_repo(
    user_id: str, repo_full_name: str, context: UserContext
) -> Dict[str, Any]:
    """Fetch the repo's YAML manifests and run the check. Read-only."""
    fetcher = GitHubFileFetcher(user_id, repo_full_name)
    try:
        branch = fetcher.default_branch()
        candidates = fetcher.list_yaml_files()
        paths = [c["path"] for c in candidates]
        files = fetcher.fetch_contents(paths, branch)
    finally:
        fetcher.close()
    logger.info(
        "[STARTUP-CHECK] scanning %s@%s: %d/%d yaml files fetched",
        repo_full_name, branch, len(files), len(candidates),
    )
    result = run_scan(files, context)
    result["repo_full_name"] = repo_full_name
    result["branch"] = branch
    result["scanned_file_count"] = len(files)
    return result
