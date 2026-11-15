"""A Slack guest's memory extraction follows the viewer switch, not the admin session."""

from utils.auth.connector_access import Access, pinned_roles
from services.memory.collector import _extract_memories


def _guest(monkeypatch, *, viewers_may_edit: bool):
    monkeypatch.setattr("services.memory.collector.get_org_id_for_user", lambda _uid: "org")
    monkeypatch.setattr("utils.auth.enforcer.get_user_roles_in_org", lambda *_a, **_k: ["admin"])
    monkeypatch.setattr(
        "utils.auth.viewer_run.org_allows_memory_edits",
        lambda _org: viewers_may_edit,
    )
    monkeypatch.setattr(
        "utils.auth.connector_access.general_viewer_access",
        lambda _uid: Access([], ["viewer"]),
    )


def test_a_slack_guest_does_not_extract_as_the_admin(monkeypatch):
    _guest(monkeypatch, viewers_may_edit=False)
    ran = {}
    monkeypatch.setattr(
        "services.memory.collector._run_extraction_agent",
        lambda *a, **k: ran.setdefault("ran", True),
    )
    result = _extract_memories("session-1", "admin-uid", access_as="general_viewer")
    assert result == {"status": "skipped", "reason": "memory_edits_disabled"}
    assert "ran" not in ran
    assert pinned_roles() is None


def test_a_slack_guest_extracts_when_viewers_may_edit(monkeypatch):
    _guest(monkeypatch, viewers_may_edit=True)
    seen = {}

    def _run(*_a, **_k):
        # The pin has to still be the viewer while the agent runs.
        seen["roles"] = pinned_roles()

    monkeypatch.setattr("services.memory.collector._run_extraction_agent", _run)
    monkeypatch.setattr(
        "services.memory.collector.get_hook",
        lambda _name: (lambda *_a, **_k: (True, "")),
    )
    monkeypatch.setattr(
        "services.memory.collector.LLMContextManager.load_context_history",
        lambda *_a, **_k: [{"role": "user", "content": "we ship Friday"}],
    )
    monkeypatch.setattr("services.memory.collector.build_memory_index", lambda *_a, **_k: "")
    result = _extract_memories("session-1", "admin-uid", access_as="general_viewer")
    assert result["status"] == "ok"
    assert seen["roles"] == ["viewer"]
    assert pinned_roles() is None
