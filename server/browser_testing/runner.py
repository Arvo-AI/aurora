"""Replay a frozen scenario with agent-selected Playwright evidence."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent import run_tool_agent
from .browser import BrowserWorkspace, RowCandidate, agent_browser_tools
from .paths import (
    ARTIFACT_DIRECTORY,
    RESULT_FILE,
    RESULT_TEMPORARY_FILE,
)
from .scenario import load_scenario


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _agent_output_schema(check_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "inspectedCheckIds": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(check_ids),
                "items": {"type": "string", "enum": check_ids},
            },
            "summary": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "required": ["inspectedCheckIds", "summary"],
        "additionalProperties": False,
    }


def _status_observations_from_evidence(
    scenario: dict[str, Any], evidence: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    observations: dict[str, dict[str, Any]] = {}
    for check in scenario["checks"]:
        if "expectedStatus" not in check:
            continue
        target_id = check.get("target", "preview")
        matches = [
            item["result"]
            for item in evidence
            if item.get("tool") in {"navigate_browser", "read_http_status"}
            and item.get("target") == target_id
            and (item.get("result") or {}).get("path") == check["path"]
        ]
        if matches:
            observations[check["id"]] = matches[-1]
    return observations


def _selections_from_candidates(
    scenario: dict[str, Any], candidates: dict[str, RowCandidate]
) -> dict[str, list[dict[str, str]]]:
    observations = []
    for check in scenario["checks"]:
        if "expectedBadge" not in check:
            continue
        matches = [
            candidate
            for candidate in candidates.values()
            if candidate.anchor_text == check["rowText"]
        ]
        if len(matches) == 1:
            observations.append(
                {"checkId": check["id"], "candidateId": matches[0].candidate_id}
            )
    return {"observations": observations}


def _agent_prompt(
    scenario: dict[str, Any], target_metadata: list[dict[str, Any]]
) -> str:
    context = {
        "incident": scenario["incident"],
        "brief": scenario["brief"],
        "scenarioHash": scenario["scenarioHash"],
        "checks": scenario["checks"],
        "targets": target_metadata,
    }
    return f"""You are the browser evidence collector for a frozen incident regression test.
Choose the inspection sequence yourself using the provided browser tools. The target IDs are the only previews you may access.

For every expectedBadge check: navigate the relevant preview, inspect the exact rowText with inspect_row, and preserve a screenshot.
For every expectedStatus check: inspect that exact target and path with navigate_browser or read_http_status, and preserve a screenshot of the navigated target.
You may read visible text and interact with the page when needed. Do not infer observations, do not change expectations, and do not decide pass or fail. Ordinary runner code will calculate the verdict only from recorded tool results.
Finish only after collecting direct evidence for every frozen check. In your final structured response, list the check IDs you inspected and briefly summarize the inspection steps without giving a verdict.

Frozen incident context, expectations, and targets:
{json.dumps(context, separators=(',', ':'), ensure_ascii=False)}
"""


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
            target_prefix = f"{check.get('target', 'preview')} "
            mismatches.append(
                f"{target_prefix}{check['path']}: expected HTTP "
                f"{check['expectedStatus']}, observed HTTP {observed}"
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
    scenario: dict[str, Any] | None = None
    result: dict[str, Any]
    tool_evidence: list[dict[str, Any]] = []
    screenshots: list[str] = []
    agent_decision: dict[str, Any] | None = None

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

        workspace_urls = {"preview": preview_url, **supplied_targets}
        target_metadata = [{"id": "preview", "kind": "preview"}, *scenario.get("targets", [])]
        agent_error: Exception | None = None
        with BrowserWorkspace(workspace_urls) as workspace:
            try:
                check_ids = [check["id"] for check in scenario["checks"]]
                agent_decision = run_tool_agent(
                    prompt=_agent_prompt(scenario, target_metadata),
                    tools=agent_browser_tools(list(workspace_urls)),
                    output_name="browser_inspection_complete",
                    output_schema=_agent_output_schema(check_ids),
                    execute_tool=workspace.execute_tool,
                    model=model,
                    max_turns=10,
                    max_tool_calls=12,
                )
            except Exception as exc:
                agent_error = exc
            tool_evidence = list(workspace.evidence)
            screenshots = list(workspace.screenshots)
            preview_origins = workspace.origins
            status_observations = _status_observations_from_evidence(
                scenario, tool_evidence
            )
            selections = _selections_from_candidates(scenario, workspace.candidates)
            verdict, reason, checks = compute_verdict(
                scenario, workspace.candidates, selections, status_observations
            )

        if agent_error is not None:
            verdict = "inconclusive"
            reason = (
                "browser agent did not complete: "
                f"{type(agent_error).__name__}: {agent_error}"
            )

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
            "agentDecision": agent_decision,
            "toolEvidence": tool_evidence,
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
            "agentDecision": agent_decision,
            "toolEvidence": tool_evidence,
            "screenshot": screenshots[0] if screenshots else None,
            "screenshots": screenshots,
        }

    RESULT_TEMPORARY_FILE.write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    RESULT_TEMPORARY_FILE.replace(result_path)
    return result
