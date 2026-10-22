"""Retry classification for LLM calls (utils.llm_errors).

Two predicates, two consumers: ``is_retryable_llm_error`` gates the Celery
retries of the summarization tasks; ``is_mid_stream_transport_error`` gates the
agent's full-graph restart, which re-bills every previous turn and must
therefore ignore everything the SDK already retried.
"""

import http.client

import pytest

from utils.llm_errors import is_mid_stream_transport_error, is_retryable_llm_error

try:  # the real botocore error when installed; a faithful stand-in otherwise (CI minimal env)
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - exercised only in the minimal CI environment

    class ClientError(Exception):  # type: ignore[no-redef]
        def __init__(self, error_response, operation_name):
            self.response = error_response
            self.operation_name = operation_name
            code = error_response.get("Error", {}).get("Code", "Unknown")
            super().__init__(f"An error occurred ({code}) when calling the {operation_name} operation")


def _client_error(code: str, status: int = 400):
    return ClientError(
        {"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "Converse",
    )


class _FakeStatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class RemoteProtocolError(Exception):
    """Same class name as httpx's; the predicate matches by name on purpose."""


class ReadError(Exception):
    """httpx.ReadError stand-in."""


class ReadTimeoutError(Exception):
    """botocore.exceptions.ReadTimeoutError stand-in."""


class ConnectTimeoutError(Exception):
    """botocore.exceptions.ConnectTimeoutError stand-in."""


class TestRetryable:
    @pytest.mark.parametrize(
        "code",
        [
            "ThrottlingException",
            "TooManyRequestsException",
            "ServiceUnavailableException",
            "InternalServerException",
            "ModelNotReadyException",
            "ModelTimeoutException",
        ],
    )
    def test_transient_aws_codes_are_retryable(self, code):
        assert is_retryable_llm_error(_client_error(code, 429)) is True

    @pytest.mark.parametrize(
        "code", ["ValidationException", "AccessDeniedException", "ResourceNotFoundException", "ModelErrorException"]
    )
    def test_final_aws_codes_are_not_retryable(self, code):
        assert is_retryable_llm_error(_client_error(code, 400)) is False

    def test_aws_code_wins_over_http_status(self):
        """A 5xx carrying a final code (rare) stays final: the code is the truth."""
        assert is_retryable_llm_error(_client_error("ValidationException", 500)) is False

    @pytest.mark.parametrize("exc_cls", [ReadTimeoutError, ConnectTimeoutError, RemoteProtocolError, ReadError])
    def test_transport_class_names_are_retryable(self, exc_cls):
        assert is_retryable_llm_error(exc_cls("boom")) is True

    @pytest.mark.parametrize("exc", [TimeoutError("t"), ConnectionError("c"), ConnectionResetError("r")])
    def test_builtin_transport_errors_are_retryable(self, exc):
        assert is_retryable_llm_error(exc) is True

    def test_incomplete_read_is_retryable(self):
        assert is_retryable_llm_error(http.client.IncompleteRead(b"partial")) is True

    @pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 502, 503, 504])
    def test_retryable_http_statuses(self, status):
        assert is_retryable_llm_error(_FakeStatusError(status)) is True

    @pytest.mark.parametrize("status", [529, 520, 521, 522, 524, 507, 599])
    def test_other_transient_5xx_are_retryable(self, status):
        """Review B3-02: the anthropic SDK raises OverloadedError with status 529 and
        CDNs / proxies answer 52x; a fixed {500, 502, 503, 504} list classified all of
        them as final and failed the summarization tasks on the first overload."""
        assert is_retryable_llm_error(_FakeStatusError(status)) is True

    @pytest.mark.parametrize("status", [501, 505])
    def test_not_implemented_server_statuses_stay_final(self, status):
        assert is_retryable_llm_error(_FakeStatusError(status)) is False

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
    def test_final_http_statuses(self, status):
        assert is_retryable_llm_error(_FakeStatusError(status)) is False

    def test_anthropic_overloaded_error_shape(self):
        """Same class name and status attribute as anthropic.OverloadedError
        (anthropic/_exceptions.py: ``status_code: Literal[529] = 529``)."""

        class APIStatusError(Exception):
            def __init__(self, message, status_code):
                super().__init__(message)
                self.status_code = status_code

        class OverloadedError(APIStatusError):
            pass

        assert is_retryable_llm_error(OverloadedError("Overloaded", 529)) is True

    @pytest.mark.parametrize("name", ["OverloadedError", "ServiceUnavailableError", "DeadlineExceededError"])
    def test_anthropic_transient_class_names_without_a_status_are_retryable(self, name):
        exc_cls = type(name, (Exception,), {})
        assert is_retryable_llm_error(exc_cls("boom")) is True

    @pytest.mark.parametrize("exc", [ValueError("bad"), KeyError("k"), RuntimeError("no provider")])
    def test_programming_errors_are_final(self, exc):
        assert is_retryable_llm_error(exc) is False

    def test_message_fragments_count(self):
        assert is_retryable_llm_error(RuntimeError("peer closed connection without sending complete message body (incomplete chunked read)")) is True

    def test_none_is_not_retryable(self):
        assert is_retryable_llm_error(None) is False  # type: ignore[arg-type]


class TestMidStreamTransport:
    @pytest.mark.parametrize("exc_cls", [RemoteProtocolError, ReadError])
    def test_httpx_stream_breakage_restarts(self, exc_cls):
        assert is_mid_stream_transport_error(exc_cls("stream reset")) is True

    def test_incomplete_read_restarts(self):
        assert is_mid_stream_transport_error(http.client.IncompleteRead(b"partial")) is True

    @pytest.mark.parametrize("exc", [ConnectionResetError("reset"), BrokenPipeError("pipe")])
    def test_socket_resets_restart(self, exc):
        assert is_mid_stream_transport_error(exc) is True

    @pytest.mark.parametrize(
        "text",
        [
            "incomplete chunked read",
            "peer closed connection without sending complete message body",
            "Connection reset by peer",
            "Server disconnected without sending a response.",
        ],
    )
    def test_message_fragments_restart(self, text):
        assert is_mid_stream_transport_error(RuntimeError(text)) is True

    @pytest.mark.parametrize(
        "code",
        ["ThrottlingException", "ModelTimeoutException", "ServiceUnavailableException", "InternalServerException"],
    )
    def test_sdk_retried_client_errors_never_restart(self, code):
        """botocore already retried these with backoff; a graph restart would only
        re-run (and re-bill) every previous turn for the same outcome."""
        assert is_mid_stream_transport_error(_client_error(code, 429)) is False

    @pytest.mark.parametrize("code", ["ValidationException", "AccessDeniedException"])
    def test_final_client_errors_never_restart(self, code):
        assert is_mid_stream_transport_error(_client_error(code, 400)) is False

    @pytest.mark.parametrize("exc_cls", [ReadTimeoutError, ConnectTimeoutError])
    def test_timeouts_never_restart(self, exc_cls):
        assert is_mid_stream_transport_error(exc_cls("Read timeout on endpoint URL")) is False

    @pytest.mark.parametrize(
        "text",
        [
            "An error occurred (ModelTimeoutException) when calling the ConverseStream operation",
            "An error occurred (ThrottlingException) when calling the ConverseStream operation: Too many requests",
            "ValidationException: The provided model identifier is invalid",
        ],
    )
    def test_wrapped_bedrock_errors_in_plain_exceptions_never_restart(self, text):
        """The old loop matched 'Timeout' anywhere in the text and restarted on
        ModelTimeoutException. The text of an SDK-retried error must never qualify,
        whatever exception type wraps it."""
        assert is_mid_stream_transport_error(RuntimeError(text)) is False

    @pytest.mark.parametrize("exc", [ValueError("bad"), TimeoutError("plain timeout"), RuntimeError("Timeout waiting")])
    def test_everything_else_is_final(self, exc):
        assert is_mid_stream_transport_error(exc) is False

    def test_none_is_final(self):
        assert is_mid_stream_transport_error(None) is False  # type: ignore[arg-type]
