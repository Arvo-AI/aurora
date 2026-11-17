"""`python -m verifier`: the same entry point as the console script."""

from __future__ import annotations

from verifier.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
