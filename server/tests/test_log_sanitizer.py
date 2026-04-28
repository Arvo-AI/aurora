"""Tests for utils.log_sanitizer -- log injection prevention utilities."""

import os
import sys

import pytest

# Ensure server/ is on sys.path
_server_dir = os.path.join(os.path.dirname(__file__), os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from utils.log_sanitizer import sanitize, safe_provider, hash_for_log


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def secret_key(monkeypatch):
    """Set FLASK_SECRET_KEY and reset the lru_cache around each test."""
    from utils.log_sanitizer import _get_log_hash_salt

    def _set(value="test-key"):  # noqa: S107
        _get_log_hash_salt.cache_clear()
        monkeypatch.setenv("FLASK_SECRET_KEY", value)
        return value

    yield _set
    _get_log_hash_salt.cache_clear()


@pytest.fixture()
def no_secret_key(monkeypatch):
    """Remove FLASK_SECRET_KEY and reset the lru_cache around each test."""
    from utils.log_sanitizer import _get_log_hash_salt

    _get_log_hash_salt.cache_clear()
    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
    yield
    _get_log_hash_salt.cache_clear()


# ---------------------------------------------------------------------------
# sanitize()
# ---------------------------------------------------------------------------


class TestSanitize:
    """Tests for the sanitize() function that strips control characters."""

    def test_normal_string_unchanged(self):
        """Ordinary ASCII text must pass through unmodified."""
        assert sanitize("hello world") == "hello world"

    def test_empty_string(self):
        """Empty input must return an empty string."""
        assert sanitize("") == ""

    def test_strips_newline(self):
        """Newlines can be used for log injection -- must be stripped."""
        assert sanitize("line1\nline2") == "line1line2"

    def test_strips_carriage_return(self):
        """Carriage returns must be removed to prevent log line forgery."""
        assert sanitize("line1\rline2") == "line1line2"

    def test_strips_tab(self):
        """Tab characters must be stripped from output."""
        assert sanitize("col1\tcol2") == "col1col2"

    def test_strips_null_byte(self):
        """Null bytes must be removed to prevent truncation attacks."""
        assert sanitize("before\x00after") == "beforeafter"

    def test_strips_unicode_line_separator(self):
        """U+2028 LINE SEPARATOR can forge new log lines in Unicode parsers."""
        assert sanitize("before\u2028after") == "beforeafter"

    def test_strips_unicode_paragraph_separator(self):
        """U+2029 PARAGRAPH SEPARATOR -- same risk as line separator."""
        assert sanitize("before\u2029after") == "beforeafter"

    def test_strips_zero_width_space(self):
        """U+200B ZERO WIDTH SPACE can hide content in logs."""
        assert sanitize("be\u200bfore") == "before"

    def test_strips_zero_width_no_break_space(self):
        """U+FEFF BOM / ZERO WIDTH NO-BREAK SPACE must be removed."""
        assert sanitize("\ufeffhello") == "hello"

    def test_strips_zero_width_non_joiner(self):
        """U+200C ZERO WIDTH NON-JOINER must be stripped."""
        assert sanitize("be\u200cfore") == "before"

    def test_strips_zero_width_joiner(self):
        """U+200D ZERO WIDTH JOINER must be stripped."""
        assert sanitize("be\u200dfore") == "before"

    def test_strips_left_to_right_mark(self):
        """U+200E LEFT-TO-RIGHT MARK must be stripped."""
        assert sanitize("hello\u200eworld") == "helloworld"

    def test_strips_right_to_left_mark(self):
        """U+200F RIGHT-TO-LEFT MARK must be stripped."""
        assert sanitize("hello\u200fworld") == "helloworld"

    def test_strips_word_joiner(self):
        """U+2060 WORD JOINER must be stripped."""
        assert sanitize("be\u2060fore") == "before"

    def test_strips_multiple_control_chars(self):
        """Multiple different control characters in one string are all removed."""
        malicious = "admin\n[INFO] Access granted\r\n"
        result = sanitize(malicious)
        assert "\n" not in result
        assert "\r" not in result

    def test_preserves_unicode_letters(self):
        """Normal Unicode letters (accents, etc.) must pass through."""
        assert sanitize("cafe resume") == "cafe resume"

    def test_non_string_input_converted(self):
        """Non-string values are converted via str() before sanitization."""
        assert sanitize(42) == "42"
        assert sanitize(None) == "None"

    def test_log_injection_attack(self):
        """Simulates a classic log injection attack via user_id field."""
        attack = "user123\n[ADMIN] Token revoked for all users"
        result = sanitize(attack)
        assert "[ADMIN]" in result
        assert "\n" not in result


# ---------------------------------------------------------------------------
# safe_provider()
# ---------------------------------------------------------------------------


class TestSafeProvider:
    """Tests for the safe_provider() allowlist function."""

    def test_known_provider_returned(self):
        """Known provider names must be returned as-is."""
        assert safe_provider("aws") == "aws"
        assert safe_provider("gcp") == "gcp"
        assert safe_provider("datadog") == "datadog"

    def test_case_insensitive(self):
        """Provider lookup must be case-insensitive."""
        assert safe_provider("AWS") == "aws"
        assert safe_provider("Datadog") == "datadog"

    def test_unknown_provider_returns_sentinel(self):
        """Unrecognized provider names must return the unknown sentinel."""
        assert safe_provider("evil_provider") == "unknown"
        assert safe_provider("anything_else") == "unknown"

    def test_empty_string(self):
        """Empty string input must return the unknown sentinel."""
        assert safe_provider("") == "unknown"

    def test_none_value(self):
        """None input must return the unknown sentinel."""
        assert safe_provider(None) == "unknown"

    def test_strips_control_chars_before_lookup(self):
        """Provider name with injected control chars should still match."""
        assert safe_provider("aws\n") == "aws"
        assert safe_provider("\tgcp") == "gcp"

    def test_strips_whitespace(self):
        """Leading and trailing whitespace must be stripped before lookup."""
        assert safe_provider("  aws  ") == "aws"

    def test_injection_in_provider_name(self):
        """Attacker tries to inject via provider field -- always gets unknown."""
        assert safe_provider("aws\n[CRITICAL] System compromised") == "unknown"

    def test_coroot_provider(self):
        """Coroot is a known connector directory in Aurora."""
        assert safe_provider("coroot") == "coroot"

    def test_kubernetes_auxiliary_provider(self):
        """kubectl is an auxiliary provider in KNOWN_PROVIDERS."""
        assert safe_provider("kubectl") == "kubectl"


# ---------------------------------------------------------------------------
# hash_for_log()
# ---------------------------------------------------------------------------


class TestHashForLog:
    """Tests for the HMAC-based log fingerprinting function."""

    def test_none_returns_dash(self):
        """None input must return the dash sentinel."""
        assert hash_for_log(None) == "-"

    def test_empty_string_returns_dash(self):
        """Empty string input must return the dash sentinel."""
        assert hash_for_log("") == "-"

    def test_without_secret_key_returns_sentinel(self, no_secret_key):
        """Without FLASK_SECRET_KEY, must degrade to '?' -- never crash."""
        assert hash_for_log("user123") == "?"

    def test_with_secret_key_returns_hex(self, secret_key):
        """With FLASK_SECRET_KEY set, returns a 12-char hex fingerprint."""
        secret_key("test-secret-key")
        result = hash_for_log("user123")
        assert result not in {"?", "-"}
        assert len(result) == 12
        assert all(c in "0123456789abcdef" for c in result)

    def test_same_input_same_hash(self, secret_key):
        """Deterministic: same input always produces the same hash."""
        secret_key()
        h1 = hash_for_log("user-abc")
        h2 = hash_for_log("user-abc")
        assert h1 == h2

    def test_different_inputs_different_hashes(self, secret_key):
        """Different inputs must produce different fingerprints."""
        secret_key()
        h1 = hash_for_log("user-A")
        h2 = hash_for_log("user-B")
        assert h1 != h2

    def test_custom_length(self, secret_key):
        """Custom length parameter must control output hex string length."""
        secret_key()
        result = hash_for_log("user123", length=8)
        assert len(result) == 8

    def test_non_string_input(self, secret_key):
        """Numeric user IDs should be handled without error."""
        secret_key()
        result = hash_for_log(12345)
        assert result not in {"?", "-"}
