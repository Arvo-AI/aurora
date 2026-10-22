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

In direct mode, Aurora auto-detects the provider from the model name prefix (e.g., `anthropic/claude-haiku-5.5` routes to Anthropic, `google/gemini-3.5-flash` routes to Google AI).

### Provider Mode (route everything through one provider)

Set `LLM_PROVIDER_MODE` to a **provider name** to send every model pick through that one backend — useful when a deployment standardizes on a single provider (e.g. a customer running entirely on AWS Bedrock):

```bash
LLM_PROVIDER_MODE=bedrock      # also accepts: vertex, anthropic, openai, google, ollama
```

A clean pick like **Claude Opus 5.5** is then translated to that provider's native id automatically (`eu.anthropic.claude-opus-5-5` on Bedrock in an EU region) — no `bedrock/` prefix or per-model setup. A model the forced provider cannot serve (e.g. an `openai/` or `google/` pick under `bedrock`) falls back to its own provider **only if that provider has credentials configured**; otherwise the call fails with a clear error. For a Bedrock-only deployment, leave the other provider keys empty so nothing can route off Bedrock. It's the same idea as `openrouter` mode, pointed at a single direct provider.

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
| **Anthropic** | `anthropic/claude-fable-5.1` | Most capable, 1M context |
| | `anthropic/claude-opus-5.5` | Default — high capability, 1M context |
| | `anthropic/claude-sonnet-5` | Near Opus quality at lower cost |
| | `anthropic/claude-opus-4.6` | Previous generation flagship |
| | `anthropic/claude-sonnet-4.6` | Previous generation balanced |
| | `anthropic/claude-opus-4.5` | Previous generation flagship |
| | `anthropic/claude-sonnet-4.5` | Balanced quality and speed |
| | `anthropic/claude-haiku-5.5` | Cheap tier — default for background RCA and every auxiliary call; 1M context |
| | `anthropic/claude-3.5-sonnet` | Legacy, still resolves |
| | `anthropic/claude-3-haiku` | Legacy, still resolves |
| **Google Gemini** | `google/gemini-4-argon` | Frontier model for hard coding and agent tasks |
| | `google/gemini-3.5-flash` | Fast, cost-effective with thinking |
| | `google/gemini-3.1-pro-preview` | Previous flagship with thinking |
| | `google/gemini-2.5-pro` | Strong for complex tasks |
| | `google/gemini-2.5-flash` | Cost-effective |
| **Vertex AI** | `vertex/gemini-4-argon` | Frontier model for hard coding and agent tasks |
| | `vertex/gemini-3.6-flash` | Latest Flash — agentic workflows, coding, multimodal (thinking) |
| | `vertex/gemini-3.5-flash-lite` | Fastest, lowest-cost — high-throughput workloads |
| | `vertex/gemini-3.5-flash` | Previous Flash generation (still supported in env vars) |
| | `vertex/gemini-3.1-pro-preview` | Strong reasoning with thinking |
| | `vertex/gemini-2.5-pro` | Strong for complex tasks |
| | `vertex/gemini-2.5-flash` | Cost-effective with IAM auth |
| **Ollama** | `ollama/llama3.1` | Meta's Llama 3.1 (8B/70B) |
| | `ollama/qwen2.5` | Alibaba's Qwen 2.5 (various sizes) |
| | Any model via `ollama pull` | |
| **AWS Bedrock** | `bedrock/eu.anthropic.claude-opus-5-5` | Native mode: a Bedrock **inference-profile** id (geo-prefixed `eu.`/`us.`; the Claude 5.x generation has no date suffix) |
| | `bedrock/eu.anthropic.claude-haiku-5-5` | Cheap tier on Bedrock (EU geo profile) |
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
# Disable thinking mode for Gemini models (reduces latency, lowers token usage).
# Recommended for Vertex deployments — Gemini on Vertex is notably slower with thinking enabled.
GEMINI_DISABLE_THINKING=true
```

:::note Gemini model IDs on Vertex
Use the stable model IDs exactly as listed above (e.g. `gemini-3.6-flash`, `gemini-3.5-flash-lite`). Both are GA on Vertex as of July 2026. Flash-Lite models do not use thinking mode — Aurora skips thinking config for any `*-flash-lite` model automatically.
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
   MAIN_MODEL=ollama/llama3.1
   ```

With no `ANTHROPIC_API_KEY` configured, the Claude cheap tier is not routable, so background RCA, the safety judge / input rail, summaries and every other auxiliary call automatically follow `MAIN_MODEL` (see [Model tiers](#model-tiers)) — an Ollama-only install needs no further model variables. Set `RCA_MODEL`, `SUMMARIZATION_MODEL`, `GUARDRAILS_LLM_MODEL`, etc. only if you want those calls on a different local model (e.g. a smaller one for the judge).

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

- **`LLM_PROVIDER_MODE=bedrock`** (recommended for native mode) — every Claude model picked in the app (e.g. "Claude Opus 5.5", the cheap-tier Haiku 5.5) routes through Bedrock automatically, translated to the matching inference-profile id for your region (`eu.anthropic.claude-opus-5-5` from an EU region). No `bedrock/` prefix and no per-model configuration needed; the picker shows clean model names. A model Bedrock cannot serve (`openai/`, `google/`, `vertex/`) falls back to its own provider **if** that provider has credentials configured — leave the other keys empty to keep everything on Bedrock (a fail-closed residency policy is a planned follow-up).
- **`LLM_PROVIDER_MODE=direct`** + explicit `bedrock/<id>` model ids (e.g. `MAIN_MODEL=bedrock/eu.anthropic.claude-opus-5-5`) — pin specific Bedrock ids per model. Use this for **gateway mode**, where the model name is whatever your gateway expects. The unset cheap-tier variables stay on the pinned route: `bedrock/eu.anthropic.claude-haiku-5-5` for an `eu.` pin in native mode, `MAIN_MODEL` for a gateway name (see [Model tiers](#model-tiers)).

#### Gateway mode (OpenAI-compatible endpoint)

For an endpoint that exposes an OpenAI-compatible API (`POST .../v1/chat/completions`) in front of Bedrock — for example an [AWS Bedrock Access Gateway](https://github.com/aws-samples/bedrock-access-gateway) running inside your VPC (the upstream sample is deprecated; prefer native mode for new deployments). No AWS credentials are needed in Aurora; the gateway (and your network boundary) handles auth.

```bash
# OpenAI-compatible base URL (Aurora appends /chat/completions)
BEDROCK_BASE_URL=https://bedrock-gateway.internal.example.com/v1

# Optional — only if your gateway requires a key. Often unset (the VPC boundary handles auth).
BEDROCK_API_KEY=

LLM_PROVIDER_MODE=direct
MAIN_MODEL=bedrock/eu.anthropic.claude-haiku-5-5   # the model name your gateway expects
```

#### Native mode (AWS SDK)

For talking to AWS Bedrock directly. Requires a region plus an AWS identity — an **IAM role** is the default path (EC2 instance profile, ECS task role or EKS IRSA; boto3's default credential chain picks it up with no keys in Aurora's config). On-demand Claude models on Bedrock require an **inference-profile id** (geo-prefixed, e.g. `eu.anthropic.claude-haiku-5-5`), not the bare model id; Aurora derives it from the region when you use clean `anthropic/...` names.

```bash
# Required: AWS region (falls back to AWS_REGION / AWS_DEFAULT_REGION).
# An EU region keeps inference inside the EU (eu.* geo profiles).
BEDROCK_REGION=eu-central-1

# Credentials: nothing to set when the host/pod has an IAM role (recommended).
# BEDROCK_* takes precedence over the standard AWS_* variables if you must use keys.
# BEDROCK_ACCESS_KEY_ID=AKIA...
# BEDROCK_SECRET_ACCESS_KEY=...
# Required only with temporary / STS credentials (e.g. an assumed role):
# BEDROCK_SESSION_TOKEN=...
# A named profile is also accepted, but it requires mounting ~/.aws into the server,
# chatbot and celery containers — Compose and the Helm chart do not do this out of the box:
# BEDROCK_PROFILE=my-bedrock-profile

# Recommended: route every model pick through Bedrock with clean model names.
LLM_PROVIDER_MODE=bedrock
MAIN_MODEL=anthropic/claude-opus-5.5   # auto-translated to eu.anthropic.claude-opus-5-5
# RCA and auxiliary calls default to the cheap tier -> eu.anthropic.claude-haiku-5-5
```

**Inference-profile geo:** the geo prefix (`us.` / `eu.` / `apac.`) is derived from
`BEDROCK_REGION`. Set `BEDROCK_REGION=global` to use the `global.` profiles instead,
which bill at the direct list price rather than the ~10% cross-region (geo CRIS)
premium; boto3 still signs against `AWS_REGION` / `AWS_DEFAULT_REGION` (default
`us-east-1`). Claude Fable 5 / 5.1 are only published as `us.` and `global.` profiles,
so on an EU or APAC install those two automatically route to `global.`.

:::warning Data residency
`global.` inference profiles may process a request in **any commercial AWS Region**
worldwide. Do not set `BEDROCK_REGION=global` (and avoid Claude Fable, which has no
`eu.` profile) on EU/GDPR-bound deployments — stay on an EU region so every call uses
`eu.*` geo profiles, which route only between EU Regions.
:::

**Requirements (native mode):**
- A Bedrock-enabled AWS account with access to the chosen model granted in the Bedrock console.
- An identity (IAM user/role) with `bedrock:InvokeModel` / `bedrock:InvokeModelWithResponseStream` permissions.

:::tip
Gateway and native are the **same** `bedrock` provider — set `BEDROCK_BASE_URL` for gateway mode, leave it unset for native. Each mode's recommended `LLM_PROVIDER_MODE` and model-id style is shown above.
:::

#### Prompt caching

In native mode Aurora sends real Converse **cache checkpoints** (`cachePoint`) on every
Claude call — nothing to configure. Each request carries three checkpoints: after the
tool definitions, after the static part of the system prompt (core rules, provider
constraints, regional rules — identical across sessions, users and incidents), and at the
end of the last message, so the conversation history is a cache read on every ReAct turn
of an investigation. Per-org and per-incident content (command policy, rendered skills with
alert data, memories) sits after the static checkpoint and never invalidates the shared
prefix. The TTL is Bedrock's default 5 minutes (refreshed on every hit); the Bedrock
maximum of 4 checkpoints per request is never exceeded.

Checkpoints only take effect when the prefix reaches the model's minimum (512 tokens for
the Claude 5.x generation, 1,024 for Sonnet 5 / 4.x, 4,096 for Opus 4.5–4.7 and Haiku 4.5);
when the static part alone is under it (e.g. background RCA on Haiku 4.5) Aurora drops the
static checkpoint and the end-of-system checkpoint covers the whole prompt instead, so the
history is still a cache read on every turn. Measure the savings in CloudWatch under
`AWS/Bedrock` with `CacheReadInputTokenCount` and `CacheWriteInputTokenCount` (per
`ModelId`). Cache reads bill at ~10% of the input price and cache writes at 125%, so a
prefix must be read at least once for the write to pay off — which an agent loop does on
its second turn.

Aurora records the same breakdown per call in its own `llm_usage_tracking` table:
`input_tokens` is the whole prompt (what LangChain reports — Bedrock's raw `inputTokens`
excludes cache tokens, Aurora adds them back), `cache_read_input_tokens` the part served
from the cache and `cache_write_input_tokens` the part written to it; `estimated_cost`
prices the three parts at their own rates (input / cache read / cache write), so a
cached turn is billed at the discounted rate instead of the full input price. A quick
cache hit-rate per model and call type:

```sql
SELECT model_name, request_type,
       SUM(input_tokens)               AS prompt_tokens,
       SUM(cache_read_input_tokens)    AS cache_read,
       SUM(cache_write_input_tokens)   AS cache_write,
       ROUND(100.0 * SUM(cache_read_input_tokens) / NULLIF(SUM(input_tokens), 0), 1) AS read_pct,
       SUM(estimated_cost)             AS cost_usd
FROM llm_usage_tracking
WHERE timestamp >= NOW() - INTERVAL '7 days'
GROUP BY model_name, request_type
ORDER BY cost_usd DESC;
```

Rows are labelled with the provider that actually served the call (`api_provider` =
`bedrock` for a Claude model under `LLM_PROVIDER_MODE=bedrock`) and every spelling of a
Bedrock model id (`bedrock/eu.anthropic.claude-opus-5-5`, dated ids) prices as its
Anthropic row times `BEDROCK_REGIONAL_PREMIUM` (default 1.10 for geo profiles, none for
`global.`). Besides the agent turns (`agent_workflow`), the auxiliary calls show up under
their own `request_type`: `command_safety`, `input_rail`, `memory_selector`,
`memory_extraction`, `tool_output_summarization`, `context_compression`,
`severity_determination`, `incident_rca_summary`, `incident_initial_summary`,
`incident_index_synopsis`, `hypothesis_extraction`, `diagnostics_plan`, `recommendation`,
`suggestion_enrichment`, `suggestion_summary`, `verdict_extraction`, `jira_rca_judge`,
`triage_decision`, `synthesis_decision`.

Gateway mode is unchanged: with the documented `LLM_PROVIDER_MODE=direct` + `bedrock/<id>`
setup Aurora sends a plain system prompt and caching is up to the gateway (the aws-samples
Bedrock Access Gateway has its own `ENABLE_PROMPT_CACHING` switch). Only `anthropic/` ids
routed through the gateway (`LLM_PROVIDER_MODE=bedrock` + `BEDROCK_BASE_URL`) carry the
Anthropic-style `cache_control` markers they already carried; whether those reach Bedrock
depends on your gateway's translation.

#### Retries, timeouts and output caps

Native-mode clients carry a botocore `Config` with the `standard` retry mode — exponential
backoff with jitter on throttling (`ThrottlingException`), 5xx and transient connection
errors — capped at `BEDROCK_MAX_ATTEMPTS` total attempts (default 3), a 10 s connect
timeout and a `BEDROCK_READ_TIMEOUT` read timeout (default 300 s, sized for a streaming
agent turn with extended thinking; the judge, summaries and the synopsis call pass their own
shorter per-call timeout). Gateway mode retries twice through the OpenAI-compatible client.
Because the SDK already retries those errors, the agent never restarts a whole run on them:
a full restart (which re-bills every previous turn) is reserved for a genuine mid-stream
transport failure and happens at most `AGENT_STREAM_RESTART_ATTEMPTS` times (default 1).

Every call also carries a `max_tokens` cap by call type (`LLM_MAX_TOKENS_JUDGE` 400,
`LLM_MAX_TOKENS_SEVERITY` 32, `LLM_MAX_TOKENS_SUMMARY` 1500, `LLM_MAX_TOKENS_REPORT` 4000,
`LLM_MAX_TOKENS_AGENT` 16000 for Claude agent turns, `LLM_MAX_TOKENS_MEMORY_AGENT` 2000) —
see [Environment Variables](/docs/configuration/environment#output-token-caps). The
auxiliary caps are applied only where thinking is really off for the call: with
`thinking: disabled` sent to Haiku 5.5 in native mode the judge verdict is capped at 400
tokens, but when an auxiliary call lands on Opus 5.5 / Sonnet 5.5 (thinking cannot be
disabled) or goes through the OpenAI-compatible gateway (no portable thinking switch), the
cap is dropped and the model keeps its default output limit, so reasoning tokens never
truncate the answer. Model instances for the non-streaming auxiliary calls are shared per
process, so the judge, summaries, severity and memory calls no longer build a new Bedrock
client (two boto3 clients each) per call.

## RCA Model Configuration

Background RCA uses the single-agent path by default (`ORCHESTRATOR_ENABLED=false`), configured via `RCA_MODEL`. An opt-in multi-agent orchestrator is also available — see [Multi-agent orchestrator](#multi-agent-orchestrator) below.

### Single-agent RCA (default)

By default, Aurora uses `anthropic/claude-haiku-5.5` (the cheap tier) for background Root Cause Analysis. You can change this to any supported provider/model.

```bash
# Format: provider/model-name
RCA_MODEL=anthropic/claude-haiku-5.5
```

**Examples:**

```bash
# Anthropic (default)
RCA_MODEL=anthropic/claude-haiku-5.5

# OpenAI
RCA_MODEL=openai/gpt-4o

# Google AI
RCA_MODEL=google/gemini-3.5-flash

# Vertex AI — recommended for RCA when LLM_PROVIDER_MODE=vertex
RCA_MODEL=vertex/gemini-3.6-flash

# Ollama (local)
RCA_MODEL=ollama/llama3.1

# AWS Bedrock (native — explicit EU inference-profile id; not needed under LLM_PROVIDER_MODE=bedrock)
RCA_MODEL=bedrock/eu.anthropic.claude-haiku-5-5
```

When `RCA_MODEL` is not set, the default depends on `RCA_OPTIMIZE_COSTS`:
- `RCA_OPTIMIZE_COSTS=true` (default; unset or empty also count as true): Uses the cheap tier — `anthropic/claude-haiku-5.5`, or `MAIN_MODEL` when the cheap tier is not routable (see [Model tiers](#model-tiers))
- `RCA_OPTIMIZE_COSTS=false`: Uses `anthropic/claude-opus-5.5`

### Model tiers

Interactive chat runs on `MAIN_MODEL` (default `anthropic/claude-opus-5.5`). Everything that runs in the background or as a classifier defaults to the **cheap tier**, `anthropic/claude-haiku-5.5`, and has its own variable so you can raise one without the others. Aurora also switches extended thinking **off** for these classifier/summary calls on Claude models that allow it (Haiku 5.5, Sonnet 5, Opus 5, Opus 4.7/4.8), so no output budget is spent on chain-of-thought.

The cheap tier is only the default **when your provider settings can route a Claude model**: `LLM_PROVIDER_MODE=openrouter`, `anthropic` or `bedrock`, or `ANTHROPIC_API_KEY` set under direct routing. Otherwise every variable in the table below falls back to `MAIN_MODEL` — the model your deployment serves by definition — so an OpenAI / Google / Vertex / Ollama-only install, or a Bedrock gateway pinned to explicit `bedrock/<id>` values, keeps working without setting each one by hand. This matters because the command-safety judge and the input rail fail closed: handing them a model nobody can route would block every message and command. An explicit value always wins over the default.

**`MAIN_MODEL` pinned to an explicit `bedrock/<id>`** keeps the cheap tier on that Bedrock route in every `LLM_PROVIDER_MODE`: in native mode, when the pin is a Claude inference profile on a geo AWS publishes Haiku 5.5 for (`us`, `eu`, `au`, `jp`, `global`), the default becomes `bedrock/<geo>.anthropic.claude-haiku-5-5` (an `eu.` pin gives `bedrock/eu.anthropic.claude-haiku-5-5`); otherwise — gateway mode, an `apac.` or unknown prefix, a non-Anthropic model — it is `MAIN_MODEL` itself. It never moves to another provider, so a stray `ANTHROPIC_API_KEY` cannot pull the judge, summaries or memory calls off your EU Bedrock route. Likewise, under `LLM_PROVIDER_MODE=bedrock` in an `ap-*` region with clean model ids the cheap tier is not derived (there is no `apac.` Haiku 5.5 profile) and the variables follow `MAIN_MODEL` — in gateway mode (`BEDROCK_BASE_URL` set) as well, because the gateway receives the same region-derived profile id as native mode; set `RCA_MODEL` etc. explicitly there, e.g. to a `bedrock/jp.` or `bedrock/au.` profile.

| Variable | Used by | Default |
|----------|---------|---------|
| `RCA_MODEL` | Background root-cause analysis agent | cheap tier (via `RCA_OPTIMIZE_COSTS`; `anthropic/claude-opus-5.5` when that is `false`) |
| `GUARDRAILS_LLM_MODEL` | Command-safety judge + prompt-injection input rail | cheap tier (`google/gemini-2.5-flash-lite` under `openrouter`) |
| `SUMMARIZATION_MODEL` | Incident/initial summaries, synopses, repo & channel metadata | cheap tier |
| `TOOL_OUTPUT_SUMMARIZATION_MODEL` | Compressing oversized tool outputs | `SUMMARIZATION_MODEL` |
| `SUGGESTION_MODEL` | Hypotheses, diagnostics plan, recommendations | cheap tier |
| `ENRICHMENT_MODEL` | Next Steps descriptions/summaries | cheap tier |
| `EMAIL_REPORT_MODEL` | RCA summary / email report | cheap tier |
| `SEVERITY_MODEL` | One-word severity classification | cheap tier |
| `MEMORY_AGENT_MODEL` | Memory extraction agent after each investigation | cheap tier |
| `MEMORY_SELECTOR_MODEL` | Per-message memory selector | cheap tier |
| `RECURRENCE_AGENT_MODEL` | Root-cause recurrence agent | cheap tier |
| `JIRA_RCA_JUDGE_MODEL` | Jira automatic-RCA yes/no judge | cheap tier |
| `VISUALIZATION_MODEL` | Infrastructure graph extraction (`VISUALIZATION_ENABLED=true`) | cheap tier |

All of them use the same `provider/model` format as `MAIN_MODEL` and an empty value means "use the default" ("cheap tier" = `anthropic/claude-haiku-5.5` when routable, else `MAIN_MODEL`, as explained above).

**Summarizer fallback.** `TOOL_OUTPUT_SUMMARIZATION_MODEL` inherits `SUMMARIZATION_MODEL`, so a value kept from an older install (for example `SUMMARIZATION_MODEL=google/gemini-2.5-flash` on a deployment that now runs `LLM_PROVIDER_MODE=bedrock` without Google credentials) would point tool-output compression at a model nobody can serve. When the configured model's provider is not set up, the shared summarizer (`summarize_text`: oversized tool outputs, conversation compaction) logs one warning per model and runs that summary on the cheap-tier default instead of hard-truncating the output. Only the "provider not configured" case triggers this; network or model errors still fail the summary and the caller truncates as before. Set `SUMMARIZATION_MODEL` / `TOOL_OUTPUT_SUMMARIZATION_MODEL` to a model your deployment serves to silence the warning. The incident-report and alert summaries (`SUMMARIZATION_MODEL`, `EMAIL_REPORT_MODEL`) are not covered by this fallback and still need a servable model.

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

1. Keep `RCA_OPTIMIZE_COSTS=true` (the default) so background RCA and every auxiliary call run on the cheap tier — see [Model tiers](#model-tiers)
2. Prompt caching is automatic for Claude models on Bedrock native, the Anthropic API and OpenRouter — see [Prompt caching](#prompt-caching); confirm hits with the CloudWatch cache metrics
3. Auxiliary calls are trimmed and capped out of the box: the safety judge sees a trimmed copy of the user message and its verdicts are cached per (session, message, command) for `COMMAND_SAFETY_VERDICT_TTL_SECONDS`; oversized tool outputs are summarized once (head + tail window) and shared between the context store and the agent loop; the memory extraction agent runs once per turn with new user input and at most 8 turns; every call type has a `max_tokens` cap — see [Retries, timeouts and output caps](#retries-timeouts-and-output-caps)
4. Background RCA runs trim their conversation history at `RCA_CONTEXT_TOKEN_BUDGET` tokens (default 200K, messages only — the system prompt and tool definitions are sent on top; the alert / task message is always kept and the cut is reused between turns so the prompt cache keeps hitting) and summarize tool outputs above `RCA_TOOL_OUTPUT_PASS_THROUGH_CHARS` characters (default 20K) — see [Environment Variables](/docs/configuration/environment#ai-features). Interactive chat is not affected
5. Measure what you save: `llm_usage_tracking` carries `cache_read_input_tokens` / `cache_write_input_tokens` per call and `estimated_cost` prices them at their own rates; on Bedrock, set `BEDROCK_REGIONAL_PREMIUM` to your invoice's geo-profile premium (default 1.10) — see [Prompt caching](#prompt-caching)
6. Use OpenRouter for flexible, pay-per-token pricing
7. Use Ollama for zero API costs (requires local GPU)

## Safety Guardrail Model

Aurora can run an LLM-based command safety judge before executing commands, catching novel dangerous behavior that deterministic rules cannot anticipate.

```bash
# Format: provider/model-name (same as MAIN_MODEL / RCA_MODEL)
GUARDRAILS_LLM_MODEL=openai/gpt-4o-mini
```

The safety judge model can be any provider supported above. A fast, cheap, **non-reasoning** model is recommended since this runs on every command execution — reasoning models waste tokens on chain-of-thought for a simple Yes/No classification. See [Command Safety Configuration](../configuration/command-safety.md) for full setup details.

## Next Steps Enrichment Model

The Next Steps recommender polishes suggestion descriptions and summaries with a separate, lightweight model.

```bash
# Defaults to the cheap tier (anthropic/claude-haiku-5.5, or MAIN_MODEL when the
# cheap tier is not routable — see "Model tiers") when unset or empty.
ENRICHMENT_MODEL=anthropic/claude-haiku-5.5
```

This is light text-formatting work (turning validated fixes into readable descriptions), so a small, cheap model is recommended. Same `provider/model` format as `MAIN_MODEL`.

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

- Native mode requires an **inference-profile id**, not the bare model id. Use a geo-prefixed id such as `bedrock/eu.anthropic.claude-opus-5-5` (`eu.`/`us.`/`apac.`), or let `LLM_PROVIDER_MODE=bedrock` derive it from clean `anthropic/...` names.

### Bedrock: "Unable to locate credentials" / "NoRegionError"

- Native mode needs a region — set `BEDROCK_REGION` (or `AWS_REGION` / `AWS_DEFAULT_REGION`).
- Provide an identity: an IAM role / the default AWS credential chain (recommended), or `BEDROCK_ACCESS_KEY_ID` + `BEDROCK_SECRET_ACCESS_KEY`. `BEDROCK_PROFILE` only works when `~/.aws` is mounted into every Aurora container.
- Confirm the identity has `bedrock:InvokeModel` permissions and that model access is granted in the Bedrock console.

### Bedrock: "AccessDeniedException" calling the gateway

- In gateway mode, set `BEDROCK_BASE_URL` to the OpenAI **base** path (ending in `/v1`); Aurora appends `/chat/completions`.
- If your gateway requires a key, set `BEDROCK_API_KEY`. If it doesn't, leave it unset.
