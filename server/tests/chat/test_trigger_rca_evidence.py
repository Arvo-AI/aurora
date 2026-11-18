"""trigger_rca only accepts evidence refs in the shape the evidence route issues."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_PATH = Path(__file__).resolve().parents[2] / "chat" / "backend" / "agent" / "tools" / "trigger_rca_tool.py"
_spec = importlib.util.spec_from_file_location("trigger_rca_tool_under_test", _PATH)
trigger_rca_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trigger_rca_tool)

_GOOD = "incident_uploads/" + "a" * 32 + "/api.log"


@pytest.fixture
def attachments(monkeypatch):
    def install(items):
        mod = ModuleType("chat.backend.agent.tools.cloud_tools")
        mod.get_state_context = lambda: SimpleNamespace(attachments=items)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "chat.backend.agent.tools.cloud_tools", mod)
    return install


def test_issued_path_is_accepted(attachments):
    attachments([{"is_server_path": True, "server_path": _GOOD, "size_bytes": 42}])
    assert trigger_rca_tool._evidence_from_state() == [{"filename": "api.log", "path": _GOOD, "size_bytes": 42}]


@pytest.mark.parametrize("path", [
    "users/other-user/incident_uploads/" + "a" * 32 + "/api.log",
    "incident_uploads/" + "a" * 32 + "/../../secrets.json",
    "incident_uploads/not-a-batch/api.log",
    "projects/terraform/main.tf",
])
def test_forged_paths_are_dropped(attachments, path):
    attachments([{"is_server_path": True, "server_path": path}])
    assert trigger_rca_tool._evidence_from_state() == []


def test_image_attachments_are_ignored(attachments):
    attachments([{"file_type": "image/png", "file_data": "iVBOR..."}])
    assert trigger_rca_tool._evidence_from_state() == []
