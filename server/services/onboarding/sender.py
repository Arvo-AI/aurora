"""Getting a setup email out the door, and telling the team about it.

Two transports, picked by configuration:

* ``graph``: Microsoft Graph with an app registration that has the Mail.Send
  application permission. Each email is sent from the guide's own mailbox,
  so it sits in their Sent Items and a reply lands in the same thread.
  Needs ONBOARDING_GRAPH_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET.
* ``smtp``: the SMTP account Aurora already uses, with the guide's name on
  the From line and their address as Reply-To.

Graph wins when both are configured.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import smtplib
import ssl
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid
from urllib.parse import urlsplit

import requests

from services.onboarding.guides import Guide
from utils.web.public_url import external_backend_url

logger = logging.getLogger(__name__)

_GRAPH_VARS = ("ONBOARDING_GRAPH_TENANT_ID", "ONBOARDING_GRAPH_CLIENT_ID", "ONBOARDING_GRAPH_CLIENT_SECRET")


def transport() -> str | None:
    if all(os.getenv(v) for v in _GRAPH_VARS):
        return "graph"
    from utils.notifications.email_service import is_email_configured
    return "smtp" if is_email_configured() else None


def emails_enabled() -> bool:
    return (
        os.getenv("ONBOARDING_EMAILS_ENABLED", "").lower() == "true"
        and bool(_unsubscribe_secret())
        and transport() is not None
        # Every email carries an unsubscribe link; one the recipient can't
        # open (localhost, a private address) means don't send at all.
        and is_public_https(external_backend_url())
    )


def is_public_https(url: str) -> bool:
    """An https URL whose host someone on the internet could reach."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." in host  # a bare name like "server" only resolves inside a network
    return ip.is_global


# ── Unsubscribe links ──


def _unsubscribe_secret() -> bytes:
    return (os.getenv("ONBOARDING_UNSUBSCRIBE_SECRET") or os.getenv("FLASK_SECRET_KEY") or "").encode()


def unsubscribe_token(user_id: str) -> str:
    return hmac.new(_unsubscribe_secret(), f"onboarding-unsub:{user_id}".encode(), hashlib.sha256).hexdigest()


def unsubscribe_token_ok(user_id: str, token: str) -> bool:
    secret = _unsubscribe_secret()
    if not secret or not user_id or not token:
        return False
    return hmac.compare_digest(unsubscribe_token(user_id), token)


def unsubscribe_url(user_id: str) -> str:
    from urllib.parse import urlencode
    return f"{external_backend_url()}/onboarding/unsubscribe?{urlencode({'u': user_id, 't': unsubscribe_token(user_id)})}"


# ── The message ──


def _message(to_email: str, from_name: str, from_addr: str, guide: Guide, subject: str,
             text: str, html_body: str, unsub_url: str, bcc_guide: bool) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to_email
    if from_addr != guide.email:
        msg["Reply-To"] = formataddr((guide.name, guide.email))
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1] or None)
    msg["List-Unsubscribe"] = f"<{unsub_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if bcc_guide:
        msg["Bcc"] = guide.email
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg


# ── Microsoft Graph ──

_token_lock = threading.Lock()
_token: tuple[str, float] | None = None  # (access token, expires at)


def _graph_token() -> str:
    global _token
    with _token_lock:
        if _token and _token[1] - 120 > time.time():
            return _token[0]
        tenant = os.environ["ONBOARDING_GRAPH_TENANT_ID"]
        resp = requests.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "client_id": os.environ["ONBOARDING_GRAPH_CLIENT_ID"],
                "client_secret": os.environ["ONBOARDING_GRAPH_CLIENT_SECRET"],
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        _token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return _token[0]


def _send_graph(guide: Guide, msg: MIMEMultipart) -> bool:
    # MIME (not the JSON form) because Graph's JSON only allows x- headers,
    # and List-Unsubscribe is what puts the unsubscribe button in Gmail/Outlook.
    from urllib.parse import quote
    try:
        resp = requests.post(
            f"https://graph.microsoft.com/v1.0/users/{quote(guide.email)}/sendMail",
            headers={"Authorization": f"Bearer {_graph_token()}", "Content-Type": "text/plain"},
            data=base64.b64encode(msg.as_bytes()),
            timeout=30,
        )
    except requests.RequestException:
        logger.exception("[ONBOARDING] Graph send failed")
        return False
    if resp.status_code != 202:
        # 403 = Mail.Send not consented, or the mailbox is outside the app's access policy.
        logger.error("[ONBOARDING] Graph send returned %s: %s", resp.status_code, resp.text[:300])
        return False
    return True


# ── SMTP ──


def _send_smtp(msg: MIMEMultipart) -> bool:
    try:
        with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587")), timeout=30) as server:
            tls = ssl.create_default_context()
            server.starttls(context=tls)
            server.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"))
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError):
        logger.exception("[ONBOARDING] SMTP send failed")
        return False


def send(to_email: str, guide: Guide, subject: str, text: str, html_body: str, unsub_url: str) -> bool:
    """Send one setup email from the guide. True once the provider accepted it."""
    bcc = os.getenv("ONBOARDING_BCC_GUIDE", "").lower() == "true"
    if transport() == "graph":
        msg = _message(to_email, guide.name, guide.email, guide, subject, text, html_body, unsub_url, bcc)
        return _send_graph(guide, msg)

    # SMTP can only use the guide's own address when that account may send as them;
    # otherwise DMARC would reject it.
    sender = os.getenv("SMTP_FROM_EMAIL") or os.getenv("SMTP_USER") or ""
    as_guide = os.getenv("ONBOARDING_SEND_AS_GUIDE", "").lower() == "true"
    from_addr = guide.email if as_guide else sender
    from_name = guide.name if as_guide else f"{guide.first_name} at Aurora"
    return _send_smtp(_message(to_email, from_name, from_addr, guide, subject, text, html_body, unsub_url, bcc))


def ping_team(text: str) -> None:
    """One line in the team's Slack, so a person can follow up by hand."""
    url = os.getenv("ONBOARDING_TEAM_WEBHOOK_URL", "")
    if not url.startswith("https://"):
        return
    try:
        requests.post(url, json={"text": text}, timeout=5)
    except requests.RequestException:
        logger.warning("[ONBOARDING] team ping failed", exc_info=True)
