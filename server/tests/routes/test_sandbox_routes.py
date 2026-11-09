"""Sandbox I/O Validation

DB/RLS behaviour is covered by the shared RLS policy loop in db_utils.

These tests pin the trust-boundary checks that run before anything is stored.
"""

import io
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def sr(monkeypatch):
    pytest.importorskip("flask")
    for heavy in ("celery_config", "celery"):
        if heavy not in sys.modules:
            monkeypatch.setitem(sys.modules, heavy, MagicMock())
    from routes import sandbox_routes
    return sandbox_routes


@pytest.fixture
def app():
    from flask import Flask
    return Flask(__name__)  # NOSONAR


def _json(app, sr, body):
    with app.test_request_context(json=body):
        parsed, err = sr._read_json_artifact()
        return parsed, (err[1] if err else None)


def _upload(app, sr, data: bytes, filename: str, mimetype: str):
    with app.test_request_context(
        data={"file": (io.BytesIO(data), filename, mimetype)},
        content_type="multipart/form-data",
    ):
        parsed, err = sr._read_upload()
        return parsed, (err[1] if err else None)


def test_pasted_log_is_stored_as_log(app, sr):
    parsed, status = _json(app, sr, {"kind": "log", "content": "ERROR db timeout\n"})
    assert status is None
    assert parsed[0] == "log" and parsed[2] == "ERROR db timeout"


@pytest.mark.parametrize("body", [
    {"kind": "script", "content": "x"},           # unknown kind
    {"kind": "log", "content": "   "},             # empty
    {"kind": "link", "content": "javascript:alert(1)"},
    {"kind": "link", "content": "https://a.com https://b.com"},  # not a single URL
])
def test_bad_json_artifacts_are_refused(app, sr, body):
    assert _json(app, sr, body)[1] == 400


def test_oversized_paste_is_refused(app, sr):
    assert _json(app, sr, {"kind": "text", "content": "x" * (sr.MAX_TEXT_CHARS + 1)})[1] == 400


def test_nul_bytes_are_stripped(app, sr):
    parsed, _ = _json(app, sr, {"kind": "text", "content": "a\x00b"})
    assert parsed[2] == "ab"


def test_png_upload_is_an_image_with_bytes_kept(app, sr):
    parsed, status = _upload(app, sr, b"\x89PNG...", "shot.png", "image/png")
    assert status is None
    kind, _, content, mime, raw = parsed
    assert (kind, content, mime, raw) == ("image", None, "image/png", b"\x89PNG...")


def test_svg_is_refused(app, sr):
    # SVG can carry script and image bytes are served back to the browser.
    assert _upload(app, sr, b"<svg/>", "x.svg", "image/svg+xml")[1] == 400


def test_log_file_becomes_log_and_markdown_becomes_document(app, sr):
    assert _upload(app, sr, b"ERROR x", "app.log", "text/plain")[0][0] == "log"
    assert _upload(app, sr, b"# PM", "pm.md", "text/markdown")[0][0] == "document"


def test_unknown_extension_and_oversized_file_are_refused(app, sr):
    assert _upload(app, sr, b"MZ", "evil.exe", "application/octet-stream")[1] == 400
    assert _upload(app, sr, b"x" * (sr.MAX_FILE_BYTES + 1), "big.txt", "text/plain")[1] == 400


def test_parse_incident_time():
    from utils.incidents import parse_incident_time
    assert parse_incident_time("") == (None, None)
    value, err = parse_incident_time("2026-06-19T09:26:00Z")
    assert err is None and value == datetime(2026, 6, 19, 9, 26) and value.tzinfo is None
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert parse_incident_time(future)[1] is not None
    assert parse_incident_time("yesterday")[1] is not None


@pytest.mark.parametrize("repo", ["../etc", "a/b/c", "", "owner", "a/b?x=1"])
def test_check_public_repo_rejects_malformed_without_network(repo, monkeypatch):
    from chat.backend.agent.tools import github_rca_tool
    monkeypatch.setattr(github_rca_tool.requests, "get", MagicMock(side_effect=AssertionError("no network")))
    assert github_rca_tool.check_public_repo(repo) is None
