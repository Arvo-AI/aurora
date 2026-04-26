"""Tests for utils.payload_timestamp -- alert timestamp extraction utilities."""

import os
import sys
import pytest
from datetime import datetime, timezone

# Ensure server/ is on sys.path
_server_dir = os.path.join(os.path.dirname(__file__), os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from utils.payload_timestamp import parse_timestamp, extract_alert_fired_at, _walk


class TestParseTimestamp:
    """Tests for coercing various formats into UTC datetime."""

    def test_none_returns_none(self):
        """None input must return None."""
        assert parse_timestamp(None) is None

    def test_empty_string_returns_none(self):
        """Empty string input must return None."""
        assert parse_timestamp("") is None

    def test_bool_returns_none(self):
        """bool is a subclass of int -- must not be treated as a timestamp."""
        assert parse_timestamp(True) is None
        assert parse_timestamp(False) is None

    def test_random_string_returns_none(self):
        """Unparseable string must return None."""
        assert parse_timestamp("not-a-date") is None

    def test_list_returns_none(self):
        """Non-scalar types like list must return None."""
        assert parse_timestamp([1, 2, 3]) is None

    def test_iso_with_z_suffix(self):
        """ISO 8601 string with Z suffix must parse correctly."""
        result = parse_timestamp("2024-06-15T10:30:00Z")
        assert result == datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)

    def test_iso_with_offset(self):
        """ISO 8601 string with explicit UTC offset must parse correctly."""
        result = parse_timestamp("2024-06-15T10:30:00+00:00")
        assert result is not None
        assert result.tzinfo is not None

    def test_iso_without_timezone(self):
        """ISO string without tz info returns a naive datetime (no UTC tag)."""
        result = parse_timestamp("2024-06-15T10:30:00")
        assert result is not None
        assert result.tzinfo is None

    def test_iso_with_microseconds(self):
        """ISO 8601 string with microseconds must preserve them."""
        result = parse_timestamp("2024-06-15T10:30:00.123456Z")
        assert result is not None
        assert result.microsecond == 123456

    def test_unix_seconds(self):
        """Standard Unix timestamp in seconds must parse correctly."""
        result = parse_timestamp(1718451000)
        assert result is not None
        assert result.year == 2024

    def test_unix_milliseconds(self):
        """Timestamps > 1e12 are treated as milliseconds."""
        result = parse_timestamp(1718451000000)
        assert result is not None
        assert result.year == 2024

    def test_unix_float(self):
        """Floating-point Unix timestamps must be accepted."""
        result = parse_timestamp(1718451000.5)
        assert result is not None

    def test_unix_zero(self):
        """Epoch 0 is a valid Unix timestamp."""
        result = parse_timestamp(0)
        assert result is not None
        assert result == datetime(1970, 1, 1, tzinfo=timezone.utc)

    def test_overflow_returns_none(self):
        """Extremely large values must not crash -- return None instead."""
        assert parse_timestamp(99999999999999999) is None

    def test_aware_datetime_passthrough(self):
        """Already timezone-aware datetime must be returned as-is."""
        dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
        assert parse_timestamp(dt) is dt

    def test_naive_datetime_tagged_utc(self):
        """Naive datetime objects must be tagged with UTC."""
        dt = datetime(2024, 1, 1)
        result = parse_timestamp(dt)
        assert result.tzinfo == timezone.utc

    def test_tz_aware_inputs_return_aware_datetime(self):
        """Inputs that carry tz info must return timezone-aware datetimes."""
        test_values = [
            "2024-06-15T10:30:00Z",
            "2024-06-15T10:30:00+00:00",
            1718451000,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        ]
        for val in test_values:
            result = parse_timestamp(val)
            assert result is not None, f"Failed for {val}"
            assert result.tzinfo is not None, f"Missing tzinfo for {val}"


class TestWalk:
    """Tests for the dot-separated path walker."""

    def test_simple_key(self):
        """Single-level dict key lookup must work."""
        assert _walk({"name": "Aurora"}, "name") == "Aurora"

    def test_nested_key(self):
        """Two-level nested dict lookup must work."""
        data = {"alert": {"status": "firing"}}
        assert _walk(data, "alert.status") == "firing"

    def test_deeply_nested(self):
        """Four-level nested dict lookup must work."""
        data = {"a": {"b": {"c": {"d": "deep"}}}}
        assert _walk(data, "a.b.c.d") == "deep"

    def test_list_index(self):
        """Numeric path components must index into lists."""
        data = {"alerts": [{"name": "first"}, {"name": "second"}]}
        assert _walk(data, "alerts.0.name") == "first"
        assert _walk(data, "alerts.1.name") == "second"

    def test_missing_key_returns_none(self):
        """Missing top-level key must return None."""
        assert _walk({"a": 1}, "b") is None

    def test_missing_nested_key_returns_none(self):
        """Missing nested key must return None."""
        assert _walk({"a": {"b": 1}}, "a.c") is None

    def test_list_index_out_of_range(self):
        """Out-of-range list index must return None."""
        assert _walk({"items": [1, 2]}, "items.5") is None

    def test_none_input(self):
        """None as root object must return None."""
        assert _walk(None, "any.path") is None

    def test_non_dict_input(self):
        """Non-dict root object must return None."""
        assert _walk("string", "key") is None

    def test_empty_dict(self):
        """Empty dict must return None for any key."""
        assert _walk({}, "key") is None

    def test_walk_through_mixed_types(self):
        """Dict containing list containing dict must be navigable."""
        data = {
            "incidents": [
                {"alerts": [{"startsAt": "2024-01-01T00:00:00Z"}]}
            ]
        }
        assert _walk(data, "incidents.0.alerts.0.startsAt") == "2024-01-01T00:00:00Z"


class TestExtractAlertFiredAt:
    """Tests for the multi-path timestamp extraction."""

    def test_first_valid_path_wins(self):
        """When multiple paths match, the first one must be returned."""
        payload = {
            "startsAt": "2024-06-15T10:30:00Z",
            "created_at": "2024-06-15T09:00:00Z",
        }
        result = extract_alert_fired_at(payload, ["startsAt", "created_at"])
        assert result == datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)

    def test_skips_invalid_paths(self):
        """Invalid paths must be skipped until a valid one is found."""
        payload = {"created_at": "2024-06-15T10:30:00Z"}
        result = extract_alert_fired_at(payload, [
            "nonexistent",
            "also.missing",
            "created_at",
        ])
        assert result is not None
        assert result.year == 2024

    def test_nested_path(self):
        """Dot-separated nested paths must be resolved correctly."""
        payload = {
            "incident": {
                "created_at": "2024-06-15T10:30:00Z"
            }
        }
        result = extract_alert_fired_at(payload, ["incident.created_at"])
        assert result is not None

    def test_no_valid_paths_returns_none(self):
        """When no paths match, must return None."""
        payload = {"unrelated": "data"}
        result = extract_alert_fired_at(payload, ["startsAt", "created_at"])
        assert result is None

    def test_empty_paths_returns_none(self):
        """Empty paths iterable must return None."""
        payload = {"startsAt": "2024-06-15T10:30:00Z"}
        result = extract_alert_fired_at(payload, [])
        assert result is None

    def test_pagerduty_style_payload(self):
        """Simulates a PagerDuty webhook payload structure."""
        payload = {
            "event": {
                "occurred_at": "2024-06-15T10:30:00Z",
                "data": {"id": "P123"}
            }
        }
        result = extract_alert_fired_at(payload, ["event.occurred_at"])
        assert result is not None

    def test_datadog_style_payload(self):
        """Simulates a Datadog webhook with Unix millisecond timestamp."""
        payload = {
            "last_updated": 1718451000000,
            "title": "CPU High",
        }
        result = extract_alert_fired_at(payload, ["last_updated"])
        assert result is not None
        assert result.year == 2024

    def test_grafana_style_payload(self):
        """Simulates a Grafana alert with nested alerts array."""
        payload = {
            "alerts": [
                {"startsAt": "2024-06-15T10:30:00Z", "status": "firing"}
            ]
        }
        result = extract_alert_fired_at(payload, ["alerts.0.startsAt"])
        assert result is not None
