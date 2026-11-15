"""Alert-to-incident correlation engine."""

from services.correlation.alert_correlator import (
    AlertCorrelator,
    CorrelationResult,
    apply_correlation_outcome,
    attach_correlation_hint,
    handle_correlated_alert,
)
from services.correlation.strategies import (
    TimeWindowStrategy,
    TopologyStrategy,
    SimilarityStrategy,
)

__all__ = [
    "AlertCorrelator",
    "CorrelationResult",
    "apply_correlation_outcome",
    "attach_correlation_hint",
    "handle_correlated_alert",
    "TimeWindowStrategy",
    "TopologyStrategy",
    "SimilarityStrategy",
]
