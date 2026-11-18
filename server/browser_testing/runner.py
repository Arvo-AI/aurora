"""Replay a frozen scenario with agent-selected Playwright evidence."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent import run_tool_agent
from .browser import BROWSER_TOOLS, BrowserSession, RowCandidate
from .paths import ARTIFACT_DIRECTORY, artifact_file
from .scenario import load_scenario


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _selection_schema(check_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "observations": {
                "type": "array",
                "minItems": len(check_ids),
                "maxItems": len(check_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "checkId": {"type": "string", "enum": check_ids},
                        "candidateId": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                    "required": ["checkId", "candidateId"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["observations"],
        "additionalProperties": False,
    }


def compute_verdict(
    scenario: dict[str, Any],
    candidates: dict[str, RowCandidate],
    selections: Any,
) -> tuple[str, str, list[dict[str, Any]]]:
    checks = scenario["checks"]
    observations = selections.get("observations") if isinstance(selections, dict) else None
    by_id: dict[str, str] = {}
    if isinstance(observations, list):
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            check_id = observation.get("checkId")
            candidate_id = observation.get("candidateId")
            if isinstance(check_id, str) and isinstance(candidate_id, str) and check_id not in by_id:
                by_id[check_id] = candidate_id

    results = []
    incomplete_reasons = []
    mismatches = []
    selected_candidates: set[str] = set()
    for check in checks:
        candidate_id = by_id.get(check["id"])
        candidate = candidates.get(candidate_id or "")
        observed: str | None = None
        evidence: str | None = None
        complete = True
        if candidate is None:
            complete = False
            incomplete_reasons.append(f"{check['id']}: agent did not select observed browser evidence")
        elif candidate_id in selected_candidates:
            complete = False
            incomplete_reasons.append(f"{check['id']}: browser row was reused")
        elif candidate.anchor_text != check["rowText"] or candidate.exact_match_count != 1:
            complete = False
            incomplete_reasons.append(f"{check['id']}: row was missing or ambiguous")
        elif len(candidate.badge_texts) != 1:
            complete = False
            incomplete_reasons.append(f"{check['id']}: badge was missing or ambiguous")
        else:
            selected_candidates.add(candidate_id or "")
            observed = candidate.badge_texts[0]
            evidence = candidate.container_text
            if observed != check["expectedBadge"]:
                mismatches.append(
                    f"{check['rowText']}: expected {check['expectedBadge']!r}, observed {observed!r}"
                )
        results.append(
            {
                "id": check["id"],
                "rowText": check["rowText"],
                "expectedBadge": check["expectedBadge"],
                "observedBadge": observed,
                "complete": complete,
                "evidence": evidence,
            }
        )

    if incomplete_reasons:
        return "inconclusive", "; ".join(incomplete_reasons), results
    if mismatches:
        return "failed", "; ".join(mismatches), results
    return "passed", "all browser observations matched the frozen expectations", results


def run_scenario(
    *,
    scenario_path: str | Path,
    preview_url: str,
    tested_commit: str,
    model: str,
) -> dict[str, Any]:
    started_at = _utc_now()
    ARTIFACT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    result_path = artifact_file("browser-result.json")
    screenshot_path = artifact_file("browser-evidence.png")
    scenario: dict[str, Any] | None = None
    result: dict[str, Any]

    try:
        scenario = load_scenario(scenario_path)
        if not COMMIT_RE.fullmatch(tested_commit):
            raise ValueError("tested commit must be a full lowercase Git commit")
        checks_json = json.dumps(scenario["checks"], separators=(",", ":"))
        prompt = f"""You are collecting browser evidence for a frozen incident regression scenario.
Use the read-only browser tools. Inspect each check's rowText exactly, then select the unique candidate ID for that row.
Do not judge pass or fail and do not invent candidate IDs; the runner computes the verdict from browser readings.
Frozen checks: {checks_json}
"""
        with BrowserSession(preview_url) as browser:
            try:
                selections = run_tool_agent(
                    prompt=prompt,
                    tools=BROWSER_TOOLS,
                    output_name="browser_evidence_selection",
                    output_schema=_selection_schema([check["id"] for check in scenario["checks"]]),
                    execute_tool=browser.execute_tool,
                    model=model,
                )
                verdict, reason, checks = compute_verdict(
                    scenario, browser.candidates, selections
                )
            except Exception as exc:
                verdict = "inconclusive"
                reason = f"browser agent did not complete: {type(exc).__name__}: {exc}"
                checks = [
                    {
                        "id": check["id"],
                        "rowText": check["rowText"],
                        "expectedBadge": check["expectedBadge"],
                        "observedBadge": None,
                        "complete": False,
                        "evidence": None,
                    }
                    for check in scenario["checks"]
                ]
            browser.screenshot("browser-evidence.png")
            preview_origin = browser.origin

        result = {
            "schemaVersion": 1,
            "incident": scenario["incident"],
            "scenarioHash": scenario["scenarioHash"],
            "testedCommit": tested_commit,
            "previewOrigin": preview_origin,
            "model": model,
            "startedAt": started_at,
            "completedAt": _utc_now(),
            "verdict": verdict,
            "reason": reason,
            "checks": checks,
            "screenshot": screenshot_path.name,
        }
    except Exception as exc:
        result = {
            "schemaVersion": 1,
            "incident": scenario.get("incident") if scenario else None,
            "scenarioHash": scenario.get("scenarioHash") if scenario else None,
            "testedCommit": tested_commit,
            "previewOrigin": None,
            "model": model,
            "startedAt": started_at,
            "completedAt": _utc_now(),
            "verdict": "inconclusive",
            "reason": f"run did not complete: {type(exc).__name__}: {exc}",
            "checks": [],
            "screenshot": screenshot_path.name if screenshot_path.exists() else None,
        }

    temporary = result_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    temporary.replace(result_path)
    return result
