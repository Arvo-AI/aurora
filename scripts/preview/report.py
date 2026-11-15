#!/usr/bin/env python3
"""Turn a preview run into one machine-readable file: verification.json.

This file is the contract between a repository's CI and Aurora. The workflow
uploads it as an artifact named ``aurora-verification``; when the run finishes,
Aurora fetches the artifact, stores the result and reports it on the pull
request. Nothing here is specific to Aurora's own repository: any workflow that
uploads a file in this shape gets the same treatment.

Schema (version 1):

    {
      "schema": 1,
      "name": "Preview environment",
      "status": "passed" | "failed",
      "checks": [
        {"group": "smoke", "name": "...", "status": "pass|warn|fail|skip", "detail": "..."}
      ],
      "covers": ["server/**", ...],        # paths the run exercises
      "defined_by": ["scripts/preview/**"] # files that define the run itself
    }

Usage (the workflow passes each step's outcome; locally they can be omitted):

    scripts/preview/report.py --boot-outcome success --smoke-outcome success \\
        --browser-outcome success

Only the standard library is used, so it runs on a bare CI runner.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parent.parent.parent

# Files that define this verification. A pull request that changes them is
# changing its own exam, and Aurora says so on the pull request.
DEFINED_BY = [
    ".github/workflows/pr-verify.yml",
    "scripts/preview/**",
    "client/tests/**",
]

_SMOKE_STATUS = {"PASS": "pass", "WARN": "warn", "FAIL": "fail"}


def _check(group: str, name: str, status: str, detail: str = "") -> Dict[str, str]:
    return {"group": group, "name": name, "status": status, "detail": detail}


def _stage_fallback(group: str, name: str, outcome: Optional[str]) -> Dict[str, str]:
    """One check standing in for a stage that left no detailed results."""
    if outcome == "success":
        return _check(group, name, "pass")
    if outcome in ("failure", "cancelled"):
        return _check(group, name, "fail", "the step failed without detailed results")
    return _check(group, name, "skip", "did not run")


def boot_checks(outcome: Optional[str]) -> List[Dict[str, str]]:
    if outcome == "success":
        return [_check("boot", "stack boots from the production images", "pass")]
    if outcome in ("failure", "cancelled"):
        return [_check("boot", "stack boots from the production images", "fail",
                       "the stack did not become ready; see the service logs")]
    return [_check("boot", "stack boots from the production images", "skip",
                   "did not run, an earlier step failed")]


def smoke_checks(evidence_dir: Path, outcome: Optional[str]) -> List[Dict[str, str]]:
    """Read the rows scripts/preview/smoke.sh wrote (status, name, detail)."""
    results = evidence_dir / "smoke.tsv"
    checks: List[Dict[str, str]] = []
    if results.is_file():
        for line in results.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or parts[0] not in _SMOKE_STATUS:
                continue
            detail = parts[2] if len(parts) > 2 else ""
            checks.append(_check("smoke", parts[1], _SMOKE_STATUS[parts[0]], detail))
    if checks:
        return checks
    return [_stage_fallback("smoke", "smoke checks", outcome)]


def _walk_playwright(suite: Dict[str, Any], trail: List[str], out: List[Dict[str, str]]) -> None:
    title = suite.get("title") or ""
    # The top-level suite is the file name; keep describe blocks, drop the file.
    here = trail + [title] if title and not title.endswith((".ts", ".js")) else trail
    for spec in suite.get("specs") or []:
        name = " > ".join(here + [spec.get("title") or "unnamed test"])
        statuses = [t.get("status") for t in spec.get("tests") or []]
        if "unexpected" in statuses or spec.get("ok") is False:
            out.append(_check("browser", name, "fail", "test failed; see the Playwright report"))
        elif "flaky" in statuses:
            out.append(_check("browser", name, "warn", "passed only after a retry"))
        elif statuses and all(s == "skipped" for s in statuses):
            out.append(_check("browser", name, "skip"))
        else:
            out.append(_check("browser", name, "pass"))
    for child in suite.get("suites") or []:
        _walk_playwright(child, here, out)


def browser_checks(candidates: List[Path], outcome: Optional[str]) -> List[Dict[str, str]]:
    """Read Playwright's JSON report from the first candidate path that exists."""
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        checks: List[Dict[str, str]] = []
        for suite in data.get("suites") or []:
            _walk_playwright(suite, [], checks)
        if checks:
            return checks
    return [_stage_fallback("browser", "browser tests", outcome)]


def read_patterns(path: Path) -> List[str]:
    if not path.is_file():
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def overall_status(checks: List[Dict[str, str]], outcomes: List[Optional[str]]) -> str:
    """Passed only when nothing failed AND every stage that reported, succeeded.

    The second half matters: if the image build fails, no stage runs, so there
    are no failed checks. Without it that run would read as a pass.
    """
    if any(check["status"] == "fail" for check in checks):
        return "failed"
    reported = [o for o in outcomes if o is not None]
    if any(o != "success" for o in reported):
        return "failed"
    if not any(check["status"] == "pass" for check in checks):
        return "failed"
    return "passed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--evidence-dir", default=str(ROOT / "preview-evidence"))
    parser.add_argument("--covers", default=str(ROOT / "scripts/preview/covers.txt"))
    parser.add_argument(
        "--playwright-json", action="append", default=None,
        help="Playwright JSON report path; repeat to try several",
    )
    parser.add_argument("--out", default=None, help="default: <evidence-dir>/verification.json")
    for stage in ("boot", "smoke", "browser"):
        parser.add_argument(f"--{stage}-outcome", default=None,
                            help="success | failure | cancelled | skipped (a CI step outcome)")
    args = parser.parse_args()

    evidence_dir = Path(args.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    playwright_paths = [Path(p) for p in (args.playwright_json or [
        str(ROOT / "client/tests/test-results/results.json"),
        str(ROOT / "client/test-results/results.json"),
    ])]

    # An empty string is what CI passes for a step that never got an outcome.
    boot = args.boot_outcome or None
    smoke = args.smoke_outcome or None
    browser = args.browser_outcome or None

    checks = (
        boot_checks(boot)
        + smoke_checks(evidence_dir, smoke)
        + browser_checks(playwright_paths, browser)
    )
    report = {
        "schema": SCHEMA_VERSION,
        "name": "Preview environment",
        "status": overall_status(checks, [boot, smoke, browser]),
        "checks": checks,
        "covers": read_patterns(Path(args.covers)),
        "defined_by": DEFINED_BY,
    }

    out_path = Path(args.out) if args.out else evidence_dir / "verification.json"
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    counts = {s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "warn", "fail", "skip")}
    print(
        f"verification: {report['status']} "
        f"({counts['pass']} passed, {counts['warn']} warnings, "
        f"{counts['fail']} failed, {counts['skip']} skipped) -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
