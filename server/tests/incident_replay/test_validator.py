"""Tests for deterministic evidence validation.

The validator is the contribution under test here: every case below asserts that
a claim the model could plausibly make is either verified against the bundle or
refused, and that a causal statement is never promoted to fact.
"""

from __future__ import annotations

import pytest

from incident_replay.evidence import (
    RECORD_DEPLOY_CHANGE,
    RECORD_DEPLOY_EVENT,
    RECORD_LOG,
    RECORD_METRIC,
    normalize_bundle,
)
from incident_replay.validator import (
    INVALID_CITATION,
    SUPPORTED_INFERENCE,
    UNVERIFIED_HYPOTHESIS,
    VERIFIED_OBSERVATION,
    validate,
)


@pytest.fixture(scope="module")
def bundle():
    return normalize_bundle("checkout-api")


def _id_of(bundle, record_type: str, needle: str) -> int:
    """Resolve a record ID by content so tests survive fixture edits."""
    for record in bundle.records:
        if record.record_type == record_type and needle in record.content:
            return record.id
    raise AssertionError(f"no {record_type} record containing {needle!r}")


def _result(*findings, timeline=None):
    return {"findings": list(findings), "timeline": timeline or []}


def _finding(claim_type, claim, ids, confidence="high"):
    return {
        "claim_type": claim_type,
        "claim": claim,
        "evidence_ids": ids,
        "confidence": confidence,
    }


class TestCitationIntegrity:
    def test_fabricated_id_is_rejected(self, bundle):
        report = validate(bundle, _result(_finding("metric_degradation", "x", [9999])))
        assert report.findings[0].status == INVALID_CITATION
        assert 9999 in report.missing_ids
        assert report.citation_resolution_rate == 0.0

    def test_cited_ids_that_exist_raise_the_resolution_rate(self, bundle):
        real = _id_of(bundle, RECORD_METRIC, "http_5xx_rate")
        report = validate(
            bundle,
            _result(_finding("metric_degradation", "http_5xx_rate rose", [real, 9999])),
        )
        # One of two IDs resolved.
        assert report.total_citations == 2
        assert report.resolved_citations == 1
        assert report.citation_resolution_rate == 0.5

    @pytest.mark.parametrize("bad", [None, True, 3.5, "ev_deploy_003", {"id": 1}])
    def test_malformed_ids_are_rejected_not_coerced(self, bundle, bad):
        real = _id_of(bundle, RECORD_METRIC, "http_5xx_rate")
        report = validate(
            bundle, _result(_finding("metric_degradation", "http_5xx_rate rose", [real, bad]))
        )
        assert report.findings[0].status == INVALID_CITATION

    def test_numeric_string_ids_are_tolerated(self, bundle):
        real = _id_of(bundle, RECORD_METRIC, "http_5xx_rate")
        report = validate(
            bundle, _result(_finding("metric_degradation", "http_5xx_rate rose", [str(real)]))
        )
        assert report.findings[0].status == VERIFIED_OBSERVATION


class TestClaimSupport:
    def test_config_change_supported_by_matching_field(self, bundle):
        pool = _id_of(bundle, RECORD_DEPLOY_CHANGE, "maxPoolSize")
        report = validate(
            bundle,
            _result(_finding("configuration_change",
                             "db.pool.maxPoolSize changed from 50 to 10.", [pool])),
        )
        assert report.findings[0].status == VERIFIED_OBSERVATION

    def test_config_change_citing_a_log_line_is_not_supported(self, bundle):
        log = _id_of(bundle, RECORD_LOG, "HikariPool")
        report = validate(
            bundle,
            _result(_finding("configuration_change",
                             "db.pool.maxPoolSize changed from 50 to 10.", [log])),
        )
        # The reference is real, but a log line cannot evidence a config change.
        assert report.findings[0].status == UNVERIFIED_HYPOTHESIS
        assert "must cite at least one deploy_change" in " ".join(report.findings[0].reasons)

    def test_config_change_naming_a_field_absent_from_evidence(self, bundle):
        pool = _id_of(bundle, RECORD_DEPLOY_CHANGE, "maxPoolSize")
        report = validate(
            bundle,
            _result(_finding("configuration_change",
                             "db.pool.replicaCount changed from 4 to 1.", [pool])),
        )
        assert report.findings[0].status == UNVERIFIED_HYPOTHESIS

    def test_error_observed_needs_a_log_with_the_same_error(self, bundle):
        log = _id_of(bundle, RECORD_LOG, "HikariPool")
        report = validate(
            bundle,
            _result(_finding("error_observed",
                             "HikariPool connection acquisition timed out.", [log])),
        )
        assert report.findings[0].status == VERIFIED_OBSERVATION

    def test_metric_degradation_needs_the_metric_name_in_the_claim(self, bundle):
        metric = _id_of(bundle, RECORD_METRIC, "p99_latency_ms")
        ok = validate(bundle, _result(_finding("metric_degradation",
                                               "p99_latency_ms rose to 2400ms", [metric])))
        assert ok.findings[0].status == VERIFIED_OBSERVATION
        mismatch = validate(bundle, _result(_finding("metric_degradation",
                                                     "cpu saturation rose", [metric])))
        assert mismatch.findings[0].status == UNVERIFIED_HYPOTHESIS

    def test_deployment_observed_cites_the_deploy_event(self, bundle):
        event = _id_of(bundle, RECORD_DEPLOY_EVENT, "deploy-4471")
        report = validate(
            bundle, _result(_finding("deployment_observed", "deploy-4471 ran", [event]))
        )
        assert report.findings[0].status == VERIFIED_OBSERVATION

    def test_deployment_observed_must_not_cite_a_field_change(self, bundle):
        change = _id_of(bundle, RECORD_DEPLOY_CHANGE, "maxPoolSize")
        report = validate(
            bundle, _result(_finding("deployment_observed", "deploy-4471 ran", [change]))
        )
        assert report.findings[0].status == UNVERIFIED_HYPOTHESIS


class TestCausalityIsNeverPromoted:
    def test_causal_claim_with_perfect_citations_stays_a_hypothesis(self, bundle):
        pool = _id_of(bundle, RECORD_DEPLOY_CHANGE, "maxPoolSize")
        log = _id_of(bundle, RECORD_LOG, "HikariPool")
        metric = _id_of(bundle, RECORD_METRIC, "db_pool_wait_queue")
        report = validate(
            bundle,
            _result(_finding(
                "causal_hypothesis",
                "Reducing db.pool.maxPoolSize caused connection timeouts and 5xx.",
                [pool, log, metric],
            )),
        )
        assert report.findings[0].status == UNVERIFIED_HYPOTHESIS
        assert report.citation_resolution_rate == 1.0, (
            "every citation resolves, and the claim is still not verified"
        )

    def test_temporal_relationship_is_an_inference_not_a_fact(self, bundle):
        event = _id_of(bundle, RECORD_DEPLOY_EVENT, "deploy-4471")
        log = _id_of(bundle, RECORD_LOG, "HikariPool")
        report = validate(
            bundle,
            _result(_finding("temporal_relationship",
                             "the deploy preceded the timeouts", [event, log])),
        )
        assert report.findings[0].status == SUPPORTED_INFERENCE


class TestTimeline:
    def test_out_of_order_timeline_is_flagged(self, bundle):
        report = validate(
            bundle,
            _result(timeline=[
                {"at": "2026-11-06T14:40:00Z", "event": "later", "evidence_ids": []},
                {"at": "2026-11-06T14:20:00Z", "event": "earlier", "evidence_ids": []},
            ]),
        )
        assert report.timeline_ok is False
        assert report.timeline_problems

    def test_timeline_citing_missing_evidence_is_flagged(self, bundle):
        report = validate(
            bundle,
            _result(timeline=[{"at": "2026-11-06T14:40:00Z", "event": "x",
                               "evidence_ids": [424242]}]),
        )
        assert report.timeline_ok is False

    def test_ordered_timeline_passes(self, bundle):
        report = validate(
            bundle,
            _result(timeline=[
                {"at": "2026-11-06T14:20:00Z", "event": "a", "evidence_ids": []},
                {"at": "2026-11-06T14:40:00Z", "event": "b", "evidence_ids": []},
            ]),
        )
        assert report.timeline_ok is True


class TestUnknownInput:
    def test_unknown_claim_type_is_reported(self, bundle):
        metric = _id_of(bundle, RECORD_METRIC, "http_5xx_rate")
        report = validate(bundle, _result(_finding("vibes", "trust me", [metric])))
        assert report.findings[0].status == UNVERIFIED_HYPOTHESIS
        assert "vibes" in report.unknown_claim_types

    def test_finding_with_no_citations(self, bundle):
        report = validate(bundle, _result(_finding("metric_degradation", "x", [])))
        assert report.findings[0].status == UNVERIFIED_HYPOTHESIS

    def test_non_dict_findings_are_skipped(self, bundle):
        report = validate(bundle, {"findings": ["nope", 42, None], "timeline": []})
        assert report.findings == []
