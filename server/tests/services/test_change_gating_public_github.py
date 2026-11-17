"""Tests for services.change_gating.public_github (no-install PR preview reads)."""

import os
from unittest.mock import MagicMock, patch

import pytest

from services.change_gating.public_github import (
    PublicGitHubReader,
    is_public_preview,
    parse_pr_url,
)
from services.change_gating.verdict import final_assistant_text


class TestIsPublicPreview:
    def test_only_the_preview_flag_on_a_pr_review_counts(self):
        assert is_public_preview({"source": "change_gating", "preview": True})
        assert not is_public_preview({"source": "change_gating"})  # installed review
        assert not is_public_preview({"source": "change_gating", "preview": "true"})
        assert not is_public_preview({"source": "grafana", "preview": True})
        assert not is_public_preview(None)
        assert not is_public_preview({})

    def test_workflow_skips_memory_extraction_for_previews(self):
        # The agent's own write_memory tools are already off for PR reviews; the
        # post-run collector must not write a stranger's PR into org memory either.
        path = os.path.join(
            os.path.dirname(__file__), os.pardir, os.pardir, "chat", "backend", "agent", "workflow.py"
        )
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        guard = source.index("if not is_public_preview(trigger_metadata):")
        call = source.index("extract_memories_from_session.delay(", guard)
        assert source.count("extract_memories_from_session.delay(") == 1
        assert call > guard


class TestParsePrUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/acme/widgets/pull/7",
            "http://github.com/acme/widgets/pull/7",
            "https://www.github.com/acme/widgets/pull/7",
            "github.com/acme/widgets/pull/7",
            "  https://github.com/acme/widgets/pull/7/  ",
            "https://github.com/acme/widgets/pull/7/files",
            "https://github.com/acme/widgets/pull/7/commits",
            "https://github.com/acme/widgets/pull/7?w=1",
            "https://github.com/acme/widgets/pull/7#discussion_r1",
        ],
    )
    def test_accepts_pr_links(self, url):
        assert parse_pr_url(url) == ("acme/widgets", 7)

    def test_keeps_dots_and_dashes_in_names(self):
        assert parse_pr_url("https://github.com/my-org/my.repo_x/pull/123") == (
            "my-org/my.repo_x",
            123,
        )

    @pytest.mark.parametrize(
        "url",
        [
            None,
            123,
            "",
            "https://github.com/acme/widgets/issues/7",
            "https://github.com/acme/widgets/pull/",
            "https://github.com/acme/widgets/pull/0",
            "https://github.com/acme/widgets/pull/7/evil",
            "https://github.com/acme/../pull/7",
            "https://github.com/-acme/widgets/pull/7",
            "https://gitlab.com/acme/widgets/pull/7",
            "https://github.com.evil.example/acme/widgets/pull/7",
            "https://api.github.com/repos/acme/widgets/pulls/7",
            "javascript:alert(1)//github.com/acme/widgets/pull/7",
            "https://github.com/acme/widgets/pull/7 extra",
        ],
    )
    def test_rejects_anything_else(self, url):
        assert parse_pr_url(url) is None


def _response(status=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    resp.raise_for_status.return_value = None
    return resp


@patch("services.change_gating.github_adapter.get_installation_token")
@patch("services.change_gating.github_adapter.requests")
class TestPublicGitHubReader:
    def test_reads_anonymously(self, mock_requests, mock_token):
        http = mock_requests.Session.return_value
        http.get.return_value = _response(json_data={"number": 7})

        reader = PublicGitHubReader("acme/widgets")
        assert reader.get_pull_request(7) == {"number": 7}

        call = http.get.call_args
        assert call.args[0] == "https://api.github.com/repos/acme/widgets/pulls/7"
        assert "Authorization" not in call.kwargs["headers"]
        mock_token.assert_not_called()  # no installation, no token minted

    def test_diff_keeps_the_diff_media_type(self, mock_requests, mock_token):
        http = mock_requests.Session.return_value
        http.get.return_value = _response(text="diff --git a/x b/x")

        assert PublicGitHubReader("acme/widgets").get_diff(7) == "diff --git a/x b/x"
        headers = http.get.call_args.kwargs["headers"]
        assert headers["Accept"] == "application/vnd.github.v3.diff"
        assert "Authorization" not in headers

    @pytest.mark.parametrize(
        "method, args",
        [
            ("post_review", (7,)),
            ("dismiss_review", (7, 1, "msg")),
            ("update_review_body", (7, 1, "body")),
            ("post_issue_comment", (7, "body")),
            ("delete_issue_comment", (1,)),
            ("supersede_review", (7, {}, "msg")),
        ],
    )
    def test_never_writes(self, mock_requests, mock_token, method, args):
        http = mock_requests.Session.return_value
        reader = PublicGitHubReader("acme/widgets")
        with pytest.raises(PermissionError):
            getattr(reader, method)(*args)
        http.post.assert_not_called()
        http.patch.assert_not_called()
        http.put.assert_not_called()
        http.delete.assert_not_called()


class TestFinalAssistantText:
    def test_returns_last_bot_message(self):
        messages = [
            {"sender": "user", "text": "prompt"},
            {"sender": "bot", "text": "first"},
            {"sender": "bot", "text": "final"},
        ]
        assert final_assistant_text(messages) == "final"

    def test_reads_json_string_and_content_field(self):
        assert final_assistant_text('[{"sender": "assistant", "content": "done"}]') == "done"

    def test_none_without_bot_message(self):
        assert final_assistant_text([{"sender": "user", "text": "hi"}]) is None
        assert final_assistant_text({"not": "a list"}) is None
