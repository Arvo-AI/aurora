#!/usr/bin/env python3
"""Register a GitHub App for Aurora via the App Manifest flow.

Run once to bootstrap a fresh GitHub App and have its credentials written into
``.env`` (id, client_id, slug, webhook URL, setup URL, webhook secret) and
Vault (private key PEM). The single human step is clicking
"Create GitHub App for X" in the browser window the script opens — GitHub
enforces that consent step and it is not bypassable.

Usage::

    # localhost-only (install flow works, GitHub cannot deliver webhooks here)
    python server/scripts/register_github_app.py

    # autodetect a running ngrok tunnel
    ngrok http 5080
    python server/scripts/register_github_app.py

    # explicit public URL (cloudflared, prod host, ...)
    python server/scripts/register_github_app.py --public-url https://aurora.example.com

    # register the App under an organization instead of your user account
    python server/scripts/register_github_app.py --org Arvo-AI

The flow:

1. Build an Aurora-tuned manifest (the 7 read permissions and 9 webhook
   events the dispatcher actually routes).
2. Spin up a one-shot HTTP listener on port 8765.
3. Open ``http://localhost:8765/`` in the browser; that page auto-submits
   the manifest as a POST form to ``github.com/settings/apps/new``.
4. After the user clicks "Create GitHub App for X", GitHub redirects back
   to ``http://localhost:8765/callback?code=<one-time-code>``.
5. The script exchanges that code for the App's credentials, writes them
   into ``.env`` and the PEM into Vault, and exits.

The webhook secret stored in ``.env`` (and any prior auto-generated value)
is overwritten with whatever GitHub returned, so the two sides stay in sync.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import secrets
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = REPO_ROOT / ".env"

CALLBACK_PORT_DEFAULT = 8765
CALLBACK_PATH = "/callback"
NGROK_API = "http://localhost:4040/api/tunnels"
VAULT_CONTAINER = "aurora-vault"
VAULT_PEM_PATH = "aurora/system/github-app/private-key"
VAULT_WEBHOOK_PATH = "aurora/system/github-app/webhook-secret"

# Webhook events the dispatcher routes. ``installation`` and
# ``installation_repositories`` are intentionally NOT listed here — GitHub
# delivers those automatically to every App when its install state changes,
# and listing them in ``default_events`` makes the manifest API reject the
# whole submission. push/release are excluded per the migration plan.
WEBHOOK_EVENTS = [
    "pull_request",
    "issues",
    "deployment",
    "deployment_status",
    "workflow_run",
    "check_run",
    "check_suite",
]

# Permissions the App needs. ``read`` everywhere — Aurora's RCA and connector
# routes only consume; they never push commits or comments back.
PERMISSIONS = {
    "actions": "read",
    "checks": "read",
    "contents": "read",
    "deployments": "read",
    "issues": "read",
    "metadata": "read",
    "pull_requests": "read",
}


def detect_ngrok_url() -> str | None:
    """Return the first https tunnel from the local ngrok agent, or None."""
    try:
        with urllib.request.urlopen(NGROK_API, timeout=2) as resp:
            data = json.load(resp)
    except Exception:
        return None
    for tunnel in data.get("tunnels", []):
        public_url = tunnel.get("public_url", "")
        if public_url.startswith("https://"):
            return public_url
    return None


def find_free_port(preferred: int) -> int:
    """Use ``preferred`` if free, else fall back to an OS-assigned port."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", preferred))
        s.close()
        return preferred
    except OSError:
        s.close()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port


def build_manifest(
    public_url: str,
    app_name: str,
    redirect_url: str,
    public: bool,
    homepage_url: str,
) -> dict:
    return {
        "name": app_name,
        # GitHub displays this in the App's public profile — must be a
        # URL that resolves for anyone who lands on the App's listing,
        # not the developer's local Aurora instance.
        "url": homepage_url,
        "hook_attributes": {
            "url": f"{public_url}/github/webhook",
            "active": True,
        },
        # Where GitHub returns the user after they click "Create GitHub App".
        "redirect_url": redirect_url,
        # Where users land after installing the App on a repo/org.
        "setup_url": f"{public_url}/github/app/install/callback",
        "setup_on_update": False,
        # ``public: true`` lets any GitHub user/org install the App on
        # their own repos. ``false`` restricts installs to the registering
        # user/org — useful for purely-internal Aurora deployments.
        "public": public,
        "default_permissions": PERMISSIONS,
        "default_events": WEBHOOK_EVENTS,
    }


class _Listener(socketserver.TCPServer):
    allow_reuse_address = True
    received_code: str | None = None
    received_state: str | None = None

    def __init__(self, addr, manifest: dict, state: str, target_url: str):
        super().__init__(addr, _Handler)
        self.manifest = manifest
        self.state = state
        self.target_url = target_url


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _Listener  # type: ignore[assignment]

    def log_message(self, *_args, **_kwargs):
        pass  # silence default access logs

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/":
            # Auto-submit form so the browser POSTs the manifest to GitHub.
            # The manifest is too large for a query string, hence the form.
            manifest_json = json.dumps(self.server.manifest)
            html = f"""<!doctype html>
<html><head><title>Aurora — registering GitHub App…</title></head>
<body style="font-family:system-ui;max-width:480px;margin:48px auto;color:#333">
<h2>Redirecting to GitHub…</h2>
<p>If this page doesn't redirect automatically, click Continue.</p>
<form id="f" action="{self.server.target_url}" method="post">
  <input type="hidden" name="manifest" value='{_html_escape(manifest_json)}'>
  <input type="hidden" name="state" value="{self.server.state}">
  <button type="submit">Continue to GitHub</button>
</form>
<script>document.getElementById('f').submit();</script>
</body></html>"""
            self._send(200, "text/html; charset=utf-8", html.encode())
            return

        if parsed.path == CALLBACK_PATH:
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [""])[0]
            state = qs.get("state", [""])[0]
            if not code or state != self.server.state:
                self._send(
                    400,
                    "text/plain",
                    b"State or code missing/mismatched. "
                    b"Re-run register_github_app.py to retry.",
                )
                return
            self.server.received_code = code
            self.server.received_state = state
            self._send(
                200,
                "text/html; charset=utf-8",
                b"<h2>App created. Returning to terminal\xe2\x80\xa6</h2>"
                b"<p>You can close this tab.</p>",
            )
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        self._send(404, "text/plain", b"not found")

    def _send(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("'", "&#39;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def exchange_code(code: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/app-manifests/{code}/conversions",
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def update_env(updates: dict[str, str]) -> None:
    """Idempotent in-place edit: replace any matching ``KEY=`` line, append if missing."""
    if not ENV_PATH.exists():
        raise FileNotFoundError(f".env not found at {ENV_PATH}")
    text = ENV_PATH.read_text()
    for key, value in updates.items():
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        replacement = f"{key}={value}"
        if pattern.search(text):
            text = pattern.sub(replacement, text)
        else:
            text = text.rstrip() + f"\n{replacement}\n"
    ENV_PATH.write_text(text)


def store_secret_in_vault(path: str, field: str, value: str) -> bool:
    try:
        subprocess.run(
            [
                "docker", "exec", "-i", VAULT_CONTAINER,
                "vault", "kv", "put", path, f"{field}=-",
            ],
            input=value.encode(),
            capture_output=True,
            timeout=15,
            check=True,
        )
        return True
    except FileNotFoundError:
        print("  warning: docker not on PATH; skipping Vault write", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else str(exc)
        print(f"  warning: vault write failed: {msg.strip()}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"  warning: vault write failed: {exc}", file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Register a GitHub App for Aurora via the manifest flow.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--public-url",
        help=(
            "Public HTTPS URL Aurora is reachable at (e.g. an ngrok tunnel). "
            "Defaults to autodetecting ngrok or http://localhost:5080."
        ),
    )
    parser.add_argument(
        "--org",
        help="Register the App under this GitHub organization instead of your user.",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="App name shown in GitHub. Default: Aurora (local-<random>)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=CALLBACK_PORT_DEFAULT,
        help=f"Local callback listener port (default {CALLBACK_PORT_DEFAULT}).",
    )
    parser.add_argument(
        "--no-vault",
        action="store_true",
        help="Skip writing the PEM to Vault; save to a local file instead.",
    )
    parser.add_argument(
        "--private",
        action="store_true",
        help=(
            "Register as a private App (only the owning user/org can install). "
            "Default is public — any GitHub user/org can install."
        ),
    )
    parser.add_argument(
        "--homepage-url",
        default="https://github.com/Arvo-AI/aurora",
        help=(
            "Public-facing 'Homepage URL' shown on the App's GitHub listing. "
            "Defaults to the Aurora repo. Cosmetic — does not affect auth or webhooks."
        ),
    )
    args = parser.parse_args(argv)

    public_url = (
        args.public_url
        or detect_ngrok_url()
        or "http://localhost:5080"
    ).rstrip("/")
    is_local = public_url.startswith(("http://localhost", "http://127."))
    if is_local:
        print(f"Public URL: {public_url}  (localhost — webhooks won't be deliverable)")
        print("  → For real GitHub→Aurora webhook delivery, run `ngrok http 5080`")
        print("    in a second terminal and re-run this script.")
    else:
        print(f"Public URL: {public_url}")

    app_name = args.name or f"Aurora (local-{secrets.token_hex(3)})"
    port = find_free_port(args.port)
    redirect_url = f"http://localhost:{port}{CALLBACK_PATH}"
    state = secrets.token_urlsafe(16)
    manifest = build_manifest(
        public_url,
        app_name,
        redirect_url,
        public=not args.private,
        homepage_url=args.homepage_url,
    )
    target_url = (
        f"https://github.com/organizations/{args.org}/settings/apps/new"
        if args.org
        else "https://github.com/settings/apps/new"
    )

    httpd = _Listener(("127.0.0.1", port), manifest, state, target_url)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    home = f"http://localhost:{port}/"
    print(f"App name:   {app_name}")
    print(f"Listener:   {home}")
    print()
    print("Opening your browser. Click 'Create GitHub App for ...' on GitHub.")
    print("This terminal will pick up the new App's credentials automatically.\n")
    webbrowser.open(home)

    timeout_sec = 600
    deadline = time.time() + timeout_sec
    while httpd.received_code is None:
        if time.time() > deadline:
            print(f"\nTimed out after {timeout_sec}s waiting for GitHub redirect.", file=sys.stderr)
            httpd.shutdown()
            return 1
        time.sleep(0.5)

    code = httpd.received_code
    print("Got code from GitHub. Exchanging for App credentials…")
    try:
        result = exchange_code(code)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"  exchange failed (HTTP {exc.code}): {body}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"  exchange failed: {exc}", file=sys.stderr)
        return 1

    app_id = str(result["id"])
    client_id = result["client_id"]
    slug = result["slug"]
    webhook_secret = result["webhook_secret"]
    pem = result["pem"]

    print(f"  App ID:    {app_id}")
    print(f"  Client ID: {client_id}")
    print(f"  Slug:      {slug}")
    print(f"  Webhook secret: {webhook_secret[:8]}… ({len(webhook_secret)} chars)")
    print(f"  Private key:    {len(pem)} bytes")

    print("\nWriting credentials to .env …")
    update_env(
        {
            "GITHUB_APP_ID": app_id,
            "GITHUB_APP_CLIENT_ID": client_id,
            "NEXT_PUBLIC_GITHUB_APP_SLUG": slug,
            "GITHUB_APP_WEBHOOK_URL": f"{public_url}/github/webhook",
            "GITHUB_APP_SETUP_URL": f"{public_url}/github/app/install/callback",
            "GITHUB_APP_WEBHOOK_SECRET": webhook_secret,
        }
    )
    print(f"  {ENV_PATH} updated.")

    pem_in_vault = False
    if not args.no_vault:
        print("\nStoring private key in Vault …")
        # Field name MUST be ``value`` — the project's Vault backend
        # (utils/secrets/vault_backend.py) hard-codes that key when
        # reading. Using any other field name silently returns "".
        if store_secret_in_vault(VAULT_PEM_PATH, "value", pem):
            print(f"  vault path: {VAULT_PEM_PATH}")
            pem_in_vault = True
        # Mirror the webhook secret to Vault so vault_keys.py reads the
        # canonical path first and falls back to the env var only if
        # Vault is unreachable.
        if store_secret_in_vault(VAULT_WEBHOOK_PATH, "value", webhook_secret):
            print(f"  vault path: {VAULT_WEBHOOK_PATH}")

    if not pem_in_vault:
        pem_path = REPO_ROOT / f"github-app-{slug}.pem"
        pem_path.write_text(pem)
        os.chmod(pem_path, 0o600)
        print(f"  PEM saved to {pem_path}")
        print("  Move it into Vault before shipping:")
        print(f"    docker exec aurora-vault vault kv put {VAULT_PEM_PATH} pem=@{pem_path}")

    print("\nNext:")
    print("  1. make rebuild-server")
    print("  2. docker logs aurora-server 2>&1 | grep github_app_status")
    print("     expect: github_app_status=enabled")
    print(f"  3. visit https://github.com/apps/{slug} to install on a repo")
    if is_local:
        print("\nReminder: webhook URL is localhost-only. The install flow will work")
        print("end-to-end, but GitHub will not be able to deliver webhook events.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
