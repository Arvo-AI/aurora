"""read_uploaded_file: the RCA agent can list, grep and read evidence uploaded with a manual incident."""

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest

_PATH = Path(__file__).resolve().parents[2] / "chat" / "backend" / "agent" / "tools" / "uploaded_evidence_tool.py"
_spec = importlib.util.spec_from_file_location("uploaded_evidence_tool_under_test", _PATH)
uploaded_evidence_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uploaded_evidence_tool)  # by path: the tools package __init__ pulls in every connector

_LOG = b"10:00:01 INFO started\n10:00:02 ERROR OOMKilled: container api exceeded 512Mi\n10:00:03 INFO restarting\n"
_FILES = [{"filename": "api.log", "path": "incident_uploads/abc/api.log", "size_bytes": len(_LOG)}]


class _Cursor:
    def __init__(self, metadata):
        self.metadata = metadata

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return (self.metadata,)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, metadata):
        self.metadata = metadata

    def cursor(self):
        return _Cursor(self.metadata)


@pytest.fixture
def incident(monkeypatch):
    def install(metadata):
        auth_mod = ModuleType("utils.auth.stateless_auth")
        auth_mod.set_rls_context = lambda *a, **k: "org-1"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "utils.auth.stateless_auth", auth_mod)

        @contextmanager
        def _admin_connection():
            yield _Conn(metadata)

        pool_mod = ModuleType("utils.db.connection_pool")
        pool_mod.db_pool = type("Pool", (), {"get_admin_connection": staticmethod(_admin_connection)})()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "utils.db.connection_pool", pool_mod)

        storage_mod = ModuleType("utils.storage.storage")
        storage_mod.get_storage_manager = lambda user_id=None: type(  # type: ignore[attr-defined]
            "Storage", (), {"download_bytes": lambda self, path, user_id=None: _LOG}
        )()
        monkeypatch.setitem(sys.modules, "utils.storage.storage", storage_mod)
    return install


def _call(**kwargs):
    return uploaded_evidence_tool.read_uploaded_file(user_id="user-1", incident_id="inc-1", **kwargs)


def test_grep_finds_the_failure_with_its_line_number(incident):
    incident({"uploaded_files": _FILES})
    out = _call(operation="grep", pattern="oomkilled")
    assert out.startswith("api.log:2:")
    assert "exceeded 512Mi" in out


def test_read_returns_numbered_lines(incident):
    incident({"uploaded_files": _FILES})
    out = _call(operation="read", filename="api.log", start_line=2, num_lines=1)
    assert out == "Lines 2-2 of 3:\n2: 10:00:02 ERROR OOMKilled: container api exceeded 512Mi"


def test_unknown_file_names_the_real_ones(incident):
    incident({"uploaded_files": _FILES})
    assert "Uploaded files: api.log" in _call(operation="read", filename="../../etc/passwd")


def test_incident_without_uploads_says_so(incident):
    incident({})
    assert _call(operation="list") == "No files were uploaded for this incident."


def test_invalid_regex_is_reported_not_raised(incident):
    incident({"uploaded_files": _FILES})
    assert _call(operation="grep", pattern="(unclosed").startswith("Error: invalid regex")
