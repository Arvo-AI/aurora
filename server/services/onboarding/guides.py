"""The people who look after new accounts, from ``ONBOARDING_GUIDES``.

``ONBOARDING_GUIDES`` is a JSON list; names and addresses live in deployment
config, not in this repo::

    [{"key": "sam", "name": "Sam Doe", "email": "sam@example.com",
      "title": "Co-founder", "avatar_url": "https://...", "booking_url": "https://..."}]

Only ``key``, ``name`` and ``email`` are required.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Guide:
    key: str
    name: str
    email: str
    title: str = ""
    avatar_url: str = ""
    booking_url: str = ""

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name.split() else self.name

    def public(self) -> dict:
        return {
            "name": self.name,
            "first_name": self.first_name,
            "title": self.title or None,
            "avatar_url": self.avatar_url or None,
            "booking_url": self.booking_url or None,
            "email": self.email,
        }


def _https_or_blank(url: object) -> str:
    # These end up in hrefs and <img src>; nothing but https gets through.
    return url if isinstance(url, str) and url.startswith("https://") else ""


def load_guides(raw: str | None = None) -> list[Guide]:
    raw = os.getenv("ONBOARDING_GUIDES", "") if raw is None else raw
    if not raw.strip():
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        logger.error("[ONBOARDING] ONBOARDING_GUIDES is not valid JSON; no guides")
        return []
    guides: list[Guide] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        key, name, email = item.get("key"), item.get("name"), item.get("email")
        if not (isinstance(key, str) and isinstance(name, str) and isinstance(email, str) and "@" in email):
            logger.warning("[ONBOARDING] skipping a guide without key, name and email")
            continue
        guides.append(Guide(
            key=key.strip()[:64],
            name=name.strip(),
            email=email.strip(),
            title=str(item.get("title") or "").strip(),
            avatar_url=_https_or_blank(item.get("avatar_url")),
            booking_url=_https_or_blank(item.get("booking_url")),
        ))
    return guides


def by_key(key: str | None) -> Guide | None:
    return next((g for g in load_guides() if g.key == key), None)


def pick(counts: dict[str, int]) -> Guide | None:
    """The guide looking after the fewest accounts; ties go to list order."""
    guides = load_guides()
    if not guides:
        return None
    return min(guides, key=lambda g: (counts.get(g.key, 0), guides.index(g)))
