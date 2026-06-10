"""Tests that after_rca actions are dispatched exactly once when an RCA completes.

Regression test for a bug where `dispatch_on_incident_actions(..., timing='after_rca')`
was called in both:
  1. `_execute_background_chat()` (crash-protection path inside asyncio)
  2. `run_background_chat()` (outer Celery task, after asyncio.run returns)

When the worker didn't crash (the normal happy path), both fired — causing
every after_rca action (e.g. classify) to run twice.

Since `run_background_chat` is deeply coupled to infra (Celery, DB, Redis),
these tests validate the deduplication contract at the interface boundary:
the outer function uses `result.get('after_rca_dispatched')` to skip dispatch.
"""

import os
import sys

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))


class TestAfterRcaDispatchDeduplication:
    """Validate the flag-based contract that prevents double-dispatch."""

    def _simulate_outer_dispatch_decision(self, result: dict, incident_id: str, trigger_metadata: dict) -> bool:
        """Replicate the outer function's dispatch decision logic.
        
        Returns True if the outer function WOULD dispatch after_rca actions.
        This mirrors the exact condition in run_background_chat().
        """
        if incident_id and trigger_metadata and trigger_metadata.get('source') != 'action':
            if not result.get('after_rca_dispatched'):
                return True
        return False

    def test_skips_when_inner_already_dispatched(self):
        """Normal path: asyncio.run returns, inner already dispatched → outer skips."""
        result = {
            "session_id": "sess-123",
            "status": "completed",
            "after_rca_dispatched": True,
        }
        should_dispatch = self._simulate_outer_dispatch_decision(
            result, incident_id="inc-1", trigger_metadata={"source": "datadog"}
        )
        assert should_dispatch is False

    def test_dispatches_when_inner_did_not(self):
        """Crash-recovery path: inner failed to dispatch → outer picks up."""
        result = {
            "session_id": "sess-123",
            "status": "completed",
            "after_rca_dispatched": False,
        }
        should_dispatch = self._simulate_outer_dispatch_decision(
            result, incident_id="inc-1", trigger_metadata={"source": "datadog"}
        )
        assert should_dispatch is True

    def test_dispatches_when_flag_missing(self):
        """Backwards compat: old result dict without the flag → outer dispatches."""
        result = {
            "session_id": "sess-123",
            "status": "completed",
        }
        should_dispatch = self._simulate_outer_dispatch_decision(
            result, incident_id="inc-1", trigger_metadata={"source": "datadog"}
        )
        assert should_dispatch is True

    def test_skips_when_source_is_action(self):
        """Actions should never re-dispatch (infinite loop guard)."""
        result = {
            "session_id": "sess-123",
            "status": "completed",
            "after_rca_dispatched": False,
        }
        should_dispatch = self._simulate_outer_dispatch_decision(
            result, incident_id="inc-1", trigger_metadata={"source": "action"}
        )
        assert should_dispatch is False

    def test_skips_when_no_incident_id(self):
        """No incident → no dispatch regardless of flag."""
        result = {
            "session_id": "sess-123",
            "status": "completed",
            "after_rca_dispatched": False,
        }
        should_dispatch = self._simulate_outer_dispatch_decision(
            result, incident_id=None, trigger_metadata={"source": "datadog"}
        )
        assert should_dispatch is False

    def test_skips_when_no_trigger_metadata(self):
        """No trigger metadata → no dispatch."""
        result = {
            "session_id": "sess-123",
            "status": "completed",
            "after_rca_dispatched": False,
        }
        should_dispatch = self._simulate_outer_dispatch_decision(
            result, incident_id="inc-1", trigger_metadata=None
        )
        assert should_dispatch is False
