#!/usr/bin/env python3
"""HMAC-sign a JSON payload and POST it to Aurora's GitHub webhook endpoint.

Used by Task 23's end-to-end suspend-chain verification (and the F3 Final
Verification Wave) to drive the ``/github/webhook`` route without an actual
GitHub.com delivery. The script reproduces the exact signature contract
documented in ``server/utils/auth/github_webhook.py`` and the three
metadata headers (``X-Hub-Signature-256``, ``X-GitHub-Event``,
``X-GitHub-Delivery``) the route requires before it accepts a body.

Why a standalone script
-----------------------
The Aurora server itself only validates incoming webhooks; there is no
production code path that signs an outgoing one. Tests that exercise the
suspend / unsuspend handlers therefore need a tiny, dependency-light
client that can be invoked from a shell. Keeping it under
``server/scripts/`` (next to ``server/`` so it shares the project) makes
it discoverable for future ops debugging and one-off replays.

The script intentionally has NO Aurora imports — it shells out to
``urllib`` from the stdlib so it runs on the host without the docker
network or the Aurora venv. The webhook secret is read from the
``GITHUB_APP_WEBHOOK_SECRET`` env var by default; the ``--secret`` flag
overrides for ad-hoc replays.

Examples
--------
    # Sign + POST a payload from a file (default URL, secret from env)
    python server/scripts/sign_webhook.py \\
        --event installation \\
        --payload-file /tmp/suspend.json

    # Override the secret + URL (e.g. point at a staging Aurora)
    python server/scripts/sign_webhook.py \\
        --event installation \\
        --payload-file /tmp/suspend.json \\
        --secret 'task11-test-webhook-secret-do-not-use-in-prod' \\
        --url 'http://localhost:5080/github/webhook'

The script prints the response status + body on stdout and exits 0 on
HTTP 2xx, 1 on any other status (so it composes naturally in shell
verification chains).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import uuid
from pathlib import Path
from urllib import error, request

# Match the canonical reference in server/utils/auth/github_webhook.py;
# we keep the constants local to avoid importing Aurora modules so the
# script runs from a bare host shell.
_SIGNATURE_HEADER = "X-Hub-Signature-256"
_DELIVERY_HEADER = "X-GitHub-Delivery"
_EVENT_HEADER = "X-GitHub-Event"
_SIGNATURE_PREFIX = "sha256="

_DEFAULT_URL = "http://localhost:5080/github/webhook"
_DEFAULT_SECRET_ENV = "GITHUB_APP_WEBHOOK_SECRET"
_REQUEST_TIMEOUT_SEC = 20


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sign_webhook.py",
        description=(
            "Sign a JSON payload with HMAC-SHA256 and POST it to Aurora's "
            "GitHub App webhook endpoint. Reproduces the exact contract "
            "verify_webhook_signature() expects."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes:\n"
            "  0  HTTP 2xx response\n"
            "  1  HTTP non-2xx response or transport error\n"
            "  2  Bad arguments (e.g. payload file not found)\n"
        ),
    )
    parser.add_argument(
        "--event",
        required=True,
        help="GitHub event type for the X-GitHub-Event header "
        "(e.g. 'installation', 'installation_repositories', 'pull_request').",
    )
    parser.add_argument(
        "--payload-file",
        required=True,
        type=Path,
        help="Path to a JSON file containing the webhook body. "
        "Sent byte-exactly so the HMAC matches.",
    )
    parser.add_argument(
        "--secret",
        default=None,
        help=(
            f"Webhook secret. Defaults to ${_DEFAULT_SECRET_ENV}. "
            "Required if the env var is unset."
        ),
    )
    parser.add_argument(
        "--url",
        default=_DEFAULT_URL,
        help=f"Webhook URL (default: {_DEFAULT_URL}).",
    )
    parser.add_argument(
        "--delivery-id",
        default=None,
        help=(
            "Override the X-GitHub-Delivery UUID. Defaults to a fresh "
            "uuid4(); supply your own to force a duplicate-delivery test."
        ),
    )
    return parser


def _resolve_secret(cli_secret: str | None) -> str:
    if cli_secret:
        return cli_secret
    env_secret = os.getenv(_DEFAULT_SECRET_ENV, "")
    if env_secret:
        return env_secret
    print(
        f"error: webhook secret not provided (use --secret or set ${_DEFAULT_SECRET_ENV})",
        file=sys.stderr,
    )
    sys.exit(2)


def _load_payload(path: Path) -> bytes:
    """Read the payload file as raw bytes; validate it parses as JSON.

    We send the original bytes (NOT a re-serialised dict) so the HMAC
    matches what the server computes from request.get_data().
    """
    if not path.exists():
        print(f"error: payload file not found: {path}", file=sys.stderr)
        sys.exit(2)
    raw = path.read_bytes()
    try:
        json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        print(f"error: payload file is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)
    return raw


def _compute_signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{_SIGNATURE_PREFIX}{digest}"


def _post(
    url: str,
    body: bytes,
    *,
    signature: str,
    event: str,
    delivery_id: str,
) -> tuple[int, str]:
    """POST ``body`` to ``url`` with the GitHub webhook headers.

    Returns ``(status, response_body_text)``. Network/timeout errors are
    surfaced as a synthetic ``(0, "<error class>: <msg>")`` tuple so the
    caller can log them and exit non-zero without exception trace noise.
    """
    headers = {
        "Content-Type": "application/json",
        _SIGNATURE_HEADER: signature,
        _EVENT_HEADER: event,
        _DELIVERY_HEADER: delivery_id,
        "User-Agent": "Aurora-sign-webhook/1.0",
    }
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=_REQUEST_TIMEOUT_SEC) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        # urllib raises on non-2xx; surface body so the user can see e.g.
        # the 401 invalid-signature JSON or the 503 App-not-enabled message.
        body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return exc.code, body_text
    except (error.URLError, TimeoutError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    secret = _resolve_secret(args.secret)
    body = _load_payload(args.payload_file)
    signature = _compute_signature(body, secret)
    delivery_id = args.delivery_id or str(uuid.uuid4())

    status, response_body = _post(
        args.url,
        body,
        signature=signature,
        event=args.event,
        delivery_id=delivery_id,
    )

    print(f"URL: {args.url}")
    print(f"Event: {args.event}")
    print(f"Delivery ID: {delivery_id}")
    print(f"Signature: {signature}")
    print(f"HTTP {status}")
    print(f"Response body: {response_body}")

    if 200 <= status < 300:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
