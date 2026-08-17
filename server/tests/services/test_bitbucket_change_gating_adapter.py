"""Tests for the Bitbucket change-gating adapter.

Pins the provider-normalization contract of ``BitbucketPRAdapter``
(design doc ``bitbucket-incident-prevention.md``):

- PR dicts normalize to the GitHub shape (OPEN → open, source.commit.hash
  → head.sha, destination branch → base.ref, connected default branch →
  base.repo.default_branch).
- ``{"error": True}`` client results RAISE (with a classifiable
  status_code) instead of flowing through as data.
- ``find_aurora_reviews`` requires marker AND allowlisted author UUID —
  a marker alone (paste attack) never qualifies.
- SAFE posting keeps the comment when the approve step fails.
- ``get_compare`` / ``get_compare_diff`` return None (full-PR fallback).
- ``parse_files_from_diff`` yields GitHub ``list_files``-shaped dicts.

All Bitbucket API calls are mocked — no I/O.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.change_gating.bitbucket_adapter import (
    BitbucketAPIError,
    BitbucketPRAdapter,
    hide_markers_for_bitbucket,
    parse_files_from_diff,
    restore_markers_from_bitbucket,
)
from services.change_gating.markers import decode_marker, encode_marker, has_aurora_marker

_REPO = "acme/api"
_BOT_UUID = "{bot-uuid-1}"
_OTHER_UUID = "{someone-else}"


def _adapter(read=None, post=None, default_branch="main"):
    read = read or MagicMock()
    post = post or read
    adapter = BitbucketPRAdapter(
        _REPO, read_client=read, post_client=post, default_branch=default_branch
    )
    # Pin identity without an API round-trip.
    adapter._aurora_uuids = {_BOT_UUID}
    return adapter, read, post


def _bb_pr(state="OPEN", head="abc123", dest="main", draft=False):
    return {
        "id": 7,
        "state": state,
        "draft": draft,
        "title": "Tighten retry loop",
        "description": "desc",
        "author": {"nickname": "octocat", "display_name": "Octo Cat"},
        "source": {"commit": {"hash": head}, "branch": {"name": "feature/x"}},
        "destination": {"commit": {"hash": "base456"}, "branch": {"name": dest}},
        "participants": [],
    }


class TestNormalization:
    def test_open_state_and_field_mapping(self):
        adapter, read, _ = _adapter()
        read.get_pull_request.return_value = _bb_pr()

        pr = adapter.get_pull_request(7)

        assert pr["state"] == "open"
        assert pr["number"] == 7
        assert pr["head"]["sha"] == "abc123"
        assert pr["base"]["ref"] == "main"
        assert pr["base"]["repo"]["default_branch"] == "main"
        assert pr["user"]["login"] == "octocat"
        assert pr["draft"] is False

    @pytest.mark.parametrize("bb_state", ["MERGED", "DECLINED", "SUPERSEDED"])
    def test_closed_states_normalize_to_closed(self, bb_state):
        adapter, read, _ = _adapter()
        read.get_pull_request.return_value = _bb_pr(state=bb_state)
        assert adapter.get_pull_request(7)["state"] == "closed"

    def test_default_branch_falls_back_to_repo_mainbranch(self):
        adapter, read, _ = _adapter(default_branch=None)
        read.get_pull_request.return_value = _bb_pr()
        read.get_repository.return_value = {"mainbranch": {"name": "develop"}}
        pr = adapter.get_pull_request(7)
        assert pr["base"]["repo"]["default_branch"] == "develop"

    def test_error_dict_raises_with_status(self):
        adapter, read, _ = _adapter()
        read.get_pull_request.return_value = {
            "error": True, "status": 404, "message": "not found",
        }
        with pytest.raises(BitbucketAPIError) as excinfo:
            adapter.get_pull_request(7)
        assert excinfo.value.response.status_code == 404

    def test_error_dict_raises_transient_status(self):
        adapter, read, _ = _adapter()
        read.get_pr_diff.return_value = {"error": True, "status": 502, "message": "bad gateway"}
        with pytest.raises(BitbucketAPIError) as excinfo:
            adapter.get_diff(7)
        assert excinfo.value.response.status_code == 502


class TestCompareFallback:
    def test_compare_returns_none(self):
        adapter, _, _ = _adapter()
        assert adapter.get_compare("a", "b") is None
        assert adapter.get_compare_diff("a", "b") is None

    def test_no_inline_comments_in_poc(self):
        adapter, _, _ = _adapter()
        assert adapter.list_review_comments(7) == []


class TestAuroraIdentity:
    def _comment(self, cid, body, uuid, inline=None, parent=None, deleted=False):
        return {
            "id": cid,
            "deleted": deleted,
            "inline": inline,
            "parent": parent,
            "content": {"raw": body},
            "user": {"uuid": uuid},
        }

    def test_marker_and_uuid_required(self):
        adapter, read, _ = _adapter()
        marker_body = "## Aurora Risk Review\n\n" + encode_marker([], "abc123")
        read.list_pr_comments.return_value = [
            self._comment(1, marker_body, _BOT_UUID),          # genuine
            self._comment(2, marker_body, _OTHER_UUID),        # paste attack
            self._comment(3, "just a human comment", _BOT_UUID),  # no marker
            self._comment(4, marker_body, _BOT_UUID, inline={"path": "x"}),  # inline
            self._comment(5, marker_body, _BOT_UUID, deleted=True),  # deleted
            self._comment(6, marker_body, _BOT_UUID, parent={"id": 1}),  # reply
        ]
        reviews = adapter.list_reviews(7)
        aurora = adapter.find_aurora_reviews(reviews)
        assert [r["id"] for r in aurora] == [1]

    def test_last_review_marked_approved_when_aurora_approves(self):
        adapter, read, _ = _adapter()
        marker_body = encode_marker([], "abc123")
        read.list_pr_comments.return_value = [
            self._comment(1, marker_body, _BOT_UUID),
            self._comment(2, marker_body, _BOT_UUID),
        ]
        adapter._last_participants = [
            {"approved": True, "user": {"uuid": _BOT_UUID}},
        ]
        aurora = adapter.find_aurora_reviews(adapter.list_reviews(7))
        assert aurora[0]["state"] == "COMMENTED"
        assert aurora[-1]["state"] == "APPROVED"

    def test_other_users_approval_does_not_mark_aurora(self):
        adapter, read, _ = _adapter()
        read.list_pr_comments.return_value = [
            self._comment(1, encode_marker([], "abc123"), _BOT_UUID),
        ]
        adapter._last_participants = [
            {"approved": True, "user": {"uuid": _OTHER_UUID}},
        ]
        aurora = adapter.find_aurora_reviews(adapter.list_reviews(7))
        assert aurora[-1]["state"] == "COMMENTED"


class TestPosting:
    def test_safe_posts_comment_and_approves(self):
        adapter, _, post = _adapter()
        post.add_pr_comment.return_value = {"id": 42}
        post.approve_pull_request.return_value = {"approved": True}

        result = adapter.post_review(
            7, commit_id="abc123", event="APPROVE", body="SAFE body", comments=[]
        )

        assert result["id"] == 42
        post.add_pr_comment.assert_called_once()
        post.approve_pull_request.assert_called_once()

    def test_safe_keeps_comment_when_approve_fails(self):
        adapter, _, post = _adapter()
        post.add_pr_comment.return_value = {"id": 42}
        post.approve_pull_request.return_value = {
            "error": True, "status": 400, "message": "You cannot approve your own pull request",
        }
        # Must NOT raise — the comment is the review.
        result = adapter.post_review(
            7, commit_id="abc123", event="APPROVE", body="SAFE body", comments=[]
        )
        assert result["id"] == 42

    def test_risky_posts_comment_only(self):
        adapter, _, post = _adapter()
        post.add_pr_comment.return_value = {"id": 43}
        adapter.post_review(
            7, commit_id="abc123", event="COMMENT", body="RISKY body",
            comments=[{"path": "a.py", "line": 3, "body": "x"}],
        )
        post.add_pr_comment.assert_called_once()
        post.approve_pull_request.assert_not_called()

    def test_comment_post_failure_raises(self):
        adapter, _, post = _adapter()
        post.add_pr_comment.return_value = {"error": True, "status": 403, "message": "forbidden"}
        with pytest.raises(BitbucketAPIError):
            adapter.post_review(7, commit_id="abc", event="COMMENT", body="b", comments=[])

    def test_dismiss_unapproves(self):
        adapter, read, post = _adapter()
        post.unapprove_pull_request.return_value = {"success": True}
        read.list_pr_comments.return_value = []
        adapter.dismiss_review(7, 42, "stale")
        post.unapprove_pull_request.assert_called_once()

    def test_dismiss_tolerates_404_no_approval(self):
        adapter, read, post = _adapter()
        post.unapprove_pull_request.return_value = {"error": True, "status": 404, "message": "gone"}
        read.list_pr_comments.return_value = []
        adapter.dismiss_review(7, 42, "stale")  # must not raise

    def test_supersede_prepends_note_and_is_idempotent(self):
        adapter, _, post = _adapter()
        prior = {"id": 42, "state": "COMMENTED", "body": "old body"}
        post.update_pr_comment.return_value = {"id": 42}
        adapter.supersede_review(7, prior, "Superseded by updated review")
        args = post.update_pr_comment.call_args[0]
        assert args[4].startswith("**Superseded by updated review**")

        post.update_pr_comment.reset_mock()
        prior_noted = {**prior, "body": "**Superseded by updated review**\n\nold body"}
        adapter.supersede_review(7, prior_noted, "Superseded by updated review")
        post.update_pr_comment.assert_not_called()

    def test_supersede_approved_also_unapproves(self):
        adapter, _, post = _adapter()
        post.update_pr_comment.return_value = {"id": 42}
        post.unapprove_pull_request.return_value = {"success": True}
        adapter.supersede_review(
            7, {"id": 42, "state": "APPROVED", "body": "b"}, "Superseded"
        )
        post.unapprove_pull_request.assert_called_once()


class TestProgressComment:
    def test_delete_uses_recorded_pr(self):
        adapter, _, post = _adapter()
        post.add_pr_comment.return_value = {"id": 99}
        post.delete_pr_comment.return_value = {"success": True}
        comment = adapter.post_issue_comment(7, "reviewing…")
        adapter.delete_issue_comment(comment["id"])
        post.delete_pr_comment.assert_called_once_with("acme", "api", 7, 99)

    def test_delete_unknown_comment_is_noop(self):
        adapter, _, post = _adapter()
        adapter.delete_issue_comment(12345)
        post.delete_pr_comment.assert_not_called()

    def test_delete_tolerates_404(self):
        adapter, _, post = _adapter()
        post.add_pr_comment.return_value = {"id": 99}
        post.delete_pr_comment.return_value = {"error": True, "status": 404, "message": "gone"}
        comment = adapter.post_issue_comment(7, "x")
        adapter.delete_issue_comment(comment["id"])  # must not raise


class TestMarkerHiding:
    """Bitbucket escapes raw HTML, so HTML-comment markers would render as
    visible text — the adapter converts them to link-reference definitions
    (`[//]: # (...)`) on write and back on read."""

    def test_round_trip_preserves_decodability(self):
        marker = encode_marker([{"severity": "HIGH", "title": "x"}], "abc123")
        body = f"## Aurora Risk Review\n\nSAFE\n\n{marker}"
        hidden = hide_markers_for_bitbucket(body)
        assert "<!--" not in hidden  # nothing left for Bitbucket to escape
        assert "[//]: # (" in hidden
        restored = restore_markers_from_bitbucket(hidden)
        assert has_aurora_marker(restored)
        assert decode_marker(restored)["head_sha"] == "abc123"

    def test_posted_review_body_carries_no_html_comment(self):
        adapter, _, post = _adapter()
        post.add_pr_comment.return_value = {"id": 42}
        marker = encode_marker([], "abc123")
        adapter.post_review(
            7, commit_id="abc123", event="COMMENT",
            body=f"RISKY body\n\n{marker}", comments=[],
        )
        sent_body = post.add_pr_comment.call_args[0][3]
        assert "<!--" not in sent_body
        assert "[//]: # (" in sent_body

    def test_list_reviews_restores_hidden_markers(self):
        adapter, read, _ = _adapter()
        marker = encode_marker([], "abc123")
        hidden_body = hide_markers_for_bitbucket(f"## Review\n\n{marker}")
        read.list_pr_comments.return_value = [
            {"id": 1, "deleted": False, "inline": None, "parent": None,
             "content": {"raw": hidden_body}, "user": {"uuid": _BOT_UUID}},
        ]
        aurora = adapter.find_aurora_reviews(adapter.list_reviews(7))
        assert len(aurora) == 1
        assert decode_marker(aurora[0]["body"])["head_sha"] == "abc123"

    def test_legacy_html_comment_bodies_still_recognized(self):
        # Comments posted before the translation existed carry raw HTML
        # comments; restore is a no-op and they must still qualify.
        adapter, read, _ = _adapter()
        legacy_body = f"## Review\n\n{encode_marker([], 'abc123')}"
        read.list_pr_comments.return_value = [
            {"id": 1, "deleted": False, "inline": None, "parent": None,
             "content": {"raw": legacy_body}, "user": {"uuid": _BOT_UUID}},
        ]
        aurora = adapter.find_aurora_reviews(adapter.list_reviews(7))
        assert len(aurora) == 1

    def test_progress_comment_marker_hidden(self):
        adapter, _, post = _adapter()
        post.add_pr_comment.return_value = {"id": 99}
        adapter.post_issue_comment(7, "<!-- aurora-change-gating:progress -->\nReviewing…")
        sent_body = post.add_pr_comment.call_args[0][3]
        assert "<!--" not in sent_body


class TestParseFilesFromDiff:
    DIFF = (
        "diff --git a/server/app.py b/server/app.py\n"
        "index 111..222 100644\n"
        "--- a/server/app.py\n"
        "+++ b/server/app.py\n"
        "@@ -1,3 +1,4 @@\n"
        " import os\n"
        "+import sys\n"
        " x = 1\n"
        "-y = 2\n"
        "+y = 3\n"
        "diff --git a/new.txt b/new.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+hello\n"
        "+world\n"
        "diff --git a/gone.txt b/gone.txt\n"
        "deleted file mode 100644\n"
        "--- a/gone.txt\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-bye\n"
    )

    def test_files_statuses_and_counts(self):
        files = parse_files_from_diff(self.DIFF)
        by_name = {f["filename"]: f for f in files}
        assert by_name["server/app.py"]["status"] == "modified"
        assert by_name["server/app.py"]["additions"] == 2
        assert by_name["server/app.py"]["deletions"] == 1
        assert by_name["new.txt"]["status"] == "added"
        assert by_name["new.txt"]["additions"] == 2
        assert by_name["gone.txt"]["status"] == "removed"
        assert by_name["gone.txt"]["deletions"] == 1

    def test_patch_starts_at_first_hunk(self):
        files = parse_files_from_diff(self.DIFF)
        app = next(f for f in files if f["filename"] == "server/app.py")
        assert app["patch"].startswith("@@ -1,3 +1,4 @@")
        assert "+import sys" in app["patch"]

    def test_patch_feeds_diff_utils_hunk_parser(self):
        from services.change_gating.diff_utils import parse_diff_hunks

        # The contract under test: each PER-FILE patch slice (not the raw
        # diff) is consumable by parse_diff_hunks — the same shape GitHub's
        # list_files patches have.
        files = parse_files_from_diff(self.DIFF)
        app = next(f for f in files if f["filename"] == "server/app.py")
        # parse_diff_hunks keys on the +++ b/ header, which the patch slice
        # strips; re-add the header the way build_per_file_diff consumers do.
        hunks = parse_diff_hunks(f"+++ b/{app['filename']}\n{app['patch']}")
        assert "server/app.py" in hunks
        # RIGHT-side commentable lines: 1-5 exist after the hunk (+import sys
        # inserted, y=3 replacing y=2).
        assert {1, 2, 3, 4} <= hunks["server/app.py"]

        gone = next(f for f in files if f["filename"] == "gone.txt")
        hunks_gone = parse_diff_hunks(f"+++ /dev/null\n{gone['patch']}")
        assert "gone.txt" not in hunks_gone  # deleted file: no RIGHT side

    def test_empty_and_none(self):
        assert parse_files_from_diff(None) == []
        assert parse_files_from_diff("") == []
