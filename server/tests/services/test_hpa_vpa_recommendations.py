"""Tests the pure lifecycle logic behind HPA/VPA right-sizing recommendations.

Three things here are the difference between an action a customer keeps enabled
and one they mute:

- ``build_workload_key`` -- computed in Python and never accepted from the LLM,
  so case or whitespace drift in model output cannot defeat a cooldown, and the
  same workload in two environments stays two independent rows.
- ``compute_severity_score`` -- makes "the mis-size materially worsened"
  computable rather than an LLM judgement call.
- ``is_materially_worse`` -- the gate that decides whether we are allowed to
  re-raise a workload a human already said no to.

Pure functions only: no DB, no network. The DB transitions themselves are
exercised against a live Postgres, not mocked here.
"""

import os
import sys

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from services.actions.hpa_vpa_recommendations import (  # noqa: E402
    HPA_VPA_COOLDOWN_DAYS,
    MATERIALLY_WORSE_FACTOR,
    STATUS_DISMISSED,
    STATUS_SUPERSEDED,
    _CLOSERS,
    _DISMISS_FIELDS,
    build_workload_key,
    close_pull_request,
    compute_severity_score,
    dismiss_recommendation,
    is_materially_worse,
    mark_superseded,
    parse_quantity,
    restore_superseded,
    severity_from_display,
)


# ---------------------------------------------------------------------------
# workload_key normalization
# ---------------------------------------------------------------------------


def test_workload_key_is_case_and_whitespace_insensitive():
    """Model output drift must not open a second PR for the same workload."""
    canonical = build_workload_key("apiv2worker", "production")
    assert build_workload_key("APIv2Worker", "Production") == canonical
    assert build_workload_key("  apiv2worker  ", " production ") == canonical


def test_workload_key_separates_environments():
    """Same workload in two envs is two independent recommendations."""
    assert build_workload_key("api", "production") != build_workload_key("api", "staging")


def test_workload_key_handles_missing_environment():
    assert build_workload_key("api", None) == "-::api"
    assert build_workload_key("api", "") == "-::api"
    assert build_workload_key("api", "   ") == "-::api"


def test_workload_key_is_stable_across_calls():
    """Pin the exact format, not self-equality: the key is a persisted dedup
    handle, so a format change silently orphans every existing cooldown row."""
    assert build_workload_key("api", "prod") == "prod::api"


# ---------------------------------------------------------------------------
# severity_score
# ---------------------------------------------------------------------------


def test_severity_is_relative_mis_size():
    score = compute_severity_score({"memory": {"current": 2048, "recommended": 768}})
    assert score == pytest.approx((2048 - 768) / 2048)


def test_severity_takes_the_worst_dimension():
    score = compute_severity_score({
        "memory": {"current": 100, "recommended": 50},   # 0.5
        "cpu": {"current": 100, "recommended": 10},      # 0.9
    })
    assert score == pytest.approx(0.9)


def test_severity_is_direction_agnostic():
    """An under-provisioned workload is just as material as an over-provisioned one."""
    down = compute_severity_score({"cpu": {"current": 100, "recommended": 50}})
    up = compute_severity_score({"cpu": {"current": 100, "recommended": 150}})
    assert down == pytest.approx(0.5)
    assert up == pytest.approx(0.5)


@pytest.mark.parametrize("dimensions", [
    {},
    None,
    {"cpu": {"current": 0, "recommended": 5}},              # non-positive current
    {"cpu": {"current": -10, "recommended": 5}},
    {"cpu": {"current": "2Gi", "recommended": "1Gi"}},       # display strings, not numbers
    {"cpu": {"current": 100}},                               # incomplete
    {"cpu": {"recommended": 100}},
    {"cpu": "not-a-dict"},
    {"cpu": {"current": True, "recommended": 50}},           # bool is an int subclass
    {"cpu": {"current": 100, "recommended": False}},
    {"cpu": {"current": float("inf"), "recommended": 50}},
    {"cpu": {"current": 100, "recommended": float("nan")}},
])
def test_severity_is_none_when_nothing_is_scoreable(dimensions):
    assert compute_severity_score(dimensions) is None


# ---------------------------------------------------------------------------
# "materially worse" -- the anti-nag gate
# ---------------------------------------------------------------------------


def test_materially_worse_at_and_above_the_factor():
    prior = 0.60
    assert is_materially_worse(prior * MATERIALLY_WORSE_FACTOR, prior) is True
    assert is_materially_worse(0.90, prior) is True


def test_not_materially_worse_below_the_factor():
    assert is_materially_worse(0.70, 0.60) is False   # 1.17x
    assert is_materially_worse(0.60, 0.60) is False   # unchanged
    assert is_materially_worse(0.30, 0.60) is False   # improved


@pytest.mark.parametrize("new,prior", [
    (None, 0.60),      # unknown new score
    (0.90, None),      # unknown prior score
    (0.90, 0.0),       # unusable prior
    (0.90, -1.0),
    ("0.9", 0.60),     # non-numeric
])
def test_unknown_scores_never_break_a_cooldown(new, prior):
    """A missing number must never become a reason to nag someone who said no."""
    assert is_materially_worse(new, prior) is False


@pytest.mark.parametrize("new,prior", [
    (float("inf"), 0.60),
    (float("-inf"), 0.60),
    (float("nan"), 0.60),
    (0.90, float("inf")),
    (0.90, float("nan")),
])
def test_non_finite_scores_never_break_a_cooldown(new, prior):
    """severity_score arrives from the LLM. Without a finiteness check, passing
    Infinity compares as worse than everything and unlocks any cooldown on
    demand -- defeating the anti-nag rule this feature exists to enforce."""
    assert is_materially_worse(new, prior) is False


def test_booleans_are_not_treated_as_scores():
    """bool is an int subclass, so True would otherwise sneak through as 1.0."""
    assert is_materially_worse(True, 0.60) is False
    assert is_materially_worse(0.90, True) is False


def test_integers_too_large_for_a_float_do_not_raise():
    """math.isfinite raises OverflowError on an arbitrary-precision int that
    cannot convert to a float, and json.loads imposes no bound -- so a model can
    emit one. Unhandled, it escapes the cooldown gate as an error instead of a
    decision, and the caller reports a generic post failure."""
    huge = 10 ** 400

    assert is_materially_worse(huge, 0.60) is False
    assert is_materially_worse(0.90, huge) is False
    assert compute_severity_score({"cpu": {"current": huge, "recommended": 50}}) is None
    assert compute_severity_score({"cpu": {"current": 100, "recommended": huge}}) is None

    # Large but representable values must still be accepted.
    assert compute_severity_score({"cpu": {"current": 10 ** 300, "recommended": 10 ** 299}}) is not None


# ---------------------------------------------------------------------------
# Cooldown + provider dispatch
# ---------------------------------------------------------------------------


def test_cooldown_window_is_thirty_days():
    assert HPA_VPA_COOLDOWN_DAYS == 30


def test_only_github_close_is_implemented_today():
    assert set(_CLOSERS) == {"github"}


@pytest.mark.parametrize("provider", ["gitlab", "bitbucket", "svn", ""])
def test_unsupported_provider_is_a_loud_error(provider):
    """A gap must be loud, not a silent no-op that leaves the PR open."""
    result = close_pull_request("user-1", provider, "owner/repo", 5)
    assert "error" in result
    if provider:
        assert provider in result["error"]


def test_missing_pr_reference_is_rejected_before_any_network_call(monkeypatch):
    """Assert the closer is never reached, not merely that an error came back --
    otherwise this passes for the wrong reason (the fake user's GitHub auth
    lookup failing) and would keep passing if the guard were removed."""
    calls = []
    monkeypatch.setitem(
        _CLOSERS, "github", lambda *args, **_kwargs: calls.append(args) or {"success": True}
    )

    assert "error" in close_pull_request("user-1", "github", "", 5)
    assert "error" in close_pull_request("user-1", "github", "owner/repo", 0)
    assert calls == []


@pytest.mark.parametrize("bad_repo", [
    "../../etc/passwd",
    "owner/repo/extra",
    "owner",
    "owner/repo?x=1",
    "owner/repo#frag",
    "https://evil.example.com/owner/repo",
])
def test_malformed_repo_name_is_rejected_before_any_network_call(monkeypatch, bad_repo):
    """The repo slug is interpolated into an API URL, so anything but owner/repo
    must be refused before a request is built."""
    calls = []
    monkeypatch.setitem(
        _CLOSERS, "github", lambda *args, **_kwargs: calls.append(args) or {"success": True}
    )

    result = close_pull_request("user-1", "github", bad_repo, 5)
    assert "error" in result
    assert calls == []


# ---------------------------------------------------------------------------
# Quantity parsing + server-derived severity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("2 Gi", 2 * 1024 ** 3),
    ("768Mi", 768 * 1024 ** 2),
    ("512 mi", 512 * 1024 ** 2),      # binary suffixes vary in case in real YAML
    ("750m", 0.75),
    ("2000 m", 2.0),
    ("10", 10.0),
    ("1.5", 1.5),
    ("4G", 4e9),
    (10, 10.0),
    (2.5, 2.5),
])
def test_parse_quantity_reads_kubernetes_display_units(text, expected):
    assert parse_quantity(text) == pytest.approx(expected)


def test_milli_and_mega_are_not_confused():
    """Kubernetes says m is milli and M is mega. Case-folding the single-letter
    suffixes would make '750m' and '750M' the same number -- a 1e9x error in the
    value that gates whether a cooldown may be broken."""
    assert parse_quantity("750m") == pytest.approx(0.75)
    assert parse_quantity("750M") == pytest.approx(750e6)


@pytest.mark.parametrize("bad", [
    None, "", "  ", "abc", "2 Gi extra", "Gi", "1/2", "-", "2,048 Mi",
    "0x10", True, False, [], {}, float("nan"), float("inf"),
])
def test_parse_quantity_returns_none_for_unparseable(bad):
    assert parse_quantity(bad) is None


def test_severity_is_derived_from_display_values_not_the_model():
    """severity_score decides whether a human's 30-day cooldown can be broken
    early. Taking it from the LLM lets the party reporting the problem also grade
    how bad it is; deriving it from the current/recommended strings it already has
    to state keeps the card and the gate consistent by construction."""
    score = severity_from_display({
        "memory": {"current": "2 Gi", "recommended": "768 Mi"},
        "cpu": {"current": "2000 m", "recommended": "750 m"},
    })
    # memory: 1 - 768Mi/2Gi = 0.625; cpu: 1 - 750/2000 = 0.625
    assert score == pytest.approx(0.625)


def test_severity_takes_the_worst_parseable_dimension():
    score = severity_from_display({
        "memory": {"current": "2 Gi", "recommended": "1 Gi"},        # 0.5
        "max_replicas": {"current": "10", "recommended": "2"},        # 0.8
    })
    assert score == pytest.approx(0.8)


def test_severity_skips_unparseable_dimensions_without_failing():
    score = severity_from_display({
        "memory": {"current": "lots", "recommended": "less"},
        "cpu": {"current": "2000 m", "recommended": "1000 m"},
    })
    assert score == pytest.approx(0.5)


@pytest.mark.parametrize("payload", [
    {},
    None,
    {"cpu": "not-a-dict"},
    {"cpu": {"current": "abc", "recommended": "def"}},
    {"cpu": {"current": "100m"}},
])
def test_unparseable_payload_yields_no_severity_and_cannot_break_a_cooldown(payload):
    score = severity_from_display(payload)
    assert score is None
    assert is_materially_worse(score, 0.6) is False


@pytest.mark.parametrize("current,recommended", [
    ("512m", "256Mi"),      # milli vs mebi -- a units mistake, not a 5e8x mis-size
    ("2Gi", "750m"),
    ("10", "1Gi"),
])
def test_mismatched_unit_families_are_not_scored(current, recommended):
    """A cross-family ratio is astronomical and would clear MATERIALLY_WORSE_FACTOR
    against any prior score, letting a units typo in the model's own display
    strings break a human's 30-day cooldown. The dimension is skipped instead."""
    score = severity_from_display({"memory": {"current": current, "recommended": recommended}})
    assert score is None
    assert is_materially_worse(score, 0.6) is False


def test_bare_and_milli_cpu_stay_comparable():
    """Kubernetes treats `cpu: 2` and `cpu: 2000m` as the same quantity, so the
    family split must not reject the mixed form the model legitimately emits."""
    score = severity_from_display({"cpu": {"current": "2", "recommended": "500m"}})
    assert score == pytest.approx(0.75)


def test_one_mismatched_dimension_does_not_suppress_the_others():
    score = severity_from_display({
        "memory": {"current": "512m", "recommended": "256Mi"},   # skipped
        "cpu": {"current": "2000 m", "recommended": "1000 m"},    # 0.5
    })
    assert score == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Superseded cooldown compensation
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Records executed SQL and reports a caller-set rowcount."""

    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))


def test_supersede_preserves_the_cooldown_timestamp():
    """mark_superseded runs BEFORE the Slack post that justifies it, so the
    dismissal has to be restorable. Nulling cooldown_until destroys the only
    record of how much anti-nag window was left -- and it buys nothing, because
    every read path already filters on status, not on the timestamp."""
    cur = _FakeCursor()
    mark_superseded(cur, "rec-1")

    sql, params = cur.statements[0]
    assert "cooldown_until = NULL" not in sql
    assert STATUS_SUPERSEDED in params


def test_restore_superseded_puts_the_dismissal_back():
    cur = _FakeCursor(rowcount=1)
    assert restore_superseded(cur, "rec-1") is True

    sql, params = cur.statements[0]
    assert STATUS_DISMISSED in params
    # Guarded on the current status, so a later genuinely-posted recommendation
    # that superseded this row is never resurrected underneath it.
    assert STATUS_SUPERSEDED in params
    assert "AND status = %s" in sql


def test_mark_merged_reports_whether_it_cleared_the_cooldown():
    """The caller tells the human "no cooldown applied" on True. A no-op must not
    report success, or the card claims a cleared window that is still running on a
    change the human accepted."""
    from services.actions.hpa_vpa_recommendations import mark_merged

    assert mark_merged(_FakeCursor(rowcount=1), "rec-1", "org-1") is True
    assert mark_merged(_FakeCursor(rowcount=0), "rec-1", "org-1") is False


def test_restore_superseded_reports_when_it_changed_nothing():
    """The caller logs a lost anti-nag window on False, so a no-op must not
    report success."""
    assert restore_superseded(_FakeCursor(rowcount=0), "rec-1") is False


def test_dismiss_returning_list_is_generated_from_the_field_tuple():
    """The RETURNING list and the dict keys are the same definition, so they
    cannot drift into mis-keyed fields -- and these fields are used to close a
    real PR."""
    cur = _FakeCursor()
    cur.fetchone = lambda: tuple(range(len(_DISMISS_FIELDS)))

    out = dismiss_recommendation(cur, "rec-1", "org-1", "U123")

    sql, _params = cur.statements[0]
    assert f"RETURNING {', '.join(_DISMISS_FIELDS)}" in sql
    assert out["repo_full_name"] == 0
    assert out["user_id"] == len(_DISMISS_FIELDS) - 1


def test_dismiss_raises_rather_than_silently_truncating_a_short_row():
    """zip(strict=True): a short row would otherwise drop trailing fields,
    including user_id -- the credential the PR close depends on."""
    cur = _FakeCursor()
    cur.fetchone = lambda: (1, 2, 3)

    with pytest.raises(ValueError):
        dismiss_recommendation(cur, "rec-1", "org-1", "U123")
