---
id: command-safety
title: Command Safety (AI Guardrails)
sidebar_label: Command Safety
---

# Command Safety

Aurora evaluates every agent-issued shell command through a layered safety pipeline. The input rail inspects the user message before the agent reasons; each shell command then passes through an org-level command policy, a static signature matcher, and an LLM safety judge. All layers run by default; any one of them can block.

## Architecture

Each user message and the commands derived from it pass through these checks, in order. Any layer can block and short-circuit the rest.

1. **Input rail** (NeMo Guardrails, per user message) -- catches prompt injection, role override, and social engineering in the latest human message before the agent starts. If it trips, the entire turn is aborted before any tool runs.
2. **Org command policy** (Postgres-backed, per command) -- per-organization deny and allow lists of compiled regex rules, managed from the admin UI. Runs at the tool boundary. Denylist is evaluated first; if an allowlist is enabled, commands must match a rule in it to be allowed. This is where operators express org-specific policy ("no `kubectl delete` in prod", "only `systemctl` verbs on fleet X").
3. **Static signature matcher** (free, ~5ms, per command) -- compiled regex rules modeled on EDR/SIEM signatures, augmented at load time with a vendored subset of the [SigmaHQ community threat-detection rule corpus](https://github.com/SigmaHQ/sigma) (DRL-1.1 licensed). Catches known-malicious patterns: LOLBins, credential access, reverse shells, crypto miners, defense evasion. Rules are tagged with MITRE ATT&CK technique IDs. Runs before the judge so matched commands are blocked without an LLM call.
4. **LLM safety judge** (~200-500ms, per command) -- a secondary LLM evaluates whether the command is inherently dangerous given the user's request context. Catches novel or context-dependent threats that static signatures cannot express. Adapted from [Meta's PurpleLlama](https://github.com/meta-llama/PurpleLlama) (MIT licensed).

The judge **always fails closed**: if the LLM call times out, errors, or cannot resolve the user's context, the command is blocked. The input rail behaves the same way.

## Enabling

Guardrails are **enabled by default**. To explicitly disable the input rail, signature matcher, and LLM judge (not recommended):

```bash
GUARDRAILS_ENABLED=false
```

The org command policy is independent of `GUARDRAILS_ENABLED`; it is always evaluated when an org has policy lists configured, and is managed from the admin UI rather than through environment variables.

## Requirements

Because guardrails are enabled by default, the LLM judge and NeMo input rail need a working LLM at startup. Without one they fail closed and every shell command is blocked.

At a minimum, you need:

- Either `MAIN_MODEL` or `GUARDRAILS_LLM_MODEL` set to a reachable model.
- The corresponding provider credentials (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, or `OLLAMA_BASE_URL`).
- Outbound network access from the `chatbot` and `celery-worker` containers to the provider endpoint (or to your Ollama host).

If your environment genuinely cannot reach an LLM, set `GUARDRAILS_ENABLED=false` explicitly. Do not rely on guardrails silently being inactive -- they are on unless you turn them off.

## Model selection

The safety judge and input rail share the same provider abstraction as the rest of Aurora. Set `GUARDRAILS_LLM_MODEL` to control the model. Format is identical to `MAIN_MODEL` and other model overrides (`provider/model`), and selection honors `LLM_PROVIDER_MODE` and the same API keys the main agent uses.

When `GUARDRAILS_LLM_MODEL` is unset, Aurora picks a default based on `LLM_PROVIDER_MODE`:

- `openrouter` -> `google/gemini-2.5-flash-lite` (small and cheap, keeps per-message cost predictable).
- Anything else -> `MAIN_MODEL` (the same model the chat agent uses).

```bash
# Use a small, fast model (recommended, since the judge runs on every command)
GUARDRAILS_LLM_MODEL=openai/gpt-4o-mini
```

Typical choices:

| Model | Notes |
|-------|-------|
| `openai/gpt-4o-mini` | Good balance of speed and quality |
| `anthropic/claude-haiku-4.5` | Strong reasoning at low cost |
| `google/gemini-2.5-flash-lite` | Lowest cost (OpenRouter default) |
| `ollama/llama3.1:8b` | Free, local (requires `OLLAMA_BASE_URL`) |

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GUARDRAILS_ENABLED` | `true` | Master switch for the input rail, signature matcher, and LLM judge. When enabled (default), all three run and every LLM check fails closed on error. Set to `false` to disable them. Does not affect the org command policy. |
| `GUARDRAILS_LLM_MODEL` | _(provider-dependent)_ | Model used by the safety judge and input rail. When unset, defaults to `google/gemini-2.5-flash-lite` under `LLM_PROVIDER_MODE=openrouter`, otherwise falls back to `MAIN_MODEL`. Same format and routing as `MAIN_MODEL`. |
| `GUARDRAILS_SIGMA_ENABLED` | `true` | Gates the SigmaHQ rule corpus on top of the hand-written signatures. Requires `GUARDRAILS_ENABLED=true`. Set to `false` to run only the hand-written rules. |

## Block responses

When a command is blocked, the caller sees:

- Through `terminal_run()` (most shell executions): the call returns `returncode=126` with stderr `Blocked by safety guardrail: <reason>`.
- Through inline-checked tools (`kubectl_onprem`, `tailscale_ssh`): the JSON response contains a code identifying the layer that blocked (`code: "POLICY_DENIED"` for the org command policy, `code: "SIGNATURE_MATCHED"` for the signature matcher, `code: "SAFETY_BLOCKED"` for the LLM judge) with the reason in `error`.

When the input rail blocks a turn, the agent refuses the request immediately without attempting any tool calls.

All blocks are logged with a sha256 command fingerprint (never raw command content) plus the matching rule/technique where available. Structured audit events are emitted for SIEM ingestion via `server/utils/security/audit_events.py`.
