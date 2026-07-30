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
    _CLOSERS,
    build_workload_key,
    close_pull_request,
    compute_severity_score,
    is_materially_worse,
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
