"""Tests for the Bitbucket large-file edit path.

Pins the behaviour that makes surgical edits on huge files safe by
construction: paged verbatim reads that never trip the tool-output
summarizer (page_file_content), in-file search for navigation
(find_in_file), the anchored edit_file action with read-SHA
compare-and-swap, the RCA read-failure contract (_get_file_content must
never turn a fetch failure into "file doesn't exist"), the api-client
Content-Type fix for .json files, and the read-only session gate.
"""

import json
import os
import re
import sys
from unittest.mock import MagicMock

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from chat.backend.agent.tools.bitbucket.utils import (  # noqa: E402
    PAGE_CONTENT_BUDGET,
    apply_edits_checked,
    page_file_content,
)
from chat.backend.agent.utils.tool_output_cap import PASS_THROUGH_CHARS  # noqa: E402
from chat.backend.agent.tools.bitbucket import repos_tool  # noqa: E402
from chat.backend.agent.tools.bitbucket.repos_tool import (  # noqa: E402
    _find_in_content,
    bitbucket_repos,
)
from chat.backend.agent.tools.bitbucket import fix_tool  # noqa: E402
from chat.backend.agent.tools.bitbucket import apply_fix_tool  # noqa: E402
from connectors.bitbucket_connector import api_client as bb_api  # noqa: E402


_SHA = "a" * 40

_HEADER_RE = re.compile(
    r"^\[lines (\d+)-(\d+) of (\d+) — "
    r"(?:pass start_line=(\d+)(?: start_char=(\d+))?(?: commit=[0-9a-f]{40})?"
    r" to continue|end of file)\]\n"
)


def _build_big_file(num_lines: int = 5000) -> str:
    """~400K chars for the default 5000 lines; every line unique."""
    return "\n".join(f"line {i:05d} " + "x" * 70 for i in range(1, num_lines + 1))


def _read_all_pages(content: str) -> tuple[list[str], list[str]]:
    """Follow continue hints; return (header-stripped pages, raw pages)."""
    stripped, raw = [], []
    start_line, start_char = 1, 0
    for _ in range(1000):
        page = page_file_content(content, start_line, start_char)
        raw.append(page)
        m = _HEADER_RE.match(page)
        if not m:
            stripped.append(page)  # single-page file, no header
            return stripped, raw
        stripped.append(page[m.end():])
        if m.group(4) is None:  # "end of file"
            return stripped, raw
        start_line = int(m.group(4))
        start_char = int(m.group(5)) if m.group(5) else 0
    pytest.fail("paging did not terminate")
    raise AssertionError("unreachable")  # pytest.fail raises; appeases linters


# ---------------------------------------------------------------------------
# page_file_content
# ---------------------------------------------------------------------------


def test_small_file_returned_unchanged_without_header():
    content = "line one\nline two\n"
    assert page_file_content(content) == content


def test_big_file_pages_reconstruct_byte_identical():
    content = _build_big_file()
    assert len(content) > 390_000
    stripped, raw = _read_all_pages(content)
    assert len(stripped) > 1
    assert "".join(stripped) == content
    # Every serialized page stays under the 40K summarizer threshold.
    for page in raw:
        assert len(json.dumps({"content": page})) < PASS_THROUGH_CHARS


def test_page_headers_carry_correct_line_ranges():
    content = _build_big_file(3000)
    first = page_file_content(content, 1, 0)
    m = _HEADER_RE.match(first)
    assert m is not None
    assert m.group(1) == "1"
    assert m.group(3) == "3000"
    next_line = int(m.group(4))
    assert int(m.group(2)) == next_line - 1
    second = page_file_content(content, next_line, 0)
    m2 = _HEADER_RE.match(second)
    assert m2 is not None
    assert int(m2.group(1)) == next_line


def test_escape_heavy_content_stays_under_cap():
    # Quotes, backslashes, and non-ASCII all expand when JSON-escaped; the
    # raw-length budget of a naive pager would overshoot the 40K cap here.
    line = ('"\\' * 30 + "é") * 5
    content = "\n".join(line for _ in range(2000))
    stripped, raw = _read_all_pages(content)
    assert len(raw) > 1
    assert "".join(stripped) == content
    for page in raw:
        assert len(json.dumps({"content": page})) < PASS_THROUGH_CHARS


def test_single_oversized_line_continues_via_start_char():
    content = "x" * (PAGE_CONTENT_BUDGET * 3 + 1000)  # one line, no newlines
    first = page_file_content(content, 1, 0)
    m = _HEADER_RE.match(first)
    assert m is not None
    assert m.group(4) == "1"  # continue on the SAME line...
    assert m.group(5) is not None  # ...at a character offset
    stripped, _raw = _read_all_pages(content)
    assert len(stripped) >= 3
    assert "".join(stripped) == content


def test_start_line_past_end_of_file():
    out = page_file_content("a\nb", start_line=10)
    assert "past the end" in out


def test_start_char_past_end_of_line_is_an_explicit_error():
    # A stale/invented start_char must never silently drop part of a line
    # while the header claims a verbatim range.
    out = page_file_content("short\nrest", start_line=1, start_char=1000)
    assert "past the end of line 1" in out
    assert "rest" not in out


def test_continue_hints_carry_commit_pin_when_ref_given():
    content = _build_big_file(3000)
    first = page_file_content(content, 1, 0, ref=_SHA)
    m = _HEADER_RE.match(first)
    assert m is not None
    assert f" commit={_SHA} to continue]" in first.split("\n", 1)[0]


# ---------------------------------------------------------------------------
# apply_edits_checked — the core surgical-edit scenario
# ---------------------------------------------------------------------------


def test_scattered_edits_on_400k_file():
    """3 anchored edits at scattered locations in one call: all hunks land,
    every byte outside the edited spans is untouched."""
    content = _build_big_file(5000)
    lines = content.split("\n")

    spans = [(0, 5), (499, 520), (1999, 2005)]  # lines 1-5, 500-520, 2000-2005
    edits = []
    expected = content
    for i, (lo, hi) in enumerate(spans, 1):
        old = "\n".join(lines[lo:hi])
        new = f"# EDIT {i} BEGIN\n" + old.replace("x" * 70, f"edited_{i}") + f"\n# EDIT {i} END"
        edits.append({"old_string": old, "new_string": new})
        assert expected.count(old) == 1
        expected = expected.replace(old, new)

    result, err = apply_edits_checked(content, edits)
    assert err is None
    assert result == expected
    for i in range(1, 4):
        assert f"# EDIT {i} BEGIN" in result


def test_whole_file_old_string_rejected_by_ratio_guard():
    content = _build_big_file(5000)
    whole = content[: int(len(content) * 0.6)]
    result, err = apply_edits_checked(content, [{"old_string": whole, "new_string": whole + "\n# extra"}])
    assert result is None
    assert "regenerating the whole file" in err


def test_wrong_anchor_fails_loudly_on_big_file():
    content = _build_big_file(5000)
    result, err = apply_edits_checked(
        content, [{"old_string": "zzz_not_in_file", "new_string": "replacement"}]
    )
    assert result is None
    assert "old_string not found" in err


def test_wrong_anchor_returns_closest_line_hints():
    content = "\n".join(f"resource_block_{i} = value_{i}" for i in range(1, 301))
    result, err = apply_edits_checked(
        # Close to line 42 but not an actual match for any replacer stage.
        content, [{"old_string": "resource_block_42 = value_43", "new_string": "changed"}]
    )
    assert result is None
    assert "old_string not found" in err
    assert "Closest lines" in err
    assert "resource_block_42" in err


def test_apply_edits_checked_noop_rejected():
    result, err = apply_edits_checked("alpha\nbeta\n", [{"old_string": "alpha", "new_string": "alpha"}])
    assert result is None
    assert err


def test_apply_edits_checked_empty_result_rejected():
    result, err = apply_edits_checked("only line", [{"old_string": "only line", "new_string": " "}])
    assert result is None
    assert "empty" in err


# ---------------------------------------------------------------------------
# find_in_file
# ---------------------------------------------------------------------------


def test_find_in_content_line_numbers():
    content = "alpha\nbeta\ngamma beta\ndelta"
    matches, total = _find_in_content(content, "beta")
    assert total == 2
    assert matches == ["L2: beta", "L3: gamma beta"]


def test_find_in_content_regex_and_literal_fallback():
    matches, total = _find_in_content("foo(1)\nbar(2)", r"foo\(\d\)")
    assert total == 1
    assert matches[0].startswith("L1:")
    # An invalid regex falls back to literal substring matching.
    matches, total = _find_in_content("a[b\nc", "a[b")
    assert total == 1
    assert matches == ["L1: a[b"]


def test_find_in_content_literal_wins_over_regex_reading():
    # 'arr[0]' is a valid regex that would match 'arr0', never the literal
    # text — literal-first matching must find the actual code.
    matches, total = _find_in_content("x = arr[0]\ny = arr0\n", "arr[0]")
    assert total == 1
    assert matches == ["L1: x = arr[0]"]


def test_find_in_content_caps_matches_and_line_length():
    content = "\n".join("needle " + "x" * 500 for _ in range(500))
    matches, total = _find_in_content(content, "needle")
    assert total == 500
    assert len(matches) == 50
    assert all(len(m) < 230 for m in matches)


# ---------------------------------------------------------------------------
# bitbucket_repos handlers (mocked client)
# ---------------------------------------------------------------------------


class _FakeBBClient:
    def __init__(self, content="hello world\n"):
        self.content = content
        self.read_result = None  # overrides the content envelope when set
        self.write_result = {"success": True, "status": 201}
        self.write_calls = []

    def get_file_contents(self, ws, repo, path, commit="HEAD"):
        if self.read_result is not None:
            return self.read_result
        return {"content": self.content, "path": path, "commit": _SHA}

    def create_or_update_file(self, ws, repo, path, content, message, branch,
                              author=None, parents=None):
        self.write_calls.append({
            "path": path, "content": content, "message": message,
            "branch": branch, "parents": parents,
        })
        return self.write_result


@pytest.fixture()
def fake_bb(monkeypatch):
    client = _FakeBBClient()
    monkeypatch.setattr(repos_tool, "get_bb_client_for_user", lambda uid: client)
    monkeypatch.setattr(repos_tool, "get_default_branch", lambda uid, ws, repo: "main")
    monkeypatch.setattr(repos_tool, "confirm_or_cancel", lambda *a, **k: None)
    return client


def test_get_file_contents_pages_large_files(fake_bb):
    fake_bb.content = _build_big_file(3000)
    out = json.loads(bitbucket_repos(
        action="get_file_contents", workspace="ws", repo_slug="r",
        path="big.py", user_id="u1",
    ))
    assert out["content"].startswith("[lines 1-")
    # Continue hints are pinned to the commit the read resolved to.
    assert f" commit={_SHA} to continue]" in out["content"].split("\n", 1)[0]
    assert len(json.dumps(out)) < PASS_THROUGH_CHARS

    out2 = json.loads(bitbucket_repos(
        action="get_file_contents", workspace="ws", repo_slug="r",
        path="big.py", start_line=2500, user_id="u1",
    ))
    assert out2["content"].startswith("[lines 2500-")


def test_get_file_contents_small_file_unchanged(fake_bb):
    fake_bb.content = "tiny\nfile\n"
    out = json.loads(bitbucket_repos(
        action="get_file_contents", workspace="ws", repo_slug="r",
        path="s.txt", user_id="u1",
    ))
    assert out["content"] == "tiny\nfile\n"


def test_find_in_file_handler(fake_bb):
    fake_bb.content = "\n".join(f"row {i}" for i in range(1, 101))
    out = json.loads(bitbucket_repos(
        action="find_in_file", workspace="ws", repo_slug="r",
        path="f.txt", query="row 42", user_id="u1",
    ))
    assert out["success"] is True
    assert out["total_matches"] == 1
    assert out["matches"] == ["L42: row 42"]
    assert "start_line" in out["hint"]


def test_edit_file_applies_edits_and_passes_read_sha_as_parents(fake_bb):
    fake_bb.content = "aaa\nbbb\nccc\n"
    out = json.loads(bitbucket_repos(
        action="edit_file", workspace="ws", repo_slug="r", path="f.txt",
        branch="feature", message="fix: change bbb",
        edits=[{"old_string": "bbb", "new_string": "BBB"}], user_id="u1",
    ))
    assert out["success"] is True
    assert len(fake_bb.write_calls) == 1
    call = fake_bb.write_calls[0]
    assert call["content"] == "aaa\nBBB\nccc\n"
    assert call["parents"] == _SHA
    assert call["branch"] == "feature"


def test_edit_file_requires_confirmation(fake_bb, monkeypatch):
    cancelled = json.dumps({"success": True, "cancelled": True,
                            "message": "Operation cancelled by user"})
    monkeypatch.setattr(repos_tool, "confirm_or_cancel", lambda *a, **k: cancelled)
    out = json.loads(bitbucket_repos(
        action="edit_file", workspace="ws", repo_slug="r", path="f.txt",
        branch="feature", message="m",
        edits=[{"old_string": "hello", "new_string": "goodbye"}], user_id="u1",
    ))
    assert out.get("cancelled") is True
    assert fake_bb.write_calls == []


def test_edit_file_forwards_read_errors(fake_bb):
    fake_bb.read_result = {"error": True, "status": 403, "message": "forbidden"}
    out = json.loads(bitbucket_repos(
        action="edit_file", workspace="ws", repo_slug="r", path="f.txt",
        branch="feature", message="m",
        edits=[{"old_string": "a", "new_string": "b"}], user_id="u1",
    ))
    assert out["error"] is True
    assert fake_bb.write_calls == []


def test_edit_file_bad_anchor_never_commits(fake_bb):
    fake_bb.content = "aaa\nbbb\n"
    out = json.loads(bitbucket_repos(
        action="edit_file", workspace="ws", repo_slug="r", path="f.txt",
        branch="feature", message="m",
        edits=[{"old_string": "zzz", "new_string": "yyy"}], user_id="u1",
    ))
    assert out["error"] is True
    assert fake_bb.write_calls == []


def test_edit_file_requires_branch_message_and_edits(fake_bb):
    base = dict(action="edit_file", workspace="ws", repo_slug="r",
                path="f.txt", user_id="u1")
    out = json.loads(bitbucket_repos(**base, edits=[{"old_string": "a", "new_string": "b"}]))
    assert out["error"] is True  # missing message
    out = json.loads(bitbucket_repos(**base, message="m"))
    assert out["error"] is True  # missing edits
    # branch must be EXPLICIT for edit_file: the saved/default branch (the
    # fixture resolves 'main') must NOT be auto-filled into a commit.
    out = json.loads(bitbucket_repos(
        **base, message="m", edits=[{"old_string": "a", "new_string": "b"}],
    ))
    assert out["error"] is True
    assert "branch" in out["message"]
    assert fake_bb.write_calls == []


def test_edit_file_fails_closed_when_read_commit_is_not_a_sha(fake_bb):
    # If ref resolution fell back, the envelope echoes the branch name —
    # committing with parents=<branch-name> would 400 or void the CAS.
    fake_bb.read_result = {"content": "aaa\nbbb\n", "path": "f.txt",
                           "commit": "feature"}
    out = json.loads(bitbucket_repos(
        action="edit_file", workspace="ws", repo_slug="r", path="f.txt",
        branch="feature", message="m",
        edits=[{"old_string": "bbb", "new_string": "BBB"}], user_id="u1",
    ))
    assert out["error"] is True
    assert "compare-and-swap" in out["message"]
    assert fake_bb.write_calls == []


# ---------------------------------------------------------------------------
# fix_tool._get_file_content — missing vs failed
# ---------------------------------------------------------------------------


class _StaticClient:
    def __init__(self, result, resolved=_SHA):
        self._result = result
        self._resolved = resolved  # None → resolution fails (ref echoed back)

    def get_file_contents(self, *args, **kwargs):
        return self._result

    def _resolve_commit(self, ws, repo, ref):
        return self._resolved if self._resolved is not None else ref


def test_get_file_content_404_means_missing(monkeypatch):
    monkeypatch.setattr(fix_tool, "get_bb_client_for_user",
                        lambda uid: _StaticClient({"error": True, "status": 404, "message": "not found"}))
    content, err, missing = fix_tool._get_file_content("u", "ws", "r", "f.py", "main")
    assert content is None
    assert err is None
    assert missing is True


def test_get_file_content_404_on_unresolvable_branch_is_hard_error(monkeypatch):
    # /src 404s identically for a missing FILE and a missing BRANCH (typo).
    # Only a resolvable ref may take the new-file path — otherwise a branch
    # typo fabricates a whole-file 'new file' suggestion.
    monkeypatch.setattr(fix_tool, "get_bb_client_for_user",
                        lambda uid: _StaticClient(
                            {"error": True, "status": 404, "message": "not found"},
                            resolved=None))
    content, err, missing = fix_tool._get_file_content("u", "ws", "r", "f.py", "mian")
    assert content is None
    assert err and "mian" in err
    assert missing is False


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_get_file_content_non_404_is_hard_error(monkeypatch, status):
    monkeypatch.setattr(fix_tool, "get_bb_client_for_user",
                        lambda uid: _StaticClient({"error": True, "status": status, "message": "boom"}))
    content, err, missing = fix_tool._get_file_content("u", "ws", "r", "f.py", "main")
    assert content is None
    assert err  # hard error — never the new-file fallback
    assert missing is False


def test_get_file_content_success(monkeypatch):
    monkeypatch.setattr(fix_tool, "get_bb_client_for_user",
                        lambda uid: _StaticClient({"content": "data", "path": "f.py", "commit": _SHA}))
    content, err, missing = fix_tool._get_file_content("u", "ws", "r", "f.py", "main")
    assert content == "data"
    assert err is None
    assert missing is False


def test_get_file_content_directory_listing_is_hard_error(monkeypatch):
    monkeypatch.setattr(fix_tool, "get_bb_client_for_user",
                        lambda uid: _StaticClient({"pagelen": 10, "values": []}))
    content, err, missing = fix_tool._get_file_content("u", "ws", "r", "somedir", "main")
    assert content is None
    assert err
    assert missing is False


# ---------------------------------------------------------------------------
# apply_fix_tool — stale-suggestion guard
# ---------------------------------------------------------------------------


def test_apply_fix_stale_suggestion_is_refused():
    # suggested_content is a whole-file body rendered at RCA time; if the
    # file moved on the base branch since, applying would revert that work.
    client = _StaticClient({"content": "CURRENT", "path": "f.py", "commit": _SHA})
    err = apply_fix_tool._check_suggestion_not_stale(client, "ws", "r", "f.py", "main", "ORIGINAL")
    assert err and "changed" in err
    assert apply_fix_tool._check_suggestion_not_stale(
        client, "ws", "r", "f.py", "main", "CURRENT") is None


def test_apply_fix_new_file_suggestion_refused_if_file_now_exists():
    client = _StaticClient({"content": "CURRENT", "path": "f.py", "commit": _SHA})
    err = apply_fix_tool._check_suggestion_not_stale(client, "ws", "r", "f.py", "main", None)
    assert err and "exists" in err
    missing = _StaticClient({"error": True, "status": 404, "message": "nf"})
    assert apply_fix_tool._check_suggestion_not_stale(
        missing, "ws", "r", "f.py", "main", None) is None


# ---------------------------------------------------------------------------
# api_client.get_file_contents — Content-Type handling
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, text="", json_data=None, content_type="text/plain"):
        self.status_code = 200
        self.text = text
        self._json = json_data
        self.headers = {"Content-Type": content_type}

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def test_json_file_returns_text_envelope(monkeypatch):
    body = '{"name": "cfg", "values": {"nested": 1}}'
    resp = _FakeResponse(text=body, json_data=json.loads(body),
                         content_type="application/json")
    monkeypatch.setattr(bb_api.requests, "get", lambda *a, **k: resp)
    client = bb_api.BitbucketAPIClient("tok")
    result = client.get_file_contents("ws", "r", "cfg.json", commit=_SHA)
    assert result == {"content": body, "path": "cfg.json", "commit": _SHA}


def test_directory_listing_passes_through_parsed(monkeypatch):
    listing = {"pagelen": 10, "values": [{"path": "a.py", "type": "commit_file"}], "page": 1}
    resp = _FakeResponse(text=json.dumps(listing), json_data=listing,
                         content_type="application/json")
    monkeypatch.setattr(bb_api.requests, "get", lambda *a, **k: resp)
    client = bb_api.BitbucketAPIClient("tok")
    result = client.get_file_contents("ws", "r", "somedir", commit=_SHA)
    assert result == listing


def test_pagination_shaped_json_file_is_still_file_text(monkeypatch):
    # A stored API-response fixture has top-level values+pagelen but its
    # entries are not commit_file/commit_directory — it is a FILE, not a
    # directory listing, and must stay readable/editable.
    body = json.dumps({"pagelen": 10, "values": [{"id": 1, "type": "pullrequest"}]})
    resp = _FakeResponse(text=body, json_data=json.loads(body),
                         content_type="application/json")
    monkeypatch.setattr(bb_api.requests, "get", lambda *a, **k: resp)
    client = bb_api.BitbucketAPIClient("tok")
    result = client.get_file_contents("ws", "r", "fixtures/prs.json", commit=_SHA)
    assert result == {"content": body, "path": "fixtures/prs.json", "commit": _SHA}


def test_create_or_update_file_sends_parents(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, data=None, files=None, timeout=None):
        captured.update({"data": data, "files": files})
        return _FakeResponse(text="{}", json_data={}, content_type="application/json")

    monkeypatch.setattr(bb_api.requests, "post", fake_post)
    client = bb_api.BitbucketAPIClient("tok")
    client.create_or_update_file("ws", "r", "f.py", "body", "msg", "feature", parents=_SHA)
    assert captured["data"]["parents"] == _SHA
    client.create_or_update_file("ws", "r", "f.py", "body", "msg", "feature")
    assert "parents" not in captured["data"]


# ---------------------------------------------------------------------------
# Read-only session gate (RCA + PR review)
# ---------------------------------------------------------------------------


# Modules that tests/auth/conftest.py replaces with bare MagicMock()s and
# never cleans up. A MagicMock is not a package, so once tests/auth/ has run,
# importing cloud_tools (which transitively imports weaviate.classes, celery
# tasks, etc.) fails in a full-suite run. Evict the leaked mocks so the real
# (or session-stubbed) modules load; real modules are never MagicMocks, so
# this is a no-op outside the polluted case.
_AUTH_CONFTEST_MOCKED_ROOTS = ("weaviate", "celery", "celery_config", "openai", "anthropic")


def _cloud_tools():
    if "chat.backend.agent.tools.cloud_tools" not in sys.modules:
        for name in list(sys.modules):
            if name.split(".")[0] in _AUTH_CONFTEST_MOCKED_ROOTS and isinstance(sys.modules[name], MagicMock):
                del sys.modules[name]
    return pytest.importorskip("chat.backend.agent.tools.cloud_tools")


def test_edit_file_is_a_gated_write_action():
    ct = _cloud_tools()
    assert "edit_file" in ct._BB_WRITE_ACTIONS
    assert "find_in_file" not in ct._BB_WRITE_ACTIONS
    assert "get_file_contents" not in ct._BB_WRITE_ACTIONS


@pytest.mark.parametrize("reason", ["RCA", "PR review"])
def test_read_only_gate_blocks_edit_file(reason):
    ct = _cloud_tools()
    inner = MagicMock(return_value="should not run")
    gated = ct._bb_read_only_gate(inner, "bitbucket_repos", reason)
    out = json.loads(gated(action="edit_file"))
    assert out["error"] is True
    assert reason in out["message"]
    inner.assert_not_called()


def test_read_only_gate_allows_read_actions():
    ct = _cloud_tools()
    inner = MagicMock(return_value="ok")
    gated = ct._bb_read_only_gate(inner, "bitbucket_repos", "RCA")
    assert gated(action="find_in_file") == "ok"
    assert gated(action="get_file_contents") == "ok"
    assert inner.call_count == 2


# Every action any Bitbucket tool accepts must be consciously classified:
# either a write (hard-gated in read-only sessions) or a known read. A new
# action that lands in a tool's Literal without touching this list fails
# here instead of silently passing the security gate.
_BB_READ_ONLY_ACTIONS = {
    "list_repos", "get_repo", "get_file_contents", "find_in_file",
    "get_directory_tree", "search_code", "list_workspaces", "get_workspace",
    "list_branches", "list_commits", "get_commit", "get_diff", "compare",
    "list_prs", "get_pr", "list_pr_comments", "get_pr_diff", "get_pr_activity",
    "list_issues", "get_issue", "list_issue_comments",
    "list_pipelines", "get_pipeline", "list_pipeline_steps", "get_step_log",
    "get_pipeline_step",
}


def test_every_bitbucket_action_is_classified_read_or_write():
    from typing import get_args

    ct = _cloud_tools()
    from chat.backend.agent.tools.bitbucket.repos_tool import BitbucketReposArgs
    from chat.backend.agent.tools.bitbucket.branches_tool import BitbucketBranchesArgs
    from chat.backend.agent.tools.bitbucket.prs_tool import BitbucketPullRequestsArgs
    from chat.backend.agent.tools.bitbucket.issues_tool import BitbucketIssuesArgs
    from chat.backend.agent.tools.bitbucket.pipelines_tool import BitbucketPipelinesArgs

    for schema in (BitbucketReposArgs, BitbucketBranchesArgs,
                   BitbucketPullRequestsArgs, BitbucketIssuesArgs,
                   BitbucketPipelinesArgs):
        for action in get_args(schema.model_fields["action"].annotation):
            assert (action in ct._BB_WRITE_ACTIONS) != (action in _BB_READ_ONLY_ACTIONS), (
                f"action '{action}' ({schema.__name__}) must be in exactly one of "
                "_BB_WRITE_ACTIONS (cloud_tools) or _BB_READ_ONLY_ACTIONS (this test)"
            )


# ---------------------------------------------------------------------------
# api_client.get_file_contents — charset handling (found by live smoke test:
# Bitbucket serves /src file bytes as text/plain with NO charset; requests
# then decodes as ISO-8859-1 and mojibakes UTF-8 content, which a later
# edit_file write-back would re-encode into permanent corruption)
# ---------------------------------------------------------------------------


class _EncodingAwareResponse:
    """Mimics requests' charset behavior: .text decodes .content with
    .encoding, which requests sets to ISO-8859-1 for text/* responses
    that declare no charset."""

    def __init__(self, content: bytes, content_type: str, encoding: str):
        self.status_code = 200
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.encoding = encoding

    @property
    def text(self):
        return self.content.decode(self.encoding, errors="replace")

    def json(self):
        raise ValueError("not json")


def test_undeclared_charset_defaults_to_utf8(monkeypatch):
    body = "# config — naïve café\n"
    resp = _EncodingAwareResponse(body.encode("utf-8"),
                                  content_type="text/plain",
                                  encoding="ISO-8859-1")  # requests' fallback
    monkeypatch.setattr(bb_api.requests, "get", lambda *a, **k: resp)
    client = bb_api.BitbucketAPIClient("tok")
    result = client.get_file_contents("ws", "r", "cfg.py", commit=_SHA)
    assert result["content"] == body  # not 'â€”' mojibake


def test_declared_charset_is_honored(monkeypatch):
    resp = _EncodingAwareResponse("café\n".encode("iso-8859-1"),
                                  content_type="text/plain; charset=iso-8859-1",
                                  encoding="iso-8859-1")
    monkeypatch.setattr(bb_api.requests, "get", lambda *a, **k: resp)
    client = bb_api.BitbucketAPIClient("tok")
    result = client.get_file_contents("ws", "r", "cfg.py", commit=_SHA)
    assert result["content"] == "café\n"


def test_regex_fallback_timeout_is_cancellable_and_loud(monkeypatch):
    # A catastrophic pattern must surface as _RegexSearchTimeout at the
    # deadline (with the regex module, the engine itself stops mid-match
    # instead of burning an abandoned thread).
    if repos_tool._regex_mod is None:
        pytest.skip("regex module not installed")
    monkeypatch.setattr(repos_tool, "_FIND_REGEX_TIMEOUT_S", 0.2)
    content = "x" * 5000 + "\nno match here"
    with pytest.raises(repos_tool._RegexSearchTimeout, match="timed out"):
        repos_tool._find_in_content(content, r"(x+)+z")
