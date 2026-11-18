import json

import pytest

import browser_testing.scenario as scenario_module
from browser_testing.paths import PathAccessError, artifact_file, scenario_file
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


def test_existing_avenue_scenario_hash_is_unchanged():
    frozen = load_scenario("avenue-rd-scenario.json")
    assert frozen["scenarioHash"] == "62b309720e26717a46a6195f47ba7d207bf356ee51dfa022bd9ca87926da3e90"


def test_http_status_check_is_frozen_and_validated():
    value = scenario()
    value["checks"] = [{"id": "items", "path": "/api/items", "expectedStatus": 200}]
    frozen = freeze_scenario(value)

    assert validate_frozen(frozen)["checks"] == value["checks"]

    value["checks"][0]["path"] = "https://example.test/api/items"
    with pytest.raises(ScenarioError, match="origin-relative"):
        freeze_scenario(value)

    value["checks"][0] = {"id": "items", "path": "/api/items", "expectedStatus": 999}
    with pytest.raises(ScenarioError, match="HTTP status"):
        freeze_scenario(value)


def test_replica_targets_are_frozen_and_require_stable_exact_labels():
    value = scenario()
    value["targets"] = [
        {
            "id": "frontend-retained",
            "kind": "kubernetes-pod-label",
            "namespace": "shop",
            "selector": "aurora.dev/replica=retained",
            "port": 80,
        }
    ]
    value["checks"] = [
        {
            "id": "retained-items",
            "target": "frontend-retained",
            "path": "/api/items",
            "expectedStatus": 200,
        }
    ]

    frozen = freeze_scenario(value)
    assert frozen["targets"] == value["targets"]
    assert frozen["checks"] == value["checks"]

    value["targets"][0]["selector"] = "app in (frontend)"
    with pytest.raises(ScenarioError, match="one exact label"):
        freeze_scenario(value)


def test_targeted_check_rejects_unknown_target():
    value = scenario()
    value["checks"] = [
        {
            "id": "missing-replica",
            "target": "not-defined",
            "path": "/api/items",
            "expectedStatus": 200,
        }
    ]
    with pytest.raises(ScenarioError, match="does not name"):
        freeze_scenario(value)


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
        scenario_file(outside)
    with pytest.raises(PathAccessError, match="must stay inside"):
        load_scenario("../outside.json")

def test_artifacts_reject_unapproved_or_traversing_paths():
    assert artifact_file("browser-evidence-frontend-retained.png").parent.name == "artifacts"
    with pytest.raises(PathAccessError, match="unsupported"):
        artifact_file("../browser-result.json")
    with pytest.raises(PathAccessError, match="unsupported"):
        artifact_file("other.json")


def test_scenario_save_uses_fixed_fixture_directory(tmp_path, monkeypatch):
    target = tmp_path / "avenue-rd-scenario.json"
    monkeypatch.setattr(scenario_module, "SCENARIO_DIRECTORY", tmp_path)
    monkeypatch.setattr(scenario_module, "AUTHORED_SCENARIO_FILE", target)

    frozen = freeze_scenario(scenario())
    save_scenario(frozen)

    assert json.loads(target.read_text(encoding="utf-8")) == frozen
    assert not (tmp_path / ".avenue-rd-scenario.json.tmp").exists()
