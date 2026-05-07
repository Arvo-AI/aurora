import json
from datetime import datetime, timezone
from pathlib import Path

from e2e_agents.runner.models import RunResult


def write_json_results(results: list[RunResult], results_dir: str) -> Path:
    """Write structured JSON results for CI consumption."""
    output_dir = Path(results_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agents_run": [r.model_dump() for r in results],
        "summary": {
            "total_agents": len(results),
            "completed": sum(1 for r in results if r.status == "completed"),
            "timed_out": sum(1 for r in results if r.status == "timed_out"),
            "errored": sum(1 for r in results if r.status in ("errored", "crashed")),
            "total_steps": sum(r.steps_used for r in results),
            "total_duration_seconds": sum(r.duration_seconds for r in results),
        },
    }

    output_path = output_dir / "run_results.json"
    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    return output_path
