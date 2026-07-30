"""Tests the server-side percentile summaries behind
``query_datadog(resource_type='metric_stats')``.

Datadog has no time-percentile capability at all -- every documented route
(``.rollup(percentile,...)``, the ``pXX:`` prefix, ``formula: "p95(a)"``, the
scalar ``percentile`` aggregator) is rejected or silently returns zero series,
and both right-sizing metrics are gauges. So percentiles are computed here, in
Python, and these tests are the only thing pinning that arithmetic.

Two properties are load-bearing and get explicit coverage:

1. A reported p95 must be a **real observed sample**, because it lands in a PR
   body as evidence a reviewer will look up in Datadog.
2. One row PER SERIES, small enough that ``_truncate_results`` can never drop a
   whole payload. The single fat dict that ``resource_type='metrics'`` returns
   truncates to ``count: 0``, which an agent cannot distinguish from "this
   workload has no data" -- and it would then recommend cutting an idle-looking
   workload. That failure mode is reproduced below to pin the contrast.

Pure functions only: no DB, no network, no Datadog credentials.
"""

import json
import os
import sys

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from chat.backend.agent.tools.datadog_tool import (  # noqa: E402
    _DATADOG_POINT_CAP,
    _MAX_STATS_SERIES,
    _MAX_INTERVAL_MS,
    _MIN_INTERVAL_MS,
    _P95_BY_SOURCE,
    _METRIC_STATS_SOURCES,
    _clamp_interval,
    _p95_datadog,
    _percentile,
    _point_cap_note,
    _pick_interval,
    _query_metric_stats,
    _query_metrics,
    _round_sig,
    _scope_and_group,
    _summarize_series,
    _truncate_results,
)
from chat.backend.agent.utils.tool_output_cap import PASS_THROUGH_CHARS  # noqa: E402

_THIRTY_DAYS_MS = 30 * 24 * 3600 * 1000


class _FakeClient:
    """Stands in for DatadogClient, recording the interval it was handed."""

    def __init__(self, series, values):
        self._series = series
        self._values = values
        self.interval = None

    def query_metrics(self, query, start_ms, end_ms, interval=None):
        self.interval = interval
        return {"data": {"attributes": {"series": self._series, "values": self._values}}}


def _make_payload(n_series, n_points, value=5.0e8):
    series = [
        {"group_tags": ["env:production", f"kube_deployment:svc-{i:03d}"],
         "unit": [{"name": "byte"}, None]}
        for i in range(n_series)
    ]
    values = [[value for _ in range(n_points)] for _ in range(n_series)]
    return series, values


# ---------------------------------------------------------------------------
# _percentile -- nearest rank
# ---------------------------------------------------------------------------


def test_percentile_single_point():
    """n=1 must not raise -- statistics.quantiles does."""
    assert _percentile([42.0], 0.95) == 42.0
    assert _percentile([42.0], 0.5) == 42.0


def test_percentile_two_points():
    """n=2 must not raise -- statistics.quantiles requires n >= 2 and interpolates."""
    assert _percentile([1.0, 2.0], 0.95) == 2.0
    assert _percentile([1.0, 2.0], 0.5) == 1.0


def test_percentile_exact_quantile_boundary():
    values = [float(i) for i in range(1, 101)]
    assert _percentile(values, 0.50) == 50.0
    assert _percentile(values, 0.95) == 95.0
    assert _percentile(values, 0.99) == 99.0
    assert _percentile(values, 1.0) == 100.0


@pytest.mark.parametrize("q", [0.5, 0.95, 0.99])
def test_percentile_returns_an_observed_sample(q):
    """The whole reason for nearest-rank: a reviewer must find this exact value."""
    values = [1.0, 7.5, 19.25, 100.0, 512.0]
    assert _percentile(values, q) in values


def test_percentile_never_indexes_out_of_range():
    for n in range(1, 40):
        values = [float(i) for i in range(n)]
        for q in (0.0, 0.5, 0.95, 0.99, 1.0):
            assert _percentile(values, q) in values


# ---------------------------------------------------------------------------
# Interval selection
# ---------------------------------------------------------------------------


def test_pick_interval_thirty_days_is_one_hour():
    """30 days -> 1h -> 720 points: legal against Datadog's 1500-point cap, and
    finer than Datadog's own 4h default for a 30-day window."""
    chosen = _pick_interval(_THIRTY_DAYS_MS)
    assert chosen == 3_600_000
    assert _THIRTY_DAYS_MS // chosen == 720


def test_pick_interval_short_window_uses_finest():
    assert _pick_interval(3600 * 1000) == _MIN_INTERVAL_MS


@pytest.mark.parametrize("days", [1, 7, 14, 30, 60, 90, 180, 250])
def test_pick_interval_stays_under_datadog_point_cap(days):
    """Every window we realistically ask for must fit in one series."""
    span = days * 24 * 3600 * 1000
    assert span // _pick_interval(span) <= _DATADOG_POINT_CAP


def test_window_beyond_the_point_cap_is_flagged_not_silently_clipped():
    """Past ~250 days even the 4h ceiling overruns Datadog's 1500-point limit, so
    the series comes back partial. A percentile over a silently-clipped window is
    not the percentile that was asked for."""
    span_days = 365
    series, values = _make_payload(2, 1500)
    client = _FakeClient(series, values)
    result = _p95_datadog(client, "q", f"-{span_days}d", "now", 100)
    assert result["interval_ms"] == _MAX_INTERVAL_MS
    assert result["window_exceeds_point_cap"] is True
    assert str(_DATADOG_POINT_CAP) in result["window_note"]


def test_point_cap_advice_matches_why_the_window_overran():
    """The advice has to be actionable: telling a caller who already pinned the
    coarsest interval to "use a coarser interval" sends them nowhere."""
    long_span = 365 * 24 * 3600 * 1000

    auto = _point_cap_note(long_span, _MAX_INTERVAL_MS, None)
    assert "coarsest supported" in auto

    pinned_below_ceiling = _point_cap_note(long_span, 3_600_000, 3_600_000)
    assert "coarser interval" in pinned_below_ceiling

    pinned_at_ceiling = _point_cap_note(long_span, _MAX_INTERVAL_MS, _MAX_INTERVAL_MS)
    assert "coarser interval" not in pinned_at_ceiling
    assert "coarsest supported" in pinned_at_ceiling

    assert _point_cap_note(30 * 24 * 3600 * 1000, 3_600_000, None) is None


def test_normal_window_carries_no_point_cap_warning():
    series, values = _make_payload(2, 720)
    result = _p95_datadog(_FakeClient(series, values), "q", "-30d", "now", 100)
    assert "window_exceeds_point_cap" not in result


def test_pick_interval_caller_value_wins():
    assert _pick_interval(_THIRTY_DAYS_MS, 300_000) == 300_000


def test_pick_interval_clamps_caller_value():
    assert _pick_interval(_THIRTY_DAYS_MS, 1) == _MIN_INTERVAL_MS
    assert _pick_interval(_THIRTY_DAYS_MS, 99_999_999) == _MAX_INTERVAL_MS


@pytest.mark.parametrize("bad", [None, "bogus", "", [], {}])
def test_clamp_interval_rejects_non_numeric(bad):
    assert _clamp_interval(bad) is None


def test_plain_metrics_does_not_auto_pick_an_interval():
    """resource_type='metrics' must keep Datadog's own default resolution when no
    interval is given. Its output contract predates this feature and the RCA
    prompts and SKILL.md depend on it -- auto-picking here would silently change
    every existing RCA metric query. Only metric_stats, which is new and owns its
    own contract, auto-picks."""
    client = _FakeClient(*_make_payload(1, 10))
    _query_metrics(client, "avg:system.cpu.user{*}", "-30d", "now", 100)
    assert client.interval is None

    # An explicit value is still honoured (and clamped).
    _query_metrics(client, "avg:system.cpu.user{*}", "-30d", "now", 100, interval=300_000)
    assert client.interval == 300_000
    _query_metrics(client, "avg:system.cpu.user{*}", "-30d", "now", 100, interval=1)
    assert client.interval == _MIN_INTERVAL_MS


# ---------------------------------------------------------------------------
# Rounding
# ---------------------------------------------------------------------------


def test_round_sig_keeps_six_significant_digits():
    assert _round_sig(536870912.0) == 536871000.0
    assert _round_sig(1.23456789) == 1.23457


def test_round_sig_handles_zero_and_none():
    assert _round_sig(0.0) == 0.0
    assert _round_sig(None) is None


# ---------------------------------------------------------------------------
# Per-series summarization
# ---------------------------------------------------------------------------


def test_scope_is_stable_and_sorted():
    """scope doubles as a dict key across calls, so tag order must not matter."""
    a, _ = _scope_and_group(["kube_deployment:api", "env:production"])
    b, _ = _scope_and_group(["env:production", "kube_deployment:api"])
    assert a == b == "env:production,kube_deployment:api"


def test_group_parses_key_values():
    _, group = _scope_and_group(["env:production", "kube_deployment:apiv2worker"])
    assert group == {"env": "production", "kube_deployment": "apiv2worker"}


def test_all_null_series_reports_no_data_not_zero_usage():
    """The distinction that stops us cutting a workload we simply cannot see."""
    row = _summarize_series(["kube_deployment:api"], [{"name": "byte"}, None], [None, None, None])
    assert row["p95"] is None
    assert row["points"] == 3
    assert row["nulls"] == 3
    assert "no non-null points" in row["note"]


def test_empty_series_reports_no_data():
    row = _summarize_series(["kube_deployment:api"], None, [])
    assert row["p95"] is None
    assert row["points"] == 0


def test_nan_does_not_corrupt_the_sort_and_understate_max():
    """The load-bearing one. NaN compares False against everything, so a single
    NaN point leaves list.sort() unsorted and vals[-1] is no longer the maximum.
    max is exactly what guards the memory-decrease rule ("allowed only when max
    never exceeded the proposed request"), so an understated max approves a
    decrease against a peak that would OOM-kill the pod."""
    row = _summarize_series(["kube_deployment:api"], [{"name": "byte"}, None],
                            [1.0, float("nan"), 3.0, 2.0])
    assert row["max"] == 3.0
    assert row["nulls"] == 1
    assert row["points"] == 4


def test_non_finite_points_never_reach_json():
    """json.dumps emits bare NaN/Infinity, which is invalid JSON and would fail
    the agent's parse of the whole tool result."""
    row = _summarize_series(["x"], None, [1.0, float("inf"), float("-inf"), 2.0])
    serialized = json.dumps(row)
    assert "Infinity" not in serialized and "NaN" not in serialized
    json.loads(serialized)
    assert row["max"] == 2.0
    assert row["nulls"] == 2


def test_booleans_are_not_counted_as_samples():
    """bool is an int subclass, so True would otherwise be summarized as 1.0."""
    row = _summarize_series(["x"], None, [True, 5.0, False])
    assert row["nulls"] == 2
    assert row["max"] == 5.0


def test_percentile_of_empty_list_returns_none():
    """Must not raise: min(-1, max(0, -1)) is -1, which would index the last
    element (or IndexError on empty) rather than signalling "no data"."""
    assert _percentile([], 0.95) is None


def test_nulls_are_counted_and_excluded_from_percentiles():
    row = _summarize_series(["kube_deployment:api"], [{"name": "byte"}, None],
                            [100.0, None, 200.0, None, 300.0])
    assert row["points"] == 5
    assert row["nulls"] == 2
    assert row["max"] == 300.0
    assert row["p50"] == 200.0


def test_summary_never_contains_raw_points():
    row = _summarize_series(["kube_deployment:api"], [{"name": "byte"}, None],
                            [float(i) for i in range(720)])
    assert "values" not in row
    assert set(row) == {"scope", "group", "unit", "points", "nulls",
                        "p50", "p95", "p99", "max", "mean"}


def test_unit_extracted_from_datadog_nullable_pair():
    row = _summarize_series([], [{"name": "byte"}, None], [1.0])
    assert row["unit"] == "byte"


def test_cpu_unit_is_surfaced_so_the_agent_can_convert_nanocores():
    """The prompt requires dividing nanocore CPU usage by 1e9 before comparing it
    against a millicore request -- comparing the two directly is a ~1e6x error
    that still looks like a plausible number in a PR body. That conversion is the
    agent's job, but it can only do it if the unit reaches it, so pin that."""
    row = _summarize_series(
        ["kube_deployment:apiv2worker"],
        [{"name": "nanocore"}, None],
        [480_000_000.0, 500_000_000.0],
    )
    assert row["unit"] == "nanocore"
    # Raw nanocores are reported as-is; scaling is deliberately not done here.
    assert row["max"] == 500_000_000.0
    assert row["p95"] == 500_000_000.0


# ---------------------------------------------------------------------------
# Handler output contract
# ---------------------------------------------------------------------------


def test_thirty_day_query_reports_720_points_at_one_hour():
    series, values = _make_payload(3, 720)
    client = _FakeClient(series, values)
    result = _p95_datadog(client, "sum:kubernetes.memory.usage{*} by {kube_deployment}",
                          "-30d", "now", 100)
    assert client.interval == 3_600_000
    assert result["interval_ms"] == 3_600_000
    assert all(row["points"] == 720 for row in result["results"])
    assert result["metrics_source"] == "datadog"


def test_seventy_series_stays_under_summarizer_threshold():
    """Above PASS_THROUGH_CHARS the output is routed through an LLM summarizer,
    which would paraphrase percentiles into plausible fiction -- numbers that
    survive but come back looking authoritative are worse than truncated ones."""
    series, values = _make_payload(70, 720)
    result = _p95_datadog(_FakeClient(series, values), "q", "-30d", "now", 100)
    assert result["series_included"] == 70
    assert not result.get("series_truncated")
    assert len(json.dumps(result)) < PASS_THROUGH_CHARS


def test_series_cap_is_reported_not_silent():
    series, values = _make_payload(400, 720)
    result = _p95_datadog(_FakeClient(series, values), "q", "-30d", "now", 1000)
    assert result["series_included"] <= _MAX_STATS_SERIES
    assert result["series_returned"] == 400
    assert result.get("series_dropped") or result.get("series_truncated")
    assert "note" in result
    assert len(json.dumps(result)) < PASS_THROUGH_CHARS


def test_zero_series_is_an_empty_answer_not_an_error():
    result = _p95_datadog(_FakeClient([], []), "q", "-30d", "now", 100)
    assert result["count"] == 0
    assert result["series_returned"] == 0
    assert result["results"] == []


def test_per_series_rows_survive_truncation_but_one_fat_dict_does_not():
    """Pins the reason for the per-series shape, both directions at once."""
    series, values = _make_payload(70, 720)

    fat = [{"series": series, "times": list(range(720)), "values": values}]
    kept_fat, truncated_fat = _truncate_results(fat, [json.dumps(i) for i in fat])
    assert kept_fat == [] and truncated_fat is True  # -> count: 0, silent wrong answer

    rows = _p95_datadog(_FakeClient(series, values), "q", "-30d", "now", 100)["results"]
    kept_rows, truncated_rows = _truncate_results(rows, [json.dumps(i) for i in rows])
    assert len(kept_rows) == len(rows) and truncated_rows is False


# ---------------------------------------------------------------------------
# Provider seam
# ---------------------------------------------------------------------------


def test_every_advertised_source_has_a_dispatch_branch():
    assert set(_P95_BY_SOURCE) >= set(_METRIC_STATS_SOURCES)


def test_unsupported_source_raises_clearly():
    client = _FakeClient([], [])
    with pytest.raises(ValueError, match="Unsupported metrics source"):
        _query_metric_stats(client, "q", "-30d", "now", 100, source="newrelic")


def test_empty_query_is_rejected():
    client = _FakeClient([], [])
    with pytest.raises(ValueError, match="query is required"):
        _query_metric_stats(client, "", "-30d", "now", 100)
