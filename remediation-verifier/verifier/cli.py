"""Command line entry point for the remediation verifier.

The interface is intentionally small while the checks are being built: the
console script exists from the first commit so `pip install` never leaves a
broken entry point behind, and `--version` is always meaningful.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from verifier import __version__

EXIT_NOT_IMPLEMENTED = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remediation-verifier",
        description="Advisory remediation verification against the incident contract.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    print(
        "verification is not implemented yet; the six checks arrive in case 4",
        file=sys.stderr,
    )
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    raise SystemExit(main())
