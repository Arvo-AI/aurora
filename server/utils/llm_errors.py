"""Classify LLM call failures: retry-worthy vs. final, SDK-retried vs. mid-stream.

Two questions keep coming up around model calls and need one answer each:

1. :func:`is_retryable_llm_error` — may a *task* (a Celery retry, a loop) run
   the same model call again and expect a different outcome? True for
   throttling, 5xx / service-unavailable, connection and read timeouts; False
   for validation errors, access denied, bad model ids, programming errors.

2. :func:`is_mid_stream_transport_error` — did the *transport* drop while a
   streaming response was in flight (HTTP/2 stream reset, incomplete chunked
   read, peer closed the socket)? These are the only failures the agent's
   full-graph restart may act on: the SDK has already retried everything it
   knows how to retry (``ThrottlingException``, ``ModelTimeoutException``,
   5xx, connect timeouts — see the botocore ``standard`` retry mode), so
   restarting the whole agent on those would only re-bill every previous turn
   for the same outcome.

Matching is by class name across the exception's MRO plus a few message
fragments, so the optional SDKs (botocore, openai, anthropic, httpx) never need
to be imported here — this module is imported by Celery tasks and the agent in
deployments that may lack some of them.
"""
from __future__ import annotations

from typing import Iterable, Optional

# botocore ClientError codes the AWS SDK itself classifies as transient
# (botocore/data/_retry.json, ``standard`` mode) plus Bedrock's own throttling
# and timeout codes.
_RETRYABLE_AWS_ERROR_CODES = frozenset(
    {
        "ThrottlingException",
        "ThrottledException",
        "Throttling",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "RequestThrottledException",
        "ProvisionedThroughputExceededException",
        "ServiceUnavailableException",
        "ServiceUnavailable",
        "InternalServerException",
        "InternalFailure",
        "InternalError",
        "ModelNotReadyException",
        "ModelTimeoutException",
        "RequestTimeout",
        "RequestTimeoutException",
    }
)

# Exception class names (any SDK) that always denote a transient failure.
_RETRYABLE_CLASS_NAMES = frozenset(
    {
        # botocore
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "EndpointConnectionError",
        "ConnectionClosedError",
        # openai / anthropic SDKs share these names
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
        # anthropic SDK: 529 Overloaded, 503, 504
        "OverloadedError",
        "ServiceUnavailableError",
        "DeadlineExceededError",
        # httpx
        "TimeoutException",
        "ReadTimeout",
        "ConnectTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ConnectError",
        "RemoteProtocolError",
        "ReadError",
        "WriteError",
        # stdlib
        "TimeoutError",
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
        "IncompleteRead",
    }
)

# Transport-level breakage of an in-flight (streaming) response.
_MID_STREAM_CLASS_NAMES = frozenset(
    {
        "RemoteProtocolError",  # httpx: server closed / malformed framing mid-body
        "ReadError",  # httpx: socket error while reading the body
        "IncompleteRead",  # http.client: body ended before Content-Length
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
        "ConnectionClosedError",  # botocore: connection closed before response
    }
)
_MID_STREAM_MESSAGE_FRAGMENTS = (
    "incomplete chunked read",
    "peer closed connection",
    "connection reset by peer",
    "server disconnected without sending a response",
    "response ended prematurely",
)

# Anything the SDK already retried, or that is final by definition: never a
# reason to restart the whole agent graph.
_NOT_MID_STREAM_FRAGMENTS = (
    "ModelTimeoutException",
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "InternalServerException",
    "ValidationException",
    "AccessDeniedException",
    "ResourceNotFoundException",
    "ModelNotReadyException",
    "ModelErrorException",
)

# 4xx status codes a client may retry (request timeout, conflict, too early, rate
# limiting). Server-side failures are handled as a range below, not as a list: the
# anthropic SDK reports overload as 529 (``OverloadedError``), CDNs and proxies
# answer 520-524, and a fixed set of 500/502/503/504 silently classified all of
# those as final.
_RETRYABLE_HTTP_STATUS = frozenset({408, 409, 425, 429})
# The two 5xx codes that cannot succeed on retry: the server does not implement
# the method / HTTP version at all.
_FINAL_SERVER_STATUS = frozenset({501, 505})


def _status_is_retryable(status: int) -> bool:
    """408/409/425/429 and every 5xx except 501 and 505."""
    if status in _RETRYABLE_HTTP_STATUS:
        return True
    return 500 <= status < 600 and status not in _FINAL_SERVER_STATUS


def _class_names(exc: BaseException) -> Iterable[str]:
    return (cls.__name__ for cls in type(exc).__mro__)


def _aws_error_code(exc: BaseException) -> Optional[str]:
    """``ClientError.response["Error"]["Code"]`` without importing botocore."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if isinstance(code, str):
                return code
    return None


def _status_int(value: object) -> Optional[int]:
    """``value`` when it is a real int status code (``bool`` is an int subclass), else None."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _response_status(response: object) -> Optional[int]:
    """Status carried by an exception's ``response``: httpx.Response /
    requests.Response expose ``.status_code``; botocore's dict response carries
    ``ResponseMetadata.HTTPStatusCode``."""
    if response is None:
        return None
    if not isinstance(response, dict):
        return _status_int(getattr(response, "status_code", None))
    meta = response.get("ResponseMetadata")
    if isinstance(meta, dict):
        value = meta.get("HTTPStatusCode")
        if isinstance(value, int):
            return value
    return None


def _http_status(exc: BaseException) -> Optional[int]:
    """Status code carried by openai/anthropic/httpx-style exceptions, if any."""
    for attr in ("status_code", "http_status", "status"):
        value = _status_int(getattr(exc, attr, None))
        if value is not None:
            return value
    return _response_status(getattr(exc, "response", None))


def is_retryable_llm_error(exc: BaseException) -> bool:
    """True when running the same model call again could reasonably succeed.

    Throttling, service-unavailable / internal-server errors, model-not-ready,
    model-side timeouts, connection and read timeouts, and HTTP 408/429 plus every
    5xx except 501/505 (including Anthropic's 529 Overloaded) are retryable.
    Validation errors, access denied, unknown model ids and ordinary programming
    errors are not — retrying those only burns the retry budget.
    """
    if exc is None:
        return False
    code = _aws_error_code(exc)
    if code is not None:
        return code in _RETRYABLE_AWS_ERROR_CODES
    status = _http_status(exc)
    if status is not None:
        return _status_is_retryable(status)
    names = set(_class_names(exc))
    if names & _RETRYABLE_CLASS_NAMES:
        return True
    # The fragments are lower-case; match the message case-insensitively, exactly
    # like is_mid_stream_transport_error ("Connection reset by peer" must count).
    lowered = str(exc).lower()
    return any(fragment in lowered for fragment in _MID_STREAM_MESSAGE_FRAGMENTS)


def is_mid_stream_transport_error(exc: BaseException) -> bool:
    """True only for a transport failure *while a response was streaming*.

    Deliberately narrow: every botocore ``ClientError`` (including
    ``ModelTimeoutException`` and ``ThrottlingException``), every read /
    connect timeout, and every error whose text names a Bedrock exception the
    SDK already retried returns False. Those have been retried by botocore's
    ``standard`` retry mode (or are final), so a full agent restart would just
    re-run — and re-bill — every previous turn for the same answer.
    """
    if exc is None:
        return False
    if _aws_error_code(exc) is not None:
        return False
    text = str(exc)
    if any(fragment in text for fragment in _NOT_MID_STREAM_FRAGMENTS):
        return False
    names = set(_class_names(exc))
    if "ReadTimeoutError" in names or "ConnectTimeoutError" in names:
        return False
    if names & _MID_STREAM_CLASS_NAMES:
        return True
    lowered = text.lower()
    return any(fragment in lowered for fragment in _MID_STREAM_MESSAGE_FRAGMENTS)
