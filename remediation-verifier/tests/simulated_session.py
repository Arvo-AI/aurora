"""The runner's session over the simulated cluster, plus its clock and sleeps.

`simulate()` is the one entry the tests use: it builds the contract, the
cluster, the session and a runner with a stepping timer, so a test reads as the
scenario it exercises and not as wiring.

A capture produced here is simulated. It is relabelled on the way out, so a
report built from a fake run cannot read as evidence of a real cluster (R1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from simulated_cluster import SimulatedCluster
from verifier.contract import load_contract
from verifier.live import from_live
from verifier.model import Contract, Observations
from verifier.runner import Attempt, Plan, Runner

CAPTURED_AT = "2026-11-15T20:00:00Z"
SIMULATED_MODE = "SIMULATED"


class SimulatedSession:
    """The runner's `Session` over a `SimulatedCluster`."""

    mode = SIMULATED_MODE

    def __init__(self, cluster: SimulatedCluster, contract: Contract) -> None:
        self.cluster = cluster
        self.contract = contract
        self.calls: list[tuple[str, Any]] = []

    def break_scenario(self, timeout_s: float) -> None:
        self.calls.append(("break", timeout_s))
        self.cluster.break_scenario()

    def apply_fix(self, steps: Sequence[str], timeout_s: float) -> None:
        self.calls.append(("fix", tuple(steps)))
        self.cluster.apply_fix(steps)

    def wait_rollout(self, timeout_s: float) -> None:
        self.calls.append(("rollout", timeout_s))
        self.cluster.wait_rollout(timeout_s)

    def wait_endpoints(self, timeout_s: float, poll_s: float) -> bool:
        self.calls.append(("endpoints", (timeout_s, poll_s)))
        return self.cluster.wait_endpoints(timeout_s, poll_s)

    def capture(self, variant: str) -> Observations:
        self.calls.append(("capture", variant))
        self.cluster.capture(variant)
        captured = from_live(
            self.contract,
            variant,
            kubectl=self.cluster,
            probe=self.cluster.probe,
            sleep=lambda _: None,
            captured_at=CAPTURED_AT,
        )
        return replace(
            captured,
            meta=replace(captured.meta, mode=SIMULATED_MODE, provenance=SIMULATED_MODE),
        )


class RecordingSleep:
    """Record the waits the runner asked for, without waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class SteppingTimer:
    """A timer that advances one unit per read, so phase timings are exact."""

    def __init__(self, step: float = 1.0) -> None:
        self._value = 0.0
        self._step = step

    def __call__(self) -> float:
        value = self._value
        self._value += self._step
        return value


class Simulation:
    """One runner execution with the fakes that produced it."""

    def __init__(
        self,
        contract: Contract,
        attempts: Sequence[Attempt],
        cluster: SimulatedCluster,
        session: SimulatedSession,
        sleep: RecordingSleep,
    ) -> None:
        self.contract = contract
        self.attempts = tuple(attempts)
        self.cluster = cluster
        self.session = session
        self.sleep = sleep

    def attempt(self, variant: str) -> Attempt:
        return next(item for item in self.attempts if item.variant == variant)


def simulate(
    scenario: str = "full",
    *,
    variants: Sequence[str] | None = None,
    repetitions: int = 1,
    plan: Plan | None = None,
) -> Simulation:
    """Run the contract's variants over one simulated cluster."""
    contract = load_contract()
    cluster = SimulatedCluster(scenario)
    session = SimulatedSession(cluster, contract)
    sleep = RecordingSleep()
    runner = Runner(
        contract,
        session,
        plan=plan or Plan(),
        repetitions=repetitions,
        sleep=sleep,
        timer=SteppingTimer(),
    )
    attempts = runner.run(variants)
    return Simulation(contract, attempts, cluster, session, sleep)
