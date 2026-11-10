#!/usr/bin/env python3
"""Run three alert candidates through Aurora's real StructuredTool boundary.

Requires the server's Python dependencies and promtool on PATH. No model,
account, credentials, database, or live monitoring system is used.

    python scripts/demo_tested_alerts.py --output-dir /tmp/aurora-alert-demo

The fixed suite contains a sustained incident, healthy traffic, a brief spike,
and recovery. The first two candidates must fail; the correction must pass.
Exported rules and tests can be rerun with: promtool test rules tests.yml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "server"))

from chat.backend.agent.tools.prometheus_alert_tool import (
    build_prometheus_alert_tool,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Export each run's exact rules, test suite, and result receipt here.",
    )
    args = parser.parse_args()

    fixtures = REPO_ROOT / "examples" / "tested-alerts"
    tests_yaml = (fixtures / "tests.yml").read_text(encoding="utf-8")
    tool = build_prometheus_alert_tool()
    print("Synthetic fixtures; actual promtool execution through Aurora's StructuredTool.")
    print("The expectations remain identical for every candidate.\n")

    expected = {"missed": "failed", "noisy": "failed", "corrected": "passed"}
    expected_failure_case = {
        "missed": "sustained incident must page by minute three and clear on recovery",
        "noisy": "brief error spike must not page",
    }
    suite_hash = None
    mismatches = []
    for candidate, expected_status in expected.items():
        rules_yaml = (fixtures / f"rules-{candidate}.yml").read_text(encoding="utf-8")
        result = json.loads(tool.invoke({
            "rules_yaml": rules_yaml,
            "tests_yaml": tests_yaml,
            "evidence_kind": "synthetic",
        }))
        hashes = result.get("input_hashes", {})
        current_suite_hash = hashes.get("tests_sha256")
        if not current_suite_hash:
            mismatches.append(f"{candidate}: result has no test-suite hash")
        elif suite_hash is None:
            suite_hash = current_suite_hash
        elif current_suite_hash != suite_hash:
            mismatches.append(f"{candidate}: test-suite hash changed")

        print(f"{candidate}: {result.get('status')} (expected {expected_status})")
        print(f"  elapsed_ms: {result.get('elapsed_ms')}")
        print(f"  input_hashes: {json.dumps(hashes, sort_keys=True)}")
        print(result.get("output", "(no evaluator output)"))

        if result.get("status") != expected_status:
            mismatches.append(f"{candidate}: expected {expected_status}, got {result.get('status')}")
        if candidate in expected_failure_case and expected_failure_case[candidate] not in result.get("output", ""):
            mismatches.append(f"{candidate}: output does not identify the intended failing case")

        if args.output_dir:
            run_dir = args.output_dir / candidate
            run_dir.mkdir(parents=True, exist_ok=True)
            # Only the evaluator's canonical exports are rerun, not reconstructed inputs.
            exports = result.get("exports", {})
            for filename in ("rules.yml", "tests.yml"):
                if filename not in exports:
                    mismatches.append(f"{candidate}: evaluator did not export {filename}")
                    continue
                (run_dir / filename).write_text(exports[filename], encoding="utf-8")
            (run_dir / "receipt.json").write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

    if mismatches:
        print("Demo checks failed:", file=sys.stderr)
        for mismatch in mismatches:
            print(f"- {mismatch}", file=sys.stderr)
        return 1

    print("Verified: missed incident -> noisy proposal -> corrected proposal.")
    print("This establishes behavior on these synthetic cases, not production coverage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
