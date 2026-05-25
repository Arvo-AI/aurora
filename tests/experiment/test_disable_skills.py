"""
Rigorous smoke tests for the DISABLE_SKILLS experiment toggle.

Run inside aurora-server container:
    docker exec -e DISABLE_SKILLS=true aurora-server python -m pytest \
        /app/../tests/experiment/test_disable_skills.py -v

(Or use the helper script: tests/experiment/run.sh)
"""
from __future__ import annotations

import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, "/app")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _force_reload_skills_modules():
    """Skills modules cache singletons; reload so env-var changes take effect."""
    for mod in [
        "chat.backend.agent.skills.loader",
        "chat.backend.agent.skills.registry",
        "chat.backend.agent.skills.load_skill_tool",
    ]:
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])


@pytest.fixture
def skills_on(monkeypatch):
    monkeypatch.delenv("DISABLE_SKILLS", raising=False)
    _force_reload_skills_modules()
    from chat.backend.agent.skills.registry import SkillRegistry
    SkillRegistry.reset()
    yield


@pytest.fixture
def skills_off(monkeypatch):
    monkeypatch.setenv("DISABLE_SKILLS", "true")
    _force_reload_skills_modules()
    from chat.backend.agent.skills.registry import SkillRegistry
    SkillRegistry.reset()
    yield


# ---------------------------------------------------------------------------
# 1. Env var reaches the process
# ---------------------------------------------------------------------------

class TestEnvVar:
    def test_disable_skills_set_in_container(self):
        assert os.getenv("DISABLE_SKILLS") == "true", (
            "DISABLE_SKILLS must be 'true' inside the container; "
            "the docker-compose common-env block should pass it through."
        )

    def test_skills_disabled_helper_truthy_values(self, monkeypatch):
        from chat.backend.agent.skills.loader import skills_disabled
        for val in ("1", "true", "TRUE", "yes", "on", "On"):
            monkeypatch.setenv("DISABLE_SKILLS", val)
            assert skills_disabled() is True, f"{val!r} should be truthy"

    def test_skills_disabled_helper_falsy_values(self, monkeypatch):
        from chat.backend.agent.skills.loader import skills_disabled
        for val in ("", "0", "false", "no", "off", "random"):
            monkeypatch.setenv("DISABLE_SKILLS", val)
            assert skills_disabled() is False, f"{val!r} should be falsy"
        monkeypatch.delenv("DISABLE_SKILLS", raising=False)
        assert skills_disabled() is False


# ---------------------------------------------------------------------------
# 2. Each gate fires when DISABLE_SKILLS=true
# ---------------------------------------------------------------------------

class TestSkillGates:
    def test_load_core_prompt_still_loads_when_disabled(self, skills_off):
        """load_core_prompt is also used for non-skill content (RCA segments,
        background scaffolding, agent identity). It MUST load unconditionally —
        the skill gate lives one level up, in callers that load integration
        content. Otherwise downstream lru_caches freeze empty strings."""
        from chat.backend.agent.skills.loader import load_core_prompt
        result = load_core_prompt("/app/chat/backend/agent/skills/core")
        assert len(result) > 1000, (
            "load_core_prompt must NOT be gated by DISABLE_SKILLS — that would "
            "strip identity/security/behavioral_rules and break downstream "
            "lru_cache callers."
        )

    def test_load_core_prompt_specific_segments_still_load(self, skills_off):
        from chat.backend.agent.skills.loader import load_core_prompt
        result = load_core_prompt(
            "/app/chat/backend/agent/skills/core",
            segments=["identity", "security"],
        )
        assert len(result) > 100, "identity + security must still load"

    def test_build_index_returns_empty(self, skills_off):
        from chat.backend.agent.skills.registry import SkillRegistry
        reg = SkillRegistry.get_instance()
        assert reg.build_index("any-user-id") == ""

    def test_load_skills_for_chat_returns_empty(self, skills_off):
        from chat.backend.agent.skills.registry import SkillRegistry
        reg = SkillRegistry.get_instance()
        assert reg.load_skills_for_chat("any-user-id") == ""

    def test_load_skills_for_rca_returns_empty(self, skills_off):
        from chat.backend.agent.skills.registry import SkillRegistry
        reg = SkillRegistry.get_instance()
        result = reg.load_skills_for_rca(
            "any-user-id", "datadog", ["aws", "gcp"], {"github": True, "datadog": True}
        )
        assert result == ""

    def test_load_skill_method_returns_empty_result(self, skills_off):
        from chat.backend.agent.skills.registry import SkillRegistry
        reg = SkillRegistry.get_instance()
        result = reg.load_skill("github", "any-user-id")
        assert result.content == ""
        assert result.is_connected is False
        assert result.token_estimate == 0

    def test_load_skill_llm_tool_returns_disabled_message(self, skills_off):
        from chat.backend.agent.skills.load_skill_tool import load_skill
        out = load_skill("github", user_id="any-user", session_id="s1")
        assert "disabled" in out.lower()

    def test_load_skills_for_role_returns_empty(self, skills_off):
        from chat.backend.agent.orchestrator.select_skills import load_skills_for_role

        class FakeRole:
            name = "analyzer"
            tools = ["datadog", "github"]

        assert load_skills_for_role("any-user-id", FakeRole()) == ""


# ---------------------------------------------------------------------------
# 3. Gates do NOT fire when DISABLE_SKILLS is unset/false
# ---------------------------------------------------------------------------

class TestSkillGatesOff:
    def test_load_core_prompt_returns_content_when_enabled(self, skills_on):
        from chat.backend.agent.skills.loader import load_core_prompt
        result = load_core_prompt("/app/chat/backend/agent/skills/core")
        assert len(result) > 1000

    def test_load_skill_llm_tool_does_not_short_circuit(self, skills_on):
        """When skills are ON, the LLM tool should attempt real work
        (will fail without DB but the failure must NOT be the disabled string)."""
        from chat.backend.agent.skills.load_skill_tool import load_skill
        out = load_skill("github", user_id="any-user", session_id="s1")
        assert "Skills are disabled" not in out


# ---------------------------------------------------------------------------
# 4. Tool list shape under DISABLE_SKILLS=true
# ---------------------------------------------------------------------------

class TestToolList:
    @pytest.fixture
    def tools(self, skills_off):
        from chat.backend.agent.tools.cloud_tools import get_cloud_tools, set_user_context
        # Use a unique user_id so the cache doesn't collide
        set_user_context("smoke-test-tools-disabled")
        return get_cloud_tools()

    def test_load_skill_tool_not_present(self, tools):
        names = {t.name for t in tools}
        assert "load_skill" not in names, (
            "load_skill must be unregistered when DISABLE_SKILLS=true"
        )

    def test_list_onprem_clusters_present(self, tools):
        names = {t.name for t in tools}
        assert "list_onprem_clusters" in names, (
            "discovery tool must be available so the LLM can find cluster IDs"
        )

    def test_required_core_tools_present(self, tools):
        names = {t.name for t in tools}
        required = {
            "cloud_exec", "iac_tool", "terminal_exec", "on_prem_kubectl",
            "list_onprem_clusters", "analyze_zip_file",
            "github_rca", "get_connected_repos", "github_commit",
            "web_search", "knowledge_base_search",
        }
        missing = required - names
        assert not missing, f"missing core tools: {missing}"


# ---------------------------------------------------------------------------
# 5. Arg schema quality — descriptions and enums visible
# ---------------------------------------------------------------------------

class TestArgSchemas:
    @pytest.fixture
    def tools_by_name(self, skills_off):
        from chat.backend.agent.tools.cloud_tools import get_cloud_tools, set_user_context
        set_user_context("smoke-test-schemas")
        return {t.name: t for t in get_cloud_tools()}

    def _schema_props(self, tool):
        assert tool.args_schema is not None, f"{tool.name} has no args_schema"
        return tool.args_schema.model_json_schema().get("properties", {})

    def _enum(self, prop):
        if "enum" in prop:
            return prop["enum"]
        for branch in prop.get("anyOf", []):
            if "enum" in branch:
                return branch["enum"]
        return None

    def test_cloud_exec_provider_is_enum(self, tools_by_name):
        props = self._schema_props(tools_by_name["cloud_exec"])
        enum = self._enum(props["provider"])
        assert enum is not None, "provider must be a Literal enum, not a free string"
        assert "aws" in enum and "gcp" in enum

    def test_cloud_exec_provider_matches_runtime_whitelist(self, tools_by_name):
        """The Literal must match the actual CLOUD_EXEC_PROVIDERS frozen set —
        otherwise the LLM will pass values that the runtime rejects."""
        from chat.backend.agent.prompt.provider_rules import CLOUD_EXEC_PROVIDERS
        props = self._schema_props(tools_by_name["cloud_exec"])
        enum = set(self._enum(props["provider"]) or [])
        # Every Literal value must be a valid runtime provider
        assert enum == set(CLOUD_EXEC_PROVIDERS), (
            f"provider Literal {enum} != CLOUD_EXEC_PROVIDERS {set(CLOUD_EXEC_PROVIDERS)}"
        )

    def test_cloud_exec_provider_excludes_kubectl(self, tools_by_name):
        """kubectl is NOT a top-level provider — it runs under aws/gcp/azure as a sub-command."""
        props = self._schema_props(tools_by_name["cloud_exec"])
        enum = self._enum(props["provider"]) or []
        assert "kubectl" not in enum

    def test_cloud_exec_fields_have_descriptions(self, tools_by_name):
        props = self._schema_props(tools_by_name["cloud_exec"])
        for field in ("provider", "command", "output_file", "account_id"):
            assert props[field].get("description"), f"{field} missing description"

    def test_iac_tool_action_is_enum(self, tools_by_name):
        props = self._schema_props(tools_by_name["iac_tool"])
        enum = self._enum(props["action"])
        assert enum is not None
        expected = {"write", "plan", "apply", "destroy", "validate", "fmt"}
        assert expected.issubset(set(enum))

    def test_analyze_zip_operation_is_enum(self, tools_by_name):
        props = self._schema_props(tools_by_name["analyze_zip_file"])
        enum = self._enum(props["operation"])
        assert enum == ["list", "extract", "analyze"]

    def test_terminal_exec_has_arg_descriptions(self, tools_by_name):
        props = self._schema_props(tools_by_name["terminal_exec"])
        for field in ("command", "working_dir", "timeout"):
            assert props[field].get("description"), f"{field} missing description"

    def test_on_prem_kubectl_mentions_list_tool(self, tools_by_name):
        """The LLM should know to call list_onprem_clusters first."""
        desc = tools_by_name["on_prem_kubectl"].description.lower()
        assert "list_onprem_clusters" in desc

    def test_list_onprem_clusters_takes_no_args(self, tools_by_name):
        props = self._schema_props(tools_by_name["list_onprem_clusters"])
        # All fields should be optional / absent (empty model body)
        required = tools_by_name["list_onprem_clusters"].args_schema.model_json_schema().get("required", [])
        assert required == []


# ---------------------------------------------------------------------------
# 6. End-to-end: list_onprem_clusters runs without error for unknown user
# ---------------------------------------------------------------------------

class TestRuntimeBehavior:
    def test_list_onprem_clusters_invocation(self, skills_off):
        from chat.backend.agent.tools.cloud_tools import get_cloud_tools, set_user_context
        set_user_context("00000000-0000-0000-0000-000000000000")
        tools = get_cloud_tools()
        loc = next((t for t in tools if t.name == "list_onprem_clusters"), None)
        assert loc is not None
        out = loc.invoke({})
        parsed = json.loads(out)
        assert isinstance(parsed, dict)
        # Every response must carry an explicit status field so the LLM can
        # distinguish "no clusters" from "DB/org-resolution failure".
        assert parsed.get("status") in ("ok", "error"), (
            f"missing/invalid status field in response: {parsed}"
        )
        assert "clusters" in parsed, "response must always include a clusters key"

    def test_list_onprem_clusters_unresolvable_user_returns_error_not_empty(self, skills_off):
        """A user_id that can't resolve to an org must produce status:error,
        NOT status:ok with an empty cluster list — otherwise the LLM thinks
        the user simply has no clusters and gives up silently."""
        from chat.backend.agent.tools.cloud_tools import get_cloud_tools, set_user_context
        # All-zeros UUID — not a real user, set_rls_context will fail to
        # resolve an org and return None
        set_user_context("00000000-0000-0000-0000-000000000000")
        tools = get_cloud_tools()
        loc = next(t for t in tools if t.name == "list_onprem_clusters")
        parsed = json.loads(loc.invoke({}))
        assert parsed["status"] == "error", (
            f"unresolvable user must surface error, got: {parsed}"
        )


# ---------------------------------------------------------------------------
# 7. System prompt no longer contains skill markers
# ---------------------------------------------------------------------------

class TestPromptComposition:
    def test_system_invariant_still_has_content_when_disabled(self, skills_off):
        """Disabling skills must NOT remove identity/security/behavioral_rules.
        Only integration-specific guidance is gated."""
        from chat.backend.agent.prompt.composer import build_system_invariant
        out = build_system_invariant(is_background=False, is_action=False)
        assert len(out) > 1000, "core agent prompt must remain even when skills off"

    def test_system_invariant_drops_interactive_load_skill_when_disabled(self, skills_off):
        """The single segment that's tied to skill loading — the directive
        telling the LLM to call load_skill — must be omitted when load_skill
        is unregistered. Without this the LLM is told to call a tool that
        doesn't exist."""
        from chat.backend.agent.prompt.composer import build_system_invariant
        out = build_system_invariant(is_background=False, is_action=False)
        # Marker phrases that uniquely identify interactive_load_skill.md
        assert "load_skill(" not in out, (
            "interactive_load_skill directive must be filtered when skills off"
        )

    def test_system_invariant_includes_interactive_load_skill_when_enabled(self, skills_on):
        from chat.backend.agent.prompt.composer import build_system_invariant
        out = build_system_invariant(is_background=False, is_action=False)
        assert "load_skill(" in out, (
            "interactive_load_skill must load normally when skills enabled"
        )

    def test_no_connected_integrations_block_when_disabled(self, skills_off):
        """The CONNECTED INTEGRATIONS header comes from build_index — must be gone."""
        from chat.backend.agent.skills.registry import SkillRegistry
        SkillRegistry.reset()
        reg = SkillRegistry.get_instance()
        idx = reg.build_index("any-user")
        assert "CONNECTED INTEGRATIONS" not in idx
        assert idx == ""

    def test_background_rca_segments_still_load_when_disabled(self, skills_off):
        """RCA background scaffolding (header, context-update, etc.) is core
        operational content, not skill markdown — must keep loading."""
        from chat.backend.agent.skills.loader import load_core_prompt
        bg_dir = "/app/chat/backend/agent/skills/rca/background"
        out = load_core_prompt(bg_dir, segments=["background_context_update"])
        assert len(out) > 100, "background RCA scaffolding must keep loading"


# ---------------------------------------------------------------------------
# 8. Tool cache invalidation on flag toggle
# ---------------------------------------------------------------------------

class TestToolCacheKey:
    def test_cache_key_differs_when_flag_toggles(self, monkeypatch):
        """If a developer flips DISABLE_SKILLS without restarting the process,
        the per-user tool cache must NOT serve stale (with-load_skill) tools."""
        from chat.backend.agent.tools.cloud_tools import get_cloud_tools, set_user_context
        # Build with flag OFF
        monkeypatch.delenv("DISABLE_SKILLS", raising=False)
        _force_reload_skills_modules()
        set_user_context("cache-toggle-user")
        names_on = {t.name for t in get_cloud_tools()}

        # Flip flag ON and rebuild — cache key must differ, so load_skill disappears
        monkeypatch.setenv("DISABLE_SKILLS", "true")
        _force_reload_skills_modules()
        set_user_context("cache-toggle-user")
        names_off = {t.name for t in get_cloud_tools()}

        assert "load_skill" in names_on
        assert "load_skill" not in names_off, (
            "cache key must include skills_disabled() — otherwise stale tool list served"
        )
