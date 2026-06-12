"""Tests for services.change_gating.diff_utils."""

from services.change_gating.diff_utils import (
    anchor_findings,
    parse_diff_hunks,
    truncate_diff_for_prompt,
)

# Right-side line math, hand-computed:
#   app/main.py hunk 1 (+10,5): context1=10, +A=11, +B=12, context2=13, context3=14
#   app/main.py hunk 2 (+41,4): ctx=41, +new=42, ctx2=43, ctx3=44
#   new_file.txt (+1,2): first=1, second=2
#   old.txt: deleted (+++ /dev/null) -> no right side at all
MULTI_FILE_DIFF = """diff --git a/app/main.py b/app/main.py
index 1111111..2222222 100644
--- a/app/main.py
+++ b/app/main.py
@@ -10,4 +10,5 @@ def handler():
 context1
-removed line
+added line A
+added line B
 context2
 context3
@@ -40,3 +41,4 @@
 ctx
+new line
 ctx2
 ctx3
diff --git a/new_file.txt b/new_file.txt
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new_file.txt
@@ -0,0 +1,2 @@
+first
+second
diff --git a/old.txt b/old.txt
deleted file mode 100644
index 4444444..0000000
--- a/old.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-gone1
-gone2
\\ No newline at end of file
"""


class TestParseDiffHunks:
    def test_multi_file_multi_hunk_right_side_line_numbers(self):
        hunks = parse_diff_hunks(MULTI_FILE_DIFF)
        assert hunks["app/main.py"] == {10, 11, 12, 13, 14, 41, 42, 43, 44}

    def test_new_file_lines(self):
        hunks = parse_diff_hunks(MULTI_FILE_DIFF)
        assert hunks["new_file.txt"] == {1, 2}

    def test_deleted_file_has_no_right_side(self):
        hunks = parse_diff_hunks(MULTI_FILE_DIFF)
        assert "old.txt" not in hunks

    def test_deletion_lines_do_not_advance_right_counter(self):
        # Hunk 1 has a "-removed line" between context1 (10) and +A (11):
        # if deletions advanced the counter, 11 would be missing.
        hunks = parse_diff_hunks(MULTI_FILE_DIFF)
        assert 11 in hunks["app/main.py"]
        assert 15 not in hunks["app/main.py"]

    def test_no_newline_marker_on_right_side_is_ignored(self):
        diff = (
            "--- a/x.txt\n"
            "+++ b/x.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "\\ No newline at end of file\n"
        )
        assert parse_diff_hunks(diff) == {"x.txt": {1}}

    def test_hunk_header_without_count_defaults_to_one(self):
        diff = (
            "--- a/y.txt\n"
            "+++ b/y.txt\n"
            "@@ -5 +7 @@\n"
            "+only\n"
        )
        assert parse_diff_hunks(diff) == {"y.txt": {7}}

    def test_empty_diff(self):
        assert parse_diff_hunks("") == {}

    def test_none_diff(self):
        assert parse_diff_hunks(None) == {}

    def test_added_line_starting_with_plus_plus_is_not_a_file_header(self):
        """Regression: an added line whose CONTENT begins '++ ' renders as
        '+++ ...' in the diff; mid-hunk it must be consumed as hunk content
        (the hunk's line counts own it), not parsed as a new file header."""
        diff = (
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,5 @@\n"
            " line1\n"
            "+++ counter overflow note\n"
            "+normal added line\n"
            " line2\n"
            " line3\n"
        )
        assert parse_diff_hunks(diff) == {"foo.py": {1, 2, 3, 4, 5}}

    def test_trailing_deletions_with_dash_dash_content(self):
        """Right side exhausted but left side still consuming: a deleted
        line starting '-- ' (rendered '--- ') must not corrupt parsing."""
        diff = (
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,4 +1,2 @@\n"
            " keep\n"
            "--- removed line starting with dashes\n"
            "-removed2\n"
            "+++ added starting with plus-plus\n"
        )
        assert parse_diff_hunks(diff) == {"x.py": {1, 2}}


class TestAnchorFindings:
    def _finding(self, path, line, title="t"):
        return {
            "severity": "HIGH",
            "file_path": path,
            "line": line,
            "title": title,
            "explanation": "e",
        }

    def test_anchored_and_unanchored_split(self):
        hunks = {"app/main.py": {10, 11, 12}}
        in_hunk = self._finding("app/main.py", 11)
        outside_hunk = self._finding("app/main.py", 99)
        missing_line = self._finding("app/main.py", None)
        unknown_file = self._finding("other.py", 10)
        no_line_key = {
            "severity": "LOW",
            "file_path": "app/main.py",
            "title": "t",
            "explanation": "e",
        }

        anchored, unanchored = anchor_findings(
            [in_hunk, outside_hunk, missing_line, unknown_file, no_line_key], hunks
        )

        assert anchored == [in_hunk]
        assert unanchored == [outside_hunk, missing_line, unknown_file, no_line_key]

    def test_empty_findings(self):
        anchored, unanchored = anchor_findings([], {"a.py": {1}})
        assert anchored == []
        assert unanchored == []


class TestTruncateDiffForPrompt:
    FILES = [
        {"filename": "a.py", "status": "modified", "additions": 3, "deletions": 1},
        {"filename": "b/c.yaml", "status": "added", "additions": 20, "deletions": 0},
    ]

    def test_small_diff_returned_unchanged(self):
        diff = "diff --git a/a.py b/a.py\n+x\n"
        assert truncate_diff_for_prompt(diff, self.FILES) == diff

    def test_diff_at_exact_limit_returned_unchanged(self):
        diff = "x" * 50
        assert truncate_diff_for_prompt(diff, self.FILES, max_chars=50) == diff

    def test_large_diff_replaced_with_file_summary(self):
        diff = "x" * 100
        result = truncate_diff_for_prompt(diff, self.FILES, max_chars=50)

        assert result != diff
        assert "a.py (modified, +3/-1)" in result
        assert "b/c.yaml (added, +20/-0)" in result
        assert "github_rca" in result
        assert "too large to inline" in result
