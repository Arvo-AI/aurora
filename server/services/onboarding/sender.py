"""Getting a setup email out the door, and telling the team about it.

The transport is deliberately thin: today it is the SMTP account Aurora
already uses. Swapping in another provider only touches ``send``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid

import requests

from services.onboarding.guides import Guide
from utils.web.public_url import external_backend_url

logger = logging.getLogger(__name__)


def emails_enabled() -> bool:
    from utils.notifications.email_service import is_email_configured
    return (
        os.getenv("ONBOARDING_EMAILS_ENABLED", "").lower() == "true"
        and bool(_unsubscribe_secret())
        and is_email_configured()
    )


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


# ── Sending ──


def send(to_email: str, guide: Guide, subject: str, text: str, html_body: str, unsub_url: str) -> bool:
    """Send as the guide: their name on the From line, their inbox on Reply-To.

    The From address stays the configured sender unless the guide's own
    address may be used (``ONBOARDING_SEND_AS_GUIDE=true``, for when the
    SMTP account is allowed to send as them); otherwise DMARC would bounce it.
    """
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM_EMAIL") or user or ""
    from_addr = guide.email if os.getenv("ONBOARDING_SEND_AS_GUIDE", "").lower() == "true" else sender
    from_name = guide.name if from_addr == guide.email else f"{guide.first_name} at Aurora"

    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to_email
    msg["Reply-To"] = formataddr((guide.name, guide.email))
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1] or None)
    msg["List-Unsubscribe"] = f"<{unsub_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if os.getenv("ONBOARDING_BCC_GUIDE", "").lower() == "true":
        msg["Bcc"] = guide.email
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError):
        logger.exception("[ONBOARDING] sending %r failed", subject)
        return False


def ping_team(text: str) -> None:
    """One line in the team's Slack, so a person can follow up by hand."""
    url = os.getenv("ONBOARDING_TEAM_WEBHOOK_URL", "")
    if not url.startswith("https://"):
        return
    try:
        requests.post(url, json={"text": text}, timeout=5)
    except requests.RequestException:
        logger.warning("[ONBOARDING] team ping failed", exc_info=True)
