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
example behind a proxy. The github-mcp-server child only takes a host
(``GITHUB_HOST``), so the override does not reach it.

Both settings must be https: credentials are sent to these hosts.
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
_GITHUB_COM_AUTHORITIES = ("github.com", "www.github.com")
# An App's page sits under one of these path segments: ``/apps/<slug>`` on
# github.com, ``/apps/<owner>/<slug>`` on GHE.com, ``/github-apps/<slug>`` on
# GitHub Enterprise Server.
_APP_PATH_SEGMENTS = ("apps", "github-apps")


def _app_setting() -> str:
    return (os.getenv(_SLUG_ENV) or "").strip()


def _parse_https_url(raw: str) -> tuple[str, str] | None:
    """``(authority, path)`` for an https URL with a host, else None.

    Credentials (App JWTs, installation tokens, the OAuth client secret) travel
    to these hosts, so plain http is refused, and so is userinfo
    (``https://user@host``), which would otherwise ride along in every request.
    The authority is rebuilt from the hostname and port so nothing else leaks
    into derived URLs.
    """
    if not raw.lower().startswith("https://"):
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname or parts.username is not None or parts.password is not None:
        return None
    authority = parts.hostname.lower() + (f":{port}" if port else "")
    return authority, parts.path.rstrip("/")


def _app_url_parts() -> tuple[str, str] | None:
    """Parsed app URL when the setting is a URL, else None (bare slug or unset)."""
    raw = _app_setting()
    if "://" not in raw:
        return None
    parsed = _parse_https_url(raw)
    if parsed is None:
        logger.warning(
            "[GITHUB-HOST] %s must be an https URL without credentials; using github.com",
            _SLUG_ENV,
        )
        return None
    authority, path = parsed
    if authority in _GITHUB_COM_AUTHORITIES:
        authority = "github.com"
    return authority, path


def github_web_url() -> str:
    """Origin of the GitHub web UI, without a trailing slash."""
    parts = _app_url_parts()
    if parts is None:
        return GITHUB_COM_WEB_URL
    return f"https://{parts[0]}"


def github_api_url() -> str:
    """Base URL of the GitHub REST API, without a trailing slash."""
    override = (os.getenv(_API_OVERRIDE_ENV) or "").strip()
    if override:
        parsed = _parse_https_url(override)
        if parsed is not None:
            return f"https://{parsed[0]}{parsed[1]}"
        logger.warning(
            "[GITHUB-HOST] %s must be an https URL without credentials; ignoring it",
            _API_OVERRIDE_ENV,
        )

    parts = _app_url_parts()
    if parts is None:
        return GITHUB_COM_API_URL
    authority = parts[0]
    host = authority.split(":", 1)[0]
    if host == "github.com":
        return GITHUB_COM_API_URL
    if host.endswith(".ghe.com"):
        return f"https://api.{authority}"
    return f"https://{authority}/api/v3"


def is_github_com() -> bool:
    """True when the web host is github.com."""
    return github_web_url() == GITHUB_COM_WEB_URL


def _app_page_path(path: str) -> str | None:
    """The App page path, trimmed of a pasted ``/installations/new``.

    None when the path does not name an App (``/apps/...`` or
    ``/github-apps/...``), so a bare host is reported instead of producing a
    broken install link.
    """
    segments = [seg for seg in path.split("/") if seg]
    if segments[-2:] == ["installations", "new"]:
        segments = segments[:-2]
    for i, seg in enumerate(segments):
        if seg in _APP_PATH_SEGMENTS and len(segments) > i + 1:
            return "/" + "/".join(segments)
    return None


def github_app_page_url() -> str | None:
    """The App's public page, or None when it is not configured or not usable."""
    raw = _app_setting()
    if not raw:
        return None
    if "://" in raw:
        parts = _app_url_parts()
        if parts is None:
            return None
        authority, path = parts
        page_path = _app_page_path(path)
        if page_path is None:
            logger.warning(
                "[GITHUB-HOST] %s does not point at a GitHub App page "
                "(expected .../apps/<name> or .../github-apps/<name>)",
                _SLUG_ENV,
            )
            return None
        return f"https://{authority}{page_path}"
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


def github_oauth_token(creds: dict | None) -> str | None:
    """A stored OAuth ``access_token``, only if the configured host issued it.

    The token is saved with the ``api_url`` it came from. A token from another
    host (the deployment was repointed, say from github.com to an enterprise
    host) must never be sent to the new one, so it is treated as absent and the
    user reconnects. Records saved before ``api_url`` existed all came from
    github.com, which was the only host then.
    """
    token = (creds or {}).get("access_token")
    if not token:
        return None
    issued_by = ((creds or {}).get("api_url") or GITHUB_COM_API_URL).rstrip("/")
    if issued_by != github_api_url():
        logger.warning(
            "[GITHUB-HOST] ignoring a stored GitHub OAuth token issued by another host"
        )
        return None
    return token
