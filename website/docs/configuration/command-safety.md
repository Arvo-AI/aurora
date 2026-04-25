---
id: command-safety
title: Command Safety (AI Guardrails)
sidebar_label: Command Safety
---

# Command Safety

Aurora includes a three-layer safety guardrail. Before the agent reasons, the input rail inspects the incoming user message for prompt injection. Before each shell command runs, a signature matcher and an LLM safety judge evaluate it. All three activate together when guardrails are enabled; there are no per-layer toggles.

## Architecture

1. **Input rail** (NeMo Guardrails, per user message) -- catches prompt injection, role override, and social engineering in the latest human message before the agent starts. If it trips, the entire turn is aborted.
2. **Static signature matcher** (free, ~5ms, per command) -- compiled regex rules modeled on EDR/SIEM signatures. Catches known-malicious patterns: LOLBins, credential access, reverse shells, crypto miners, defense evasion. Rules are tagged with MITRE ATT&CK technique IDs.
3. **LLM safety judge** (~200-500ms, per command) -- a secondary LLM evaluates whether the command is inherently dangerous given the user's request context. Adapted from [Meta's PurpleLlama](https://github.com/meta-llama/PurpleLlama) (MIT licensed).

The signature matcher runs before the judge. If it matches, the command is blocked immediately without an LLM call.

The judge **always fails closed**: if the LLM call times out, errors, or cannot resolve the user's context, the command is blocked.

## Enabling

Guardrails are **enabled by default**. To explicitly disable them (not recommended):

```bash
GUARDRAILS_ENABLED=false
```

That's the only switch. When enabled (default), all three layers run. When `false`, none run.

## Model selection

The safety judge and input rail share the same provider abstraction as the rest of Aurora. By default they use `MAIN_MODEL`; set `GUARDRAILS_LLM_MODEL` to override. Format is identical to `MAIN_MODEL` and other model overrides (`provider/model`), and selection honors `LLM_PROVIDER_MODE` and the same API keys the main agent uses.

```bash
# Use a small, fast model (recommended, since the judge runs on every command)
GUARDRAILS_LLM_MODEL=openai/gpt-4o-mini
```

Typical choices:

| Model | Notes |
|-------|-------|
| `openai/gpt-4o-mini` | Good balance of speed and quality |
| `anthropic/claude-haiku-4.5` | Strong reasoning at low cost |
| `google/gemini-2.5-flash-lite` | Lowest cost |
| `ollama/llama3.1:8b` | Free, local (requires `OLLAMA_BASE_URL`) |

If `GUARDRAILS_LLM_MODEL` is empty, `MAIN_MODEL` is used.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GUARDRAILS_ENABLED` | `true` | Master switch. When enabled (default), all three layers run and every LLM check fails closed on error. Set to `false` to disable all guardrails. |
| `GUARDRAILS_LLM_MODEL` | _(MAIN_MODEL)_ | Model used by the safety judge and input rail. Same format and routing as `MAIN_MODEL`. |

## Block responses

When a command is blocked, the caller sees:

- Through `terminal_run()` (most shell executions): the call returns `returncode=126` with stderr `Blocked by safety guardrail: <reason>`.
- Through inline-checked tools (`kubectl_onprem`, `tailscale_ssh`): the JSON response contains `code: "SIGNATURE_MATCHED"` or `code: "SAFETY_BLOCKED"` with the reason in `error`.

When the input rail blocks a turn, the agent refuses the request immediately without attempting any tool calls.

All blocks are logged with a sha256 command fingerprint (never raw command content) plus the matching rule/technique where available. Structured audit events are emitted for SIEM ingestion via `server/utils/security/audit_events.py`.
