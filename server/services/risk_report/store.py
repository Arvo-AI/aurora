"""Redis-backed state for Risk Report runs and the growth funnel.

A run is a small JSON document (status, progress, report) keyed by id and
owned by one user. Funnel events are plain counters so time-to-first-value
can be measured from day one without a migration; each event is also logged
as one structured line for whatever log pipeline is attached.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from utils.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

RUN_TTL_SECONDS = 7 * 24 * 3600
_RUN_KEY = "risk_report:run:{}"
_FUNNEL_KEY = "risk_report:funnel"

# entry_card_clicked -> report_requested -> report_completed -> install_cta_clicked
FUNNEL_EVENTS = (
    "entry_card_clicked",
    "report_requested",
    "report_completed",
    "report_failed",
    "install_cta_clicked",
)


def new_run(user_id: str, mode: str, target: str) -> Dict[str, Any]:
    run = {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "mode": mode,
        "target": target,
        "status": "queued",
        "progress": {"stage": "queued"},
        "created_at": time.time(),
        "report": None,
        "error": None,
    }
    save_run(run)
    return run


def save_run(run: Dict[str, Any]) -> None:
    r = get_redis_client()
    if r is None:
        raise RuntimeError("Redis unavailable")
    r.set(_RUN_KEY.format(run["id"]), json.dumps(run), ex=RUN_TTL_SECONDS)


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    r = get_redis_client()
    raw = r.get(_RUN_KEY.format(run_id)) if r is not None else None
    return json.loads(raw) if raw else None


def update_run(run_id: str, **fields: Any) -> None:
    run = get_run(run_id)
    if run is None:
        return
    run.update(fields)
    save_run(run)


def track(event: str, user_id: Optional[str], **props: Any) -> bool:
    if event not in FUNNEL_EVENTS:
        return False
    logger.info("risk_report_funnel event=%s user=%s props=%s", event, user_id, json.dumps(props))
    r = get_redis_client()
    if r is not None:
        r.hincrby(_FUNNEL_KEY, event, 1)
        if props.get("mode"):
            r.hincrby(_FUNNEL_KEY, f"{event}:{props['mode']}", 1)
    return True


def funnel() -> Dict[str, int]:
    r = get_redis_client()
    raw = r.hgetall(_FUNNEL_KEY) if r is not None else {}
    return {k: int(v) for k, v in (raw or {}).items()}
