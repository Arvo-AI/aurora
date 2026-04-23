---
sidebar_position: 2
---

# LLM Providers

Aurora requires an LLM provider for its AI-powered investigation and Root Cause Analysis (RCA) capabilities. You can use a single gateway like OpenRouter, connect directly to individual providers, or run models locally with Ollama.

## Supported Providers

| Provider | Mode | Environment Variable | Get API Key |
|----------|------|---------------------|-------------|
| **OpenRouter** | Gateway | `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) |
| **OpenAI** | Direct | `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com/api-keys) |
| **Anthropic** | Direct | `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com/) |
| **Google AI** | Direct | `GOOGLE_AI_API_KEY` | [ai.google.dev](https://ai.google.dev/) |
| **Vertex AI** | Direct | `VERTEX_AI_PROJECT` + credentials | [console.cloud.google.com](https://console.cloud.google.com/) |
| **Ollama** | Direct | `OLLAMA_BASE_URL` | [ollama.com](https://ollama.com/) (free, local) |

Only **one** provider is required.

## Provider Modes

Aurora supports two routing modes, controlled by `LLM_PROVIDER_MODE`:

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

In direct mode, Aurora auto-detects the provider from the model name prefix (e.g., `anthropic/claude-3-haiku` routes to Anthropic, `google/gemini-2.5-flash` routes to Google AI).

:::note
Vertex AI and Ollama always use their native SDKs regardless of `LLM_PROVIDER_MODE`. OpenAI, Anthropic, and Google AI models can all be routed through OpenRouter.
:::

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
| **Google Gemini** | `google/gemini-3.1-pro-preview` | Latest flagship with thinking |
| | `google/gemini-3-flash-preview` | Fast, outperforms 2.5 Pro |
| | `google/gemini-2.5-pro` | Strong for complex tasks |
| | `google/gemini-2.5-flash` | Cost-effective |
| | `google/gemini-2.5-flash-lite` | Cheapest Gemini option |
| **Vertex AI** | `vertex/gemini-3.1-pro-preview` | Latest flagship with thinking |
| | `vertex/gemini-3-flash-preview` | Fast, enterprise-grade |
| | `vertex/gemini-2.5-pro` | Strong for complex tasks |
| | `vertex/gemini-2.5-flash` | Cost-effective with IAM auth |
| | `vertex/gemini-2.5-flash-lite` | Cheapest Vertex option |
| **Ollama** | `ollama/llama3.1` | Meta's Llama 3.1 (8B/70B) |
| | `ollama/qwen2.5` | Alibaba's Qwen 2.5 (various sizes) |
| | Any model via `ollama pull` | |

Model names use the `provider/model` format. New models from each provider are generally supported automatically — update your `RCA_MODEL` or select them in the UI.

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

LLM_PROVIDER_MODE=direct
```

**Authentication options:**
1. **Service account JSON** (recommended): Set `VERTEX_AI_SERVICE_ACCOUNT_JSON` to the full JSON contents of your service account key file.
2. **Application Default Credentials (ADC)**: If running on GCP (Cloud Run, GKE), ADC is automatic — just set `VERTEX_AI_PROJECT`.
3. **Credentials file path**: Set `GOOGLE_APPLICATION_CREDENTIALS` to the path of your service account key file.

:::tip
The project ID is automatically extracted from the service account JSON if `VERTEX_AI_PROJECT` is not set.
:::

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

## RCA Model Configuration

By default, Aurora uses `anthropic/claude-3-haiku` for background Root Cause Analysis. You can change this to any supported provider/model.

```bash
# Format: provider/model-name
RCA_MODEL=anthropic/claude-3-haiku
```

**Examples:**

```bash
# Anthropic (default)
RCA_MODEL=anthropic/claude-3-haiku

# OpenAI
RCA_MODEL=openai/gpt-4o

# Google AI
RCA_MODEL=google/gemini-2.5-flash

# Vertex AI
RCA_MODEL=vertex/gemini-2.5-flash

# Ollama (local)
RCA_MODEL=ollama/llama3.1
```

When `RCA_MODEL` is not set, the default depends on `RCA_OPTIMIZE_COSTS`:
- `RCA_OPTIMIZE_COSTS=true` (default): Uses `anthropic/claude-3-haiku`
- `RCA_OPTIMIZE_COSTS=false`: Uses `anthropic/claude-opus-4.5`

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

The safety judge model can be any provider supported above. A fast, cheap model is recommended since this runs on every command execution. See [Command Safety Configuration](../configuration/command-safety.md) for full setup details.

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
