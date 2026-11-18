"""Record one live attempt per sample, including failures, without an answer key.

Run from the repository root with the server dependencies installed:
    python scripts/evaluate_sandbox.py --output /tmp/aurora-live-results.json
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
load_dotenv(ROOT / ".env")

from services.sandbox.evidence import CASE_IDS, load_case  # noqa: E402
from services.sandbox.runner import SandboxUnavailable, configured_model, investigate  # noqa: E402


async def record_case(case_id):
    started = time.monotonic()
    try:
        result = await investigate(case_id)
    except Exception as exc:
        result = {
            "case_id": case_id, "status": "error",
            "error_type": type(exc).__name__,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
        if isinstance(exc, SandboxUnavailable):
            # Only our own controlled messages are safe to retain, never raw SDK errors.
            result["message"] = str(exc)
    bundle = load_case(case_id).model_dump_json()
    result["evidence_sha256"] = hashlib.sha256(bundle.encode()).hexdigest()
    # Print no raw provider exceptions, requests or configuration secrets.
    print(f"{case_id}: {result['status']} ({result['elapsed_ms'] / 1000:.1f}s)", flush=True)
    return result


async def record(case_ids=CASE_IDS):
    started = time.monotonic()
    timestamp = datetime.now(timezone.utc).isoformat()
    model = configured_model()
    results = await asyncio.gather(*(record_case(case_id) for case_id in case_ids))
    return {
        "started_at": timestamp, "model": model,
        "comparison_elapsed_ms": round((time.monotonic() - started) * 1000),
        "attempts_per_case": 1,
        "note": "Live calls on synthetic evidence. Review diagnostic quality manually; completion is not an accuracy score.",
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", dest="case_ids", action="append", choices=CASE_IDS,
                        help="Run only this case; repeat to select several. Default: all three.")
    args = parser.parse_args()
    report = asyncio.run(record(args.case_ids or CASE_IDS))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if all(result["status"] == "complete" for result in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
