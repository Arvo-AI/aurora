"""The frozen data model of the incident contract.

These dataclasses are what the rest of the verifier receives. They are frozen
and built only by `verifier.contract.load_contract`, so a caller can read a
field but cannot invent one or change one after validation.
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
