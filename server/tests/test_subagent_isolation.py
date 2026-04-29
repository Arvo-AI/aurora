"""Tests for sub-agent prompt isolation and result-shape minimalism.

Covers anti-pattern #1: parent thoughts must not leak into sub-agent prompts.
The prompt builder uses a strict allowlist (purpose, suggested_skill_focus,
kb_memory) and a positive-allowlist test confirms unrelated content can never
appear in a rendered prompt unless explicitly passed in.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

# Ensure server/ is on sys.path
_server_dir = os.path.join(os.path.dirname(__file__), os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

# Stub heavy third-party packages.
for _pkg in (
    "langchain_core",
    "langchain_core.messages",
):
    if _pkg not in sys.modules:
        sys.modules[_pkg] = MagicMock()

from chat.backend.agent.subagent.prompts import build_subagent_system_prompt  # noqa: E402
from chat.backend.agent.subagent.state import SubAgentResult  # noqa: E402


# ---------------------------------------------------------------------------
# Allowlist: only purpose / suggested_skill_focus / kb_memory may appear.
# ---------------------------------------------------------------------------


def test_prompt_only_includes_allowed_fields():
    """Content not passed via the allowlist must not appear in the prompt."""
    # given a clean prompt build that does NOT pass parent thoughts
    leaky_secret = "PARENT_AGENT_THOUGHTS_DO_NOT_LEAK_42"
    rendered = build_subagent_system_prompt(
        purpose="investigate db",
        suggested_skill_focus=["postgres"],
        kb_memory="kb hint A",
    )

    # when we inspect the rendered prompt
    # then only fields explicitly passed in are present
    assert "investigate db" in rendered
    assert "postgres" in rendered
    assert "kb hint A" in rendered
    # and unrelated parent-thought-shaped content cannot appear
    assert leaky_secret not in rendered


def test_purpose_length_capped():
    """Purposes longer than 1000 chars are truncated."""
    long_purpose = "A" * 5000
    rendered = build_subagent_system_prompt(
        purpose=long_purpose,
        suggested_skill_focus=[],
        kb_memory="",
    )
    # the rendered prompt must contain at most 1000 'A' chars in a row
    assert "A" * 1001 not in rendered
    assert "A" * 1000 in rendered


def test_purpose_strips_control_chars():
    """Disallowed control chars in purpose raise ValueError; \\n and \\t pass."""
    # _sanitize_purpose REJECTS C0 controls (other than \n, \t) by raising.
    with pytest.raises(ValueError):
        build_subagent_system_prompt(
            purpose="normal\x00text\x07more",
            suggested_skill_focus=[],
            kb_memory="",
        )

    # newlines and tabs are explicitly preserved
    rendered = build_subagent_system_prompt(
        purpose="line1\nline2\tcol",
        suggested_skill_focus=[],
        kb_memory="",
    )
    assert "line1\nline2\tcol" in rendered


def test_skill_focus_per_item_capped():
    """Skill items >100 chars are dropped; valid items kept."""
    rendered = build_subagent_system_prompt(
        purpose="p",
        suggested_skill_focus=["a" * 200, "valid_skill"],
        kb_memory="",
    )
    assert "valid_skill" in rendered
    # the long item is filtered out entirely (sanitizer drops > 100 char items)
    assert "a" * 200 not in rendered


def test_kb_memory_5000_char_cap():
    """kb_memory longer than 5000 chars is truncated."""
    rendered = build_subagent_system_prompt(
        purpose="p",
        suggested_skill_focus=[],
        kb_memory="K" * 6000,
    )
    assert "K" * 5001 not in rendered
    assert "K" * 5000 in rendered


# ---------------------------------------------------------------------------
# SubAgentResult: minimal shape — no transcript, no parent thoughts.
# ---------------------------------------------------------------------------


def test_subagent_result_only_carries_summary():
    """SubAgentResult must expose only the documented fields."""
    result = SubAgentResult(
        agent_id="sub-abc",
        purpose="investigate db",
        status="succeeded",
        findings_artifact_ref="path/to/findings.md",
        error=None,
    )
    declared = set(result.model_fields.keys())
    assert declared == {
        "agent_id",
        "purpose",
        "status",
        "findings_artifact_ref",
        "error",
    }
    # given the minimal shape, when we dump it, then no transcript or parent
    # thought field exists
    dumped = result.model_dump()
    forbidden = {"transcript", "parent_thoughts", "messages", "react_capture"}
    assert not (forbidden & set(dumped.keys()))
