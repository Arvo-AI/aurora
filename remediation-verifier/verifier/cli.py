"""Command line entry point for the remediation verifier.

Three modes, one contract:

- `snapshot` reads the five fixtures and never touches a cluster or a network.
- `live` runs the lab and writes the evidence bundle, with no snapshot.
- `capture` runs the lab and writes the fixtures a later `snapshot` reads.

Exit codes follow R2. A verdict is advisory, so a REJECTED run still exits 0
when the report was written. 2 means nothing ran and the operator can fix the
cause. 3 means the verifier itself crashed (D56).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import traceback
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from verifier import __version__
from verifier.contract import load_contract
from verifier.model import Contract, ContractError, Observations
from verifier.observations import to_snapshot
from verifier.report import Run, write_markdown, write_report
from verifier.snapshots import SnapshotError, fixture_path, render_table, runs_of, verify

EXIT_OK = 0
EXIT_NOT_RUN = 2
EXIT_INTERNAL = 3

MODES = ("snapshot", "live", "capture")
DEFAULT_FIXTURES = Path("fixtures/snapshots")
DEFAULT_OUT = Path("out")
REPORT_JSON = "report.json"
REPORT_MARKDOWN = "report.md"


class UsageError(RuntimeError):
    """The command line asks for something this verifier cannot do."""


class Captured(Protocol):
    """The part of a finished attempt a capture reads, without the runner."""

    @property
    def variant(self) -> str: ...

    @property
    def end(self) -> Observations | None: ...


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remediation-verifier",
        description=(
            "Advisory remediation verification against the incident contract. "
            "It never blocks a merge."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--mode",
        choices=MODES,
        default="snapshot",
        help="snapshot reads fixtures, live runs the lab, capture writes fixtures",
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=None,
        help="the contract to load (default: contracts/scenario_12.yaml next to the package)",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURES,
        help=f"where the snapshots live (default: {DEFAULT_FIXTURES})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"where the report bundle lands (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--lab",
        type=Path,
        default=None,
        help="the aurora-lab checkout; required by live and capture",
    )
    parser.add_argument(
        "--variants",
        nargs="+",
        metavar="NAME",
        default=None,
        help="remediations to run, in this order; the baseline always runs first",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="repetitions per variant, live and capture only (default: 1)",
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        metavar="ISO",
        help="the report timestamp, so a rerun is byte-identical",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        generated_at = _timestamp(args.generated_at)
        if args.mode == "snapshot":
            return _snapshot(args, generated_at)
        return _live(args, generated_at)
    except (ContractError, SnapshotError, UsageError) as error:
        return _refuse(error)
    except Exception:
        traceback.print_exc()
        print("remediation-verifier: internal error, no verdict was produced", file=sys.stderr)
        return EXIT_INTERNAL


def _snapshot(args: argparse.Namespace, generated_at: str) -> int:
    if args.lab is not None:
        print("snapshot mode never touches the lab; --lab is ignored", file=sys.stderr)
    if args.variants is not None or args.repeat != 1:
        print(
            "snapshot mode decides every fixture once; --variants and --repeat are ignored",
            file=sys.stderr,
        )
    contract = load_contract(args.contract)
    attempts = verify(contract, args.fixtures)
    runs = runs_of(attempts)
    _write_bundle(contract, runs, args.out, generated_at)
    print(render_table(runs))
    _print_paths(args.out, None, 0)
    return EXIT_OK


def _live(args: argparse.Namespace, generated_at: str) -> int:
    if args.lab is None:
        raise UsageError("--lab is required with --mode live and --mode capture")
    if args.repeat < 1:
        raise UsageError("--repeat has to be at least 1")
    contract = load_contract(args.contract)
    if args.mode == "capture":
        _require_pinned(args.lab, contract)

    from verifier.lab import Lab, LiveSession
    from verifier.runner import Runner

    session = LiveSession(contract, Lab(args.lab))
    attempts = Runner(contract, session, repetitions=args.repeat).run(args.variants)
    runs = runs_of(attempts)
    _write_bundle(contract, runs, args.out, generated_at)
    written: tuple[Path, ...] = ()
    if args.mode == "capture":
        written = _write_snapshots(attempts, args.fixtures)
    print(render_table(runs))
    _print_paths(args.out, args.fixtures if args.mode == "capture" else None, len(written))
    return EXIT_OK


def _write_bundle(
    contract: Contract, runs: Sequence[Run], out: Path | str, generated_at: str
) -> tuple[Path, Path]:
    directory = Path(out)
    json_path = write_report(contract, runs, directory / REPORT_JSON, generated_at=generated_at)
    markdown_path = write_markdown(
        contract, runs, directory / REPORT_MARKDOWN, generated_at=generated_at
    )
    return json_path, markdown_path


def _write_snapshots(attempts: Sequence[Captured], directory: Path | str) -> tuple[Path, ...]:
    """Write one fixture per variant that reached an end capture."""
    written: list[Path] = []
    missing: list[str] = []
    for attempt in attempts:
        if attempt.end is None:
            missing.append(attempt.variant)
            continue
        written.append(to_snapshot(attempt.end, fixture_path(directory, attempt.variant)))
    if missing:
        print(
            f"no snapshot written for {', '.join(missing)}: the variant never reached a capture",
            file=sys.stderr,
        )
    return tuple(written)


def _require_pinned(lab: Path, contract: Contract) -> None:
    """Refuse a capture whose lab is not at the commit the contract names."""
    head = _lab_head(lab)
    if head is None:
        raise UsageError(
            f"cannot read the git HEAD of {lab}; a capture has to prove the pinned commit"
        )
    if head != contract.source.commit:
        raise UsageError(f"the lab at {lab} is at {head}, not the pinned {contract.source.commit}")


def _lab_head(lab: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(lab), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise UsageError(f"--generated-at is not an ISO 8601 timestamp: {value}") from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _print_paths(out: Path | str, snapshots: Path | str | None, count: int) -> None:
    directory = Path(out)
    print("")
    print(f"report: {directory / REPORT_JSON}")
    print(f"markdown: {directory / REPORT_MARKDOWN}")
    if snapshots is not None:
        print(f"snapshots: {snapshots} ({count} files)")


def _refuse(error: Exception) -> int:
    print(f"remediation-verifier: {error}", file=sys.stderr)
    return EXIT_NOT_RUN


if __name__ == "__main__":
    raise SystemExit(main())
