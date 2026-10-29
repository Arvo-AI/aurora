"""The stall sweep reads chat_sessions.updated_at, so a live run has to move it."""
from unittest.mock import MagicMock


def test_touch_only_moves_an_in_progress_session(monkeypatch):
    import utils.cache.redis_client as redis_client

    monkeypatch.setattr(redis_client, "get_redis_client", MagicMock)
    from chat.backend.agent.workflow import _touch_in_progress_session

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
    monkeypatch.setattr(
        "chat.backend.agent.workflow.set_rls_context", lambda *a, **k: True,
    )
    _touch_in_progress_session("sess-1", "uid-1")
    assert "status = 'in_progress'" in executed["sql"]
    assert "updated_at = NOW()" in executed["sql"]
    assert executed["params"] == ("sess-1",)
    assert executed["committed"] is True
