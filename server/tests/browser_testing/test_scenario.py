import json

import pytest

from browser_testing.paths import PathAccessError, artifact_file
from browser_testing.scenario import (
    ScenarioError,
    freeze_scenario,
    load_scenario,
    save_scenario,
    validate_frozen,
)


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


def test_scenario_reads_and_writes_reject_paths_outside_fixture_directory(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(PathAccessError, match="must stay inside"):
        load_scenario(outside)
    with pytest.raises(PathAccessError, match="must stay inside"):
        save_scenario(outside, freeze_scenario(scenario()))
    with pytest.raises(PathAccessError, match="must stay inside"):
        load_scenario("../outside.json")

    with pytest.raises(PathAccessError, match="authoring output"):
        save_scenario("another-scenario.json", freeze_scenario(scenario()))


def test_artifacts_reject_unapproved_or_traversing_paths():
    with pytest.raises(PathAccessError, match="unsupported"):
        artifact_file("../browser-result.json")
    with pytest.raises(PathAccessError, match="unsupported"):
        artifact_file("other.json")
