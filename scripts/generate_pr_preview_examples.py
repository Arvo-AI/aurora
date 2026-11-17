#!/usr/bin/env python3
"""Regenerate the pre-run examples shown on the /pr-preview page.

Each example is a real run of the no-install PR preview (POST /api/pr-preview,
then GET until it finishes) against a running Aurora. The verdict is written
to client/src/app/pr-preview/examples.json exactly as the API returned it:
never edit that file by hand, including when Aurora misses a known risk.

Most examples are PRs that a public postmortem, issue or revert names as the
change behind an incident, so a reader can compare Aurora's verdict with what
really happened. One is a small fix from the same repository that merged
without trouble (no revert, no regression issue), as a control.

Usage (Aurora running locally, e.g. `make prod-prebuilt`):

    AURORA_USER_ID=<user uuid> AURORA_ORG_ID=<org uuid> \\
    INTERNAL_API_SECRET=<from .env> AURORA_MODEL_LABEL="<model the stack runs>" \\
    python3 scripts/generate_pr_preview_examples.py

Optional: AURORA_API_URL (default http://localhost:5080).
Uses the standard library only. Each run costs one PR review per example.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / "client/src/app/pr-preview/examples.json"
POLL_SECONDS = 5
TIMEOUT_SECONDS = 15 * 60

EXAMPLES = [
    {
        "url": "https://github.com/prometheus/prometheus/pull/17349",
        "kind": "Service discovery filter",
        "incident": (
            "Consul service discovery broke in Prometheus 3.11.0. In the issue, the PR's "
            "author wrote that the regression was caused by this PR: the filter it began "
            "passing to Consul's Health API is not one that API supports."
        ),
        "incident_url": "https://github.com/prometheus/prometheus/issues/18479",
    },
    {
        "url": "https://github.com/argoproj/argo-cd/pull/25151",
        "kind": "Cache side effect",
        "incident": (
            "After upgrading, the manifest-generate-paths annotation was ignored and the "
            "repo-server regenerated manifests on every commit, causing load spikes. The "
            "PR was reverted; its author wrote they had missed that UpdateRevisionForPaths "
            "updates the stored cache."
        ),
        "incident_url": "https://github.com/argoproj/argo-cd/pull/27241",
    },
    {
        "url": "https://github.com/kubernetes/ingress-nginx/pull/12338",
        "kind": "Toolchain upgrade",
        "incident": (
            "Controller v1.11.4 stopped loading CA secrets whose chain holds a certificate "
            "with a negative serial number, because Go 1.23's x509 rejects them. This PR "
            "moved the v1.11 line from Go 1.22.8 to 1.23.3; the issue blames the Go "
            "upgrade but does not name the PR."
        ),
        "incident_url": "https://github.com/kubernetes/ingress-nginx/issues/12620",
    },
    {
        "url": "https://github.com/PostHog/posthog/pull/38686",
        "kind": "Database timeout",
        "incident": (
            "Feature flags outage, Sep 29 2025: about 78% of US flag requests failed "
            "for 1h48m. The postmortem lists this deployment's cut of the database "
            "connection timeout from 1s to 300ms as the first contributing factor."
        ),
        "incident_url": "https://posthog.com/handbook/company/post-mortems/2025-09-29-flags-is-down.md",
    },
    {
        "url": "https://github.com/prometheus/prometheus/pull/18323",
        "kind": "Small bug fix (control)",
        "incident": None,
        "incident_url": None,
    },
]


def _request(url: str, *, method: str = "GET", body: dict | None = None, headers: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _github_pr(url: str) -> dict:
    owner, repo, _, number = url.removeprefix("https://github.com/").split("/")[:4]
    return _request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}",
        headers={"Accept": "application/vnd.github+json"},
    )


def _run_example(example: dict, api: str, headers: dict, model: str) -> dict:
    pr = _github_pr(example["url"])
    started = time.monotonic()
    preview = _request(
        f"{api}/api/pr-preview", method="POST", body={"url": example["url"]},
        headers={**headers, "Content-Type": "application/json"},
    )
    status_url = f"{api}/api/pr-preview/{preview['session_id']}"
    while True:
        result = _request(status_url, headers=headers)
        if result["status"] != "in_progress":
            break
        if time.monotonic() - started > TIMEOUT_SECONDS:
            raise TimeoutError(f"{example['url']} did not finish in {TIMEOUT_SECONDS}s")
        time.sleep(POLL_SECONDS)
    print(f"{example['url']}: {result['status']} {(result['verdict'] or {}).get('verdict')}", file=sys.stderr)
    return {
        **example,
        "repo": preview["repo"],
        "pr_number": preview["pr_number"],
        "title": pr["title"],
        "head_sha": pr["head"]["sha"],
        "status": result["status"],
        "verdict": result["verdict"],
        "model": model,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "duration_seconds": round(time.monotonic() - started),
    }


def main() -> int:
    try:
        headers = {
            "X-User-ID": os.environ["AURORA_USER_ID"],
            "X-Org-ID": os.environ["AURORA_ORG_ID"],
            "X-Internal-Secret": os.environ["INTERNAL_API_SECRET"],
        }
        model = os.environ["AURORA_MODEL_LABEL"]
    except KeyError as missing:
        print(f"Missing environment variable {missing}; see the module docstring.", file=sys.stderr)
        return 2
    api = os.environ.get("AURORA_API_URL", "http://localhost:5080").rstrip("/")

    try:
        with ThreadPoolExecutor(max_workers=len(EXAMPLES)) as pool:
            examples = list(pool.map(lambda e: _run_example(e, api, headers, model), EXAMPLES))
    except urllib.error.HTTPError as exc:
        print(f"Aurora or GitHub returned {exc.code}: {exc.read().decode(errors='replace')}", file=sys.stderr)
        return 1

    OUTPUT.write_text(json.dumps(examples, indent=2) + "\n")
    print(f"Wrote {len(examples)} examples to {OUTPUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
