"""Celery task stubs for New Relic webhook/polling processing.

Full implementation lives on feat/new-relic/rca. These stubs exist solely
to satisfy the import in newrelic_routes.py so the blueprint registers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from celery_config import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30,
    name="newrelic.process_issue",
)
def process_newrelic_issue(
    self,
    raw_payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    logger.info("[NEWRELIC] process_newrelic_issue stub called (user=%s)", user_id)


@celery_app.task(
    bind=True, max_retries=2, default_retry_delay=60,
    name="newrelic.poll_issues",
)
def poll_newrelic_issues(
    self,
    user_id: str,
) -> None:
    logger.info("[NEWRELIC] poll_newrelic_issues stub called (user=%s)", user_id)
