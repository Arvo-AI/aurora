---
slug: /
sidebar_position: 1
---

# Introduction

Aurora is an automated **root cause analysis** investigation tool that uses AI agents to help Site Reliability Engineers resolve incidents faster.

## What is Aurora?

An alert becomes an incident. Aurora investigates across your cloud accounts, Kubernetes clusters, and observability tools, then leaves a structured root cause and whatever follow-up you configured as an Action. The Generate Postmortem action writes the postmortem when the incident is resolved.

You can also just ask:

- "Why did the API latency spike at 3am?"
- "Show me errors from the checkout service in the last hour"
- "What changed in our Kubernetes deployments this week?"

The product surfaces (Chat, Incidents, Actions, Monitor, Memory) are described in [Product](/docs/product).

## Key Features

- **Investigation** — Agent or Ask mode. The agent runs `kubectl`, cloud CLIs, and log queries in a sandbox and cites what came back.
- **Incidents** — Alerts from PagerDuty, Datadog, Grafana, ServiceNow, incident.io, CloudWatch, and the other [connectors](/docs/integrations/connectors) start a background RCA.
- **Actions** — Instructions that run on an incident, on a schedule, or when you click Run. Built-ins write the postmortem, audit alert gaps, right-size workloads, and consolidate memory.
- **Monitor** — Fleet runs, SRE timings, tool usage, living artifacts, and an audit log.
- **Memory** — Runbooks and notes the agent reads and updates, shared across the org.
- **Anywhere you already talk** — Slack, Microsoft Teams, and Google Chat.
- **Your model** — OpenRouter, OpenAI, Anthropic, Gemini, Vertex AI, AWS Bedrock, or Ollama. See [LLM providers](/docs/integrations/llm-providers).
- **Self-hosted** — Docker Compose or Helm. Credentials stay in Vault or AWS Secrets Manager. [Command safety](/docs/configuration/command-safety) guardrails are on by default.

## Quick Links

- [**Product**](/docs/product) — What each screen does
- [**Quickstart**](/docs/getting-started/quickstart) — Running in a few minutes
- [**Connectors**](/docs/integrations/connectors) — Cloud, Kubernetes, alerts, chat, and docs
- [**Architecture**](/docs/architecture/overview) — How the services fit together

## Requirements

- Docker and Docker Compose >= 28.x
- At least one LLM: an API key for OpenRouter, OpenAI, Anthropic, Gemini, Vertex AI, or Bedrock, or a reachable Ollama (`OLLAMA_BASE_URL`, no key)

**No cloud provider accounts required** to get started. Cloud connectors are optional.

## License

Aurora is open source under the [Apache License 2.0](https://github.com/arvo-ai/aurora/blob/main/LICENSE).
