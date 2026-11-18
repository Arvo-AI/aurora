from browser_testing.browser import RowCandidate, agent_browser_tools
from browser_testing.runner import (
    _selections_from_candidates,
    _status_observations_from_evidence,
    compute_verdict,
)
from browser_testing.scenario import freeze_scenario


def scenario():
    return freeze_scenario(
        {
            "schemaVersion": 1,
            "runnerVersion": "1",
            "name": "Road restriction badges",
            "incident": {"id": "incident-1", "reference": "https://example.test/incidents/1"},
            "brief": "Check both badges.",
            "baselineCommit": "a" * 40,
            "checks": [
                {"id": "avenue", "rowText": "Avenue Rd", "expectedBadge": "Lane restriction"},
                {"id": "queen", "rowText": "Queen St E", "expectedBadge": "Road closed"},
            ],
        }
    )


def candidate(candidate_id, row, badge, count=1):
    return RowCandidate(candidate_id, row, count, "article", f"{row}\n{badge}", (badge,))


def test_browser_mismatch_is_failed_only_when_evidence_is_complete():
    candidates = {
        "a": candidate("a", "Avenue Rd", "Road closed"),
        "q": candidate("q", "Queen St E", "Road closed"),
    }
    verdict, reason, checks = compute_verdict(
        scenario(),
        candidates,
        {"observations": [{"checkId": "avenue", "candidateId": "a"}, {"checkId": "queen", "candidateId": "q"}]},
    )
    assert verdict == "failed"
    assert "observed 'Road closed'" in reason
    assert checks[0]["observedBadge"] == "Road closed"


def test_missing_or_ambiguous_evidence_is_inconclusive():
    candidates = {"a": candidate("a", "Avenue Rd", "Lane restriction", count=2)}
    verdict, reason, _ = compute_verdict(
        scenario(), candidates, {"observations": [{"checkId": "avenue", "candidateId": "a"}]}
    )
    assert verdict == "inconclusive"
    assert "ambiguous" in reason


def test_complete_matching_evidence_passes():
    candidates = {
        "a": candidate("a", "Avenue Rd", "Lane restriction"),
        "q": candidate("q", "Queen St E", "Road closed"),
    }
    verdict, _, _ = compute_verdict(
        scenario(),
        candidates,
        {"observations": [{"checkId": "avenue", "candidateId": "a"}, {"checkId": "queen", "candidateId": "q"}]},
    )
    assert verdict == "passed"


def test_http_status_match_passes_and_mismatch_fails():
    value = scenario()
    value["checks"] = [{"id": "items", "path": "/api/items", "expectedStatus": 200}]

    verdict, _, checks = compute_verdict(
        value, {}, {"observations": []}, {"items": {"status": 200, "error": None}}
    )
    assert verdict == "passed"
    assert checks[0]["observedStatus"] == 200
    assert checks[0]["verdict"] == "passed"

    verdict, reason, checks = compute_verdict(
        value, {}, {"observations": []}, {"items": {"status": 502, "error": None}}
    )
    assert verdict == "failed"
    assert "expected HTTP 200, observed HTTP 502" in reason
    assert checks[0]["path"] == "/api/items"
    assert checks[0]["verdict"] == "failed"


def test_missing_http_response_is_inconclusive():
    value = scenario()
    value["checks"] = [{"id": "items", "path": "/api/items", "expectedStatus": 200}]

    verdict, reason, checks = compute_verdict(
        value,
        {},
        {"observations": []},
        {"items": {"status": None, "error": "navigation timed out"}},
    )
    assert verdict == "inconclusive"
    assert "navigation timed out" in reason
    assert checks[0]["complete"] is False
    assert checks[0]["verdict"] == "inconclusive"


def test_targeted_replica_statuses_identify_the_failing_label():
    value = scenario()
    value["targets"] = [
        {
            "id": "retained",
            "kind": "kubernetes-pod-label",
            "namespace": "shop",
            "selector": "aurora.dev/replica=retained",
            "port": 80,
        },
        {
            "id": "restarted",
            "kind": "kubernetes-pod-label",
            "namespace": "shop",
            "selector": "aurora.dev/replica=restarted",
            "port": 80,
        },
    ]
    value["checks"] = [
        {"id": "retained-items", "target": "retained", "path": "/api/items", "expectedStatus": 200},
        {"id": "restarted-items", "target": "restarted", "path": "/api/items", "expectedStatus": 200},
    ]
    value = freeze_scenario(value)

    verdict, reason, checks = compute_verdict(
        value,
        {},
        {"observations": []},
        {
            "retained-items": {"status": 200, "error": None},
            "restarted-items": {"status": 504, "error": None},
        },
    )

    assert verdict == "failed"
    assert "observed HTTP 504" in reason
    assert checks[0]["targetSelector"] == "aurora.dev/replica=retained"
    assert checks[1]["targetSelector"] == "aurora.dev/replica=restarted"


def test_shared_agent_tools_cover_navigation_reading_interaction_and_evidence():
    tools = agent_browser_tools(["preview", "retained"])

    assert {tool["name"] for tool in tools} == {
        "navigate_browser",
        "read_visible_text",
        "read_http_status",
        "inspect_row",
        "click_visible_text",
        "fill_by_label",
        "take_screenshot",
    }
    for tool in tools:
        target = tool["parameters"]["properties"]["target"]
        assert target["enum"] == ["preview", "retained"]


def test_status_evidence_is_bound_to_the_frozen_target_and_path():
    value = scenario()
    value["targets"] = [
        {
            "id": "retained",
            "kind": "kubernetes-pod-label",
            "namespace": "shop",
            "selector": "aurora.dev/replica=retained",
            "port": 80,
        },
        {
            "id": "restarted",
            "kind": "kubernetes-pod-label",
            "namespace": "shop",
            "selector": "aurora.dev/replica=restarted",
            "port": 80,
        },
    ]
    value["checks"] = [
        {"id": "retained-items", "target": "retained", "path": "/api/items", "expectedStatus": 200},
        {"id": "restarted-items", "target": "restarted", "path": "/api/items", "expectedStatus": 200},
    ]
    evidence = [
        {"tool": "navigate_browser", "target": "retained", "result": {"path": "/api/items", "status": 200}},
        {"tool": "navigate_browser", "target": "restarted", "result": {"path": "/healthz", "status": 200}},
        {"tool": "read_http_status", "target": "restarted", "result": {"path": "/api/items", "status": 504}},
    ]

    assert _status_observations_from_evidence(value, evidence) == {
        "retained-items": {"path": "/api/items", "status": 200},
        "restarted-items": {"path": "/api/items", "status": 504},
    }


def test_badge_selection_is_derived_from_unique_recorded_candidates():
    candidates = {
        "a": candidate("a", "Avenue Rd", "Lane restriction"),
        "q": candidate("q", "Queen St E", "Road closed"),
    }

    selections = _selections_from_candidates(scenario(), candidates)

    assert selections == {
        "observations": [
            {"checkId": "avenue", "candidateId": "a"},
            {"checkId": "queen", "candidateId": "q"},
        ]
    }
