"""Bitbucket file-op helpers: paged verbatim reads + edit guards."""

import os
import sys

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from chat.backend.agent.tools.bitbucket.utils import (  # noqa: E402
    _FILE_PAGE_CHARS,
    apply_edits_checked,
    page_file_content,
)


def test_page_file_content_small_file_unchanged():
    body = "hello\nworld\n"
    assert page_file_content(body) == body


def test_page_file_content_large_is_verbatim_prefix():
    lines = [f"line-{i:05d}-{'x' * 40}\n" for i in range(2000)]
    content = "".join(lines)
    assert len(content) > _FILE_PAGE_CHARS

    page = page_file_content(content, start_line=1)
    assert "Summarized from longer output" not in page
    assert "Summarized from larger output" not in page
    assert page.startswith("lines 1-")
    assert "pass start_line=" in page

    header, _, body = page.partition("\n")
    assert body == content[: len(body)]
    assert len(body) <= _FILE_PAGE_CHARS


def test_page_file_content_contiguous_pages_rebuild_original():
    lines = [f"L{i:04d}\n" for i in range(8000)]
    content = "".join(lines)
    assert len(content) > _FILE_PAGE_CHARS

    chunks: list[str] = []
    start = 1
    while True:
        page = page_file_content(content, start_line=start)
        header, _, body = page.partition("\n")
        chunks.append(body)
        if "pass start_line=" not in header:
            break
        start = int(header.split("pass start_line=")[1].split()[0])

    assert "".join(chunks) == content


def test_apply_edits_checked_rejects_noop():
    original = "alpha\nbeta\n"
    result, err = apply_edits_checked(
        original, [{"old_string": "beta", "new_string": "beta"}]
    )
    assert result is None
    assert err and ("no-op" in err or "no change" in err)


def test_apply_edits_checked_rejects_empty():
    original = "only\n"
    result, err = apply_edits_checked(
        original, [{"old_string": "only\n", "new_string": "   \n"}]
    )
    assert result is None
    assert err and "empty" in err.lower()


def test_apply_edits_checked_applies_real_edit():
    original = "foo = 1\nbar = 2\n"
    result, err = apply_edits_checked(
        original, [{"old_string": "foo = 1", "new_string": "foo = 3"}]
    )
    assert err is None
    assert result == "foo = 3\nbar = 2\n"
