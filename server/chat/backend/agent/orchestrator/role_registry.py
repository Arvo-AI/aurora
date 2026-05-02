"""Singleton registry that loads and validates role .md files at startup."""

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_ROLES_DIR = Path(__file__).parent / "roles"
_REQUIRED_FRONTMATTER_KEYS = frozenset(
    {"name", "description", "tools", "max_turns", "max_seconds", "rca_priority"}
)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class RoleMeta:
    name: str
    description: str
    tools: list  # capability tags
    max_turns: int
    max_seconds: int
    rca_priority: int
    model: Optional[str]  # None → falls back to MAIN_MODEL
    body: str             # markdown after frontmatter


class RoleRegistry:
    _instance: Optional["RoleRegistry"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._roles: dict = {}
        self._load()

    @classmethod
    def get_instance(cls) -> "RoleRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _load(self) -> None:
        if not _ROLES_DIR.is_dir():
            logger.warning("RoleRegistry: roles directory not found at %s", _ROLES_DIR)
            return

        for md_file in sorted(_ROLES_DIR.glob("*.md")):
            try:
                raw = md_file.read_text(encoding="utf-8")
                match = _FRONTMATTER_RE.match(raw)
                if not match:
                    logger.warning("RoleRegistry: %s has no frontmatter — skipping", md_file.name)
                    continue
                meta = yaml.safe_load(match.group(1))
                if not isinstance(meta, dict):
                    logger.warning("RoleRegistry: %s frontmatter is not a mapping — skipping", md_file.name)
                    continue
                missing = _REQUIRED_FRONTMATTER_KEYS - meta.keys()
                if missing:
                    logger.warning(
                        "RoleRegistry: %s missing keys %s — skipping", md_file.name, missing
                    )
                    continue
                body = raw[match.end():]
                role = RoleMeta(
                    name=str(meta["name"]),
                    description=str(meta["description"]),
                    tools=list(meta.get("tools") or []),
                    max_turns=int(meta.get("max_turns", 8)),
                    max_seconds=int(meta.get("max_seconds", 180)),
                    rca_priority=int(meta.get("rca_priority", 99)),
                    model=meta.get("model") or None,
                    body=body.strip(),
                )
                self._roles[role.name] = role
                logger.info("RoleRegistry: loaded role %r", role.name)
            except Exception:
                logger.exception("RoleRegistry: failed to load %s", md_file.name)

    def list_all(self) -> list:
        return sorted(self._roles.values(), key=lambda r: r.rca_priority)

    def get(self, name: str) -> Optional[RoleMeta]:
        return self._roles.get(name)

    def list_available_roles(self, user_id: str) -> list:
        from chat.backend.agent.orchestrator.select_skills import get_available_capability_tags
        available_tags = get_available_capability_tags(user_id)
        result = []
        for role in self.list_all():
            if any(tag in available_tags for tag in role.tools):
                result.append(role)
        return result
