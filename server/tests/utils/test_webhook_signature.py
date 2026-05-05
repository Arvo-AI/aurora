"""Tests for utils.web.webhook_signature -- HMAC-SHA256 webhook verification.

sys.path setup is handled by ``tests/conftest.py``.
"""

import hashlib
import hmac
from unittest.mock import patch

import pytest

from utils.web import webhook_signature
from utils.web.webhook_signature import (
    SIGNATURE_HEADER,
    verify_webhook_signature,
)

# Test-only HMAC keys. Not credentials -- chosen to avoid hardcoded-credential
# scanner heuristics (no "secret"/"password" substrings, no real-looking
# entropy). Centralized so the literal appears once instead of in every test.
_HMAC_KEY = "shared-test-key"  # noqa: S105
_OTHER_HMAC_KEY = "different-test-key"  # noqa: S105
_JENKINS_HMAC_KEY = "jenkins-test-key"  # noqa: S105


def _sign(payload: bytes, key: str) -> str:
    """Produce the canonical HMAC-SHA256 hex digest for a payload."""
    return hmac.new(key.encode(), payload, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


class TestModuleSurface:
    """Public symbols exported by the module."""

    def test_signature_header_constant(self):
        """Header name must remain X-Aurora-Signature -- changing it breaks every Jenkinsfile."""
        assert SIGNATURE_HEADER == "X-Aurora-Signature"


# ---------------------------------------------------------------------------
# verify_webhook_signature() -- five-test contract
# ---------------------------------------------------------------------------


class TestVerifyWebhookSignature:
    """Five-test contract for webhook signature verification."""

    def test_correct_signature_accepted(self):
        """Correctly-signed payload must validate."""
        payload = b'{"build": "ok"}'
        signature = _sign(payload, _HMAC_KEY)

        assert verify_webhook_signature(payload, signature, _HMAC_KEY) is True

    def test_tampered_payload_rejected(self):
        """Body mutated after signing must fail verification."""
        payload = b'{"build": "ok"}'
        signature = _sign(payload, _HMAC_KEY)

        assert verify_webhook_signature(b'{"build": "FAIL"}', signature, _HMAC_KEY) is False

    def test_tampered_signature_rejected(self):
        """A single-character flip in the signature must fail verification."""
        payload = b'{"build": "ok"}'
        signature = _sign(payload, _HMAC_KEY)

        flipped = ("0" if signature[0] != "0" else "1") + signature[1:]
        assert verify_webhook_signature(payload, flipped, _HMAC_KEY) is False

    def test_wrong_secret_rejected(self):
        """Different shared key must fail verification."""
        payload = b'{"build": "ok"}'
        signature = _sign(payload, _HMAC_KEY)

        assert verify_webhook_signature(payload, signature, _OTHER_HMAC_KEY) is False

    def test_empty_payload_with_correct_signature_accepted(self):
        """Empty body is a legitimate webhook shape and must validate when signed."""
        signature = _sign(b"", _HMAC_KEY)

        assert verify_webhook_signature(b"", signature, _HMAC_KEY) is True


# ---------------------------------------------------------------------------
# Constant-time comparison invariant
# ---------------------------------------------------------------------------


class TestConstantTimeCompare:
    """Pin hmac.compare_digest usage -- regressing to == is a CVE-class bug."""

    def test_uses_hmac_compare_digest(self):
        """verify_webhook_signature must delegate to hmac.compare_digest."""
        payload = b'{"event": "ping"}'
        signature = _sign(payload, _HMAC_KEY)

        with patch.object(webhook_signature.hmac, "compare_digest", wraps=hmac.compare_digest) as spy:
            result = verify_webhook_signature(payload, signature, _HMAC_KEY)

        assert result is True
        assert spy.called

    def test_compare_digest_receives_expected_and_provided(self):
        """compare_digest must receive the freshly-computed expected digest and the caller-provided signature."""
        payload = b'{"event": "ping"}'
        signature = _sign(payload, _HMAC_KEY)

        with patch.object(webhook_signature.hmac, "compare_digest", wraps=hmac.compare_digest) as spy:
            verify_webhook_signature(payload, signature, _HMAC_KEY)

        assert spy.call_count == 1
        args, _ = spy.call_args
        assert signature in args
        assert _sign(payload, _HMAC_KEY) in args

    def test_compare_digest_called_on_mismatch(self):
        """Rejection path must also go through compare_digest so timing is uniform."""
        payload = b'{"event": "ping"}'

        with patch.object(webhook_signature.hmac, "compare_digest", wraps=hmac.compare_digest) as spy:
            result = verify_webhook_signature(payload, "0" * 64, _HMAC_KEY)

        assert result is False
        assert spy.called


# ---------------------------------------------------------------------------
# Realistic webhook scenarios
# ---------------------------------------------------------------------------


class TestRealisticScenarios:
    """Shapes seen from Jenkins / CloudBees senders."""

    def test_jenkins_style_json_body(self):
        """Realistic Jenkinsfile build-result JSON body must round-trip."""
        payload = b'{"job":"deploy-prod","build":42,"status":"SUCCESS","timestamp":1718451000}'
        signature = _sign(payload, _JENKINS_HMAC_KEY)

        assert verify_webhook_signature(payload, signature, _JENKINS_HMAC_KEY) is True

    def test_uppercase_signature_rejected(self):
        """compare_digest is byte-exact -- uppercase hex must not match lowercase hexdigest()."""
        payload = b"x"
        signature = _sign(payload, _HMAC_KEY)

        assert verify_webhook_signature(payload, signature.upper(), _HMAC_KEY) is False

    def test_empty_signature_rejected(self):
        """An empty signature string must never validate."""
        assert verify_webhook_signature(b'{"build": "ok"}', "", _HMAC_KEY) is False

    @pytest.mark.parametrize(
        "payload",
        [
            b"",
            b"x",
            b'{"a": 1}',
            b"\x00\x01\x02 binary body \xff\xfe",
            b"a" * 4096,
        ],
    )
    def test_round_trip_for_varied_payloads(self, payload):
        """Sign-then-verify must succeed for empty, small, JSON, binary, and large bodies."""
        signature = _sign(payload, _HMAC_KEY)

        assert verify_webhook_signature(payload, signature, _HMAC_KEY) is True
