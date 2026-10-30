"""INVESTIGATION_TIMEOUT_SECONDS parsing. Default is one hour."""

from chat.investigation_timeout import (
    DEFAULT_INVESTIGATION_TIMEOUT_SECONDS,
    MAX_INVESTIGATION_TIMEOUT_SECONDS,
    MIN_INVESTIGATION_TIMEOUT_SECONDS,
    investigation_soft_timeout_seconds,
    investigation_stale_after_seconds,
    investigation_timeout_label,
    investigation_timeout_seconds,
)


def test_default_is_one_hour(monkeypatch):
    monkeypatch.delenv("INVESTIGATION_TIMEOUT_SECONDS", raising=False)
    assert investigation_timeout_seconds() == DEFAULT_INVESTIGATION_TIMEOUT_SECONDS
    assert investigation_timeout_seconds() == 3600
    assert investigation_soft_timeout_seconds() == 3540
    assert investigation_timeout_label() == "60 minutes"


def test_blank_uses_the_default(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_TIMEOUT_SECONDS", "  ")
    assert investigation_timeout_seconds() == 3600


def test_custom_value(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_TIMEOUT_SECONDS", " 7200 ")
    assert investigation_timeout_seconds() == 7200
    assert investigation_soft_timeout_seconds() == 7140
    assert investigation_timeout_label() == "120 minutes"


def test_non_integer_falls_back(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_TIMEOUT_SECONDS", "an hour")
    assert investigation_timeout_seconds() == 3600


def test_below_minimum_is_clamped(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_TIMEOUT_SECONDS", "30")
    assert investigation_timeout_seconds() == MIN_INVESTIGATION_TIMEOUT_SECONDS
    assert investigation_soft_timeout_seconds() == 60


def test_above_maximum_is_clamped(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_TIMEOUT_SECONDS", "999999")
    assert investigation_timeout_seconds() == MAX_INVESTIGATION_TIMEOUT_SECONDS
    assert investigation_timeout_label() == "180 minutes"


def test_odd_duration_stays_in_seconds(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_TIMEOUT_SECONDS", "150")
    assert investigation_timeout_label() == "150 seconds"


def test_python_int_syntax(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_TIMEOUT_SECONDS", "+7200")
    assert investigation_timeout_seconds() == 7200
    monkeypatch.setenv("INVESTIGATION_TIMEOUT_SECONDS", "9_000")
    assert investigation_timeout_seconds() == 9000


def test_stale_sweeper_waits_past_the_hard_kill(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_TIMEOUT_SECONDS", "3600")
    assert investigation_stale_after_seconds() == 3900
