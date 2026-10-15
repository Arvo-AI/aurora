"""Live progress for the Slack "Thinking…" placeholder during background chats.

Long investigations can run 10–20 minutes with nothing visible in Slack.
``SlackProgressReporter`` rewrites the placeholder every few seconds with the
elapsed time, the tool currently running and the agent's latest streamed
sentence — clearly labelled as a work-in-progress note, never the answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30.0
_MIN_INTERVAL_SECONDS = 10.0
_THOUGHT_MAX_CHARS = 280
_COMMAND_MAX_CHARS = 90
_TEXT_BUFFER_MAX_CHARS = 4000
# Give up on the placeholder after this many failed edits (deleted message, revoked token…).
_MAX_CONSECUTIVE_FAILURES = 3

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


def progress_interval_seconds() -> float:
    """Interval between placeholder edits; env-tunable, floored to stay within Slack rate limits."""
    raw = os.environ.get("SLACK_PROGRESS_UPDATE_INTERVAL_SECONDS", "")
    try:
        value = float(raw) if raw else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        value = DEFAULT_INTERVAL_SECONDS
    return max(value, _MIN_INTERVAL_SECONDS)


def format_elapsed(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    # Hours only show up on runaway investigations; keep the common case short
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _slack_escape(text: str) -> str:
    """Slack mrkdwn reads <...> as links and & as an entity; neutralise both."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _strip_markdown(text: str) -> str:
    """Flatten chat markdown into plain Slack-safe text for a one-line excerpt."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return text


def latest_thought(buffer: str, max_chars: int = _THOUGHT_MAX_CHARS) -> str:
    """Return the most recent complete sentence(s) from streamed text, capped at *max_chars*."""
    # Headings and bullets end a thought even without punctuation, so split on
    # line breaks as well as sentence ends before flattening each piece.
    sentences = [_one_line(s) for s in _SENTENCE_END.split(_strip_markdown(buffer))]
    sentences = [s for s in sentences if s]
    if not sentences:
        return ""
    # Headings and the fragment the model is still typing lack terminal
    # punctuation; skip them unless they're all we have
    complete = [s for s in sentences if re.search(r"[.!?]$", s)]
    sentences = complete or sentences[-1:]
    picked: list[str] = []
    length = 0
    for sentence in reversed(sentences):
        if picked and length + len(sentence) + 1 > max_chars:
            break
        picked.insert(0, sentence)
        length += len(sentence) + 1
    excerpt = " ".join(picked)
    if len(excerpt) > max_chars:
        excerpt = excerpt[: max_chars - 1].rstrip() + "…"
    return _slack_escape(excerpt)


def _shorten(text: str, max_chars: int) -> str:
    text = _one_line(text)
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def describe_tool(tool_name: str, tool_input: Any) -> str:
    """One-line description of a running tool for the progress note."""
    label = f"`{_slack_escape(tool_name)}`"
    command = None
    if isinstance(tool_input, dict):
        for key in ("command", "query", "cmd"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                command = value
                break
    if command:
        # Backticks inside the command would break the inline code span
        label += " — `" + _slack_escape(_shorten(command.replace("`", "'"), _COMMAND_MAX_CHARS)) + "`"
    return label


def render_progress_note(
    *,
    elapsed_seconds: float,
    completed_tool_calls: int,
    running_tool: Optional[str],
    thought: str,
) -> str:
    """Build the Slack mrkdwn body for an in-progress edit of the placeholder."""
    tools_part = ""
    if completed_tool_calls:
        noun = "tool call" if completed_tool_calls == 1 else "tool calls"
        tools_part = f" · {completed_tool_calls} {noun} so far"
    lines = [
        f":hourglass_flowing_sand: *Still working on this…* — {format_elapsed(elapsed_seconds)} elapsed{tools_part}",
        "_Live progress note, not the answer. This message is replaced with the full response when I'm done._",
    ]
    if running_tool:
        lines += ["", f"*Currently running:* {running_tool}"]
    if thought:
        lines += ["", "*Latest thought* _(may change)_:", f"> {thought}"]
    return "\n".join(lines)


def _capture_lock(capture: Any):
    """ToolContextCapture guards its dicts with ``lock``; fall back to a no-op for stubs."""
    lock = getattr(capture, "lock", None)
    return lock if lock is not None else threading.Lock()


class SlackProgressReporter:
    """Edits the Slack placeholder on a timer while the background workflow runs.

    ``observe`` receives every frame the workflow would have streamed to a browser
    (via ``BackgroundWebSocket``); the running tool is read from the workflow's
    ``ToolContextCapture`` because tool events never reach that stream.
    """

    def __init__(
        self,
        *,
        user_id: str,
        channel: str,
        message_ts: str,
        workflow: Any = None,
        interval_seconds: Optional[float] = None,
    ):
        self.user_id = user_id
        self.channel = channel
        self.message_ts = message_ts
        self.workflow = workflow
        self.interval = interval_seconds if interval_seconds is not None else progress_interval_seconds()
        self.started_at = time.monotonic()
        self._text_buffer = ""
        self._lock = threading.Lock()
        self._stop_event: Optional[asyncio.Event] = None
        self._stopped = False
        self._client = None
        self._failures = 0
        self.updates_sent = 0

    # -- observation -------------------------------------------------------

    def observe(self, message: Any) -> None:
        """Collect streamed assistant text; ignores every other frame type."""
        if isinstance(message, str):
            try:
                message = json.loads(message)
            except (TypeError, ValueError):
                return
        if not isinstance(message, dict) or message.get("type") != "message":
            return
        data = message.get("data")
        text = data.get("text") if isinstance(data, dict) else data
        if not isinstance(text, str) or not text:
            return
        with self._lock:
            self._text_buffer = (self._text_buffer + text)[-_TEXT_BUFFER_MAX_CHARS:]

    def _tool_capture(self):
        agent = getattr(self.workflow, "agent", None)
        return getattr(agent, "tool_capture_instance", None)

    def _running_tool(self) -> Optional[str]:
        capture = self._tool_capture()
        if capture is None:
            return None
        with _capture_lock(capture):
            calls = list(getattr(capture, "current_tool_calls", {}).values())
        running = [c for c in calls if isinstance(c, dict) and not c.get("completed_at")]
        if not running:
            return None
        # Insertion order follows start order, so the last entry is the newest call
        latest = running[-1]
        return describe_tool(str(latest.get("tool_name", "tool")), latest.get("input"))

    def _completed_tool_calls(self) -> int:
        capture = self._tool_capture()
        if capture is None:
            return 0
        with _capture_lock(capture):
            history = list(getattr(capture, "tool_history", []))
        return sum(1 for h in history if isinstance(h, dict) and h.get("completed_at"))

    def snapshot(self) -> str:
        with self._lock:
            buffer = self._text_buffer
        note = render_progress_note(
            elapsed_seconds=time.monotonic() - self.started_at,
            completed_tool_calls=self._completed_tool_calls(),
            running_tool=self._running_tool(),
            thought=latest_thought(buffer),
        )
        # Same secret scrubbing the UI messages get; a Slack channel is a wider audience
        from utils.security.output_redaction import redact

        redacted, _ = redact(note)
        return redacted

    # -- lifecycle ---------------------------------------------------------

    def _get_client(self):
        if self._client is None:
            from connectors.slack_connector.client import get_slack_client_for_user

            self._client = get_slack_client_for_user(self.user_id)
        return self._client

    def _push(self, text: str) -> None:
        client = self._get_client()
        # No Slack client means the final send will fail too; nothing useful to do here
        if client is None:
            raise RuntimeError("no Slack client for user")
        client.update_message(channel=self.channel, ts=self.message_ts, text=text)

    async def run(self) -> None:
        """Edit the placeholder every ``interval`` seconds until ``stop()`` is called."""
        self._stop_event = asyncio.Event()
        # stop() may have been called before the task got scheduled
        if self._stopped:
            return
        while True:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.to_thread(self._push, self.snapshot())
                self._failures = 0
                self.updates_sent += 1
            except Exception as exc:
                self._failures += 1
                logger.warning("[SlackProgress] Placeholder edit failed (%d/%d): %s",
                               self._failures, _MAX_CONSECUTIVE_FAILURES, exc)
                # The placeholder is gone or Slack is rejecting us; stop spending calls on it
                if self._failures >= _MAX_CONSECUTIVE_FAILURES:
                    return

    def stop(self) -> None:
        self._stopped = True
        if self._stop_event is not None:
            self._stop_event.set()
