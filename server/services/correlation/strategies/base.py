"""
Base class for alert-to-incident correlation strategies.

Each strategy produces a score in [0.0, 1.0] representing how strongly
an incoming alert correlates with an existing incident.
"""

from abc import ABC, abstractmethod


class CorrelationStrategy(ABC):
    """Abstract base for correlation strategies.

    Subclasses implement ``score()`` which must return a float in [0.0, 1.0].
    A score of 1.0 means near-certain correlation; 0.0 means no relation.
    """

    @abstractmethod
    def score(self, *args, **kwargs) -> float:
        """Compute a correlation score between an alert and an incident.

        Returns:
            float: A value clamped to [0.0, 1.0].
        """
        ...
