"""Where GitHub lives: github.com, GitHub Enterprise Cloud (``*.ghe.com``) or
GitHub Enterprise Server.

Every GitHub web link and API call goes through this module so a deployment
pointed at an enterprise host never falls back to github.com.

The host comes from ``NEXT_PUBLIC_GITHUB_APP_SLUG``, which takes either:

- a bare slug (``aurora``): the app lives on github.com, as before;
- the app's full URL, as GitHub shows it on the app's page
  (``https://acme.ghe.com/apps/acme/aurora``): the web host is that URL's
  origin, and the install link is ``<app URL>/installations/new``.

The API host follows the web host:

- ``github.com``  -> ``https://api.github.com``
- ``<x>.ghe.com`` -> ``https://api.<x>.ghe.com``
- anything else   -> ``https://<host>/api/v3`` (GitHub Enterprise Server)

``GITHUB_API_URL`` overrides the API host when that rule does not hold, for
example behind a proxy.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

GITHUB_COM_WEB_URL = "https://github.com"
GITHUB_COM_API_URL = "https://api.github.com"

_SLUG_ENV = "NEXT_PUBLIC_GITHUB_APP_SLUG"
_API_OVERRIDE_ENV = "GITHUB_API_URL"


def _app_setting() -> str:
    return (os.getenv(_SLUG_ENV) or "").strip()


def _parse_http_url(raw: str) -> tuple[str, str, str] | None:
    """``(scheme, host, path)`` for an http(s) URL with a host, else None."""
    if not raw.lower().startswith(("https://", "http://")):
        return None
    parts = urlsplit(raw)
    if not parts.hostname:
        return None
    return parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/")


def _app_url_parts() -> tuple[str, str, str] | None:
    """Parsed app URL when the setting is a URL, else None (bare slug or unset)."""
    raw = _app_setting()
    if "://" not in raw:
        return None
    parsed = _parse_http_url(raw)
    if parsed is None:
        logger.warning(
            "[GITHUB-HOST] %s is not a valid http(s) URL; using github.com", _SLUG_ENV
        )
    return parsed


def github_web_url() -> str:
    """Origin of the GitHub web UI, without a trailing slash."""
    parts = _app_url_parts()
    if parts is None:
        return GITHUB_COM_WEB_URL
    scheme, netloc, _ = parts
    return f"{scheme}://{netloc}"


def github_api_url() -> str:
    """Base URL of the GitHub REST API, without a trailing slash."""
    override = (os.getenv(_API_OVERRIDE_ENV) or "").strip().rstrip("/")
    if override:
        if _parse_http_url(override) is not None:
            return override
        logger.warning(
            "[GITHUB-HOST] %s is not a valid http(s) URL; ignoring it", _API_OVERRIDE_ENV
        )

    parts = _app_url_parts()
    if parts is None:
        return GITHUB_COM_API_URL
    scheme, netloc, _ = parts
    host = netloc.split(":", 1)[0]
    if host in ("github.com", "www.github.com"):
        return GITHUB_COM_API_URL
    if host.endswith(".ghe.com"):
        return f"{scheme}://api.{netloc}"
    return f"{scheme}://{netloc}/api/v3"


def is_github_com() -> bool:
    """True when the web host is github.com."""
    return github_web_url() == GITHUB_COM_WEB_URL


def github_app_page_url() -> str | None:
    """The App's public page, or None when the slug is not configured."""
    raw = _app_setting()
    if not raw:
        return None
    parts = _app_url_parts()
    if parts is not None:
        scheme, netloc, path = parts
        return f"{scheme}://{netloc}{path}"
    if "://" in raw:
        return None
    slug = raw.strip("/")
    return f"{GITHUB_COM_WEB_URL}/apps/{slug}" if slug else None


def github_app_install_url(state: str) -> str | None:
    """Link that starts an App install carrying ``state``, or None if unset."""
    page = github_app_page_url()
    if page is None:
        return None
    return f"{page}/installations/new?state={state}"


def github_installation_settings_url(
    installation_id: int, account_login: str | None, account_type: str | None
) -> str:
    """Where an installation is managed on GitHub."""
    web = github_web_url()
    if account_type == "Organization" and account_login:
        return f"{web}/organizations/{account_login}/settings/installations/{installation_id}"
    return f"{web}/settings/installations/{installation_id}"


def github_avatar_url(account_login: str | None) -> str | None:
    """Avatar for a user or organization login."""
    if not account_login:
        return None
    return f"{github_web_url()}/{account_login}.png?size=40"


def github_mcp_host() -> str:
    """``GITHUB_HOST`` for github-mcp-server: empty on github.com, else the web host."""
    return "" if is_github_com() else github_web_url()
