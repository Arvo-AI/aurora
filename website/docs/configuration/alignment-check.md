---
id: alignment-check
title: Alignment Check (AI Safety Guardrail)
sidebar_label: Alignment Check
---

# Alignment Check

Aurora includes an optional LLM-based safety guardrail that verifies agent actions are aligned with the user's original objective before executing commands. This catches dangerous autonomous behavior that deterministic rules cannot anticipate.

## How It Works

When enabled, every command the agent proposes goes through a two-layer defense:

1. **Deterministic checks** (fast, regex-based) -- organization command policy, blocklists, privilege escalation patterns. These run first and block at near-zero cost.
2. **Alignment check** (LLM-based) -- a secondary LLM evaluates whether the proposed command is semantically aligned with what the user actually asked for. Only fires for commands that pass all deterministic checks.

The alignment check uses [Meta's AlignmentCheck prompt](https://github.com/meta-llama/PurpleLlama/tree/main/LlamaFirewall) (MIT licensed) to detect when an agent's actions diverge from the user's intent -- for example, attempting to compile exploits, generate SSH keys, or access private files when the user asked for infrastructure monitoring.

## Enabling the Check

Set the following environment variable:

```bash
ALIGNMENT_CHECK_ENABLED=true
```

You also need a working LLM provider. The alignment check will use your `MAIN_MODEL` by default, but a dedicated fast/cheap model is recommended.

## Model Selection

The `ALIGNMENT_CHECK_MODEL` variable uses the same `provider/model` format as `MAIN_MODEL`:

| Model | Cost | Latency | Notes |
|-------|------|---------|-------|
| `openai/gpt-4o-mini` | ~$0.0003/check | ~300ms | Good balance of speed and quality |
| `anthropic/claude-haiku-4.5` | ~$0.0003/check | ~400ms | Strong reasoning at low cost |
| `google/gemini-2.5-flash-lite` | Cheapest | ~200ms | Lowest cost option |
| `ollama/llama3.1:8b` | Free | ~500ms | Requires local Ollama instance |

Example:

```bash
ALIGNMENT_CHECK_MODEL=openai/gpt-4o-mini
```

If left empty, the check uses whatever `MAIN_MODEL` is configured -- this works but costs more per check.

## Custom Endpoints

For self-hosted models (vLLM, Together AI, etc.), set `ALIGNMENT_CHECK_BASE_URL` to bypass the provider registry:

```bash
# Together AI
ALIGNMENT_CHECK_BASE_URL=https://api.together.xyz/v1
ALIGNMENT_CHECK_API_KEY=your-together-key
ALIGNMENT_CHECK_MODEL=meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8

# Self-hosted vLLM
ALIGNMENT_CHECK_BASE_URL=http://your-gpu-server:8000/v1
ALIGNMENT_CHECK_API_KEY=not-needed
ALIGNMENT_CHECK_MODEL=meta-llama/Llama-3.1-8B-Instruct
```

When `ALIGNMENT_CHECK_BASE_URL` is set, the check calls that endpoint directly using an OpenAI-compatible client instead of routing through Aurora's provider registry.

## Fail Mode

The `ALIGNMENT_CHECK_FAIL_MODE` controls what happens when the LLM call itself fails (network error, timeout, provider outage):

- **`open`** (default) -- allow the command to proceed. Same as how `command_policy` behaves on error. Appropriate for most deployments where availability is prioritized over safety.
- **`closed`** -- block the command. More secure, but commands will be blocked during LLM outages. Appropriate for high-security environments.

```bash
ALIGNMENT_CHECK_FAIL_MODE=closed
```

## Timeout

The check has a configurable timeout to prevent execution stalls:

```bash
# Max seconds to wait for alignment check LLM (default: 10)
ALIGNMENT_CHECK_TIMEOUT=10
```

If the LLM does not respond within this window, it is treated as an error and follows the configured fail mode.

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `ALIGNMENT_CHECK_ENABLED` | `false` | Enable/disable the alignment check |
| `ALIGNMENT_CHECK_MODEL` | _(uses MAIN_MODEL)_ | Provider/model for the check |
| `ALIGNMENT_CHECK_FAIL_MODE` | `open` | Behavior on error: `open` or `closed` |
| `ALIGNMENT_CHECK_TIMEOUT` | `10` | Seconds before timeout |
| `ALIGNMENT_CHECK_BASE_URL` | _(empty)_ | Custom API endpoint override |
| `ALIGNMENT_CHECK_API_KEY` | _(empty)_ | API key for custom endpoint |

## Performance

- Each check adds 200-1500ms latency depending on the model and provider.
- Cost is approximately $0.0003-0.001 per check (~1500 tokens input, ~100 tokens output).
- Every command is checked independently -- no caching or skipping.
- The check only fires after deterministic checks pass, so most blocked commands never reach the LLM.

## Integration Points

The alignment check is integrated into all command execution tools:

- `terminal_exec` -- general terminal commands
- `cloud_exec` -- AWS, GCP, Azure, OVH, Scaleway CLI commands
- `tailscale_ssh` -- remote command execution via Tailscale SSH
- `kubectl_onprem` -- on-premise Kubernetes commands

When a command is blocked, the response includes `"code": "ALIGNMENT_BLOCKED"` and the LLM's reasoning in the error message.
