"""SkillStore — singleton that reads skills from the DB and serves them to the agent."""

from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300  # 5 minutes


@dataclass
class SkillMeta:
    id: str
    name: str
    description: str
    tags: list
    providers: list
    mode_restriction: Optional[str]
    prompt_behavior: str
    scope: str
    version: str


def _set_rls(cur, conn, user_id: str, org_id: str) -> None:
    cur.execute("SET myapp.current_user_id = %s;", (user_id,))
    cur.execute("SET myapp.current_org_id = %s;", (org_id or "",))
    conn.commit()


class SkillStore:
    _instance: Optional["SkillStore"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._catalog_cache: Dict[Tuple, Tuple[float, List[SkillMeta]]] = {}

    @classmethod
    def get_instance(cls) -> "SkillStore":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def invalidate_cache(self) -> None:
        self._catalog_cache.clear()

    def get_catalog(
        self,
        user_id: str,
        org_id: str,
        providers: Optional[List[str]] = None,
        mode: Optional[str] = None,
    ) -> List[SkillMeta]:
        cache_key = (user_id, org_id)
        now = time.time()
        cached = self._catalog_cache.get(cache_key)
        if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
            entries = cached[1]
        else:
            entries = self._query_catalog(user_id, org_id)
            self._catalog_cache[cache_key] = (now, entries)

        if not providers and not mode:
            return entries

        result = entries
        if providers:
            pl = [p.lower() for p in providers]
            result = [
                s for s in result
                if not s.providers or any(p.lower() in pl for p in s.providers)
            ]
        if mode:
            result = [
                s for s in result
                if s.mode_restriction is None or s.mode_restriction == mode
            ]
        return result

    def _query_catalog(self, user_id: str, org_id: str) -> List[SkillMeta]:
        from utils.db.connection_pool import db_pool

        sql = """
            SELECT id, name, description, tags, providers,
                   mode_restriction, prompt_behavior, scope, version
            FROM skills
            WHERE is_active = true
              AND (
                scope = 'global'
                OR (scope = 'org' AND org_id = %s)
                OR (scope = 'user' AND user_id = %s)
              )
            ORDER BY scope, name;
        """
        try:
            with db_pool.get_user_connection() as conn:
                with conn.cursor() as cur:
                    _set_rls(cur, conn, user_id, org_id)
                    cur.execute(sql, (org_id, user_id))
                    rows = cur.fetchall()
        except Exception as e:
            logger.warning(f"[SkillStore] Error querying catalog: {e}")
            return []

        return [
            SkillMeta(
                id=str(row[0]),
                name=row[1],
                description=row[2],
                tags=row[3] if row[3] else [],
                providers=row[4] if row[4] else [],
                mode_restriction=row[5],
                prompt_behavior=row[6],
                scope=row[7],
                version=row[8],
            )
            for row in rows
        ]

    def build_catalog_prompt(
        self,
        user_id: str,
        org_id: str,
        providers: Optional[List[str]] = None,
        mode: Optional[str] = None,
    ) -> str:
        catalog = self.get_catalog(user_id, org_id, providers, mode)
        if not catalog:
            return ""

        lines = [
            "========================================",
            "SKILLS CATALOG (load with load_skill tool)",
            "========================================",
            "Available procedural skills for complex workflows. Load a skill ONLY when you need",
            "step-by-step guidance for a specific task. Do not load skills speculatively.",
            "",
        ]
        for s in catalog:
            lines.append(f"- {s.name}: {s.description}")
        lines.append("")
        lines.append('To use: call load_skill(skill_name="<name>") to get full instructions.')
        lines.append("========================================")
        return "\n".join(lines)

    def get_skill_body(
        self, skill_name: str, user_id: str, org_id: str
    ) -> Optional[str]:
        from utils.db.connection_pool import db_pool

        sql = """
            SELECT body, prompt_behavior
            FROM skills
            WHERE name = %s AND is_active = true
              AND (
                scope = 'global'
                OR (scope = 'org' AND org_id = %s)
                OR (scope = 'user' AND user_id = %s)
              )
            LIMIT 1;
        """
        try:
            with db_pool.get_user_connection() as conn:
                with conn.cursor() as cur:
                    _set_rls(cur, conn, user_id, org_id)
                    cur.execute(sql, (skill_name, org_id, user_id))
                    row = cur.fetchone()
        except Exception as e:
            logger.warning(f"[SkillStore] Error fetching skill body for '{skill_name}': {e}")
            return None

        if not row:
            return None

        body, prompt_behavior = row[0], row[1]

        if prompt_behavior == "override":
            return (
                "SKILL OVERRIDE: Follow these instructions. Where they conflict "
                "with your base instructions for this domain, prefer the skill.\n\n"
                + body
            )
        elif prompt_behavior == "exclusive":
            return (
                "EXCLUSIVE SKILL: For this domain, follow ONLY these instructions. "
                "Ignore base prompt context for this domain.\n\n"
                + body
            )
        return body

    def get_skill_resource(
        self, skill_name: str, filename: str, user_id: str, org_id: str
    ) -> Optional[str]:
        from utils.db.connection_pool import db_pool

        sql = """
            SELECT references_data
            FROM skills
            WHERE name = %s AND is_active = true
              AND (
                scope = 'global'
                OR (scope = 'org' AND org_id = %s)
                OR (scope = 'user' AND user_id = %s)
              )
            LIMIT 1;
        """
        try:
            with db_pool.get_user_connection() as conn:
                with conn.cursor() as cur:
                    _set_rls(cur, conn, user_id, org_id)
                    cur.execute(sql, (skill_name, org_id, user_id))
                    row = cur.fetchone()
        except Exception as e:
            logger.warning(f"[SkillStore] Error fetching resource for '{skill_name}': {e}")
            return None

        if not row or not row[0]:
            return None

        refs = row[0] if isinstance(row[0], dict) else {}
        return refs.get(filename)
