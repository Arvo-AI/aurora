"""The source-specific "critical requirements" segment is a table lookup; Slack
and Google Chat must still get their own segment and everything else the general
one, or @mention replies lose their reply-format instructions."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from chat.backend.agent.prompt import background as bg


def _segments_for(source: str) -> list:
    appended = []

    def fake_append(parts, name, *args, **kwargs):
        appended.append(name)

    state = SimpleNamespace(
        is_background=True,
        rca_context={"source": source, "providers": ["aws"], "integrations": {}},
        model="anthropic/claude",
    )
    with patch.object(bg, "_append_segment", side_effect=fake_append):
        bg.build_background_mode_segment(state)
    return appended


@pytest.mark.parametrize("source,expected", [
    ("slack", "background_source_slack"),
    ("SLACK", "background_source_slack"),
    ("google_chat", "background_source_google_chat"),
])
def test_platform_sources_pick_their_segment(source, expected):
    names = _segments_for(source)
    assert expected in names
    assert "background_source_general" not in names


@pytest.mark.parametrize("source", ["grafana", "chat", "team_routing"])
def test_other_sources_fall_through_to_general(source):
    names = _segments_for(source)
    assert "background_source_general" in names
    assert "background_source_slack" not in names
