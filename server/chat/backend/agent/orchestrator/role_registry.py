"""Singleton registry that loads and validates role .md files at startup."""

import logging
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
def _split_frontmatter(text: str) -> Optional[tuple[str, str]]:
    """Linear-time split of a `---`-fenced YAML frontmatter from a markdown body.

    Replaces a regex (``^---\\s*\\n(.*?)\\n---\\s*\\n``) flagged by SonarQube as
    backtracking-prone (S5852). Returns ``(yaml_text, body_after_fence)`` if the
    text opens with a ``---`` line and contains a closing ``---`` line; else None.
    """
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None


@dataclass
class RoleMeta:
    name: str
    description: str
    tools: list[str]  # capability tags
    max_turns: int
    max_seconds: int
    rca_priority: int
    model: Optional[str]  # None → falls back to ModelConfig.RCA_SUBAGENT_MODEL
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
                parts = _split_frontmatter(raw)
                if parts is None:
                    logger.warning("RoleRegistry: %s has no frontmatter — skipping", md_file.name)
                    continue
                yaml_text, body = parts
                meta = yaml.safe_load(yaml_text)
                if not isinstance(meta, dict):
                    logger.warning("RoleRegistry: %s frontmatter is not a mapping — skipping", md_file.name)
                    continue
                missing = _REQUIRED_FRONTMATTER_KEYS - meta.keys()
                if missing:
                    logger.warning(
                        "RoleRegistry: %s missing keys %s — skipping", md_file.name, missing
                    )
                    continue
                role = RoleMeta(
                    name=str(meta["name"]),
                    description=str(meta["description"]),
                    tools=list(meta.get("tools") or []),
                    max_turns=int(meta["max_turns"]),
                    max_seconds=int(meta["max_seconds"]),
                    rca_priority=int(meta["rca_priority"]),
                    model=meta.get("model") or None,
                    body=body.strip(),
                )
                self._roles[role.name] = role
                logger.info("RoleRegistry: loaded role %r", role.name)
            except Exception:
                logger.exception("RoleRegistry: failed to load %s", md_file.name)

    def list_all(self) -> list[RoleMeta]:
        return sorted(self._roles.values(), key=lambda r: r.rca_priority)

    def get(self, name: str) -> Optional[RoleMeta]:
        return self._roles.get(name)

    def list_available_roles(self, user_id: str) -> list[RoleMeta]:
        """Return roles whose capability tags intersect the user's reachable tags.

        Per-user filtering: a role is included only if at least one of its
        ``tools`` (capability tags) is contributed by a tool the user can
        actually invoke (built-in, or skill-owned and connected).
        """
        from chat.backend.agent.orchestrator.select_skills import get_available_capability_tags
        available_tags = get_available_capability_tags(user_id)
        result = []
        for role in self.list_all():
            if any(tag in available_tags for tag in role.tools):
                result.append(role)
        return result
