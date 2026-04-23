---
id: command-safety
title: Command Safety (AI Guardrails)
sidebar_label: Command Safety
---

# Command Safety

Aurora includes a multi-layer safety guardrail stack that evaluates every command before execution. This catches dangerous behavior that deterministic rules cannot anticipate.

## Architecture

Commands pass through two guardrail layers (after the L1 organization command policy):

1. **L2 -- Static Signature Matcher** (free, ~5ms) -- compiled regex rules modeled on EDR/SIEM signatures. Catches known-malicious patterns: LOLBins, credential access, reverse shells, crypto miners, defense evasion. Maps to MITRE ATT&CK technique IDs.
2. **L4 -- Command Safety Judge** (LLM-based, ~250ms) -- a secondary LLM evaluates whether the command is inherently dangerous given the user's request context. Adapted from [Meta's PurpleLlama AlignmentCheck](https://github.com/meta-llama/PurpleLlama) (MIT licensed).

L2 runs first. If it matches, the command is blocked immediately without an LLM call. L4 only fires for commands that pass both L1 and L2.

## Enabling

```bash
GUARDRAILS_ENABLED=true
```

Both L2 and L4 activate by default. Disable either layer independently:

```bash
GUARDRAILS_SIGNATURE_CHECK=false   # disable L2
GUARDRAILS_LLM_JUDGE=false         # disable L4
```

## Model Selection

`GUARDRAILS_LLM_MODEL` uses the same `provider/model` format as `MAIN_MODEL`:

| Model | Cost | Latency | Notes |
|-------|------|---------|-------|
| `openai/gpt-4o-mini` | ~$0.0003/check | ~300ms | Good balance of speed and quality |
| `anthropic/claude-haiku-4.5` | ~$0.0003/check | ~400ms | Strong reasoning at low cost |
| `google/gemini-2.5-flash-lite` | Cheapest | ~200ms | Lowest cost option |
| `ollama/llama3.1:8b` | Free | ~500ms | Requires local Ollama instance |

```bash
GUARDRAILS_LLM_MODEL=openai/gpt-4o-mini
```

If empty, falls back to `MAIN_MODEL`.

## Custom Endpoints

For self-hosted models (vLLM, Together AI, etc.):

```bash
# Together AI
GUARDRAILS_LLM_BASE_URL=https://api.together.xyz/v1
GUARDRAILS_LLM_API_KEY=your-together-key
GUARDRAILS_LLM_MODEL=meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8

# Self-hosted vLLM
GUARDRAILS_LLM_BASE_URL=http://your-gpu-server:8000/v1
GUARDRAILS_LLM_API_KEY=not-needed
GUARDRAILS_LLM_MODEL=meta-llama/Llama-3.1-8B-Instruct
```

## Fail Mode

`GUARDRAILS_LLM_FAIL_MODE` controls behavior when the L4 LLM call fails:

- **`open`** (default) -- allow the command. L2 still protects against known patterns.
- **`closed`** -- block the command. More secure, but commands are blocked during LLM outages.

```bash
GUARDRAILS_LLM_FAIL_MODE=closed
```

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `GUARDRAILS_ENABLED` | `false` | Master switch for the guardrail stack |
| `GUARDRAILS_SIGNATURE_CHECK` | `true` | L2: static pattern matching |
| `GUARDRAILS_LLM_JUDGE` | `true` | L4: LLM-based command safety judge |
| `GUARDRAILS_LLM_MODEL` | _(MAIN_MODEL)_ | Provider/model for L4 |
| `GUARDRAILS_LLM_FAIL_MODE` | `open` | L4 error behavior: `open` or `closed` |
| `GUARDRAILS_LLM_BASE_URL` | _(empty)_ | Custom API endpoint override |
| `GUARDRAILS_LLM_API_KEY` | _(empty)_ | API key for custom endpoint |

## Performance

- **L2**: <5ms, zero cost. Compiled regex patterns evaluated in-process.
- **L4**: 200-1500ms, ~$0.0002-0.001/check depending on model.
- L2 catches ~80% of known-malicious patterns before L4 is invoked.
- Both layers only fire after L1 deterministic policy passes.

## Integration Points

Guardrails are enforced at the `terminal_run()` execution layer, covering all shell-executing tools automatically. Tools that bypass `terminal_run()` (SSH, on-prem kubectl) have direct L2+L4 checks.

When a command is blocked:
- Through `terminal_run()`: the call returns `returncode=126` with stderr `Blocked by safety guardrail: <reason>`.
- Through the inline-checked tools (`kubectl_onprem`, `tailscale_ssh`): the JSON response contains `code: "SIGNATURE_MATCHED"` or `code: "SAFETY_BLOCKED"` with the reason in `error`.
