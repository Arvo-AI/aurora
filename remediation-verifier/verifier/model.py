"""The frozen data model of the contract and of the observations.

These dataclasses are what the rest of the verifier receives. The contract
types are built only by `verifier.contract.load_contract`; the observation
types are built only by `verifier.observations.from_snapshot`. Both are frozen,
so a caller can read a field but cannot invent one or change one after
validation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class ContractError(ValueError):
    """The contract file is missing, unreadable, or not valid."""


@dataclass(frozen=True)
class Source:
    """Where the frozen values come from, and when they were read."""

    repo: str
    url: str
    commit: str
    commit_date: str
    read_on: str
    fork_repo: str
    fork_commit: str
    files: Mapping[str, str]


@dataclass(frozen=True)
class AgentView:
    """Everything an agent may see. One field, by design."""

    alert: str


@dataclass(frozen=True)
class Check:
    """One check, as the contract declares it."""

    id: str
    type: str
    source: str
    container: str | None = None
    pattern: str | None = None
    batch_size_source: str | None = None
    min_occurrences: int | None = None


@dataclass(frozen=True)
class Fault:
    """One fault with its workload identity and its checks."""

    name: str
    namespace: str
    checks: tuple[Check, ...]
    deployment: str | None = None
    container: str | None = None
    selector: str | None = None
    service: str | None = None
    endpoint_slice_selector: str | None = None
    selector_expected: str | None = None
    http_route: str | None = None
    http_method: str | None = None
    expected_status: int | None = None
    expected_json: Mapping[str, Any] | None = None
    requests: int | None = None
    request_interval_seconds: float | None = None
    request_timeout_seconds: float | None = None


@dataclass(frozen=True)
class Fix:
    """One accepted remediation, as literal commands."""

    id: str
    source: str
    steps: tuple[str, ...]


@dataclass(frozen=True)
class Variant:
    """One run of the lab: a reproduction or a remediation."""

    name: str
    kind: str
    fix: str | None


@dataclass(frozen=True)
class Observation:
    """How long the verifier watches, in seconds."""

    window_seconds: int


@dataclass(frozen=True)
class OracleView:
    """The verifier-only half of the contract."""

    observation: Observation
    expected_faults: tuple[str, ...]
    faults: Mapping[str, Fault]
    fixes: Mapping[str, Fix]
    variants: Mapping[str, Variant]


@dataclass(frozen=True)
class Contract:
    """The whole contract, loaded and checked."""

    scenario: str
    title: str
    source: Source
    agent: AgentView
    oracle: OracleView


class ObservationError(ValueError):
    """The observation document is unreadable or malformed."""


@dataclass(frozen=True)
class Meta:
    """Where an observation came from and when it was captured."""

    lab_commit: str
    variant: str
    mode: str
    captured_at: str
    provenance: str


@dataclass(frozen=True)
class ContainerStatus:
    """One container of a pod, flattened to what the checks read."""

    name: str
    ready: bool
    restart_count: int
    waiting_reason: str | None = None
    last_terminated_reason: str | None = None
    last_exit_code: int | None = None


@dataclass(frozen=True)
class Pod:
    """One pod. `last_state` is flattened to the terminated reason and exit code."""

    name: str
    uid: str
    phase: str
    ready: bool
    restart_count: int
    waiting_reason: str | None = None
    last_terminated_reason: str | None = None
    last_exit_code: int | None = None
    containers: tuple[ContainerStatus, ...] = ()


@dataclass(frozen=True)
class Service:
    """One service and the selector a check compares with the pods."""

    name: str
    selector: Mapping[str, str]
    ports: tuple[int, ...] = ()
    cluster_ip: str = ""


@dataclass(frozen=True)
class EndpointAddress:
    """One address of an EndpointSlice and whether it is ready."""

    address: str
    ready: bool


@dataclass(frozen=True)
class EndpointSlice:
    """One EndpointSlice for the service under test."""

    name: str
    addresses: tuple[EndpointAddress, ...] = ()


@dataclass(frozen=True)
class HttpProbe:
    """One request through the service. `error` is set when it did not answer."""

    route: str
    status: int | None = None
    body: str | None = None
    duration_seconds: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class Logs:
    """The two log streams the checks read."""

    worker: tuple[str, ...] = ()
    loadgen: tuple[str, ...] = ()


@dataclass(frozen=True)
class Observations:
    """One normalized capture, from kubectl or from a snapshot file."""

    meta: Meta
    pods: tuple[Pod, ...]
    services: tuple[Service, ...]
    endpoints: tuple[EndpointSlice, ...]
    http: tuple[HttpProbe, ...]
    logs: Logs
    timing: Mapping[str, float]
    batch_size: int | None = None
    missing_metadata: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        """True when every field the checks need was captured."""
        return not self.missing_metadata

    def require_complete(self) -> Observations:
        """Return self, or raise when the capture cannot support a verdict."""
        if self.missing_metadata:
            missing = ", ".join(self.missing_metadata)
            raise ObservationError(f"observation is missing required metadata: {missing}")
        return self
