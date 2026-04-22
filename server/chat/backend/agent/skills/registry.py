"""
SkillRegistry — discovers, checks connectivity, and loads integration skill files.

Thread-safe singleton. Skill metadata is parsed once at startup.
Content is cached for performance.
"""

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .loader import (
    SkillLoadResult,
    SkillMetadata,
    discover_skill_files,
    estimate_tokens,
    parse_skill_file,
    resolve_template,
)

logger = logging.getLogger(__name__)

INTEGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "integrations")
RCA_DIR = os.path.join(os.path.dirname(__file__), "rca")

def _get_rca_token_budget() -> int:
    """Resolve RCA skill token budget from env, with a safe default."""
    raw = (
        os.getenv("RCA_SKILLS_TOKEN_BUDGET")
        or os.getenv("RCA_TOKEN_BUDGET")
        or ""
    ).strip()
    if not raw:
        return 12000

    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            f"Invalid RCA token budget '{raw}' — using default (12000)"
        )
        return 12000

    if value < 500:
        logger.warning(
            f"RCA token budget too low ({value}) — using minimum (500)"
        )
        return 500
    return value


# Default token budget for auto-loaded RCA skills
RCA_TOKEN_BUDGET = _get_rca_token_budget()


class SkillRegistry:
    """
    Discovers skill files at startup, checks integration connectivity,
    and provides on-demand loading for the agent.
    """

    _instance: Optional["SkillRegistry"] = None
    _lock = threading.Lock()

    _CONNECTION_CACHE_TTL = 30  # seconds

    def __init__(self) -> None:
        self._skills: Dict[str, SkillMetadata] = {}
        self._bodies: Dict[str, str] = {}  # skill_id -> raw body (pre-template)
        self._rca_skills: Dict[str, SkillMetadata] = {}
        self._rca_bodies: Dict[str, str] = {}
        self._tool_to_skill: Dict[str, str] = {}  # tool_name -> skill_id
        self._connection_cache: Dict[Tuple[str, str], Tuple[float, bool, Dict[str, Any]]] = {}
        self._discover()

    @classmethod
    def get_instance(cls) -> "SkillRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = SkillRegistry()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton (for testing)."""
        with cls._lock:
            cls._instance = None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _discover(self) -> None:
        """Parse all .md files in integrations/ and rca/ directories."""
        for path in discover_skill_files(INTEGRATIONS_DIR):
            meta, body = parse_skill_file(path)
            if meta:
                self._skills[meta.id] = meta
                self._bodies[meta.id] = body
                for tool_name in meta.tools:
                    self._tool_to_skill[tool_name] = meta.id
                logger.info(f"Discovered skill: {meta.id} ({meta.name})")

        for path in discover_skill_files(RCA_DIR):
            meta, body = parse_skill_file(path)
            if meta:
                self._rca_skills[meta.id] = meta
                self._rca_bodies[meta.id] = body
                logger.info(f"Discovered RCA skill: {meta.id} ({meta.name})")

    # ------------------------------------------------------------------
    # Connection checking
    # ------------------------------------------------------------------

    def check_connection(
        self, skill_id: str, user_id: str
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Check if an integration is connected for a user.

        Returns (is_connected, context_data).
        context_data contains template variables (e.g. username).
        Results are cached per (user_id, skill_id) for up to _CONNECTION_CACHE_TTL seconds.
        """
        cache_key = (user_id, skill_id)
        cached = self._connection_cache.get(cache_key)
        if cached is not None:
            ts, is_conn, ctx = cached
            if time.monotonic() - ts < self._CONNECTION_CACHE_TTL:
                return is_conn, ctx

        meta = self._skills.get(skill_id) or self._rca_skills.get(skill_id)
        if not meta:
            return False, {}

        check = meta.connection_check
        if not check:
            logger.debug(f"Skill '{skill_id}' has no connection_check — treating as disconnected")
            return False, {}
        if not isinstance(check, dict):
            logger.warning(
                f"Skill '{skill_id}' has invalid connection_check (expected dict, got {type(check)})"
            )
            return False, {}

        method = check.get("method", "")

        try:
            is_connected, ctx_data = self._dispatch_check(skill_id, method, check, user_id)
            self._connection_cache[cache_key] = (time.monotonic(), is_connected, ctx_data)
            return is_connected, ctx_data
        except Exception as e:
            logger.warning(f"Connection check failed for {skill_id}: {e}")
            return False, {}

    def _dispatch_check(
        self, skill_id: str, method: str, check: Dict[str, Any], user_id: str
    ) -> Tuple[bool, Dict[str, Any]]:
        """Dispatch to the correct connection check function."""
        provider_key = check.get("provider_key", "")

        def _has_required_fields(creds: Any) -> bool:
            if not isinstance(creds, dict):
                return False

            required_field = check.get("required_field")
            if required_field:
                if isinstance(required_field, str):
                    return bool(creds.get(required_field))
                else:
                    logger.warning(
                        f"Invalid required_field type for skill '{skill_id}': {type(required_field)}"
                    )
                    return False

            required_any_fields = check.get("required_any_fields")
            if required_any_fields:
                if isinstance(required_any_fields, list):
                    return any(creds.get(str(field)) for field in required_any_fields)
                logger.warning(
                    f"Invalid required_any_fields type for skill '{skill_id}': {type(required_any_fields)}"
                )
                return False

            return bool(creds)

        if method == "get_credentials_from_db":
            from utils.auth.stateless_auth import get_credentials_from_db

            creds = get_credentials_from_db(user_id, provider_key)
            if _has_required_fields(creds):
                # Enrich with extra context for specific skills
                if skill_id == "bitbucket":
                    creds.update(self._get_bitbucket_workspace_context(user_id))
                return True, creds
            return False, {}

        elif method == "get_token_data":
            # Check feature flag first if specified
            feature_flag = check.get("feature_flag")
            if feature_flag:
                if not self._check_feature_flag(feature_flag):
                    return False, {}

            from utils.auth.token_management import get_token_data

            creds = get_token_data(user_id, provider_key)
            if _has_required_fields(creds):
                return True, creds
            return False, {}

        elif method == "is_connected_function":
            module_path = check.get("module", "")
            func_name = check.get("function", "")
            if not module_path or not func_name:
                return False, {}

            # Only allow imports from known safe module paths
            _ALLOWED_PREFIXES = (
                "chat.backend.agent.tools.",
                "utils.auth.",
                "utils.flags.",
            )
            if not any(module_path.startswith(p) for p in _ALLOWED_PREFIXES):
                logger.warning(
                    f"Blocked import of untrusted module '{module_path}' "
                    f"in skill connection check. Allowed prefixes: {_ALLOWED_PREFIXES}"
                )
                return False, {}

            import importlib

            mod = importlib.import_module(module_path)
            func = getattr(mod, func_name)
            connected = func(user_id)
            ctx_data: Dict[str, Any] = {}

            # Build extra context for specific skills
            if bool(connected) and skill_id == "kubectl_onprem":
                ctx_data = self._get_kubectl_onprem_context(user_id)

            return bool(connected), ctx_data

        elif method == "provider_in_preference":
            # For provider-bound skills (e.g., ovh/scaleway/tailscale/grafana),
            # only load if the provider is actually connected for this user.
            from utils.auth.stateless_auth import get_connected_providers

            target_provider = (
                str(
                    check.get("provider_key")
                    or check.get("provider")
                    or skill_id
                )
                .strip()
                .lower()
            )
            connected = [
                str(p).strip().lower()
                for p in (get_connected_providers(user_id) or [])
                if p
            ]
            return target_provider in connected, {}

        elif method == "always":
            return True, {}

        else:
            logger.warning(f"Unknown connection check method: {method}")
            return False, {}

    @staticmethod
    def _check_feature_flag(flag_name: str) -> bool:
        """Check a feature flag by name."""
        try:
            from utils.flags import feature_flags

            fn = getattr(feature_flags, flag_name, None)
            if not callable(fn):
                logger.warning(f"Unknown feature flag function '{flag_name}'")
                return False
            return bool(fn())
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Index building
    # ------------------------------------------------------------------

    def get_connected_skills(self, user_id: str) -> List[SkillMetadata]:
        """Return metadata for all skills whose integration is connected."""
        connected = []
        for skill_id, meta in self._skills.items():
            is_connected, _ = self.check_connection(skill_id, user_id)
            if is_connected:
                connected.append(meta)
        return connected

    def get_connected_skill_ids(self, user_id: str) -> List[str]:
        """Return IDs for all connected integration skills."""
        return [meta.id for meta in self.get_connected_skills(user_id)]

    def build_index(self, user_id: str) -> str:
        """
        Build the compact always-loaded index string (~300 tokens).
        Only includes connected integrations.
        """
        connected = self.get_connected_skills(user_id)
        if not connected:
            return ""

        lines = [
            "CONNECTED INTEGRATIONS (MANDATORY: call load_skill('id') BEFORE using ANY integration tool below):",
            "You MUST call load_skill first to get the workflow, syntax, and constraints. Using tools without loading the skill first will produce wrong results.",
            "",
        ]
        for meta in sorted(connected, key=lambda m: m.name):
            if meta.tools:
                tools_str = ", ".join(meta.tools[:4])
                if len(meta.tools) > 4:
                    tools_str += ", ..."
                lines.append(f"- {meta.id}: {meta.index} [tools: {tools_str}]")
            else:
                lines.append(f"- {meta.id}: {meta.index}")

            # Inline dynamic context the LLM needs at tool-call time, so it
            # doesn't have to call load_skill first. Kept short to stay within
            # the ~300-token budget for this always-loaded index. Reuses the
            # ctx_data that check_connection already cached (30s TTL).
            if meta.id == "kubectl_onprem":
                _, ctx = self.check_connection(meta.id, user_id)
                if ctx.get("has_clusters") and ctx.get("cluster_list"):
                    lines.append(f"  Connected clusters:\n{ctx['cluster_list']}")

        lines.append("")
        return "\n".join(lines)

    def load_skills_for_chat(self, user_id: str) -> str:
        """Auto-load full skill content for all connected integrations.

        Used in interactive chat so the agent has detailed guidance
        without needing to call load_skill explicitly.
        """
        connected = self.get_connected_skills(user_id)
        if not connected:
            return ""

        parts: List[str] = ["CONNECTED INTEGRATIONS:"]
        for meta in sorted(connected, key=lambda m: m.name):
            result = self.load_skill(meta.id, user_id)
            if result.is_connected and result.content:
                parts.append(result.content)

        return "\n\n".join(parts) if len(parts) > 1 else ""

    # ------------------------------------------------------------------
    # Skill loading
    # ------------------------------------------------------------------

    def load_skill(
        self,
        skill_id: str,
        user_id: str,
        extra_context: Optional[Dict[str, Any]] = None,
        _prevalidated_context: Optional[Dict[str, Any]] = None,
    ) -> SkillLoadResult:
        """
        Load a skill's full content, resolving template variables.

        extra_context: additional template variables (e.g. service_name,
        recent_deploys_section) merged on top of connection data.
        _prevalidated_context: if provided, skip the connection check (caller
        already verified connectivity). Value is the connection context dict.
        """
        meta = self._skills.get(skill_id)
        body = self._bodies.get(skill_id)

        if not meta or body is None:
            return SkillLoadResult(
                skill_id=skill_id,
                name=skill_id,
                content=f"Unknown skill: '{skill_id}'",
                token_estimate=0,
                tools=[],
                is_connected=False,
            )

        if _prevalidated_context is not None:
            is_connected = True
            context = _prevalidated_context
        else:
            is_connected, context = self.check_connection(skill_id, user_id)

        if not is_connected:
            return SkillLoadResult(
                skill_id=skill_id,
                name=meta.name,
                content=f"Integration '{meta.name}' is not connected for this user.",
                token_estimate=0,
                tools=meta.tools,
                is_connected=False,
            )

        if extra_context:
            context = {**context, **extra_context}
        rendered = resolve_template(body, context)
        return SkillLoadResult(
            skill_id=skill_id,
            name=meta.name,
            content=rendered,
            token_estimate=estimate_tokens(rendered),
            tools=meta.tools,
            is_connected=True,
        )

    def load_skills_for_rca(
        self,
        user_id: str,
        source: str,
        providers: List[str],
        integrations: Dict[str, bool],
        alert_details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Auto-load relevant skills for RCA mode.

        Loads integration skills that are connected + RCA provider skills
        for connected cloud providers. Respects a token budget.

        alert_details: alert payload used to derive dynamic template variables
        (service_name, recent_deploys, etc.)
        """
        parts: List[str] = []
        tokens_used = 0

        # Build dynamic context from alert details
        extra_ctx = self._build_rca_context(
            user_id, source, alert_details or {}, integrations
        )

        # 1. Load connected integration skills, ordered by rca_priority
        connected: List[Tuple[SkillMetadata, Dict[str, Any]]] = []
        for skill_id, meta in self._skills.items():
            if integrations.get(skill_id, False):
                _, ctx_data = self.check_connection(skill_id, user_id)
                connected.append((meta, ctx_data))
            else:
                is_conn, ctx_data = self.check_connection(skill_id, user_id)
                if is_conn:
                    connected.append((meta, ctx_data))

        connected.sort(key=lambda pair: pair[0].rca_priority)

        loaded_ids: set = set()
        for meta, ctx_data in connected:
            if tokens_used >= RCA_TOKEN_BUDGET:
                remaining = [m.id for m, _ in connected if m.id not in loaded_ids]
                if remaining:
                    parts.append(
                        f"\n(Token budget reached. Additional integrations via load_skill: {', '.join(remaining)})"
                    )
                break

            result = self.load_skill(
                meta.id, user_id,
                extra_context=extra_ctx,
                _prevalidated_context=ctx_data,
            )
            if result.is_connected and result.content:
                parts.append(result.content)
                tokens_used += result.token_estimate
                loaded_ids.add(meta.id)

        # 2. Load RCA provider investigation skills for connected cloud providers
        providers_lower = [p.lower() for p in providers] if providers else []
        for provider in providers_lower:
            rca_id = f"provider_{provider}"
            if rca_id in self._rca_skills and tokens_used < RCA_TOKEN_BUDGET:
                body = self._rca_bodies.get(rca_id, "")
                if body:
                    parts.append(resolve_template(body, extra_ctx))
                    tokens_used += estimate_tokens(body)

        # 3. Load general RCA skills (k8s, ssh) if any cloud provider is connected
        if providers_lower:
            for general_id in ("general_k8s", "ssh_investigation"):
                if general_id in self._rca_skills and tokens_used < RCA_TOKEN_BUDGET:
                    body = self._rca_bodies.get(general_id, "")
                    if body:
                        rendered = resolve_template(body, extra_ctx)
                        parts.append(rendered)
                        tokens_used += estimate_tokens(rendered)

        return "\n\n".join(parts) if parts else ""

    # ------------------------------------------------------------------
    # Dynamic RCA context
    # ------------------------------------------------------------------

    @staticmethod
    def _get_kubectl_onprem_context(user_id: str) -> Dict[str, Any]:
        """Fetch connected on-prem cluster names and IDs for template rendering."""
        try:
            from utils.db.connection_pool import db_pool
            from utils.auth.stateless_auth import set_rls_context

            with db_pool.get_user_connection() as conn:
                with conn.cursor() as cur:
                    set_rls_context(cur, conn, user_id, log_prefix="[SkillRegistry:kubectl]")
                    cur.execute(
                        """SELECT c.cluster_id, t.cluster_name
                           FROM active_kubectl_connections c
                           JOIN kubectl_agent_tokens t ON c.token = t.token
                           WHERE t.user_id = %s AND c.status = 'active'
                           ORDER BY t.cluster_name""",
                        (user_id,),
                    )
                    rows = cur.fetchall()

            if rows:
                lines = [f"- {name} (cluster_id: `{cid}`)" for cid, name in rows]
                return {"cluster_list": "\n".join(lines), "has_clusters": True}
            return {"cluster_list": "(no active clusters)", "has_clusters": False}
        except Exception as e:
            logger.warning(f"Failed to fetch kubectl on-prem clusters: {e}")
            return {"cluster_list": "(cluster data unavailable)", "has_clusters": False}

    @staticmethod
    def _get_bitbucket_workspace_context(user_id: str) -> Dict[str, Any]:
        """Fetch the user's Bitbucket workspace selection for template rendering."""
        try:
            from utils.auth.stateless_auth import get_credentials_from_db

            # Fetch display_name from bitbucket credentials
            bb_creds = get_credentials_from_db(user_id, "bitbucket") or {}
            display_name = bb_creds.get("display_name", "")

            selection = get_credentials_from_db(user_id, "bitbucket_workspace_selection") or {}
            ws = selection.get("workspace")
            repo = selection.get("repository")
            branch = selection.get("branch")

            # workspace/repository may be dicts with slug/name keys or plain strings
            ws_slug = ws.get("slug", ws) if isinstance(ws, dict) else (ws or "")
            repo_name = repo.get("name", repo) if isinstance(repo, dict) else (repo or "")
            branch_name = branch.get("name", branch) if isinstance(branch, dict) else (branch or "")

            return {
                "display_name": display_name or "(unknown)",
                "workspace_slug": ws_slug or "(not selected)",
                "repo_name": repo_name or "(not selected)",
                "branch_name": branch_name or "(not selected)",
            }
        except Exception as e:
            logger.warning(f"Failed to fetch bitbucket workspace selection: {e}")
            return {
                "display_name": "(unavailable)",
                "workspace_slug": "(unavailable)",
                "repo_name": "(unavailable)",
                "branch_name": "(unavailable)",
            }

    @staticmethod
    def _build_rca_context(
        user_id: str,
        source: str,
        alert_details: Dict[str, Any],
        integrations: Dict[str, bool],
    ) -> Dict[str, Any]:
        """
        Build dynamic template variables for RCA skill rendering.

        Returns a dict with keys like service_name, recent_deploys_section
        that get substituted into skill templates.
        """
        ctx: Dict[str, Any] = {}

        # Service name from alert
        service_name = alert_details.get("labels", {}).get("service", "") or alert_details.get("title", "")
        if source == "netdata":
            service_name = alert_details.get("host", "") or service_name
        ctx["service_name"] = service_name

        # Escaped service name for JQL queries
        escaped = service_name.replace("\\", "\\\\").replace('"', '\\"')
        ctx["escaped_service"] = escaped

        # Recent Jenkins/CloudBees deployments
        for provider_key in ("jenkins", "cloudbees"):
            if integrations.get(provider_key):
                try:
                    deploys = SkillRegistry._get_recent_deploys(user_id, service_name, provider_key)
                    section = SkillRegistry._format_deploys(deploys)
                    ctx[f"{provider_key}_deploys_section"] = section
                except Exception as e:
                    logger.warning(f"Failed to fetch {provider_key} deployments: {e}")
                    ctx[f"{provider_key}_deploys_section"] = "(deployment data unavailable)"

        # Jira mode
        ctx["jira_mode"] = integrations.get("jira_mode", "comment_only")

        # Bitbucket workspace selection
        if integrations.get("bitbucket"):
            ctx.update(SkillRegistry._get_bitbucket_workspace_context(user_id))

        # kubectl on-prem cluster list
        if integrations.get("kubectl_onprem"):
            ctx.update(SkillRegistry._get_kubectl_onprem_context(user_id))

        return ctx

    @staticmethod
    def _get_recent_deploys(user_id: str, service_name: str, provider: str) -> List[Dict]:
        """Fetch recent deployment records from the database."""
        try:
            from utils.db.connection_pool import db_pool
            from utils.auth.stateless_auth import set_rls_context

            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    set_rls_context(cur, conn, user_id, log_prefix="[SkillRegistry:_get_recent_deploys]")
                    cur.execute(
                        """SELECT service, environment, result, build_number,
                                  commit_sha, trace_id, received_at
                           FROM jenkins_deployment_events
                           WHERE user_id = %s AND provider = %s
                                 AND received_at >= NOW() - INTERVAL '24 hours'
                           ORDER BY received_at DESC
                           LIMIT 10""",
                        (user_id, provider),
                    )
                    rows = cur.fetchall()

            return [
                {
                    "service": r[0],
                    "environment": r[1],
                    "result": r[2],
                    "build_number": r[3],
                    "commit_sha": r[4],
                    "trace_id": r[5],
                    "received_at": str(r[6]) if r[6] else "?",
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"Failed to fetch recent deploys for provider={provider}: {e}")
            return []

    @staticmethod
    def _format_deploys(deploys: List[Dict]) -> str:
        """Format deployment records for template injection."""
        if not deploys:
            return "No recent deployments found in the last 24 hours."

        lines = ["Recent deployments (potential change correlation):"]
        for dep in deploys:
            sha = (dep.get("commit_sha") or "?")[:8]
            lines.append(
                f"- [{dep['result']}] {dep['service']} -> {dep.get('environment', '?')} "
                f"at {dep['received_at']} (commit: {sha}, build: #{dep.get('build_number', '?')})"
            )
            if dep.get("trace_id"):
                lines.append(f"  OTel Trace ID: {dep['trace_id']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Reverse lookups
    # ------------------------------------------------------------------

    def get_skill_for_tool(self, tool_name: str) -> Optional[str]:
        """Given a tool name, return the skill_id that governs it."""
        return self._tool_to_skill.get(tool_name)

    def get_all_skill_ids(self) -> List[str]:
        """Return all registered skill IDs."""
        return list(self._skills.keys())
