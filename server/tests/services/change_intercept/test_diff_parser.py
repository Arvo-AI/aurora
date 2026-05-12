"""Unit tests for the unified-diff parser used by the validator.

These tests pin the parser's behaviour against the kinds of diffs
GitHub returns via ``Accept: application/vnd.github.diff``. The
validator uses the resulting ``DiffIndex`` to verify every finding
references a line the engineer actually changed; a regression in the
parser would silently let hallucinated findings through.
"""

from __future__ import annotations

from services.change_intercept.diff_parser import (
    DiffHunk,
    DiffIndex,
    parse_unified_diff,
)


def test_empty_input_returns_empty_index() -> None:
    assert parse_unified_diff("").files_changed() == ()
    assert parse_unified_diff("   \n\n").files_changed() == ()


def test_whitespace_only_input_returns_empty_index() -> None:
    assert parse_unified_diff("\n\n\t\n").files_changed() == ()


def test_single_file_single_hunk_marks_added_lines_correctly() -> None:
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "index abc..def 100644\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -10,3 +10,4 @@ def hello():\n"
        " ctx_a\n"
        "-removed\n"
        "+added1\n"
        "+added2\n"
        " ctx_b\n"
    )
    index = parse_unified_diff(diff)

    assert index.files_changed() == ("foo.py",)
    # ``ctx_a`` is at new-file line 10, ``+added1`` at 11, ``+added2`` at 12,
    # ``ctx_b`` at 13. The validator should accept findings on 11/12.
    assert index.is_changed_line("foo.py", 11)
    assert index.is_changed_line("foo.py", 12)
    assert not index.is_changed_line("foo.py", 10)
    assert not index.is_changed_line("foo.py", 13)
    # Lax-anchor check: lines 10–13 are all "within the hunk."
    assert index.is_in_hunk("foo.py", 10)
    assert index.is_in_hunk("foo.py", 13)
    assert not index.is_in_hunk("foo.py", 14)


def test_file_creation_marks_all_new_lines() -> None:
    diff = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+line1\n"
        "+line2\n"
        "+line3\n"
    )
    index = parse_unified_diff(diff)

    assert index.is_changed_line("new.py", 1)
    assert index.is_changed_line("new.py", 2)
    assert index.is_changed_line("new.py", 3)


def test_file_deletion_yields_no_anchors() -> None:
    diff = (
        "diff --git a/old.py b/old.py\n"
        "deleted file mode 100644\n"
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-gone1\n"
        "-gone2\n"
    )
    index = parse_unified_diff(diff)

    # ``old.py`` is still in the file list (we want the validator to
    # explicitly drop findings against deleted files rather than treat
    # them as unknown), but with no hunks.
    assert "old.py" in index.files
    assert index.files["old.py"] == []
    assert not index.is_changed_line("old.py", 1)


def test_multiple_files_in_one_diff() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -5,1 +5,2 @@\n"
        " ctx\n"
        "+new_in_a\n"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -1,1 +1,2 @@\n"
        " keep\n"
        "+new_in_b\n"
    )
    index = parse_unified_diff(diff)

    assert set(index.files_changed()) == {"a.py", "b.py"}
    assert index.is_changed_line("a.py", 6)
    assert index.is_changed_line("b.py", 2)
    # Cross-file isolation: a.py's added line is not b.py's added line.
    assert not index.is_changed_line("a.py", 2)


def test_multiple_hunks_in_one_file() -> None:
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -10,1 +10,2 @@\n"
        " a\n"
        "+b\n"
        "@@ -50,1 +60,2 @@\n"
        " c\n"
        "+d\n"
    )
    index = parse_unified_diff(diff)

    assert index.is_changed_line("foo.py", 11)
    assert index.is_changed_line("foo.py", 61)
    # First hunk only covers 10–11, second hunk only 60–61; the gap
    # between them is unchanged code.
    assert not index.is_in_hunk("foo.py", 30)


def test_omitted_counts_default_to_one() -> None:
    # GitHub omits the count when it is 1: ``@@ -10 +10 @@``.
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -10 +10 @@\n"
        "-x\n"
        "+y\n"
    )
    index = parse_unified_diff(diff)

    assert index.is_changed_line("foo.py", 10)


def test_no_newline_marker_is_ignored() -> None:
    # ``\ No newline at end of file`` lines must not advance the cursor.
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,2 +1,2 @@\n"
        " keep\n"
        "-old_tail\n"
        "\\ No newline at end of file\n"
        "+new_tail\n"
        "\\ No newline at end of file\n"
    )
    index = parse_unified_diff(diff)

    # ``+new_tail`` is at new-file line 2 (after the kept line at 1).
    assert index.is_changed_line("foo.py", 2)


def test_binary_diff_yields_empty_hunks() -> None:
    diff = (
        "diff --git a/img.png b/img.png\n"
        "index abc..def 100644\n"
        "Binary files a/img.png and b/img.png differ\n"
    )
    index = parse_unified_diff(diff)

    assert "img.png" in index.files
    assert index.files["img.png"] == []


def test_rename_only_yields_empty_hunks() -> None:
    diff = (
        "diff --git a/old_name.py b/new_name.py\n"
        "similarity index 100%\n"
        "rename from old_name.py\n"
        "rename to new_name.py\n"
    )
    index = parse_unified_diff(diff)

    # We prefer the post-change (``b/``) path. Validator-level decision
    # is to drop findings against pure renames; here we only assert
    # the parser doesn't crash and exposes the file.
    assert "new_name.py" in index.files


def test_malformed_input_returns_empty_index_instead_of_raising() -> None:
    diff = "this is not a diff at all\nnope\nstill nope"

    # Tolerant parsing: no hunks, no files. The validator interprets
    # this as "drop every finding," which is the safe failure mode.
    index = parse_unified_diff(diff)
    assert index.files_changed() == ()


def test_diff_hunk_is_frozen_dataclass() -> None:
    # The validator stores ``DiffHunk`` instances by reference; a
    # mutable hunk would let a downstream caller silently rewrite the
    # parsed diff. Pin the frozen contract.
    hunk = DiffHunk(new_start=1, new_count=1, added_lines=frozenset({1}))
    try:
        hunk.new_start = 99  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("DiffHunk should be frozen")


def test_diff_index_initial_state_is_empty() -> None:
    idx = DiffIndex()
    assert idx.files_changed() == ()
    assert not idx.is_changed_line("anything.py", 1)
    assert not idx.is_in_hunk("anything.py", 1)
