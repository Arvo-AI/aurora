---
id: command-safety
title: Command Safety (AI Guardrails)
sidebar_label: Command Safety
---

# Command Safety

Aurora includes a two-layer safety guardrail that evaluates every shell command before execution. Both layers run when guardrails are enabled; there are no per-layer toggles.

## Architecture

Commands pass through two checks, in order, after the organization command policy:

1. **Static signature matcher** (free, ~5ms) -- compiled regex rules modeled on EDR/SIEM signatures. Catches known-malicious patterns: LOLBins, credential access, reverse shells, crypto miners, defense evasion. Rules are tagged with MITRE ATT&CK technique IDs.
2. **LLM safety judge** (~200-500ms) -- a secondary LLM evaluates whether the command is inherently dangerous given the user's request context. Adapted from [Meta's PurpleLlama](https://github.com/meta-llama/PurpleLlama) (MIT licensed).

The signature matcher runs first. If it matches, the command is blocked immediately without an LLM call. The judge only fires for commands that pass signature matching.

The judge **always fails closed**: if the LLM call times out, errors, or cannot resolve the user's context, the command is blocked.

## Enabling

```bash
GUARDRAILS_ENABLED=true
```

That's the only switch. When `true`, both layers run on every command. When `false`, neither layer runs.

## Model selection

The safety judge uses the same provider abstraction as the rest of Aurora. By default it shares `MAIN_MODEL`; set `GUARDRAILS_LLM_MODEL` to override. Format is identical to `MAIN_MODEL` and other model overrides (`provider/model`), and selection honors `LLM_PROVIDER_MODE` and the same API keys the main agent uses.

```bash
# Use a small, fast model for the judge (recommended, since it runs on every command)
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
| `GUARDRAILS_ENABLED` | `false` | Master switch. When `true`, both layers run on every command. |
| `GUARDRAILS_LLM_MODEL` | _(MAIN_MODEL)_ | Model used by the safety judge. Same format and routing as `MAIN_MODEL`. |

## Block responses

When a command is blocked, the caller sees:

- Through `terminal_run()` (most shell executions): the call returns `returncode=126` with stderr `Blocked by safety guardrail: <reason>`.
- Through inline-checked tools (`kubectl_onprem`, `tailscale_ssh`): the JSON response contains `code: "SIGNATURE_MATCHED"` or `code: "SAFETY_BLOCKED"` with the reason in `error`.

All blocks are logged with a sha256 command fingerprint (never raw command content) plus the matching rule/technique where available.
