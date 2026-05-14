"""Tests for the Part 3 live-review surface of the GitHub adapter.

`post_verdict` and `dismiss_prior` hit two REST endpoints that, in
production, would post a customer-visible review. The tests below
mock the HTTP layer and verify the wire shape: GitHub Reviews API
payload, multi-line comment encoding, error-tolerance on
already-dismissed reviews, and the safe-default guard on missing
``installation_id`` / ``repo``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from services.change_intercept.adapters import github as adapter_module
from services.change_intercept.adapters.base import (
    NormalizedChangeEvent,
    PostedVerdict,
)
from services.change_intercept.adapters.github import (
    GitHubChangeAdapter,
    GitHubFetchError,
)


def _event(**overrides: Any) -> NormalizedChangeEvent:
    base: dict[str, Any] = {
        "vendor": "github",
        "kind": "code_change",
        "org_id": "org-1",
        "installation_id": 99999,
        "external_id": "42",
        "dedup_key": "github:acme/widgets:42",
        "repo": "acme/widgets",
        "ref": "feat/foo",
        "base_ref": "main",
        "commit_sha": "deadbeef",
        "actor": "alice",
        "target_env": "prod",
        "action": "opened",
    }
    base.update(overrides)
    return NormalizedChangeEvent(**base)


def _investigation(
    *,
    verdict_event: str = "REQUEST_CHANGES",
    body: str = "summary text",
    inline_comments: list[dict[str, Any]] | None = None,
    commit_sha: str | None = "deadbeef",
) -> dict[str, Any]:
    return {
        "verdict_event": verdict_event,
        "body": body,
        "inline_comments": inline_comments
        if inline_comments is not None
        else [
            {
                "path": "foo.py",
                "start_line": 11,
                "end_line": None,
                "body": "[HIGH] Missing timeout — see line 11",
            }
        ],
        "commit_sha": commit_sha,
    }


# ─── post_verdict happy path ─────────────────────────────────────────


def test_post_verdict_builds_reviews_api_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, installation_id: int, *, json_body: dict[str, Any], **_kw: Any) -> Any:
        captured["url"] = url
        captured["installation_id"] = installation_id
        captured["payload"] = json_body
        response = MagicMock()
        response.json.return_value = {"id": 555}
        return response

    monkeypatch.setattr(adapter_module, "_post", fake_post)
    monkeypatch.setattr(adapter_module, "_paginated_get", lambda *_a, **_kw: [])

    result = GitHubChangeAdapter().post_verdict(_event(), _investigation())

    assert isinstance(result, PostedVerdict)
    assert result.verdict_id == "555"
    assert captured["url"].endswith("/repos/acme/widgets/pulls/42/reviews")
    payload = captured["payload"]
    assert payload["event"] == "REQUEST_CHANGES"
    assert payload["body"] == "summary text"
    assert payload["commit_id"] == "deadbeef"
    assert len(payload["comments"]) == 1
    assert payload["comments"][0]["path"] == "foo.py"
    assert payload["comments"][0]["line"] == 11
    assert payload["comments"][0]["side"] == "RIGHT"


def test_post_verdict_encodes_multiline_range_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_post(*_a: Any, json_body: dict[str, Any], **_kw: Any) -> Any:
        captured["payload"] = json_body
        response = MagicMock()
        response.json.return_value = {"id": 1}
        return response

    monkeypatch.setattr(adapter_module, "_post", fake_post)
    monkeypatch.setattr(adapter_module, "_paginated_get", lambda *_a, **_kw: [])

    investigation = _investigation(
        inline_comments=[
            {
                "path": "foo.py",
                "start_line": 10,
                "end_line": 15,
                "body": "multi-line note",
            }
        ]
    )
    GitHubChangeAdapter().post_verdict(_event(), investigation)
    comment = captured["payload"]["comments"][0]
    assert comment["start_line"] == 10
    assert comment["line"] == 15
    assert comment["start_side"] == "RIGHT"
    assert comment["side"] == "RIGHT"


def test_post_verdict_fetches_inline_comment_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        adapter_module,
        "_post",
        lambda *_a, **_kw: _resp({"id": 555}),
    )
    monkeypatch.setattr(
        adapter_module,
        "_paginated_get",
        lambda *_a, **_kw: [{"id": 10001}, {"id": 10002}],
    )
    result = GitHubChangeAdapter().post_verdict(_event(), _investigation())
    assert result.inline_comment_ids == ["10001", "10002"]


def test_post_verdict_omits_comments_when_inline_list_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_post(*_a: Any, json_body: dict[str, Any], **_kw: Any) -> Any:
        captured["payload"] = json_body
        return _resp({"id": 1})

    monkeypatch.setattr(adapter_module, "_post", fake_post)
    monkeypatch.setattr(adapter_module, "_paginated_get", lambda *_a, **_kw: [])

    GitHubChangeAdapter().post_verdict(
        _event(),
        _investigation(verdict_event="APPROVE", inline_comments=[]),
    )
    assert "comments" not in captured["payload"]
    assert captured["payload"]["event"] == "APPROVE"


def test_post_verdict_drops_malformed_inline_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_post(*_a: Any, json_body: dict[str, Any], **_kw: Any) -> Any:
        captured["payload"] = json_body
        return _resp({"id": 1})

    monkeypatch.setattr(adapter_module, "_post", fake_post)
    monkeypatch.setattr(adapter_module, "_paginated_get", lambda *_a, **_kw: [])

    investigation = _investigation(
        inline_comments=[
            {"path": "", "start_line": 11, "body": "x"},
            {"path": "foo.py", "start_line": 11, "body": "valid"},
        ]
    )
    GitHubChangeAdapter().post_verdict(_event(), investigation)
    assert len(captured["payload"]["comments"]) == 1
    assert captured["payload"]["comments"][0]["body"] == "valid"


def test_post_verdict_succeeds_without_commit_sha_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_post(*_a: Any, json_body: dict[str, Any], **_kw: Any) -> Any:
        captured["payload"] = json_body
        return _resp({"id": 1})

    monkeypatch.setattr(adapter_module, "_post", fake_post)
    monkeypatch.setattr(adapter_module, "_paginated_get", lambda *_a, **_kw: [])

    investigation = _investigation(commit_sha=None)
    GitHubChangeAdapter().post_verdict(_event(commit_sha=None), investigation)
    assert "commit_id" not in captured["payload"]


# ─── post_verdict error paths ────────────────────────────────────────


def test_post_verdict_raises_when_repo_missing() -> None:
    with pytest.raises(GitHubFetchError):
        GitHubChangeAdapter().post_verdict(_event(repo=None), _investigation())


def test_post_verdict_raises_when_installation_missing() -> None:
    with pytest.raises(GitHubFetchError):
        GitHubChangeAdapter().post_verdict(
            _event(installation_id=None), _investigation()
        )


def test_post_verdict_raises_when_pr_id_non_numeric() -> None:
    with pytest.raises(GitHubFetchError):
        GitHubChangeAdapter().post_verdict(
            _event(external_id="not-a-number"), _investigation()
        )


def test_post_verdict_raises_when_response_missing_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter_module, "_post", lambda *_a, **_kw: _resp({}))
    with pytest.raises(GitHubFetchError):
        GitHubChangeAdapter().post_verdict(_event(), _investigation())


def test_post_verdict_survives_comment_id_fetch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise GitHubFetchError("comments endpoint down")

    monkeypatch.setattr(adapter_module, "_post", lambda *_a, **_kw: _resp({"id": 7}))
    monkeypatch.setattr(adapter_module, "_paginated_get", boom)

    result = GitHubChangeAdapter().post_verdict(_event(), _investigation())
    assert result.verdict_id == "7"
    assert result.inline_comment_ids == []


# ─── dismiss_prior paths ────────────────────────────────────────────


def test_dismiss_prior_calls_dismissals_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_put(url: str, installation_id: int, *, json_body: dict[str, Any], **_kw: Any) -> Any:
        captured["url"] = url
        captured["payload"] = json_body
        return MagicMock(status_code=200)

    monkeypatch.setattr(adapter_module, "_put", fake_put)

    GitHubChangeAdapter().dismiss_prior(
        _event(), PostedVerdict(verdict_id="555")
    )
    assert captured["url"].endswith("/pulls/42/reviews/555/dismissals")
    assert captured["payload"]["event"] == "DISMISS"


def test_dismiss_prior_silently_ignores_already_dismissed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """422 / 404 are benign — the prior review is already dismissed
    or the PR was merged. Either case must not raise."""

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise GitHubFetchError(
            "status=422 review already dismissed", status_code=422
        )

    monkeypatch.setattr(adapter_module, "_put", boom)
    GitHubChangeAdapter().dismiss_prior(_event(), PostedVerdict(verdict_id="555"))


def test_dismiss_prior_propagates_real_outages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5xx / auth / network failures MUST propagate so the live-posting
    path doesn't stack a new REQUEST_CHANGES on top of a stale one
    when GitHub is genuinely down."""

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise GitHubFetchError("status=503 upstream", status_code=503)

    monkeypatch.setattr(adapter_module, "_put", boom)
    with pytest.raises(GitHubFetchError):
        GitHubChangeAdapter().dismiss_prior(_event(), PostedVerdict(verdict_id="555"))


def test_dismiss_prior_noops_on_missing_install() -> None:
    GitHubChangeAdapter().dismiss_prior(
        _event(installation_id=None), PostedVerdict(verdict_id="555")
    )  # no exception


def test_dismiss_prior_noops_on_empty_verdict_id() -> None:
    GitHubChangeAdapter().dismiss_prior(_event(), PostedVerdict(verdict_id=""))


# ─── helpers ─────────────────────────────────────────────────────────


def _resp(payload: dict[str, Any]) -> Any:
    """Build a minimal requests-Response-shaped mock."""
    response = MagicMock()
    response.json.return_value = payload
    response.status_code = 200
    response.text = ""
    return response
