"""Tests for services.change_gating.try_review (zero-connection preview)."""

import json
from unittest.mock import MagicMock

import pytest

from services.change_gating.try_review import (
    PREVIEW_NOTE,
    PublicGitHubReader,
    PublicPRRef,
    TryReviewError,
    build_preview_prompt,
    load_pasted_diff,
    load_public_pr,
    parse_public_pr_url,
    run_preview,
    split_unified_diff,
    validate_pasted_diff,
)

DIFF = """diff --git a/deploy/api.yaml b/deploy/api.yaml
index 111..222 100644
--- a/deploy/api.yaml
+++ b/deploy/api.yaml
@@ -10,7 +10,6 @@ spec:
   replicas: 3
   template:
     spec:
-      readinessProbe:
-        httpGet: {path: /healthz, port: 8080}
       containers:
       - name: api
diff --git a/docs/NOTES.md b/docs/NOTES.md
new file mode 100644
--- /dev/null
+++ b/docs/NOTES.md
@@ -0,0 +1,2 @@
+-- not a header
++++ also not a header
"""


# ---------------------------------------------------------------------------
# URL parsing (the only user input that reaches the network)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/acme/widgets/pull/42", PublicPRRef("acme", "widgets", 42)),
        ("  https://github.com/acme/widgets/pull/42/files  ", PublicPRRef("acme", "widgets", 42)),
        ("https://www.github.com/a-b/c.d_e/pull/7#issuecomment-1", PublicPRRef("a-b", "c.d_e", 7)),
        ("https://github.com/acme/widgets/pull/42?diff=split", PublicPRRef("acme", "widgets", 42)),
    ],
)
def test_parse_accepts_public_pr_urls(url, expected):
    assert parse_public_pr_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://github.com/acme/widgets/pull/1",  # not https
        "https://github.com.evil.com/acme/widgets/pull/1",
        "https://github.com@evil.com/acme/widgets/pull/1",
        "https://evil.com/github.com/acme/widgets/pull/1",
        "https://github.example.com/acme/widgets/pull/1",  # GHES host
        "https://github.com/acme/widgets/issues/1",
        "https://github.com/acme/widgets/pull/abc",
        "https://github.com/acme/widgets/pull/1234567890",  # too many digits
        "https://github.com/../widgets/pull/1",
        "https://github.com/acme/../pull/1",
        "https://github.com/acme/wid gets/pull/1",
        "https://api.github.com/repos/acme/widgets/pulls/1",
        "file:///etc/passwd",
        "https://169.254.169.254/latest/meta-data",
    ],
)
def test_parse_rejects_everything_else(url):
    with pytest.raises(TryReviewError) as exc:
        parse_public_pr_url(url)
    assert exc.value.code == "invalid_url"


# ---------------------------------------------------------------------------
# Public reader: unauthenticated, no redirects, friendly errors
# ---------------------------------------------------------------------------


def _resp(status=200, json_data=None, text="", headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data
    r.text = text
    r.headers = headers or {}
    return r


def _reader(responses):
    session = MagicMock()
    session.get.side_effect = responses
    return PublicGitHubReader(PublicPRRef("acme", "widgets", 42), session=session), session


def test_reader_sends_no_credentials_and_never_follows_redirects():
    reader, session = _reader([_resp(json_data={"title": "t"})])
    reader.get_pull_request()
    _, kwargs = session.get.call_args
    assert "Authorization" not in kwargs["headers"]
    assert kwargs["allow_redirects"] is False
    assert session.get.call_args[0][0] == "https://api.github.com/repos/acme/widgets/pulls/42"


def test_reader_records_every_request_for_transparency():
    reader, _ = _reader([_resp(json_data={}), _resp(json_data=[])])
    reader.get_pull_request()
    reader.list_files()
    assert reader.requests_made == [
        "GET /repos/acme/widgets/pulls/42",
        "GET /repos/acme/widgets/pulls/42/files",
    ]


@pytest.mark.parametrize(
    "response,code",
    [
        (_resp(404), "not_found_or_private"),
        (_resp(403, headers={"X-RateLimit-Remaining": "0"}), "rate_limited"),
        (_resp(403, text="API rate limit exceeded"), "rate_limited"),
        (_resp(429), "rate_limited"),
        (_resp(301), "moved"),
        (_resp(307), "moved"),
        (_resp(500), "github_error"),
    ],
)
def test_reader_maps_failures_to_actionable_codes(response, code):
    reader, _ = _reader([response])
    with pytest.raises(TryReviewError) as exc:
        reader.get_pull_request()
    assert exc.value.code == code


def test_private_repo_error_points_at_the_paste_fallback():
    reader, _ = _reader([_resp(404)])
    with pytest.raises(TryReviewError) as exc:
        reader.get_pull_request()
    assert "paste its diff" in exc.value.message.lower()


def test_reader_paginates_files_until_short_page():
    page1 = [{"filename": f"f{i}"} for i in range(100)]
    page2 = [{"filename": "last"}]
    reader, session = _reader([_resp(json_data=page1), _resp(json_data=page2)])
    assert len(reader.list_files()) == 101
    assert session.get.call_count == 2


def test_oversized_diff_degrades_to_none_but_404_still_raises():
    reader, _ = _reader([_resp(406)])
    assert reader.get_diff() is None
    reader, _ = _reader([_resp(404)])
    with pytest.raises(TryReviewError):
        reader.get_diff()


def test_load_public_pr_builds_context_and_skips_diff_when_patches_exist():
    reader, session = _reader(
        [
            _resp(json_data={"title": "Drop probes", "state": "closed", "merged": True,
                             "head": {"sha": "abc123"}}),
            _resp(json_data=[{"filename": "deploy/api.yaml", "patch": "@@ -1 +1 @@\n-a\n+b"}]),
        ]
    )
    ctx = load_public_pr("https://github.com/acme/widgets/pull/42", reader=reader)
    assert ctx.source["merged"] is True and ctx.source["head_sha"] == "abc123"
    assert ctx.diff is None  # per-file patches were enough
    assert session.get.call_count == 2
    assert ctx.transparency["read"] == [
        "GET /repos/acme/widgets/pulls/42",
        "GET /repos/acme/widgets/pulls/42/files",
    ]


# ---------------------------------------------------------------------------
# Pasted diff
# ---------------------------------------------------------------------------


def test_split_unified_diff_extracts_files_status_and_hunk_aware_counts():
    files = split_unified_diff(DIFF)
    assert [f["filename"] for f in files] == ["deploy/api.yaml", "docs/NOTES.md"]
    api, notes = files
    assert api["status"] == "modified" and (api["additions"], api["deletions"]) == (0, 2)
    # "-- not a header" / "++++ also..." are CONTENT lines, not headers.
    assert notes["status"] == "added" and (notes["additions"], notes["deletions"]) == (2, 0)
    assert api["patch"].startswith("@@")


def test_validate_pasted_diff_rejects_non_diffs_and_oversized_input():
    with pytest.raises(TryReviewError) as exc:
        validate_pasted_diff("just some prose")
    assert exc.value.code == "not_a_diff"
    with pytest.raises(TryReviewError) as exc:
        validate_pasted_diff("diff --git a/x b/x\n" + "x" * 400_000)
    assert exc.value.code == "diff_too_large"
    assert validate_pasted_diff(DIFF) == DIFF


def test_pasted_diff_transparency_claims_nothing_was_fetched():
    ctx = load_pasted_diff(DIFF)
    assert "Only the text you pasted" in ctx.transparency["read"][0]
    assert ctx.source == {"kind": "diff", "files": 2}


# ---------------------------------------------------------------------------
# Prompt + orchestration
# ---------------------------------------------------------------------------


def test_preview_prompt_leads_with_the_no_live_context_note():
    ctx = load_pasted_diff(DIFF)
    prompt = build_preview_prompt(ctx.repo_label, ctx.pr, ctx.files, ctx.diff)
    assert prompt.startswith(PREVIEW_NOTE)
    assert "Never invent alerts" in prompt
    assert "deploy/api.yaml" in prompt  # the production builder still ran


def _verdict(**kw):
    base = {
        "verdict": "RISKY",
        "summary": "Removes the readiness probe.",
        "findings": [
            {
                "severity": "HIGH",
                "file_path": "deploy/api.yaml",
                "line": 13,
                "title": "Readiness probe removed",
                "explanation": "Traffic is routed to pods before they can serve.",
            }
        ],
    }
    base.update(kw)
    return json.dumps(base)


def test_run_preview_returns_findings_with_in_diff_flag_and_markdown():
    ctx = load_pasted_diff(DIFF)
    seen = {}

    def agent(prompt, rail):
        seen["prompt"], seen["rail"] = prompt, rail
        return _verdict()

    result = run_preview(ctx, agent)
    assert result["verdict"] == "RISKY"
    assert result["findings"][0]["title"] == "Readiness probe removed"
    assert result["findings"][0]["in_diff"] is True
    assert "**Verdict: RISKY**" in result["markdown"]
    assert "Diff-only review" in result["scope_note"]
    assert seen["rail"].startswith("Pasted diff")  # rails see the author-controlled text


def test_run_preview_flags_findings_that_point_outside_the_diff():
    ctx = load_pasted_diff(DIFF)
    bad = json.loads(_verdict())
    bad["findings"][0]["line"] = 999
    result = run_preview(ctx, lambda p, r: json.dumps(bad))
    assert result["findings"][0]["in_diff"] is False


def test_run_preview_normalises_like_production():
    ctx = load_pasted_diff(DIFF)
    # SAFE never carries findings.
    safe = run_preview(ctx, lambda p, r: _verdict(verdict="SAFE"))
    assert safe["verdict"] == "SAFE" and safe["findings"] == []
    # RISKY with nothing actionable is demoted to SAFE.
    demoted = run_preview(ctx, lambda p, r: _verdict(findings=[]))
    assert demoted["verdict"] == "SAFE"


def test_run_preview_surfaces_agent_and_parse_failures():
    ctx = load_pasted_diff(DIFF)
    with pytest.raises(TryReviewError) as exc:
        run_preview(ctx, lambda p, r: None)
    assert exc.value.code == "agent_failed"
