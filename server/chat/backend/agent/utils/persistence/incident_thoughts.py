"""Reusable incident_thoughts writer.

Single-agent (interactive chat → main_chatbot.py) and multi-agent (orchestrator
+ sub-agent ReAct loops) both need to persist live thoughts so the existing
ThoughtsPanel polling UI surfaces mid-run activity. This helper exposes one
sentence-boundary-aware accumulator that's safe to share across agents.

incident_thoughts is NOT RLS-protected (CASCADE-deleted from incidents), so
no set_rls_context is needed.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Optional

from utils.text.text_utils import clean_markdown

logger = logging.getLogger(__name__)

_SENTENCE_BOUNDARY = re.compile(r"[.!?](?:\s|$)")
_MIN_CHUNK_CHARS = 20
_PERIODIC_FLUSH_SECONDS = 1.0
_PERIODIC_FLUSH_CHARS = 50


def _persist_row(*, incident_id: str, content: str, agent_id: Optional[str]) -> None:
    """Single insert. Uses the global write_batcher if batching is enabled."""
    sql = (
        "INSERT INTO incident_thoughts (incident_id, timestamp, content, thought_type, agent_id) "
        "VALUES (%s, %s, %s, %s, %s)"
    )
    params = (incident_id, datetime.now(), content, "analysis", agent_id)
    try:
        from chat.backend.agent.utils.write_batcher import (
            batching_enabled,
            get_default_batcher,
        )
        if batching_enabled():
            get_default_batcher().enqueue(sql, params)
            return
    except Exception as e:
        logger.debug("incident_thoughts: write_batcher unavailable, falling back to direct insert (%s)", e)

    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
        conn.commit()


class IncidentThoughtAccumulator:
    """Sentence-boundary buffered writer. Mirrors the closure that lived in
    main_chatbot.py:save_incident_thought before extraction."""

    def __init__(self, incident_id: Optional[str], *, agent_id: Optional[str] = None):
        self.incident_id = incident_id
        self.agent_id = agent_id
        self._buf: list[str] = []
        self._last_save = time.time()

    def push(self, content: str, *, force: bool = False) -> None:
        if not self.incident_id:
            return
        if content:
            self._buf.append(content)

        text = "".join(self._buf)
        now = time.time()
        elapsed = now - self._last_save
        should_check = force or elapsed >= _PERIODIC_FLUSH_SECONDS or len(text) >= _PERIODIC_FLUSH_CHARS
        if not should_check:
            return
        # On force=True we save whatever's buffered, even if it's shorter than
        # _MIN_CHUNK_CHARS — otherwise tail-end short fragments are dropped.
        if not force and len(text) <= _MIN_CHUNK_CHARS:
            return

        matches = list(_SENTENCE_BOUNDARY.finditer(text))
        if not matches and not force:
            return

        if force and not matches:
            to_save, remaining = text, ""
        else:
            split_pos = matches[-1].end()
            to_save = text[:split_pos].strip()
            remaining = text[split_pos:].strip()

        if len(to_save) <= _MIN_CHUNK_CHARS:
            return

        cleaned = clean_markdown(to_save)
        if len(cleaned) <= _MIN_CHUNK_CHARS:
            return

        try:
            _persist_row(
                incident_id=self.incident_id,
                content=cleaned,
                agent_id=self.agent_id,
            )
            logger.info(
                "[incident_thoughts] saved %d chars for incident=%s agent=%s",
                len(cleaned), self.incident_id, self.agent_id or "(none)",
            )
        except Exception as e:
            logger.error("[incident_thoughts] save failed: %s", e)
            return

        self._buf.clear()
        if remaining:
            self._buf.append(remaining)
        self._last_save = now

    def flush(self) -> None:
        self.push("", force=True)


def save_incident_thought(
    *,
    incident_id: Optional[str],
    content: str,
    agent_id: Optional[str] = None,
) -> None:
    """One-shot write — bypasses the accumulator. Caller is responsible for
    chunking + cleaning. Safe for callers that already have a complete sentence
    or block to persist."""
    if not incident_id or not content:
        return
    cleaned = clean_markdown(content).strip()
    if len(cleaned) <= _MIN_CHUNK_CHARS:
        return
    try:
        _persist_row(incident_id=incident_id, content=cleaned, agent_id=agent_id)
    except Exception as e:
        logger.error("[incident_thoughts] one-shot save failed: %s", e)
