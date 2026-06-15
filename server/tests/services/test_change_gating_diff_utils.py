"""Tests for services.change_gating.diff_utils."""

from services.change_gating.diff_utils import (
    anchor_findings,
    build_per_file_diff,
    parse_diff_hunks,
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

    def test_added_only_excludes_context_lines(self):
        # Incremental mode: only ADDED (+) right-side lines, not context.
        # app/main.py: +A=11, +B=12 (hunk1), +new=42 (hunk2); 10/13/14/41/43/44
        # are context and must be excluded. new_file.txt is all-added.
        hunks = parse_diff_hunks(MULTI_FILE_DIFF, added_only=True)
        assert hunks["app/main.py"] == {11, 12, 42}
        assert hunks["new_file.txt"] == {1, 2}
        # Sanity: the default (context-inclusive) set is a strict superset.
        full = parse_diff_hunks(MULTI_FILE_DIFF)
        assert hunks["app/main.py"] < full["app/main.py"]

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


class TestBuildPerFileDiff:
    FILES = [
        {
            "filename": "a.py", "status": "modified", "additions": 3, "deletions": 1,
            "patch": "@@ -1,2 +1,4 @@\n context\n+added a.py line\n",
        },
        {
            "filename": "b/c.yaml", "status": "added", "additions": 20, "deletions": 0,
            "patch": "@@ -0,0 +1,2 @@\n+replicas: 1\n+image: app:latest\n",
        },
    ]

    def test_renders_one_labelled_section_per_file(self):
        result = build_per_file_diff(self.FILES)
        # Each file gets its own heading and fenced diff block.
        assert "### a.py (modified, +3/-1)" in result
        assert "### b/c.yaml (added, +20/-0)" in result
        assert "+added a.py line" in result
        assert "+replicas: 1" in result
        assert result.count("```diff") == 2

    def test_no_deprecated_github_rca_pointer(self):
        # The oversized-diff fallback must not steer the agent to github_rca.
        files = [{"filename": "big.py", "status": "modified", "additions": 9, "deletions": 0}]
        result = build_per_file_diff(files)
        assert "github_rca" not in result
        # File served without a patch is flagged, pointing at PR-reading tools.
        assert "### big.py" in result
        assert "GitHub PR-reading tools" in result

    def test_per_file_cap_truncates_one_huge_file(self):
        files = [{
            "filename": "huge.py", "status": "modified", "additions": 999, "deletions": 0,
            "patch": "@@ -1 +1 @@\n" + "+x\n" * 5000,
        }]
        result = build_per_file_diff(files, max_file_chars=200)
        assert "truncated at 200 chars" in result
        assert "GitHub PR-reading tools" in result

    def test_total_budget_omits_later_files(self):
        files = [
            {"filename": f"f{i}.py", "status": "modified", "additions": 1, "deletions": 0,
             "patch": "@@ -1 +1 @@\n" + "+y\n" * 200}
            for i in range(5)
        ]
        result = build_per_file_diff(files, max_total_chars=900, max_file_chars=800)
        # At least the first file rendered; later ones flagged as omitted.
        assert "### f0.py" in result
        assert "omitted to stay within the diff budget" in result

    def test_escape_applied_to_patch_not_scaffolding(self):
        files = [{
            "filename": "x.py", "status": "modified", "additions": 1, "deletions": 0,
            "patch": "@@ -1 +1 @@\n+```malicious fence```\n",
        }]
        result = build_per_file_diff(files, escape=lambda s: s.replace("```", "X"))
        # Our own ```diff fence survives; the author's backticks are defanged.
        assert "```diff" in result
        assert "Xmalicious fenceX" in result

    def test_escape_applied_to_author_controlled_filename(self):
        # A crafted filename must be defanged too — it lands in the header,
        # truncation note, and omitted footer, all outside the ```diff fence.
        files = [{
            "filename": "evil```name.py", "status": "modified",
            "additions": 1, "deletions": 0,
            "patch": "@@ -1 +1 @@\n+x\n",
        }]
        result = build_per_file_diff(files, escape=lambda s: s.replace("```", "X"))
        assert "evil```name.py" not in result  # raw backticks gone
        assert "evilXname.py" in result

    def test_all_no_patch_falls_back_to_raw_diff(self):
        # Every file is binary/over-limit (no patch) but GitHub served the diff:
        # the agent still gets real content, not only "no inline diff" notes.
        files = [{"filename": "a.bin", "status": "modified", "additions": 0, "deletions": 0}]
        result = build_per_file_diff(files, diff="@@ real diff content @@")
        assert "no per-file patches available" in result
        assert "real diff content" in result
        assert "```diff" in result

    def test_empty_files_falls_back_to_fenced_raw_diff(self):
        result = build_per_file_diff([], diff="raw diff text")
        assert "raw diff text" in result
        assert "```diff" in result  # fenced, not bare (structural separation)
        assert "No file-level changes" in build_per_file_diff([], diff=None)
