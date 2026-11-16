"""Run one variant from a clean lab to a verdict.

The runner owns the order: break, capture the broken state, apply the fix, wait
for the rollout, wait for an endpoint, open the window, capture both ends, run
the checks and decide. It talks to a `Session`: `verifier.lab.LiveSession` on a
real cluster, a simulated cluster in tests. One sequence, and no second
implementation for LIVE.

Failures stay local. A variant that cannot finish becomes EXECUTION_ERROR and
the rest of the table still runs, because one broken row is more useful than no
table at all. A remediation is gated twice: the baseline of its cycle has to
confirm the incident, and that variant's own pre-fix capture has to fail every
declared check.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol

from verifier.checks import run_checks
from verifier.decision import (
    CONFIRMED,
    Verdict,
    decide_remediation,
    decide_reproduction,
    execution_error,
)
from verifier.kubectl import LiveError
from verifier.model import (
    CheckResult,
    Contract,
    ContractError,
    Fix,
    ObservationError,
    Observations,
    Variant,
)
from verifier.report import Run

BASELINE_VARIANT = "baseline"
REMEDIATION = "remediation"
NOT_CAPTURED = "not captured"


class RunnerError(RuntimeError):
    """The run cannot continue in the sequence the contract asks for."""


RUN_ERRORS = (LiveError, ObservationError, OSError, RunnerError)


@dataclass(frozen=True)
class Plan:
    """Every wait a variant may take, in seconds.

    The break budget covers what `lab.sh break` does: a reset that waits up to
    240s for the deployments and a fault wait of up to 240s (lab.sh#L60-L83).
    The settle it sleeps afterwards is zero: the verifier's own window settles.

    The rollout uses the 120s the plan sets for a single replica, which covers
    an image pull on a cold node. The endpoint wait is a pacing step, not a
    check: five seconds is longer than the controller needs to fill a slice,
    and if it is still empty the window runs anyway and the two endpoint checks
    decide on the end capture; the poll is one second for the same reason.
    """

    break_timeout_s: float = 720.0
    fix_timeout_s: float = 60.0
    rollout_timeout_s: float = 120.0
    endpoints_timeout_s: float = 5.0
    endpoint_poll_s: float = 1.0
    variant_timeout_s: float = 900.0


class Session(Protocol):
    """What the runner needs from a cluster, real or simulated."""

    @property
    def mode(self) -> str: ...

    def break_scenario(self, timeout_s: float) -> None: ...

    def apply_fix(self, steps: Sequence[str], timeout_s: float) -> None: ...

    def wait_rollout(self, timeout_s: float) -> None: ...

    def wait_endpoints(self, timeout_s: float, poll_s: float) -> bool: ...

    def capture(self, variant: str) -> Observations: ...


@dataclass(frozen=True)
class Attempt:
    """One variant, from its first break to the verdict it produced."""

    variant: str
    mode: str
    verdict: Verdict
    pre_fix: Observations | None = None
    start: Observations | None = None
    end: Observations | None = None
    timing: Mapping[str, float] = field(default_factory=dict)

    @property
    def captured_at(self) -> str:
        """The capture the attempt ends on, or a label that says there is none."""
        for observation in (self.end, self.start, self.pre_fix):
            if observation is not None and observation.meta.captured_at:
                return observation.meta.captured_at
        return NOT_CAPTURED


class Runner:
    """Execute the contract's variants against one session."""

    def __init__(
        self,
        contract: Contract,
        session: Session,
        *,
        plan: Plan | None = None,
        repetitions: int = 1,
        sleep: Callable[[float], None] = time.sleep,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        if repetitions < 1:
            raise ValueError("repetitions has to be at least 1")
        self._contract = contract
        self._session = session
        self._plan = plan or Plan()
        self._repetitions = repetitions
        self._sleep = sleep
        self._timer = timer

    def run(self, variants: Sequence[str] | None = None) -> tuple[Attempt, ...]:
        """Run the baseline and every remediation, once per repetition.

        The attempts come back grouped by variant, in contract order, with one
        entry per repetition, which is the order the report table reads best.
        """
        names = self._remediation_names(variants)
        attempts: dict[str, list[Attempt]] = {BASELINE_VARIANT: []}
        attempts.update({name: [] for name in names})
        for _ in range(self._repetitions):
            baseline = self._baseline()
            attempts[BASELINE_VARIANT].append(baseline)
            for name in names:
                attempts[name].append(self._remediation(name, baseline.verdict))
        return tuple(attempt for name in (BASELINE_VARIANT, *names) for attempt in attempts[name])

    def _baseline(self) -> Attempt:
        variant = self._contract.oracle.variants[BASELINE_VARIANT]
        timeline = _Timeline(self._timer, self._plan.variant_timeout_s)
        try:
            with timeline.phase("break"):
                self._session.break_scenario(self._plan.break_timeout_s)
            start = self._capture(variant.name, timeline, "capture_start")
            self._window(timeline)
            end = self._capture(variant.name, timeline, "capture_end")
            with timeline.phase("checks"):
                results = run_checks(self._contract, start, end)
            verdict = decide_reproduction(self._contract, variant, results)
            return self._attempt(variant, verdict, timeline, start=start, end=end)
        except RUN_ERRORS as error:
            return self._failed(variant, timeline, error)

    def _remediation(self, name: str, baseline: Verdict) -> Attempt:
        variant = self._contract.oracle.variants[name]
        gate = baseline
        timeline = _Timeline(self._timer, self._plan.variant_timeout_s)
        try:
            with timeline.phase("break"):
                self._session.break_scenario(self._plan.break_timeout_s)
            pre_fix = self._capture(name, timeline, "pre_fix")
            with timeline.phase("pre_fix_checks"):
                pre_fix_results = run_checks(self._contract, pre_fix, pre_fix)
            gate = self._gate(baseline, pre_fix_results)
            fix = self._fix_of(variant)
            with timeline.phase("fix"):
                self._session.apply_fix(fix.steps, self._plan.fix_timeout_s)
            with timeline.phase("rollout"):
                self._session.wait_rollout(self._plan.rollout_timeout_s)
            with timeline.phase("endpoints"):
                self._session.wait_endpoints(
                    self._plan.endpoints_timeout_s, self._plan.endpoint_poll_s
                )
            start = self._capture(name, timeline, "capture_start")
            self._window(timeline)
            end = self._capture(name, timeline, "capture_end")
            with timeline.phase("checks"):
                results = run_checks(self._contract, start, end)
            verdict = decide_remediation(self._contract, variant, results, baseline=gate)
            return self._attempt(variant, verdict, timeline, pre_fix=pre_fix, start=start, end=end)
        except RUN_ERRORS as error:
            return self._failed(variant, timeline, error, gate=gate)

    def _capture(self, variant: str, timeline: _Timeline, phase: str) -> Observations:
        with timeline.phase(phase):
            return self._session.capture(variant)

    def _window(self, timeline: _Timeline) -> None:
        with timeline.phase("window"):
            self._sleep(self._contract.oracle.observation.window_seconds)

    def _gate(self, baseline: Verdict, pre_fix_results: Sequence[CheckResult]) -> Verdict:
        """Return the verdict that decides whether this remediation may pass."""
        if baseline.reproduction_status != CONFIRMED:
            return baseline
        reproduction = self._contract.oracle.variants[BASELINE_VARIANT]
        return decide_reproduction(self._contract, reproduction, pre_fix_results)

    def _fix_of(self, variant: Variant) -> Fix:
        fix = self._contract.oracle.fixes.get(variant.fix or "")
        if fix is None:
            raise ContractError(f"the variant {variant.name} declares no accepted fix")
        return fix

    def _attempt(
        self,
        variant: Variant,
        verdict: Verdict,
        timeline: _Timeline,
        *,
        pre_fix: Observations | None = None,
        start: Observations | None = None,
        end: Observations | None = None,
    ) -> Attempt:
        return Attempt(
            variant=variant.name,
            mode=self._session.mode,
            verdict=verdict,
            pre_fix=pre_fix,
            start=start,
            end=end,
            timing=dict(timeline.timing),
        )

    def _failed(
        self,
        variant: Variant,
        timeline: _Timeline,
        error: Exception,
        *,
        gate: Verdict | None = None,
    ) -> Attempt:
        reason = f"{timeline.current} failed: {error}"
        verdict = execution_error(self._contract, variant, reason, baseline=gate)
        return Attempt(
            variant=variant.name,
            mode=self._session.mode,
            verdict=verdict,
            timing=dict(timeline.timing),
        )

    def _remediation_names(self, variants: Sequence[str] | None) -> tuple[str, ...]:
        declared = self._contract.oracle.variants
        if variants is None:
            return tuple(name for name, variant in declared.items() if variant.kind == REMEDIATION)
        names = tuple(variants)
        for name in names:
            if name == BASELINE_VARIANT:
                raise ContractError("the baseline runs first; list only remediation variants")
            if name not in declared:
                raise ContractError(f"the contract declares no variant {name}")
            if declared[name].kind != REMEDIATION:
                raise ContractError(f"the variant {name} is not a remediation")
        return names


class _Timeline:
    """Measure each phase and refuse to start one past the variant budget."""

    def __init__(self, timer: Callable[[], float], limit_s: float) -> None:
        self._timer = timer
        self._limit_s = limit_s
        self.current = "start"
        self.timing: dict[str, float] = {}

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        if sum(self.timing.values()) > self._limit_s:
            raise RunnerError(f"the variant exceeded {self._limit_s:g}s before {name}")
        self.current = name
        started = self._timer()
        try:
            yield
        finally:
            self.timing[name] = round(self._timer() - started, 3)


def attempts_to_runs(attempts: Sequence[Attempt]) -> tuple[Run, ...]:
    """Project finished attempts onto the report the CLI writes."""
    return tuple(
        Run(attempt.verdict, attempt.mode, attempt.captured_at, attempt.timing)
        for attempt in attempts
    )
