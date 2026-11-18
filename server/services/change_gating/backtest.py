"""Revert backtest: would Aurora's pre-merge review have caught PRs a repo later reverted?

Zero-install time-to-value for pre-merge review. Given a PUBLIC GitHub repo,
find merged PRs that were later reverted (positives) plus merged PRs with no
revert found (negative controls), then review each one exactly as it looked
before merge.

Hindsight isolation (the backtest must not be able to cheat):
- The model sees only the PR title, author, branch names and the merge-time
  file diff. The PR body is omitted because it can be edited after merge
  ("this broke prod, reverted in #123"); comments, reviews, labels and the
  timeline are never fetched.
- The review is a single tool-free LLM call, not the agentic run, so it cannot
  read the repo at HEAD, search the web, or query incident memory, any of
  which could surface the revert itself.

GitHub budget: ``find_candidates`` costs 1 repo lookup + 2 search calls (+1 per revert that
names a commit SHA instead of a PR); ``review_pr`` costs 2 REST calls. A full
run of 5 PRs fits well inside the 60 req/hr unauthenticated limit.
``GITHUB_TOKEN`` (optional) raises the limit; private repos are refused so a
server token can never expose private code through this endpoint.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
_TIMEOUT_SECONDS = 20
_MAX_FILES = 100
# Merged long enough ago that a revert, if one was coming, has likely landed.
_CONTROL_MIN_AGE_DAYS = 30

_REPO_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?(?:[#?].*)?$"
)
_SHORT_RE = re.compile(r"^(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/(?P<repo>[A-Za-z0-9._-]{1,100})$")
_REVERTS_PR_RE = re.compile(r"\bReverts\s+(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<num>\d+)", re.IGNORECASE)
_TITLE_PR_RE = re.compile(r"\(#(?P<num>\d+)\)")
_REVERTS_SHA_RE = re.compile(r"\bThis reverts commit (?P<sha>[0-9a-f]{7,40})", re.IGNORECASE)

_BACKTEST_NOTE = """BACKTEST MODE (overrides the tool list above):
This PR was merged in the past. You are reviewing it exactly as it looked
before merge. You have NO tools and NO live signals in this run: no monitoring,
no deploy history, no repository access beyond the diff below. Judge only from
the diff, and do not claim to have checked anything else. Apply the same
VERDICT DECISION TEST and respond with the same JSON format."""


class BacktestError(Exception):
    """User-facing failure (bad URL, private repo, rate limit, not found)."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def parse_repo_url(value: str) -> Tuple[str, str]:
    """Parse ``https://github.com/owner/repo`` or ``owner/repo``; reject anything else."""
    text = (value or "").strip()
    match = _REPO_URL_RE.match(text) or _SHORT_RE.match(text)
    if not match:
        raise BacktestError("Enter a public GitHub repository, e.g. https://github.com/owner/repo")
    repo = match.group("repo")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if repo in (".", ".."):
        raise BacktestError("Invalid repository name")
    return match.group("owner"), repo


def extract_reverted_ref(
    title: str, body: str, repo_full_name: str
) -> Tuple[Optional[int], Optional[str]]:
    """Return ``(pr_number, sha)`` the revert PR points at; either may be None.

    Handles GitHub's revert button (``Reverts owner/repo#123``), squash-merge
    titles (``Revert "Add cache (#123)"``) and ``git revert`` bodies
    (``This reverts commit <sha>``).
    """
    body = body or ""
    for m in _REVERTS_PR_RE.finditer(body):
        ref_repo = m.group("repo")
        if ref_repo is None or ref_repo.lower() == repo_full_name.lower():
            return int(m.group("num")), None
    quoted = re.search(r'Revert\s+"(.+)"', title or "", re.IGNORECASE)
    if quoted:
        m = _TITLE_PR_RE.search(quoted.group(1))
        if m:
            return int(m.group("num")), None
    m = _REVERTS_SHA_RE.search(body)
    if m:
        return None, m.group("sha").lower()
    return None, None


def is_revert_title(title: str) -> bool:
    """A revert, but not a revert-of-a-revert (that one re-lands the change)."""
    t = (title or "").strip().lower()
    return t.startswith("revert") and not t.startswith(('revert "revert', "revert 'revert"))


class PublicGitHubClient:
    """Read-only GitHub REST client for public repositories."""

    def __init__(self, token: Optional[str] = None):
        self._token = token if token is not None else os.getenv("GITHUB_TOKEN") or None
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        response = self._session.get(
            f"{GITHUB_API_BASE}{path}", headers=headers, params=params, timeout=_TIMEOUT_SECONDS
        )
        if response.status_code in (403, 429) and (
            response.headers.get("X-RateLimit-Remaining") == "0"
            or "rate limit" in (response.text or "").lower()
        ):
            raise BacktestError(
                "GitHub API rate limit reached. Try again later, or set GITHUB_TOKEN on the server.",
                status_code=429,
            )
        if response.status_code == 404:
            raise BacktestError("Not found. Only public GitHub repositories are supported.", status_code=404)
        if response.status_code == 422:
            raise BacktestError("GitHub could not search this repository.", status_code=400)
        if response.status_code >= 400:
            logger.warning("[Backtest] GitHub API error status=%s path=%s", response.status_code, path)
            raise BacktestError("GitHub API request failed.", status_code=502)
        return response.json()

    def search_prs(self, query: str, per_page: int) -> List[Dict[str, Any]]:
        data = self._get(
            "/search/issues",
            {"q": query, "sort": "created", "order": "desc", "per_page": per_page},
        )
        return [item for item in (data.get("items") or []) if isinstance(item, dict)]

    def get_repo(self, repo_full_name: str) -> Dict[str, Any]:
        return self._get(f"/repos/{repo_full_name}")

    def get_pull(self, repo_full_name: str, number: int) -> Dict[str, Any]:
        return self._get(f"/repos/{repo_full_name}/pulls/{int(number)}")

    def list_files(self, repo_full_name: str, number: int) -> List[Dict[str, Any]]:
        files = self._get(f"/repos/{repo_full_name}/pulls/{int(number)}/files", {"per_page": _MAX_FILES})
        return files if isinstance(files, list) else []

    def pulls_for_commit(self, repo_full_name: str, sha: str) -> List[Dict[str, Any]]:
        pulls = self._get(f"/repos/{repo_full_name}/commits/{sha}/pulls")
        return pulls if isinstance(pulls, list) else []


def find_candidates(
    client: PublicGitHubClient,
    owner: str,
    repo: str,
    limit: int = 5,
    controls: int = 2,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Find up to ``limit`` reverted PRs and ``controls`` non-reverted merged PRs."""
    repo_full_name = f"{owner}/{repo}"
    # Search results carry PR titles, so refuse private repos before searching.
    if client.get_repo(repo_full_name).get("private") is not False:
        raise BacktestError("Only public repositories are supported.", status_code=403)
    reverts = client.search_prs(f"repo:{repo_full_name} is:pr is:merged in:title revert", per_page=30)

    reverted: List[Dict[str, Any]] = []
    reverted_numbers: set = set()
    sha_lookups = 0
    for item in reverts:
        if not is_revert_title(item.get("title", "")):
            continue
        number, sha = extract_reverted_ref(item.get("title", ""), item.get("body") or "", repo_full_name)
        # Collect every reverted PR we can resolve cheaply, so none become controls.
        if number is None and sha and len(reverted) < limit and sha_lookups < limit:
            sha_lookups += 1
            try:
                merged = [p for p in client.pulls_for_commit(repo_full_name, sha) if p.get("merged_at")]
            except BacktestError:
                merged = []
            number = merged[0]["number"] if merged else None
        if number is None or number in reverted_numbers:
            continue
        reverted_numbers.add(number)
        if len(reverted) < limit:
            reverted.append({
                "pr_number": number,
                "kind": "reverted",
                "revert_pr_number": item.get("number"),
                "revert_title": item.get("title"),
                "reverted_at": item.get("closed_at"),
            })

    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=_CONTROL_MIN_AGE_DAYS)).date().isoformat()
    control_items = client.search_prs(f"repo:{repo_full_name} is:pr is:merged merged:<{cutoff}", per_page=20)
    control_list: List[Dict[str, Any]] = []
    for item in control_items:
        number = item.get("number")
        if number is None or number in reverted_numbers or "revert" in (item.get("title") or "").lower():
            continue
        control_list.append({"pr_number": number, "kind": "control", "title": item.get("title")})
        if len(control_list) >= controls:
            break

    return {"repo": repo_full_name, "reverted": reverted, "controls": control_list}


def isolate_pre_merge_pr(pr: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only fields fixed at merge time; drop the editable body."""
    base = pr.get("base") or {}
    head = pr.get("head") or {}
    return {
        "number": pr.get("number"),
        "title": pr.get("title") or "",
        "body": "",
        "user": {"login": (pr.get("user") or {}).get("login")},
        "base": {"ref": base.get("ref")},
        "head": {"ref": head.get("ref"), "sha": head.get("sha")},
    }


def build_backtest_prompt(repo_full_name: str, pr: Dict[str, Any], files: List[Dict[str, Any]]) -> str:
    from services.change_gating.verdict import build_review_prompt

    prompt = build_review_prompt(repo_full_name, isolate_pre_merge_pr(pr), files[:_MAX_FILES])
    return f"{prompt}\n\n{_BACKTEST_NOTE}"


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        )
    return str(content or "")


def review_pr(client: PublicGitHubClient, repo_full_name: str, pr_number: int, llm=None) -> Dict[str, Any]:
    """Review one merged PR as of merge time. Returns verdict + PR metadata."""
    pr = client.get_pull(repo_full_name, pr_number)
    if ((pr.get("base") or {}).get("repo") or {}).get("private"):
        raise BacktestError("Only public repositories are supported.", status_code=403)
    if not pr.get("merged_at"):
        raise BacktestError(f"PR #{pr_number} was not merged.")
    files = client.list_files(repo_full_name, pr_number)
    prompt = build_backtest_prompt(repo_full_name, pr, files)

    from services.change_gating.verdict import extract_verdict_with_llm, parse_verdict

    if llm is None:
        from chat.backend.agent.llm import ModelConfig
        from chat.backend.agent.providers import create_chat_model

        llm = create_chat_model(ModelConfig.RCA_MODEL, temperature=0.0, streaming=False)
    text = _message_text(llm.invoke(prompt))
    verdict = parse_verdict(text) or extract_verdict_with_llm(text)
    if not verdict:
        raise BacktestError("The model did not return a verdict.", status_code=502)
    if verdict["verdict"] == "RISKY" and not verdict.get("findings"):
        verdict["verdict"] = "SAFE"
    if verdict["verdict"] == "SAFE":
        verdict["findings"] = []

    return {
        "pr_number": pr_number,
        "title": pr.get("title"),
        "url": pr.get("html_url"),
        "merged_at": pr.get("merged_at"),
        "files_reviewed": min(len(files), _MAX_FILES),
        "files_truncated": len(files) >= _MAX_FILES,
        "verdict": verdict["verdict"],
        "summary": verdict.get("summary", ""),
        "findings": verdict.get("findings", []),
    }


def main(argv: Optional[List[str]] = None) -> None:
    """Evidence run: ``python -m services.change_gating.backtest owner/repo [...]``."""
    import sys

    repos = argv if argv is not None else sys.argv[1:]
    client = PublicGitHubClient()
    totals = {"reverted": [0, 0], "control": [0, 0]}
    try:
        for value in repos:
            owner, repo = parse_repo_url(value)
            found = find_candidates(client, owner, repo, limit=3)
            for cand in found["reverted"] + found["controls"]:
                try:
                    result = review_pr(client, found["repo"], cand["pr_number"])
                except BacktestError as exc:
                    print(f"{found['repo']}#{cand['pr_number']}\t{cand['kind']}\tERROR\t{exc}")
                    continue
                flagged = result["verdict"] == "RISKY"
                totals[cand["kind"]][0] += int(flagged)
                totals[cand["kind"]][1] += 1
                print(f"{found['repo']}#{cand['pr_number']}\t{cand['kind']}\t{result['verdict']}\t{result['title']}")
    finally:
        client.close()
    print(f"\nreverted flagged: {totals['reverted'][0]}/{totals['reverted'][1]}")
    print(f"controls flagged: {totals['control'][0]}/{totals['control'][1]}")


if __name__ == "__main__":
    main()
