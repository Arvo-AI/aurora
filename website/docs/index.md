---
slug: /
sidebar_position: 1
---

# Introduction

Aurora is an automated **root cause analysis** investigation tool that uses AI agents to help Site Reliability Engineers resolve incidents faster.

## What is Aurora?

Aurora connects to your cloud infrastructure (GCP, AWS, Azure) and observability tools (Datadog, Grafana, PagerDuty) to help you investigate incidents using natural language. Instead of manually querying logs, metrics, and traces across multiple dashboards, you can ask Aurora questions like:

- "Why did the API latency spike at 3am?"
- "Show me all errors from the payment service in the last hour"
- "What changed in our Kubernetes deployments this week?"

Aurora uses LLM-powered agents to understand your question, query the relevant systems, and synthesize the findings into actionable insights.

## Key Features

- **AI-Powered Investigation** - Natural language queries powered by LLMs (OpenRouter, OpenAI, Anthropic)
- **Multi-Cloud Support** - Connect to GCP, AWS, Azure, and OVH
- **Observability Integrations** - Datadog, Grafana, PagerDuty, Netdata
- **Communication Tools** - Slack and GitHub integrations
- **Local-First** - Run entirely on your machine with Docker
- **Secure** - HashiCorp Vault for secrets management

## Quick Links

- [**Quickstart**](/docs/getting-started/quickstart) - Get Aurora running in 5 minutes
- [**Development Setup**](/docs/getting-started/dev-setup) - Set up for local development
- [**Architecture**](/docs/architecture/overview) - Understand how Aurora works
- [**Configuration**](/docs/configuration/environment) - Environment variables reference

## Requirements

- Docker and Docker Compose >= 28.x
- At least one LLM API key (OpenRouter, OpenAI, or Anthropic)

**No cloud provider accounts required** to get started. Cloud connectors are optional.

## License

Aurora is open source under the [Apache License 2.0](https://github.com/arvo-ai/aurora/blob/main/LICENSE).
