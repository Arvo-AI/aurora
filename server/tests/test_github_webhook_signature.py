"""Unit tests for the GitHub webhook signature validator.

Verifies the security contract of :func:`verify_webhook_signature`:

* Valid signature → ``True``.
* Tampered body / wrong secret → ``False`` (well-formed but mismatching).
* Missing or malformed header → :class:`GitHubWebhookError` (caller responds 401).
* Comparison MUST go through :func:`hmac.compare_digest` — verified with a
  spy — so no timing oracle is exposed.
* The validator stays replay-agnostic: re-presenting the same body+sig+secret
  still returns ``True``. Replay deduplication is a route-level concern (the
  ``X-GitHub-Delivery`` UNIQUE constraint), NOT a validator concern.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import patch

import pytest

from utils.auth import github_webhook
from utils.auth.github_webhook import (
    GitHubWebhookError,
    verify_webhook_signature,
)


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_accepted(webhook_secret: str) -> None:
    body = b'{"action":"opened","number":42}'
    signature = _sign(body, webhook_secret)

    assert verify_webhook_signature(body, signature, webhook_secret) is True


def test_tampered_body_rejected(webhook_secret: str) -> None:
    original = b'{"action":"opened","number":42}'
    tampered = b'{"action":"opened","number":99}'
    signature = _sign(original, webhook_secret)

    assert verify_webhook_signature(tampered, signature, webhook_secret) is False


def test_wrong_secret_rejected(webhook_secret: str) -> None:
    body = b'{"action":"closed"}'
    signature = _sign(body, webhook_secret)

    assert verify_webhook_signature(body, signature, "different-secret") is False


def test_missing_signature_raises(webhook_secret: str) -> None:
    body = b'{"action":"opened"}'

    with pytest.raises(GitHubWebhookError) as exc_info:
        verify_webhook_signature(body, "", webhook_secret)

    assert "X-Hub-Signature-256" in str(exc_info.value)


def test_constant_time_comparison_used(webhook_secret: str) -> None:
    """Spy on :func:`hmac.compare_digest` and assert the validator uses it.

    A naive ``==`` comparison would leak signature bytes via timing — this
    test pins the constant-time guarantee against silent regressions.
    """
    body = b'{"action":"opened"}'
    signature = _sign(body, webhook_secret)

    real_compare = hmac.compare_digest
    with patch.object(
        github_webhook.hmac, "compare_digest", wraps=real_compare
    ) as spy:
        result = verify_webhook_signature(body, signature, webhook_secret)

    assert result is True
    assert spy.call_count == 1


def test_replay_with_same_delivery_uuid_handled_at_route_level(
    webhook_secret: str,
) -> None:
    """The validator MUST stay replay-agnostic.

    Replay protection lives at the route layer (``webhook_deliveries``
    UNIQUE on ``delivery_id``). The signature validator returns ``True``
    for repeated identical (body, sig, secret) inputs — that is the
    correct behaviour. Documenting the contract here keeps a future
    refactor from accidentally adding stateful "have we seen this body"
    bookkeeping into a pure crypto helper.
    """
    body = b'{"action":"opened","delivery":"uuid-abc-123"}'
    signature = _sign(body, webhook_secret)

    first = verify_webhook_signature(body, signature, webhook_secret)
    second = verify_webhook_signature(body, signature, webhook_secret)
    third = verify_webhook_signature(body, signature, webhook_secret)

    assert first is second is third is True
