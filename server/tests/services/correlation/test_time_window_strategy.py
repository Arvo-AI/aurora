"""Tests for TimeWindowStrategy."""

from datetime import datetime, timedelta

from services.correlation.strategies.time_window import TimeWindowStrategy


class TestTimeWindowStrategy:
    """Suite for linear-decay time-window scoring."""

    def setup_method(self):
        self.strategy = TimeWindowStrategy(time_window_seconds=300)

    # ------------------------------------------------------------------
    # Core behaviour
    # ------------------------------------------------------------------

    def test_zero_gap_returns_one(self):
        """Simultaneous events should score ~1.0."""
        now = datetime(2025, 6, 1, 12, 0, 0)
        assert self.strategy.score(now, now) == pytest.approx(1.0)

    def test_mid_window_returns_half(self):
        """Gap of half the window → score ≈ 0.5."""
        base = datetime(2025, 6, 1, 12, 0, 0)
        alert = base + timedelta(seconds=150)
        assert self.strategy.score(alert, base) == pytest.approx(0.5)

    def test_at_boundary_returns_zero(self):
        """Gap exactly at window boundary → score 0.0."""
        base = datetime(2025, 6, 1, 12, 0, 0)
        alert = base + timedelta(seconds=300)
        assert self.strategy.score(alert, base) == 0.0

    def test_past_window_returns_zero(self):
        """Gap exceeding window → score 0.0."""
        base = datetime(2025, 6, 1, 12, 0, 0)
        alert = base + timedelta(seconds=400)
        assert self.strategy.score(alert, base) == 0.0

    def test_negative_gap_returns_zero(self):
        """Alert before incident update → score 0.0."""
        base = datetime(2025, 6, 1, 12, 5, 0)
        alert = base - timedelta(seconds=60)
        assert self.strategy.score(alert, base) == 0.0

    def test_custom_window_size(self):
        """Strategy respects a custom window."""
        strategy = TimeWindowStrategy(time_window_seconds=600)
        base = datetime(2025, 6, 1, 12, 0, 0)
        alert = base + timedelta(seconds=300)
        # 300 / 600 = 0.5 → 1.0 - 0.5 = 0.5
        assert strategy.score(alert, base) == pytest.approx(0.5)

    def test_very_small_gap(self):
        """A 1-second gap in a 300s window → score ≈ 0.997."""
        base = datetime(2025, 6, 1, 12, 0, 0)
        alert = base + timedelta(seconds=1)
        score = self.strategy.score(alert, base)
        assert score > 0.99
        assert score <= 1.0


# Allow ``pytest`` to import approx at module level
import pytest  # noqa: E402
