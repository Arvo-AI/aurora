"""Unit tests for onboarding -> Slack memory folding (no real DB)."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from services.memory import slack_memory
from services.memory.slack_memory import (
    ONBOARDING_MARKER,
    apply_onboarding_to_slack_memory,
)


class _FakeCursor:
    """Minimal cursor: SELECT returns a queued row, UPDATE captures its content."""

    def __init__(self, existing_content):
        self._existing = existing_content
        self.updated_content = None

    def execute(self, sql, params=None):
        self._last_sql = sql
        if sql.strip().upper().startswith("UPDATE"):
            # content is the first bound param in our UPDATE
            self.updated_content = params[0]

    def fetchone(self):
        # Every SELECT in the helper wants (id, content).
        if self._existing is None:
            return None
        return ("artifact-1", self._existing)


@contextmanager
def _fake_conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = lambda *_: cursor
    conn.cursor.return_value.__exit__ = lambda *_: False
    yield conn


def _run(answers, existing_content):
    cur = _FakeCursor(existing_content)

    @contextmanager
    def _get_admin_connection():
        with _fake_conn(cur) as conn:
            yield conn

    with patch.object(slack_memory.db_pool, "get_admin_connection", _get_admin_connection), \
         patch.object(slack_memory, "set_rls_context", return_value="org1"), \
         patch.object(slack_memory, "create_version", return_value=2):
        ok = apply_onboarding_to_slack_memory("u1", answers)
    return ok, cur.updated_content


def test_empty_answers_is_noop():
    ok, content = _run({"tone": "", "quiet_channels": "  "}, "existing policy")
    assert ok is False
    assert content is None  # never wrote


def test_non_dict_answers_rejected():
    assert apply_onboarding_to_slack_memory("u1", ["not", "a", "dict"]) is False
    assert apply_onboarding_to_slack_memory("", {"tone": "x"}) is False


def test_appends_section_with_only_nonempty_bullets():
    ok, content = _run(
        {"tone": "concise", "verbosity": "", "quiet_channels": "#random"},
        "This is the default policy.",
    )
    assert ok is True
    assert "This is the default policy." in content  # preserved
    assert ONBOARDING_MARKER in content
    assert "- Preferred tone: concise" in content
    assert "- Stay quiet in: #random" in content
    assert "Verbosity" not in content  # blank answer skipped


def test_unknown_key_gets_deslugified_label():
    ok, content = _run({"escalation_policy": "page platform team"}, "policy")
    assert ok is True
    assert "- Escalation policy: page platform team" in content


def test_resubmit_replaces_existing_section_no_duplicate():
    existing = (
        "Base policy here.\n\n"
        f"{ONBOARDING_MARKER}\n- Preferred tone: old tone\n"
    )
    ok, content = _run({"tone": "new tone"}, existing)
    assert ok is True
    assert content.count(ONBOARDING_MARKER) == 1  # replaced, not stacked
    assert "old tone" not in content
    assert "- Preferred tone: new tone" in content
    assert "Base policy here." in content  # content above the marker preserved
