import json

import pytest

from browser_testing.scenario import ScenarioError, freeze_scenario, validate_frozen


def scenario():
    return {
        "schemaVersion": 1,
        "runnerVersion": "1",
        "name": "Road restriction badges",
        "incident": {"id": "incident-1", "reference": "https://example.test/incidents/1"},
        "brief": "Check the two road badges.",
        "baselineCommit": "a" * 40,
        "checks": [
            {"id": "avenue", "rowText": "Avenue Rd", "expectedBadge": "Lane restriction"},
            {"id": "queen", "rowText": "Queen St E", "expectedBadge": "Road closed"},
        ],
    }


def test_freeze_is_canonical_and_detects_edits():
    first = freeze_scenario(scenario())
    reordered = json.loads(json.dumps(first, sort_keys=True))
    assert validate_frozen(reordered)["scenarioHash"] == first["scenarioHash"]

    reordered["checks"][0]["expectedBadge"] = "Road closed"
    with pytest.raises(ScenarioError, match="hash mismatch"):
        validate_frozen(reordered)


def test_scenario_rejects_duplicate_checks_and_unknown_fields():
    value = scenario()
    value["checks"][1]["id"] = value["checks"][0]["id"]
    with pytest.raises(ScenarioError, match="duplicated"):
        freeze_scenario(value)

    value = scenario()
    value["checks"][0]["selector"] = ".badge"
    with pytest.raises(ScenarioError, match="unsupported fields"):
        freeze_scenario(value)
