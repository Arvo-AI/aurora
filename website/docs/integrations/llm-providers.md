---
sidebar_position: 2
---

# LLM Providers

Aurora requires an LLM provider for its AI-powered investigation and Root Cause Analysis (RCA) capabilities. You can use a single gateway like OpenRouter, connect directly to individual providers, or run models locally with Ollama.

## Supported Providers

| Provider | Mode | Environment Variable | Get API Key |
|----------|------|---------------------|-------------|
| **OpenRouter** | `openrouter` | `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) |
| **OpenAI** | Direct | `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com/api-keys) |
| **Anthropic** | Direct | `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com/) |
| **Google AI** | Direct | `GOOGLE_AI_API_KEY` | [ai.google.dev](https://ai.google.dev/) |
| **Vertex AI** | `vertex` | `VERTEX_AI_PROJECT` + credentials | [console.cloud.google.com](https://console.cloud.google.com/) |
| **Ollama** | Direct | `OLLAMA_BASE_URL` | [ollama.com](https://ollama.com/) (free, local) |
| **AWS Bedrock** | `bedrock` | `BEDROCK_BASE_URL` (gateway) or `BEDROCK_REGION` (native) | [aws.amazon.com/bedrock](https://aws.amazon.com/bedrock/) |

Only **one** provider is required.

## Provider Modes

Aurora supports three routing modes, controlled by `LLM_PROVIDER_MODE`:

### OpenRouter Mode (default)

Routes all LLM requests through [OpenRouter](https://openrouter.ai), giving you access to multiple model providers with a single API key.

```bash
LLM_PROVIDER_MODE=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
```

### Direct Mode

Connects directly to each provider's native API. Use this when running models locally with Ollama or when you prefer direct API access.

```bash
LLM_PROVIDER_MODE=direct
```

In direct mode, Aurora auto-detects the provider from the model name prefix (e.g., `anthropic/claude-3-haiku` routes to Anthropic, `google/gemini-3.5-flash` routes to Google AI).

### Provider Mode (route everything through one provider)

Set `LLM_PROVIDER_MODE` to a **provider name** to send every model pick through that one backend — useful when a deployment standardizes on a single provider (e.g. a customer running entirely on AWS Bedrock):

```bash
LLM_PROVIDER_MODE=bedrock      # also accepts: vertex, anthropic, openai, google, ollama
```

A clean pick like **Claude Opus 4.7** is then translated to that provider's native id automatically (`us.anthropic.claude-opus-4-7` on Bedrock) — no `bedrock/` prefix or per-model setup. Models the provider can't serve (e.g. Gemini under `bedrock`) fall back to their own provider. It's the same idea as `openrouter` mode, pointed at a single direct provider.

## Supported Models

| Provider | Model | Notes |
|----------|-------|-------|
| **OpenAI** | `openai/gpt-5.4` | Latest flagship, 1M context |
| | `openai/gpt-5.2` | Previous flagship |
| | `openai/o3` | Strong reasoning model |
| | `openai/o4-mini` | Fast reasoning, lower cost |
| | `openai/o3-mini` | Compact reasoning model |
| | `openai/gpt-4.1` | Reliable all-rounder |
| | `openai/gpt-4.1-mini` | Fast and affordable |
| | `openai/gpt-4o` | Multimodal (text + vision) |
| | `openai/gpt-4o-mini` | Cheapest OpenAI option |
| **Anthropic** | `anthropic/claude-opus-4.6` | Most capable, 1M context |
| | `anthropic/claude-sonnet-4.6` | Near Opus quality at lower cost |
| | `anthropic/claude-opus-4.5` | Previous generation flagship |
| | `anthropic/claude-sonnet-4.5` | Balanced quality and speed |
| | `anthropic/claude-haiku-4.5` | Fast, affordable |
| | `anthropic/claude-3.5-sonnet` | Widely used, reliable |
| | `anthropic/claude-3-haiku` | Cheapest (default RCA model) |
| **Google Gemini** | `google/gemini-3.5-flash` | Fast, cost-effective with thinking |
| | `google/gemini-3.1-pro-preview` | Latest flagship with thinking |
| | `google/gemini-2.5-pro` | Strong for complex tasks |
| | `google/gemini-2.5-flash` | Cost-effective |
| **Vertex AI** | `vertex/gemini-3.5-flash` | Fast, cost-effective with thinking |
| | `vertex/gemini-3.1-pro-preview` | Latest flagship with thinking |
| | `vertex/gemini-2.5-pro` | Strong for complex tasks |
| | `vertex/gemini-2.5-flash` | Cost-effective with IAM auth |
| **Ollama** | `ollama/llama3.1` | Meta's Llama 3.1 (8B/70B) |
| | `ollama/qwen2.5` | Alibaba's Qwen 2.5 (various sizes) |
| | Any model via `ollama pull` | |
| **AWS Bedrock** | `bedrock/us.anthropic.claude-sonnet-4-5-v1:0` | Native mode: a Bedrock **inference-profile** id (region-prefixed `us.`/`eu.`/`apac.`) |
| | `bedrock/us.anthropic.claude-haiku-4-5-v1:0` | Faster, cheaper Claude on Bedrock |
| | Gateway: the model name your gateway expects | Gateway mode passes the suffix through to your OpenAI-compatible endpoint |

Model names use the `provider/model` format. New models from each provider are generally supported automatically — update the relevant env var (`MAIN_MODEL`, `RCA_MODEL`) or select chat models in the UI.

## Provider Setup

### OpenRouter (Recommended for Getting Started)

The easiest way to get started. One API key gives you access to models from OpenAI, Anthropic, Google, Meta, and more.

```bash
OPENROUTER_API_KEY=sk-or-v1-...
LLM_PROVIDER_MODE=openrouter
```

### OpenAI

```bash
OPENAI_API_KEY=sk-...
LLM_PROVIDER_MODE=direct
```

### Anthropic

```bash
ANTHROPIC_API_KEY=sk-ant-...
LLM_PROVIDER_MODE=direct
```

### Google AI (Gemini via API Key)

For using Gemini models with a Google AI Studio API key.

```bash
GOOGLE_AI_API_KEY=AIza...
LLM_PROVIDER_MODE=direct
```

### Vertex AI (Gemini via Google Cloud)

For organizations using Google Cloud. Vertex AI provides enterprise-grade access to Gemini models with IAM-based authentication.

**Requirements:**
- A Google Cloud project with Vertex AI API enabled
- A service account with the `Vertex AI User` role

**Setup:**

```bash
# Required: Google Cloud project ID
VERTEX_AI_PROJECT=my-gcp-project

# Required: Service account credentials (JSON string)
VERTEX_AI_SERVICE_ACCOUNT_JSON={"type":"service_account","project_id":"...","private_key":"..."}

# Optional: Location (default: global)
VERTEX_AI_LOCATION=global

# Route every model pick through Vertex (recommended — works with clean
# model names like "Gemini 2.5 Pro" in the picker, no vertex/ prefix needed).
LLM_PROVIDER_MODE=vertex
```

:::tip Two ways to route to Vertex
- **`LLM_PROVIDER_MODE=vertex`** (recommended) — forces every supported model pick through Vertex; models Vertex can't serve fall back to their own provider.
- **`LLM_PROVIDER_MODE=direct`** — only routes to Vertex when the model id carries a `vertex/` prefix (e.g. `MAIN_MODEL=vertex/gemini-2.5-pro`). A clean `google/...` id goes to the Google AI provider instead, not Vertex.
:::

**Authentication options:**
1. **Service account JSON** (recommended): Set `VERTEX_AI_SERVICE_ACCOUNT_JSON` to the full JSON contents of your service account key file.
2. **Application Default Credentials (ADC)**: If running on GCP (Cloud Run, GKE), ADC is automatic — just set `VERTEX_AI_PROJECT`.
3. **Credentials file path**: Set `GOOGLE_APPLICATION_CREDENTIALS` to the path of your service account key file.

:::note
`VERTEX_AI_PROJECT` is required — the provider is considered unavailable without it. The service account JSON only supplies credentials, not the project id.
:::

**Optional configuration:**

```bash
# Disable thinking mode for Gemini models (reduces latency, lowers token usage)
GEMINI_DISABLE_THINKING=true
```

### Ollama (Local Models)

Run models locally on your own hardware with [Ollama](https://ollama.com/). No API key needed.

**Setup:**

1. [Install Ollama](https://ollama.com/download) on your host machine
2. Pull the models you want to use:
   ```bash
   ollama pull llama3.1
   ollama pull qwen2.5:32b
   ```
3. Configure Aurora:
   ```bash
   OLLAMA_BASE_URL=http://host.docker.internal:11434
   LLM_PROVIDER_MODE=direct
   ```

:::info Docker networking
`host.docker.internal` allows Docker containers to reach services running on the host machine. This works out of the box on macOS and Windows. On Linux, Aurora's Docker Compose files include the `extra_hosts` configuration needed for this to work.
:::

**Recommended models for RCA:**

| Model | Size | Notes |
|-------|------|-------|
| `llama3.1:70b` | 70B | Best quality for complex RCA |
| `qwen2.5:32b` | 32B | Good balance of quality and speed |
| `llama3.2` | 3B | Fast, but limited tool calling |

### AWS Bedrock

Use [AWS Bedrock](https://aws.amazon.com/bedrock/) for Claude (and other Bedrock models) either through an OpenAI-compatible gateway or directly via the AWS SDK. Aurora picks the mode automatically: if `BEDROCK_BASE_URL` is set it uses **gateway mode**, otherwise it uses **native mode**.

Bedrock is configured by an admin via environment variables. There are two ways to route models to it:

- **`LLM_PROVIDER_MODE=bedrock`** (recommended for native mode) — every model picked in the app (e.g. "Claude Opus 4.7") routes through Bedrock automatically, translated to the matching inference-profile id (region-aware). No `bedrock/` prefix and no per-model configuration needed; the picker shows clean model names.
- **`LLM_PROVIDER_MODE=direct`** + explicit `bedrock/<id>` model ids (e.g. `MAIN_MODEL=bedrock/us.anthropic.claude-sonnet-4-5-v1:0`) — pin specific Bedrock ids per model. Use this for **gateway mode**, where the model name is whatever your gateway expects.

#### Gateway mode (OpenAI-compatible endpoint)

For an endpoint that exposes an OpenAI-compatible API (`POST .../v1/chat/completions`) in front of Bedrock — for example an [AWS Bedrock Access Gateway](https://github.com/aws-samples/bedrock-access-gateway) running inside your VPC. No AWS credentials are needed in Aurora; the gateway (and your network boundary) handles auth.

```bash
# OpenAI-compatible base URL (Aurora appends /chat/completions)
BEDROCK_BASE_URL=https://bedrock-gateway.internal.example.com/v1

# Optional — only if your gateway requires a key. Often unset (the VPC boundary handles auth).
BEDROCK_API_KEY=

LLM_PROVIDER_MODE=direct
MAIN_MODEL=bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0   # the model name your gateway expects
```

#### Native mode (AWS SDK)

For talking to AWS Bedrock directly. Requires a region plus AWS credentials (or an IAM role). On-demand Claude models on Bedrock require an **inference-profile id** (region-prefixed, e.g. `us.anthropic.claude-sonnet-4-5-v1:0`), not the bare model id.

```bash
# Required: AWS region (falls back to AWS_REGION / AWS_DEFAULT_REGION)
BEDROCK_REGION=us-east-1

# Credentials — omit these to use an IAM role or the default AWS credential chain.
# BEDROCK_* takes precedence over the standard AWS_* variables.
BEDROCK_ACCESS_KEY_ID=AKIA...
BEDROCK_SECRET_ACCESS_KEY=...
# Required only with temporary / STS credentials (e.g. an assumed role):
# BEDROCK_SESSION_TOKEN=...
# Or use a named profile instead of explicit keys:
# BEDROCK_PROFILE=my-bedrock-profile

# Recommended: route every model pick through Bedrock with clean model names.
LLM_PROVIDER_MODE=bedrock
MAIN_MODEL=anthropic/claude-sonnet-4.6   # auto-translated to us.anthropic.claude-sonnet-4-6
```

**Requirements (native mode):**
- A Bedrock-enabled AWS account with access to the chosen model granted in the Bedrock console.
- An identity (IAM user/role) with `bedrock:InvokeModel` / `bedrock:InvokeModelWithResponseStream` permissions.

:::tip
Gateway and native are the **same** `bedrock` provider — set `BEDROCK_BASE_URL` for gateway mode, leave it unset for native. Each mode's recommended `LLM_PROVIDER_MODE` and model-id style is shown above.
:::

## RCA Model Configuration

Background RCA uses the single-agent path by default (`ORCHESTRATOR_ENABLED=false`), configured via `RCA_MODEL`. An opt-in multi-agent orchestrator is also available — see [Multi-agent orchestrator](#multi-agent-orchestrator) below.

### Single-agent RCA (default)

By default, Aurora uses `anthropic/claude-haiku-4.5` for background Root Cause Analysis. You can change this to any supported provider/model.

```bash
# Format: provider/model-name
RCA_MODEL=anthropic/claude-haiku-4.5
```

**Examples:**

```bash
# Anthropic (default)
RCA_MODEL=anthropic/claude-haiku-4.5

# OpenAI
RCA_MODEL=openai/gpt-4o

# Google AI
RCA_MODEL=google/gemini-3.5-flash

# Vertex AI
RCA_MODEL=vertex/gemini-3.5-flash

# Ollama (local)
RCA_MODEL=ollama/llama3.1

# AWS Bedrock (native — inference-profile id)
RCA_MODEL=bedrock/us.anthropic.claude-haiku-4-5-v1:0
```

When `RCA_MODEL` is not set, the default depends on `RCA_OPTIMIZE_COSTS`:
- `RCA_OPTIMIZE_COSTS=true` (default): Uses `anthropic/claude-haiku-4.5`
- `RCA_OPTIMIZE_COSTS=false`: Uses `anthropic/claude-opus-4.6`

### Multi-agent orchestrator

Opt-in via `ORCHESTRATOR_ENABLED=true`. A lead orchestrator triages each incident and may fan out parallel read-only sub-agents. When enabled, `RCA_MODEL` is bypassed and two additional models are required:

```bash
ORCHESTRATOR_ENABLED=true
RCA_ORCHESTRATOR_MODEL=anthropic/claude-opus-4.7   # * triage + synthesis
RCA_SUBAGENT_MODEL=anthropic/claude-sonnet-4.6     # * sub-agent investigators
```

The split exists because triage/synthesis needs reliable structured-output JSON while sub-agents need reliable tool-calling. Per-role overrides are supported — set `model:` in the frontmatter of `server/chat/backend/agent/orchestrator/roles/*.md`.

## Cost Considerations

LLM costs depend on:

- **Tokens processed**: Longer investigations use more tokens
- **Model choice**: Larger models cost more per token
- **Frequency**: More investigations = higher costs

### Cost Optimization

1. Set `RCA_OPTIMIZE_COSTS=true` to use cheaper models for background RCA
2. Use OpenRouter for flexible, pay-per-token pricing
3. Use Ollama for zero API costs (requires local GPU)

## Safety Guardrail Model

Aurora can run an LLM-based command safety judge before executing commands, catching novel dangerous behavior that deterministic rules cannot anticipate.

```bash
# Format: provider/model-name (same as MAIN_MODEL / RCA_MODEL)
GUARDRAILS_LLM_MODEL=openai/gpt-4o-mini
```

The safety judge model can be any provider supported above. A fast, cheap, **non-reasoning** model is recommended since this runs on every command execution — reasoning models waste tokens on chain-of-thought for a simple Yes/No classification. See [Command Safety Configuration](../configuration/command-safety.md) for full setup details.

## Troubleshooting

### "Invalid API key"

- Check key is correctly copied (no extra spaces)
- Verify key is active in provider dashboard
- Ensure correct environment variable name

### "Rate limit exceeded"

- Wait and retry
- Consider upgrading your API tier
- Reduce concurrent investigations

### "Model not available"

- Check provider status page
- Try a different model
- Ensure your API key has access to the model

### Vertex AI: "DefaultCredentialsError"

- Verify `VERTEX_AI_PROJECT` is set
- Check that `VERTEX_AI_SERVICE_ACCOUNT_JSON` contains valid JSON
- Ensure the service account has the `Vertex AI User` role

### Ollama: "Provider not available"

- Verify Ollama is running: `curl http://localhost:11434/api/tags`
- Check that the model is pulled: `ollama list`
- Ensure `OLLAMA_BASE_URL` is correct (use `host.docker.internal` in Docker)

### Bedrock: "on-demand throughput isn't supported"

- Native mode requires an **inference-profile id**, not the bare model id. Use a region-prefixed id such as `bedrock/us.anthropic.claude-sonnet-4-5-v1:0` (`us.`/`eu.`/`apac.`).

### Bedrock: "Unable to locate credentials" / "NoRegionError"

- Native mode needs a region — set `BEDROCK_REGION` (or `AWS_REGION` / `AWS_DEFAULT_REGION`).
- Provide credentials via `BEDROCK_ACCESS_KEY_ID` + `BEDROCK_SECRET_ACCESS_KEY`, a `BEDROCK_PROFILE`, or an IAM role / the default AWS credential chain.
- Confirm the identity has `bedrock:InvokeModel` permissions and that model access is granted in the Bedrock console.

### Bedrock: "AccessDeniedException" calling the gateway

- In gateway mode, set `BEDROCK_BASE_URL` to the OpenAI **base** path (ending in `/v1`); Aurora appends `/chat/completions`.
- If your gateway requires a key, set `BEDROCK_API_KEY`. If it doesn't, leave it unset.
