"""Write snapshot fixtures from a simulated run, for the snapshot tests.

A simulated capture carries the SIMULATED label so it can never pass as
evidence (R1). The snapshot reader needs the shape a LIVE capture writes, so
this helper relabels the meta of a simulated capture on purpose. Only tests
import it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from verifier.observations import MODE_LIVE, PROVENANCE_LIVE, to_snapshot

if TYPE_CHECKING:
    from simulated_session import Simulation


def fixtures_from(directory: Path, simulation: Simulation) -> Path:
    """Write one fixture per attempt and return the directory."""
    directory.mkdir(parents=True, exist_ok=True)
    for attempt in simulation.attempts:
        assert attempt.end is not None, "a finished attempt has an end capture"
        captured = replace(
            attempt.end,
            meta=replace(attempt.end.meta, mode=MODE_LIVE, provenance=PROVENANCE_LIVE),
        )
        to_snapshot(captured, directory / f"{attempt.variant}.json")
    return directory
