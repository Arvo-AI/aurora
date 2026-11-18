"""Replay a frozen scenario with agent-selected Playwright evidence."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent import run_tool_agent
from .browser import BROWSER_TOOLS, BrowserSession, RowCandidate
from .paths import (
    ARTIFACT_DIRECTORY,
    RESULT_FILE,
    RESULT_TEMPORARY_FILE,
    SCREENSHOT_FILE,
)
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


def _compute_status_results(
    checks: list[dict[str, Any]],
    observations: dict[str, dict[str, Any]],
    targets: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    results = {}
    incomplete_reasons = []
    mismatches = []
    for check in checks:
        observation = observations.get(check["id"])
        observed = observation.get("status") if observation else None
        error = observation.get("error") if observation else "status was not observed"
        complete = observed is not None
        check_verdict = "passed"
        if not complete:
            check_verdict = "inconclusive"
            incomplete_reasons.append(f"{check['id']}: {error}")
        elif observed != check["expectedStatus"]:
            check_verdict = "failed"
            mismatches.append(
                f"{check['path']}: expected HTTP {check['expectedStatus']}, observed HTTP {observed}"
            )
        result = {
            "id": check["id"],
            "path": check["path"],
            "expectedStatus": check["expectedStatus"],
            "observedStatus": observed,
            "verdict": check_verdict,
            "complete": complete,
            "evidence": error if not complete else f"HTTP {observed}",
        }
        target_id = check.get("target")
        if target_id:
            result["target"] = target_id
            result["targetSelector"] = targets[target_id]["selector"]
        results[check["id"]] = result
    return results, incomplete_reasons, mismatches


def compute_verdict(
    scenario: dict[str, Any],
    candidates: dict[str, RowCandidate],
    selections: Any,
    status_observations: dict[str, dict[str, Any]] | None = None,
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

    status_checks = [check for check in checks if "expectedStatus" in check]
    targets = {target["id"]: target for target in scenario.get("targets", [])}
    results, incomplete_reasons, mismatches = _compute_status_results(
        status_checks, status_observations or {}, targets
    )
    selected_candidates: set[str] = set()
    badge_checks = [check for check in checks if "expectedBadge" in check]
    for check in badge_checks:
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
        results[check["id"]] = {
            "id": check["id"],
            "rowText": check["rowText"],
            "expectedBadge": check["expectedBadge"],
            "observedBadge": observed,
            "complete": complete,
            "evidence": evidence,
        }

    ordered_results = [results[check["id"]] for check in checks]

    if incomplete_reasons:
        return "inconclusive", "; ".join(incomplete_reasons), ordered_results
    if mismatches:
        return "failed", "; ".join(mismatches), ordered_results
    return "passed", "all browser observations matched the frozen expectations", ordered_results


def run_scenario(
    *,
    scenario_path: str | Path,
    preview_url: str,
    tested_commit: str,
    model: str,
    target_urls: dict[str, str] | None = None,
) -> dict[str, Any]:
    started_at = _utc_now()
    ARTIFACT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    result_path = RESULT_FILE
    screenshot_path = SCREENSHOT_FILE
    scenario: dict[str, Any] | None = None
    result: dict[str, Any]

    try:
        scenario = load_scenario(scenario_path)
        if not COMMIT_RE.fullmatch(tested_commit):
            raise ValueError("tested commit must be a full lowercase Git commit")
        configured_targets = {target["id"]: target for target in scenario.get("targets", [])}
        supplied_targets = target_urls or {}
        missing_targets = set(configured_targets) - set(supplied_targets)
        unknown_targets = set(supplied_targets) - set(configured_targets)
        if missing_targets:
            raise ValueError(f"missing target URLs: {', '.join(sorted(missing_targets))}")
        if unknown_targets:
            raise ValueError(f"unknown target URLs: {', '.join(sorted(unknown_targets))}")

        badge_checks = [check for check in scenario["checks"] if "expectedBadge" in check]
        checks_json = json.dumps(badge_checks, separators=(",", ":"))
        prompt = f"""You are collecting browser evidence for a frozen incident regression scenario.
Use the read-only browser tools. Inspect each check's rowText exactly, then select the unique candidate ID for that row.
Do not judge pass or fail and do not invent candidate IDs; the runner computes the verdict from browser readings.
Frozen checks: {checks_json}
"""
        status_checks = [check for check in scenario["checks"] if "expectedStatus" in check]
        status_observations: dict[str, dict[str, Any]] = {}
        preview_origins: dict[str, str] = {}
        screenshots: list[str] = []
        grouped_checks: dict[str, list[dict[str, Any]]] = {}
        for check in status_checks:
            grouped_checks.setdefault(check.get("target", "preview"), []).append(check)

        try:
            for target_id, target_checks in grouped_checks.items():
                target_url = preview_url if target_id == "preview" else supplied_targets[target_id]
                screenshot_name = (
                    "browser-evidence.png"
                    if target_id == "preview"
                    else f"browser-evidence-{target_id}.png"
                )
                with BrowserSession(target_url, open_preview=False) as status_browser:
                    preview_origins[target_id] = status_browser.origin
                    for index, check in enumerate(target_checks):
                        status_observations[check["id"]] = status_browser.observe_status(
                            check["path"], screenshot_name if index == 0 else None
                        )
                    screenshots.append(screenshot_name)

            candidates: dict[str, RowCandidate] = {}
            selections = {"observations": []}
            if badge_checks:
                with BrowserSession(preview_url) as browser:
                    preview_origins.setdefault("preview", browser.origin)
                    selections = run_tool_agent(
                        prompt=prompt,
                        tools=BROWSER_TOOLS,
                        output_name="browser_evidence_selection",
                        output_schema=_selection_schema([check["id"] for check in badge_checks]),
                        execute_tool=browser.execute_tool,
                        model=model,
                    )
                    candidates = dict(browser.candidates)
                    if "browser-evidence.png" not in screenshots:
                        browser.screenshot("browser-evidence.png")
                        screenshots.append("browser-evidence.png")

            verdict, reason, checks = compute_verdict(
                scenario,
                candidates,
                selections,
                status_observations,
            )
        except Exception as exc:
            verdict = "inconclusive"
            reason = f"browser agent did not complete: {type(exc).__name__}: {exc}"
            checks = [
                (
                    {
                        "id": check["id"],
                        "path": check["path"],
                        "expectedStatus": check["expectedStatus"],
                        "observedStatus": None,
                        "verdict": "inconclusive",
                        "complete": False,
                        "evidence": None,
                        **({
                            "target": check["target"],
                            "targetSelector": configured_targets[check["target"]]["selector"],
                        } if check.get("target") else {}),
                    }
                    if "expectedStatus" in check
                    else {
                        "id": check["id"],
                        "rowText": check["rowText"],
                        "expectedBadge": check["expectedBadge"],
                        "observedBadge": None,
                        "complete": False,
                        "evidence": None,
                    }
                )
                for check in scenario["checks"]
            ]

        preview_origin = preview_origins.get("preview") or next(iter(preview_origins.values()), None)

        result = {
            "schemaVersion": 1,
            "incident": scenario["incident"],
            "scenarioHash": scenario["scenarioHash"],
            "testedCommit": tested_commit,
            "previewOrigin": preview_origin,
            "previewOrigins": preview_origins,
            "model": model,
            "startedAt": started_at,
            "completedAt": _utc_now(),
            "verdict": verdict,
            "reason": reason,
            "checks": checks,
            "screenshot": screenshots[0] if screenshots else None,
            "screenshots": screenshots,
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

    RESULT_TEMPORARY_FILE.write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    RESULT_TEMPORARY_FILE.replace(result_path)
    return result
