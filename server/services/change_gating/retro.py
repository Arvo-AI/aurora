"""Retro review: run Aurora's pre-merge review on a repo's past PRs.

Pure logic only (no I/O), so it is cheap to test:

- find PRs that were later reverted (ground truth: something went wrong)
- pick a control set of merged PRs that were never reverted
- keep only PRs that touch infra/deploy files, the risk Aurora reviews for
- build the review prompt in retrospective mode
- score the verdicts

Hindsight leakage is the main correctness risk. The reviewer must see only
what existed at merge time: the PR's title, description and diff. It never
sees comments, the revert, or anything later, and it runs with no tools, so
it cannot go and look (see tasks/retro_review.py).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from services.change_gating.verdict import build_review_prompt

# Upper bounds on one job. Each reviewed PR is one LLM call.
DEFAULT_MAX_REVERTED = 8
DEFAULT_MAX_CONTROLS = 8
MAX_PER_SET = 15

_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_URL_PREFIX_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/", re.IGNORECASE)

# GitHub's "Revert" button writes the body as "Reverts owner/repo#123".
_BODY_REVERTS_RE = r"\bReverts\s+(?:{repo})?#(\d+)\b"
# Squash merges put "(#123)" at the end of the title, so a reverted squash
# merge's revert PR is titled: Revert "Add retries (#123)"
_TITLE_RE = re.compile(r'^Revert\s+"(.*)"', re.IGNORECASE | re.DOTALL)
_TITLE_PR_RE = re.compile(r"\(#(\d+)\)\s*$")

# Aurora's review prompt only flags infra/deploy risk. A PR that touches none
# of these paths is out of scope for BOTH sets: reviewing it would measure the
# prompt's scope, not its judgement, and drag the catch rate down unfairly.
_INFRA_PATH_RE = re.compile(
    r"""(
        (^|/)Dockerfile[^/]*$
      | (^|/)(docker-)?compose[^/]*\.ya?ml$
      | \.(tf|tfvars|hcl)$
      | (^|/)Chart\.ya?ml$ | (^|/)values[^/]*\.ya?ml$ | (^|/)(charts|helm)/
      | (^|/)(k8s|kubernetes|manifests|deploy|deployment|deployments|infra|infrastructure|ops)/
      | (^|/)(kustomization|skaffold)\.ya?ml$
      | (^|/)\.github/workflows/ | (^|/)\.gitlab-ci\.ya?ml$ | (^|/)Jenkinsfile$ | (^|/)\.circleci/
      | (^|/)(migrations?|alembic)/ | \.sql$
      | (^|/)(config|conf)/ | (^|/)\.env[^/]*$ | \.(toml|ini|conf)$
      | (^|/)(Procfile|serverless\.ya?ml|vercel\.json|app\.ya?ml)$
    )""",
    re.VERBOSE | re.IGNORECASE,
)

RETRO_NOTE = """RETROSPECTIVE MODE:
This PR was merged in the past. You are reviewing it exactly as it looked at
merge time, to measure whether a pre-merge review would have caught a problem.
You have NO tools in this mode: you cannot read other files, query monitoring,
or see deployment history. Judge only from the PR description and the diff
below. Do not guess what happened after the merge. Ignore the tool-related
steps in the workflow above; everything else (the verdict decision test, what
to flag, what not to flag, and the JSON output format) applies unchanged."""


def normalize_repo(raw: str) -> str:
    """Accept "owner/repo" or a github.com URL; return "owner/repo".

    Raises ValueError for anything else, so user input never reaches a URL
    path unvalidated.
    """
    text = (raw or "").strip()
    text = _URL_PREFIX_RE.sub("", text)
    text = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    parts = text.split("/")
    if len(parts) > 2:
        text = "/".join(parts[:2])  # drop /pulls, /tree/main, ...
    if not _REPO_RE.match(text):
        raise ValueError("Enter a public GitHub repository as owner/repo")
    return text


def parse_reverted_pr_number(repo_full_name: str, revert_pr: Dict[str, Any]) -> Optional[int]:
    """Return the number of the PR that ``revert_pr`` reverts, or None.

    Only trusts the two shapes GitHub itself produces: the "Reverts
    owner/repo#N" body from the Revert button, and the squash-merge
    "(#N)" suffix inside a Revert "..." title. Anything vaguer is skipped:
    a missed revert costs a data point, a wrong one corrupts the score.
    """
    title = revert_pr.get("title") or ""
    title_match = _TITLE_RE.match(title.strip())
    if not title_match:
        return None
    inner = title_match.group(1)
    if inner.lower().startswith("revert "):
        return None  # revert of a revert: the original came back, not a failure

    body = revert_pr.get("body") or ""
    body_re = re.compile(_BODY_REVERTS_RE.format(repo=re.escape(repo_full_name)), re.IGNORECASE)
    body_match = body_re.search(body)
    if body_match:
        number = int(body_match.group(1))
    else:
        suffix = _TITLE_PR_RE.search(inner)
        if not suffix:
            return None
        number = int(suffix.group(1))

    if number == revert_pr.get("number"):
        return None
    return number


def find_reverted(repo_full_name: str, revert_prs: Iterable[Dict[str, Any]],
                  limit: int) -> List[Dict[str, Any]]:
    """Map revert PRs to the originals they undid, newest first, deduped."""
    seen: set[int] = set()
    out: List[Dict[str, Any]] = []
    for revert in revert_prs:
        original = parse_reverted_pr_number(repo_full_name, revert)
        if original is None or original in seen:
            continue
        seen.add(original)
        out.append({
            "pr_number": original,
            "kind": "reverted",
            "revert_pr_number": revert.get("number"),
            "revert_url": revert.get("html_url"),
        })
        if len(out) >= limit:
            break
    return out


def pick_controls(merged_prs: Iterable[Dict[str, Any]], exclude: set[int],
                  limit: int) -> List[Dict[str, Any]]:
    """Merged PRs that were never reverted and are not reverts themselves.

    "Not reverted" here means not reverted by a PR we found. That is an
    approximation (a fix-forward can also signal a bad PR), and it biases
    the false-alarm rate upward, which is the conservative direction.
    """
    out: List[Dict[str, Any]] = []
    for pr in merged_prs:
        number = pr.get("number")
        title = (pr.get("title") or "").strip().lower()
        if number in exclude or title.startswith("revert"):
            continue
        out.append({"pr_number": number, "kind": "control"})
        if len(out) >= limit:
            break
    return out


def infra_files(files: Iterable[Dict[str, Any]]) -> List[str]:
    """Paths in this PR that Aurora's review is designed to judge.

    Empty means the PR is out of scope. Renames count by either name, so
    moving a Helm chart still counts as an infra change.
    """
    hits: List[str] = []
    for f in files:
        for path in (f.get("filename"), f.get("previous_filename")):
            if path and _INFRA_PATH_RE.search(path):
                hits.append(path)
                break
    return hits


def build_retro_prompt(repo_full_name: str, pr: Dict[str, Any],
                       files: List[Dict[str, Any]], diff: Optional[str]) -> str:
    """The live review prompt plus the retrospective-mode note."""
    base = build_review_prompt(repo_full_name, pr, files, diff)
    return f"{base}\n\n{RETRO_NOTE}"


def normalize_verdict(verdict: Dict[str, Any]) -> Dict[str, Any]:
    """Same rules the live review applies before posting.

    SAFE never carries findings; RISKY without findings is demoted to SAFE.
    """
    verdict = dict(verdict)
    if verdict.get("verdict") == "SAFE":
        verdict["findings"] = []
    elif verdict.get("verdict") == "RISKY" and not verdict.get("findings"):
        verdict["verdict"] = "SAFE"
        verdict["findings"] = []
    return verdict


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 3) if denominator else None


def score(items: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Report card for a finished (or partial) job.

    caught      = reverted PRs Aurora called RISKY before merge
    false alarm = control PRs Aurora called RISKY
    Items that were skipped or errored count in neither denominator.
    """
    items = list(items)
    reviewed = [i for i in items if i.get("verdict") in ("SAFE", "RISKY")]
    reverted = [i for i in reviewed if i.get("kind") == "reverted"]
    controls = [i for i in reviewed if i.get("kind") == "control"]
    caught = sum(1 for i in reverted if i["verdict"] == "RISKY")
    false_alarms = sum(1 for i in controls if i["verdict"] == "RISKY")
    return {
        "reverted_reviewed": len(reverted),
        "caught": caught,
        "catch_rate": _rate(caught, len(reverted)),
        "controls_reviewed": len(controls),
        "false_alarms": false_alarms,
        "false_alarm_rate": _rate(false_alarms, len(controls)),
        "skipped": sum(1 for i in items if i.get("status") == "skipped"),
        "errored": sum(1 for i in items if i.get("status") == "error"),
        "out_of_scope": sum(1 for i in items if i.get("status") == "out_of_scope"),
    }
