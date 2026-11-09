#!/usr/bin/env python3
"""Print the Recall evidence tables as markdown, for a PR description.

Same code as the tests (server/tests/services/memory/aurora_lab_corpus.py), on
the vendored aurora-lab fixtures:

1. retrieval quality: recall@1 / recall@5 / MRR of the legacy search_runbooks
   filter, the legacy injector shortlist and MemoryIndex, on 21 alert, change
   and root-cause queries;
2. check_change replay: hit rate, precision and leave-one-out false-positive
   rate across a MATCH_THRESHOLD x MIN_STRONG_TERMS grid.

    python scripts/eval_memory_retrieval.py              # both tables
    python scripts/eval_memory_retrieval.py --quality    # table 1 only
    python scripts/eval_memory_retrieval.py --sweep      # table 2 only

Needs the server's Python dependencies (run it in the server venv or the
aurora-server container).
"""
import argparse
import pathlib
import sys

SERVER = pathlib.Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

from services.memory import change_check as cc  # noqa: E402
from tests.services.memory import aurora_lab_corpus as lab  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--quality", action="store_true", help="retrieval quality table only")
    ap.add_argument("--sweep", action="store_true", help="check_change sweep only")
    args = ap.parse_args()
    both = not (args.quality or args.sweep)
    scenarios = lab.load_scenarios()

    if both or args.quality:
        print(f"### Retrieval quality ({len(scenarios)} aurora-lab runbooks, alert lines removed)\n")
        print(lab.quality_markdown(lab.quality(scenarios), len(scenarios)))
        print()
    if both or args.sweep:
        replay = lab.Replay(scenarios)
        current = (cc.MATCH_THRESHOLD, cc.MIN_STRONG_TERMS)
        print("### check_change replay (postmortem + runbook per scenario; leave-one-out for false positives)\n")
        for kind in ("change", "break.sh"):
            print(lab.sweep_markdown(lab.sweep(replay, kind), kind, current))
            print()


if __name__ == "__main__":
    main()
