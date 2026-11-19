"""Celery task: build a Risk Report in the background and store it."""

from __future__ import annotations

import logging
import time

from celery_config import celery_app
from services.risk_report import store
from services.risk_report.public_github import PublicGitHubError
from services.risk_report.report import build_pr_report, build_report

logger = logging.getLogger(__name__)


@celery_app.task(
    name="services.risk_report.tasks.run_risk_report",
    time_limit=1800,
    soft_time_limit=1740,
)
def run_risk_report(run_id: str) -> dict:
    run = store.get_run(run_id)
    if run is None:
        return {"status": "missing"}
    user_id, mode, target = run["user_id"], run["mode"], run["target"]
    store.update_run(run_id, status="running")

    def on_progress(p: dict) -> None:
        store.update_run(run_id, progress=p)

    started = time.monotonic()
    try:
        if mode == "pr":
            report = build_pr_report(target, user_id, on_progress=on_progress)
        else:
            report = build_report(target, user_id, on_progress=on_progress)
    except (ValueError, PublicGitHubError) as exc:  # user-facing: bad link, private, rate limit
        store.update_run(run_id, status="failed", error=str(exc))
        store.track("report_failed", user_id, mode=mode, reason=type(exc).__name__)
        return {"status": "failed"}
    except Exception:
        logger.exception("risk_report run=%s failed", run_id)
        store.update_run(run_id, status="failed", error="Something went wrong building the report.")
        store.track("report_failed", user_id, mode=mode, reason="internal")
        return {"status": "failed"}

    store.update_run(run_id, status="done", report=report)
    store.track(
        "report_completed", user_id, mode=mode,
        # Time to first value, measured from the user's click to a finished report.
        seconds=round(time.monotonic() - started, 1),
        prs=len(report.get("prs") or []),
    )
    return {"status": "done"}
