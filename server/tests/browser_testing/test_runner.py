from browser_testing.browser import RowCandidate
from browser_testing.runner import compute_verdict
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
