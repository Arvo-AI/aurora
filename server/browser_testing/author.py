"""Author and freeze a browser scenario from an incident brief."""

from __future__ import annotations

import json
from typing import Any

from .agent import AgentError, run_tool_agent
from .browser import BROWSER_TOOLS, BrowserSession
from .scenario import freeze_scenario


AUTHOR_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 256},
        "checks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,63}$"},
                    "rowText": {"type": "string", "minLength": 1, "maxLength": 256},
                    "expectedBadge": {"type": "string", "minLength": 1, "maxLength": 256},
                },
                "required": ["id", "rowText", "expectedBadge"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "checks"],
    "additionalProperties": False,
}


def author_scenario(
    *,
    brief: str,
    preview_url: str,
    incident_id: str,
    incident_reference: str,
    baseline_commit: str,
    model: str,
) -> dict[str, Any]:
    prompt = f"""You are authoring a small frozen browser scenario from a past incident.
Read the visible page and inspect every row named by the incident brief. Propose only checks that the brief explicitly requires.
The current page may still contain the incident bug: expectedBadge must come from the brief, never from the current badge.
Use stable visible row text and concise kebab-case check IDs. Do not use CSS selectors.
Every rowText in the final answer must exactly equal the short anchorText you passed to inspect_row
(for example, a road name), never the returned containerText or a multi-line row dump.

Incident brief:
{brief}
"""
    with BrowserSession(preview_url) as browser:
        proposal = run_tool_agent(
            prompt=prompt,
            tools=BROWSER_TOOLS,
            output_name="browser_scenario_proposal",
            output_schema=AUTHOR_SCHEMA,
            execute_tool=browser.execute_tool,
            model=model,
        )
        evidence = []
        for check in proposal.get("checks", []):
            matches = [
                candidate
                for candidate in browser.candidates.values()
                if candidate.anchor_text == check.get("rowText")
            ]
            if len(matches) != 1 or matches[0].exact_match_count != 1:
                raise AgentError(f"authored row is missing or ambiguous: {check.get('rowText')}")
            evidence.append(
                {
                    "rowText": matches[0].anchor_text,
                    "observedBadges": list(matches[0].badge_texts),
                }
            )

    scenario = {
        "schemaVersion": 1,
        "runnerVersion": "1",
        "name": proposal["name"],
        "incident": {"id": incident_id, "reference": incident_reference},
        "brief": brief.strip(),
        "baselineCommit": baseline_commit,
        "checks": proposal["checks"],
    }
    return freeze_scenario(
        scenario,
        {
            "model": model,
            "previewOrigin": browser.origin,
            "observations": evidence,
        },
    )
