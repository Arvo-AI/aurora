"""The stall sweep reads chat_sessions.updated_at, so a live run has to move it."""
import re
from pathlib import Path
from unittest.mock import MagicMock


def _load_touch():
    # Importing workflow pulls the agent, which the test job does not install.
    text = (Path(__file__).resolve().parents[2] / "chat/backend/agent/workflow.py").read_text()
    match = re.search(
        r"^def _touch_in_progress_session\(.*?\n(?=# |\ndef )",
        text,
        re.S | re.M,
    )
    ns = {"logger": MagicMock(), "set_rls_context": lambda *_a, **_k: True}
    exec(match.group(0), ns)
    return ns["_touch_in_progress_session"]


def test_touch_only_moves_an_in_progress_session(monkeypatch):
    touch = _load_touch()
    executed = {}

    class _Cursor:
        def execute(self, sql, params=None):
            executed["sql"] = sql
            executed["params"] = params

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            executed["committed"] = True

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *_args):
            return False

    class _Pool:
        def get_admin_connection(self):
            return _Ctx()

    monkeypatch.setattr("utils.db.connection_pool.db_pool", _Pool())
    touch("sess-1", "uid-1")
    assert "status = 'in_progress'" in executed["sql"]
    assert "updated_at = NOW()" in executed["sql"]
    assert executed["params"] == ("sess-1",)
    assert executed["committed"] is True
