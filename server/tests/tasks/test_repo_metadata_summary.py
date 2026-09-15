"""Repo description summaries must survive Gemini thinking-mode list content.

Gemini 2.5/3.x models (google or vertex provider) return ``response.content`` as a
list of ``thinking`` + ``text`` blocks once ``include_thoughts`` is on. The old
``response.content.strip()`` raised ``AttributeError: 'list' object has no attribute
'strip'`` on every Bitbucket/GitLab repo, every retry (Bombora, Aug 2026).
"""
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from utils.repo_metadata import _generate_summary


def _run(content):
    """Call _generate_summary with the LLM plumbing replaced by stubs.

    The function imports its collaborators locally, so the stubs go into
    ``sys.modules`` where those imports resolve at call time.
    """
    providers = MagicMock()
    providers.create_chat_model.return_value = MagicMock(name="llm")
    llm_mod = MagicMock()
    llm_mod.ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL = "gemini-3.6-flash"
    tracker = MagicMock()
    tracker.tracked_invoke.return_value = SimpleNamespace(content=content)
    hooks = MagicMock()
    hooks.get_hook.return_value = lambda *_args, **_kwargs: (True, "")
    auth = MagicMock()
    auth.get_org_id_for_user.return_value = "org-1"
    messages = MagicMock()
    messages.HumanMessage = lambda content: content

    with patch.dict(sys.modules, {
        "chat.backend.agent.providers": providers,
        "chat.backend.agent.llm": llm_mod,
        "chat.backend.agent.utils.llm_usage_tracker": tracker,
        "utils.hooks": hooks,
        "utils.auth.stateless_auth": auth,
        "langchain_core.messages": messages,
    }):
        return _generate_summary("user-1", "README.md: Terraform modules for Datadog monitors")


def test_thinking_mode_list_content_yields_text_only():
    content = [
        {"type": "thinking", "thinking": "The user wants a one-line summary..."},
        {"type": "text", "text": "  Terraform modules that define Datadog monitors.  "},
    ]
    assert _run(content) == "Terraform modules that define Datadog monitors."


def test_plain_string_content_is_stripped():
    assert _run("  Terraform modules that define Datadog monitors.  ") == (
        "Terraform modules that define Datadog monitors."
    )


def test_empty_or_thinking_only_content_falls_back():
    assert _run("") == "No summary generated"
    assert _run([{"type": "thinking", "thinking": "only thoughts"}]) == "No summary generated"
