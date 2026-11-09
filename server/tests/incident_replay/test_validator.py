"""Tests for deterministic evidence validation.

The validator is the contribution under test here: every case below asserts that
a claim the model could plausibly make is either verified against the bundle or
refused, and that a causal statement is never promoted to fact.
"""

from __future__ import annotations

import pytest

from incident_replay.evidence import (
    RECORD_ALERT,
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


def _ids_of(bundle, record_type: str, needle: str) -> list[int]:
    """Every matching record id, for claims that span several samples."""
    ids = [
        record.id for record in bundle.records
        if record.record_type == record_type and needle in record.content
    ]
    if not ids:
        raise AssertionError(f"no {record_type} record containing {needle!r}")
    return ids


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
        # A claim spanning a window cites the samples it quotes from.
        metrics = _ids_of(bundle, RECORD_METRIC, "p99_latency_ms")
        ok = validate(bundle, _result(_finding("metric_degradation",
                                               "p99_latency_ms rose to 2400ms", metrics)))
        assert ok.findings[0].status == VERIFIED_OBSERVATION
        mismatch = validate(bundle, _result(_finding("metric_degradation",
                                                     "cpu saturation rose", metrics)))
        assert mismatch.findings[0].status == UNVERIFIED_HYPOTHESIS

    def test_alert_claim_is_verified_against_an_alert_record(self, bundle):
        # Without this claim type the only bucket that fits an alert is
        # error_observed, which demands a log line and demotes a correct claim.
        alert = _id_of(bundle, RECORD_ALERT, "PD-88213")
        report = validate(
            bundle,
            _result(_finding("alert_observed",
                             "PD-88213 fired for checkout-api HTTP 5xx rate 7.8%.", [alert])),
        )
        assert report.findings[0].status == VERIFIED_OBSERVATION

    def test_alert_claim_citing_a_log_is_refused(self, bundle):
        log_id = _id_of(bundle, RECORD_LOG, "HikariPool")
        report = validate(
            bundle,
            _result(_finding("alert_observed", "PD-88213 fired.", [log_id])),
        )
        assert report.findings[0].status == UNVERIFIED_HYPOTHESIS
        assert "must cite at least one alert record" in " ".join(report.findings[0].reasons)

    def test_metric_named_in_prose_rather_than_by_identifier(self, bundle):
        # A record calls it http_5xx_rate; a model writing prose calls it "5xx
        # rate". Requiring the exact identifier demoted correct claims.
        metrics = _ids_of(bundle, RECORD_METRIC, "http_5xx_rate")
        report = validate(
            bundle,
            _result(_finding("metric_degradation",
                             "5xx rate rose from 0.4% to 7.8% during the window.", metrics)),
        )
        assert report.findings[0].status == VERIFIED_OBSERVATION

    def test_one_shared_word_is_not_enough_to_match_a_metric(self, bundle):
        # "rate" alone appears in several metrics; that must not count as naming one.
        metrics = _ids_of(bundle, RECORD_METRIC, "http_5xx_rate")
        report = validate(
            bundle,
            _result(_finding("metric_degradation", "the rate was elevated.", metrics)),
        )
        assert report.findings[0].status == UNVERIFIED_HYPOTHESIS

    def test_metric_claim_stating_a_value_the_samples_do_not_have(self, bundle):
        metrics = _ids_of(bundle, RECORD_METRIC, "p99_latency_ms")
        report = validate(
            bundle,
            _result(_finding("metric_degradation", "p99_latency_ms reached 9000ms", metrics)),
        )
        assert report.findings[0].status == UNVERIFIED_HYPOTHESIS
        assert "values the cited samples do not contain" in " ".join(report.findings[0].reasons)

    def test_config_claim_stating_the_wrong_new_value(self, bundle):
        pool = _id_of(bundle, RECORD_DEPLOY_CHANGE, "maxPoolSize")
        report = validate(
            bundle,
            _result(_finding("configuration_change",
                             "db.pool.maxPoolSize changed from 50 to 25.", [pool])),
        )
        assert report.findings[0].status == UNVERIFIED_HYPOTHESIS
        assert "value the evidence does not record" in " ".join(report.findings[0].reasons)

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
    @staticmethod
    def _bundle(records):
        from incident_replay.evidence import EvidenceBundle

        bundle = EvidenceBundle(name="synthetic")
        bundle.records = records
        return bundle

    @staticmethod
    def _log(record_id, at):
        from incident_replay.evidence import EvidenceRecord

        return EvidenceRecord(
            id=record_id, source="synthetic", locator=f"l{record_id}",
            record_type=RECORD_LOG, at=at, at_raw=at, content="timeout",
        )

    def test_entry_timestamp_must_match_its_single_citation(self):
        # An entry may not cite a 14:35 record while claiming the event was at
        # 14:20; a monotonic list alone would not notice.
        bundle = self._bundle([self._log(1, "2026-11-06T14:35:21Z")])
        report = validate(
            bundle,
            _result(timeline=[{"at": "2026-11-06T14:20:00Z", "event": "x",
                               "evidence_ids": [1]}]),
        )
        assert report.timeline_ok is False
        assert "evidence is stamped" in " ".join(report.timeline_problems)

    def test_quoting_a_second_timestamp_from_the_record_content_is_accepted(self):
        # A deploy record states its start *and* its completion. An entry that
        # quotes the completion is accurate even though the record's own stamp
        # is the start.
        from incident_replay.evidence import EvidenceRecord

        bundle = self._bundle([
            EvidenceRecord(
                id=1, source="d", locator="l1", record_type=RECORD_DEPLOY_EVENT,
                at="2026-11-06T14:32:08Z", at_raw="",
                content="deploy deploy-4471 completed_at=2026-11-06T14:33:41Z",
            )
        ])
        report = validate(
            bundle,
            _result(timeline=[{"at": "2026-11-06T14:33:41Z", "event": "x",
                               "evidence_ids": [1]}]),
        )
        assert report.timeline_ok is True

    def test_minute_level_rounding_is_tolerated(self):
        bundle = self._bundle([self._log(1, "2026-11-06T14:35:21Z")])
        report = validate(
            bundle,
            _result(timeline=[{"at": "2026-11-06T14:35:00Z", "event": "x",
                               "evidence_ids": [1]}]),
        )
        assert report.timeline_ok is True

    def test_multi_citation_entry_must_fall_inside_their_span(self):
        bundle = self._bundle([
            self._log(1, "2026-11-06T14:35:21Z"),
            self._log(2, "2026-11-06T14:40:00Z"),
        ])
        inside = validate(
            bundle,
            _result(timeline=[{"at": "2026-11-06T14:38:00Z", "event": "x",
                               "evidence_ids": [1, 2]}]),
        )
        assert inside.timeline_ok is True
        outside = validate(
            bundle,
            _result(timeline=[{"at": "2026-11-06T15:30:00Z", "event": "x",
                               "evidence_ids": [1, 2]}]),
        )
        assert outside.timeline_ok is False

    def test_entries_without_citations_are_counted_as_unevidenced(self, bundle):
        # Order can be fine while nothing on the timeline is actually tied to
        # evidence. Reporting only "consistent" hid that; the counts now show it.
        report = validate(
            bundle,
            _result(timeline=[
                {"at": "2026-11-06T14:35:00Z", "event": "a", "evidence_ids": []},
                {"at": "2026-11-06T14:40:00Z", "event": "b", "evidence_ids": []},
            ]),
        )
        assert report.timeline_ok is True
        assert report.timeline_entries == 2
        assert report.timeline_evidenced == 0

    def test_entry_with_a_citation_and_a_time_is_evidenced(self, bundle):
        log_id = _id_of(bundle, RECORD_LOG, "HikariPool")
        report = validate(
            bundle,
            _result(timeline=[
                {"at": "2026-11-06T14:35:21Z", "event": "a", "evidence_ids": [log_id]},
            ]),
        )
        assert report.timeline_entries == 1
        assert report.timeline_evidenced == 1

    def test_payload_declares_the_narration_unverified(self, bundle):
        payload = validate(bundle, _result()).to_dict()
        assert payload["narration"]["verified"] is False
        assert payload["narration"]["source"] == "model"

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
