"""apply_correlation_outcome: mode routing for a positive rule-correlator match.

off/shadow: legacy attach (handle_correlated_alert) and True — caller commits
and skips incident creation. live: hint stashed on alert_metadata and False —
caller falls through to normal incident creation.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from services.correlation.alert_correlator import (  # noqa: E402
    CorrelationResult,
    apply_correlation_outcome,
    attach_correlation_hint,
)


def _call(mode, alert_metadata, hint_only_eligible=True):
    result = CorrelationResult(
        is_correlated=True, incident_id="inc-1", score=0.72, strategy="similarity"
    )
    env = {"RECURRENCE_DETECTION_MODE": mode}
    with patch.dict(os.environ, env):
        with patch(
            "services.correlation.alert_correlator.handle_correlated_alert"
        ) as mock_handle:
            returned = apply_correlation_outcome(
                cursor=MagicMock(name="cursor"),
                user_id="u1",
                incident_id="inc-1",
                source_type="datadog",
                source_alert_id=7,
                alert_title="High CPU",
                alert_service="api",
                alert_severity="critical",
                correlation_result=result,
                alert_metadata=alert_metadata,
                raw_payload={"k": "v"},
                org_id="org-1",
                hint_only_eligible=hint_only_eligible,
            )
    return returned, mock_handle


class TestApplyCorrelationOutcome:
    def test_off_delegates_to_legacy_attach(self):
        meta = {}
        returned, mock_handle = _call("off", meta)
        assert returned is True
        mock_handle.assert_called_once()
        assert "correlation_hint" not in meta

    def test_shadow_delegates_to_legacy_attach(self):
        meta = {}
        returned, mock_handle = _call("shadow", meta)
        assert returned is True
        mock_handle.assert_called_once()
        assert "correlation_hint" not in meta

    def test_live_stashes_hint_and_returns_false(self):
        meta = {}
        returned, mock_handle = _call("live", meta)
        assert returned is False
        mock_handle.assert_not_called()
        hint = meta["correlation_hint"]
        assert hint["incident_id"] == "inc-1"
        assert hint["score"] == pytest.approx(0.72)
        assert hint["strategy"] == "similarity"
        assert hint["computed_at"]

    def test_live_ineligible_fallthrough_keeps_legacy_attach(self):
        # When the caller's fall-through would NOT create an incident (e.g.
        # RCA disabled), live mode must not drop the alert: legacy attach.
        meta = {}
        returned, mock_handle = _call("live", meta, hint_only_eligible=False)
        assert returned is True
        mock_handle.assert_called_once()
        assert "correlation_hint" not in meta

    def test_unknown_mode_treated_as_off(self):
        meta = {}
        returned, mock_handle = _call("bananas", meta)
        assert returned is True
        mock_handle.assert_called_once()
        assert "correlation_hint" not in meta


class TestAttachCorrelationHint:
    def test_hint_is_json_serializable(self):
        # The mutated dict is inserted via json.dumps by every provider task.
        meta = {}
        attach_correlation_hint(
            meta,
            CorrelationResult(
                is_correlated=True, incident_id="inc-2", score=0.61, strategy="topology"
            ),
        )
        json.dumps(meta)
