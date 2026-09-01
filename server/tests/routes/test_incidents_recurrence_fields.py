"""Pure-function tests for the recurrence fields emitted by
``routes.incidents_routes._format_incident_response`` (root-cause dedup, layer 2).

The list/detail SELECTs append ``recurrence_of_incident_id`` and the anchor's
title as the last two columns of the ``include_merge_target`` tuple; the
narrower branches must default the pointer to ``None``.
"""

import uuid
from datetime import datetime, timezone

import pytest

ANCHOR_ID = uuid.uuid4()
INCIDENT_ID = uuid.uuid4()
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _base_row():
    """The 19 leading columns shared by every formatter branch."""
    return [
        INCIDENT_ID, "user-1", "datadog", "alert-1", "analyzed", "high",
        "CPU high", "api", "prod", "idle", "summary", None,
        NOW, NOW, "thoughts", NOW, NOW, None, NOW,
    ]


def _merge_target_row(recurrence_of=None, recurrence_title=None):
    return tuple(
        _base_row()
        + [
            {"k": "v"}, 2, ["api"],  # alert_metadata, correlated_alert_count, affected_services
            None, None,  # merged_into_incident_id, merged_into_title
            recurrence_of, recurrence_title,
        ]
    )


@pytest.fixture
def fmt(monkeypatch):
    from routes import incidents_routes

    # The formatter resolves the alert's source URL through the DB; stub it so
    # these stay pure-function tests (CI runs without Postgres).
    monkeypatch.setattr(incidents_routes, "_build_source_url", lambda *_: "")
    return incidents_routes._format_incident_response


def test_member_row_emits_pointer_and_anchor_title(fmt):
    result = fmt(
        _merge_target_row(ANCHOR_ID, "Earlier CPU high"),
        include_metadata=True, include_correlation=True, include_merge_target=True,
    )
    assert result["recurrenceOf"] == str(ANCHOR_ID)
    assert result["recurrenceOfTitle"] == "Earlier CPU high"
    assert "mergedIntoIncidentId" not in result
    assert result["correlatedAlertCount"] == 2


def test_anchor_row_emits_null_pointer_without_title(fmt):
    result = fmt(
        _merge_target_row(),
        include_metadata=True, include_correlation=True, include_merge_target=True,
    )
    assert result["recurrenceOf"] is None
    assert "recurrenceOfTitle" not in result


@pytest.mark.parametrize(
    "kwargs, extra_columns",
    [
        ({"include_correlation": True}, [{"k": "v"}, 2, ["api"]]),
        ({"include_metadata": True}, [{"k": "v"}]),
        ({}, []),
    ],
)
def test_narrower_branches_default_pointer_to_null(fmt, kwargs, extra_columns):
    result = fmt(tuple(_base_row() + extra_columns), **kwargs)
    assert result["id"] == str(INCIDENT_ID)
    assert result["recurrenceOf"] is None
    assert "recurrenceOfTitle" not in result
