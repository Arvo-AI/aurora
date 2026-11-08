"""Tests for evidence bundle normalization."""

from __future__ import annotations

from datetime import timezone

import pytest

from incident_replay.evidence import (
    BUNDLE_NAMES,
    RECORD_ALERT,
    RECORD_DEPLOY_CHANGE,
    RECORD_DEPLOY_EVENT,
    EvidenceBundle,
    parse_ts,
    normalize_bundle,
)


class TestParseTimestamp:
    def test_accepts_zulu_and_normalizes_to_utc(self):
        parsed = parse_ts("2026-11-06T14:36:12Z")
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.astimezone(timezone.utc).hour == 14

    def test_accepts_explicit_offset_and_converts(self):
        parsed = parse_ts("2026-11-06T09:36:12-05:00")
        assert parsed is not None
        assert parsed.astimezone(timezone.utc).hour == 14

    @pytest.mark.parametrize(
        "raw",
        ["", None, "yesterday", "2026/11/06 14:36", "14:36:12", "2026-13-40T00:00:00Z"],
    )
    def test_rejects_everything_else_instead_of_guessing(self, raw):
        # A timestamp the parser does not understand must not be assumed to be
        # UTC; the record keeps at=None and the bundle reports a warning.
        assert parse_ts(raw) is None


class TestNormalizeBundle:
    def test_rejects_unknown_bundle_name(self):
        with pytest.raises(ValueError):
            normalize_bundle("../../etc/passwd")

    def test_ships_the_checkout_bundle(self):
        assert "checkout-api" in BUNDLE_NAMES

    def test_produces_numbered_records_without_warnings(self):
        bundle = normalize_bundle("checkout-api")
        assert bundle.warnings == []
        assert len(bundle.records) >= 40
        # IDs are dense, one-based and ascend with the timeline, so citation [n]
        # both addresses a record and reads in chronological order.
        assert [r.id for r in bundle.records] == list(range(1, len(bundle.records) + 1))

    def test_every_record_has_provenance(self):
        bundle = normalize_bundle("checkout-api")
        for record in bundle.records:
            assert record.source, record
            assert record.locator, record
            assert record.content.strip(), record

    def test_timestamped_records_are_sorted(self):
        bundle = normalize_bundle("checkout-api")
        timed = [r.at for r in bundle.records if r.at]
        assert timed == sorted(timed)

    def test_runbook_records_sort_last(self):
        bundle = normalize_bundle("checkout-api")
        types = [r.record_type for r in bundle.records]
        first_runbook = types.index("runbook")
        assert all(t == "runbook" for t in types[first_runbook:]), (
            "records without a timestamp must not interleave with the timeline"
        )

    def test_deployment_splits_into_event_and_field_changes(self):
        # "a deploy happened" and "this value changed" are different claims and
        # must be separately citable.
        bundle = normalize_bundle("checkout-api")
        events = [r for r in bundle.records if r.record_type == RECORD_DEPLOY_EVENT]
        changes = [r for r in bundle.records if r.record_type == RECORD_DEPLOY_CHANGE]
        assert len(events) == 1
        assert len(changes) >= 2
        assert any("maxPoolSize" in c.content for c in changes)

    def test_alert_record_present(self):
        bundle = normalize_bundle("checkout-api")
        alerts = [r for r in bundle.records if r.record_type == RECORD_ALERT]
        assert len(alerts) == 1
        assert "5xx" in alerts[0].content

    def test_prompt_block_lists_every_id(self):
        bundle = normalize_bundle("checkout-api")
        block = bundle.to_prompt_block()
        for record in bundle.records:
            assert f"[{record.id}]" in block

    def test_by_id_is_bounds_safe(self):
        bundle: EvidenceBundle = normalize_bundle("checkout-api")
        assert bundle.by_id(1) is not None
        assert bundle.by_id(99999) is None
