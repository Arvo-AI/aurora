# Prompt + Skills Architecture (Modular)

This document explains the new modular prompt architecture for the Aurora agent.

It covers:
- what moved out of the old monolithic prompt builder
- where each responsibility now lives
- how interactive chat and background RCA prompts are assembled
- how to extend the system safely

## 1) High-level goals

The refactor separates:
- prompt content (Markdown files)
- prompt orchestration (Python modules)
- skill metadata and connection logic (YAML frontmatter + registry)
- runtime loading policy (connected integrations, RCA budget, on-demand load)

This gives:
- faster prompt iteration (edit `.md`, not Python string blocks)
- lower risk changes (small focused modules)
- better context discipline (load only relevant skills)
- clearer ownership boundaries

## 2) Directory layout

### Prompt package

`server/chat/backend/agent/prompt/`

- `prompt_builder.py`
  - Backward-compatible facade for existing imports.
  - Re-exports key functions/constants from split modules.
- `schema.py`
  - `PromptSegments` dataclass.
- `composer.py`
  - Top-level orchestration:
    - `build_system_invariant()`
    - `build_prompt_segments()`
    - `assemble_system_prompt()`
- `provider_rules.py`
  - Provider/mode/rule segments and validation helpers:
    - `CLOUD_EXEC_PROVIDERS`
    - provider constraints/context/prerequisites
    - Terraform validation
    - model overlay
    - failure recovery
    - regional rules
    - mode rules (`ask` vs `agent`)
- `context_fetchers.py`
  - DB-backed dynamic context:
    - managed VM access hints
    - knowledge base memory
- `background.py`
  - Background RCA system-prompt assembly for autonomous investigations.
  - Loads source-specific markdown segments (Slack/Google Chat/general).
- `cache_registration.py`
  - Prefix-cache segment registration (`register_prompt_cache_breakpoints`).

### Skills package

`server/chat/backend/agent/skills/`

- `loader.py`
  - parses YAML frontmatter + markdown body
  - discovers skills and segment files
  - template substitution and token estimation
- `registry.py`
  - singleton registry for discovered skills
  - connection checks
  - connected skill index
  - on-demand skill loading
  - RCA preloading with token budget
- `load_skill_tool.py`
  - LangChain tool used by the model to load integration skill details on demand

### Markdown prompt content

`server/chat/backend/agent/skills/core/`
- always-loaded core system prompt segments

`server/chat/backend/agent/skills/integrations/*/SKILL.md`
- one skill per integration (YAML frontmatter + markdown instructions)

`server/chat/backend/agent/skills/rca/*.md`
- RCA provider/general investigation guides

`server/chat/backend/agent/skills/rca/segments/*.md`
- shared RCA requirements/output blocks (used by `server/chat/background/rca_prompt_builder.py`)

`server/chat/backend/agent/skills/rca/background/*.md`
- background RCA source-mode blocks (used by `prompt/background.py`)

## 3) Runtime flow

## 3.1 Interactive/normal chat flow

1. `agent.py` calls `build_prompt_segments(...)`.
2. `composer.py` builds `PromptSegments` by combining:
   - provider rules (`provider_rules.py`)
   - background segment if in background mode (`background.py`)
   - DB context (`context_fetchers.py`)
   - skill index (`SkillRegistry.build_index`)
   - core prompt markdown (`skills/core/*.md`)
3. `assemble_system_prompt(...)` concatenates segments in stable order.
4. `cache_registration.py` registers cache boundaries for stable segments.
5. Model can call `load_skill(...)` tool to fetch full integration guidance only when needed.

## 3.2 Background RCA flow

There are two related pieces:

1. Background system prompt assembly (`prompt/background.py`)
   - builds channel/source operating instructions
   - injects connected skill content from `SkillRegistry.load_skills_for_rca(...)`
   - uses `skills/rca/background/*.md` for source-specific behavior

2. RCA investigation prompt assembly (`chat/background/rca_prompt_builder.py`)
   - builds alert details prompt
   - appends RCA skills/guides via registry
   - appends RCA shared requirement segments from `skills/rca/segments/*.md`

## 4) Connection checks and loading policy

`SkillRegistry` evaluates connectivity using `connection_check` frontmatter.

Supported methods:
- `get_credentials_from_db`
- `get_token_data`
- `is_connected_function`
- `provider_in_preference`
- `always`

Supported field requirements:
- `required_field`
- `required_any_fields`
- optional `feature_flag` function name

RCA preloading:
- connected skills are sorted by `rca_priority`
- loaded until token budget reached
- defaults to `4000` tokens
- overridable by env:
  - `RCA_SKILLS_TOKEN_BUDGET`
  - `RCA_TOKEN_BUDGET` (fallback)

## 5) Prompt segment categories

You can reason about all prompt content in these categories:

- Core invariants (`skills/core/*.md`)
- Provider/mode rules (`provider_rules.py`)
- Dynamic org/user context (`context_fetchers.py`)
- Connected integration index (`SkillRegistry.build_index`)
- On-demand integration guidance (`load_skill_tool.py` + `integrations/*/SKILL.md`)
- RCA provider/general playbooks (`skills/rca/*.md`)
- RCA shared requirement segments (`skills/rca/segments/*.md`)
- Background source/channel behavior (`skills/rca/background/*.md`)

## 6) How to extend

## 6.1 Add a new integration skill

1. Create `server/chat/backend/agent/skills/integrations/<id>/SKILL.md`.
2. Add YAML frontmatter:
   - `id`, `name`, `tools`, `connection_check`, `index`, `rca_priority`
3. Add markdown body with usage workflow and constraints.
4. Ensure the listed tool names exist in `get_cloud_tools()`.
5. If using `is_connected_function`, expose a safe import path.

## 6.2 Add a new background source behavior

1. Add a new markdown segment under `skills/rca/background/`.
2. Update branch selection in `prompt/background.py` to load the segment.
3. Keep branch behavior read-only unless intentionally changing policy.

## 6.3 Tune RCA requirements

1. Edit `skills/rca/segments/*.md` files.
2. Keep section titles stable if downstream logic expects them.
3. Validate prompt output in a background RCA session.

## 7) Backward compatibility

`prompt_builder.py` remains as a facade so existing imports still work:

- `build_prompt_segments`
- `assemble_system_prompt`
- `register_prompt_cache_breakpoints`
- `CLOUD_EXEC_PROVIDERS`
- other previously exported helpers

No call sites need to change immediately.

## 8) Caching behavior

Prefix-cache boundaries are registered by `cache_registration.py`.
Stable sections are cached with long TTL behavior.
Dynamic/user-sensitive sections use ephemeral TTL (`300s`).

Segments typically cached:
- system invariant
- provider constraints
- regional rules
- provider context
- integration index
- prerequisite checks
- terraform validation
- model overlay
- failure recovery
- tool manifest
- ephemeral mode rules

## 9) Guardrails and safety assumptions

- The model should not blindly execute provider tools outside selected providers.
- `provider_in_preference` ensures provider-bound skills are only shown when connected.
- Feature-flag-gated skills are hidden when disabled.
- Unknown/invalid connection methods fail closed (not connected).
- Untrusted module paths for `is_connected_function` are blocked.

## 10) Validation checklist after edits

Run:

```bash
python -m compileall -q server/chat/backend/agent/prompt
python -m compileall -q server/chat/backend/agent/skills
python -m compileall -q server/chat/background/rca_prompt_builder.py
```

Then smoke-check:
- interactive chat prompt creation
- background RCA prompt creation
- `load_skill` tool availability
- one integration connected/disconnected scenario

## 11) Common navigation shortcuts

Start here:
- `prompt/prompt_builder.py` (facade entrypoint)

Then jump to:
- orchestration: `prompt/composer.py`
- background mode: `prompt/background.py`
- provider logic: `prompt/provider_rules.py`
- DB context: `prompt/context_fetchers.py`
- skills registry/loader: `skills/registry.py`, `skills/loader.py`

If editing prompt content only:
- `skills/core/*.md`
- `skills/rca/segments/*.md`
- `skills/rca/background/*.md`
- `skills/integrations/*/SKILL.md`

## 12) Current tradeoff

Total lines across modules may be similar or slightly higher than the old single file.
The benefit is not line-count reduction alone, but local reasoning:
- each file has one concern
- content changes happen in markdown
- orchestration changes happen in focused Python modules
- debugging has clear entrypoints
