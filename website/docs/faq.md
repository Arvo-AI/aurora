---
sidebar_position: 7
---

# FAQ

Frequently asked questions about Aurora.

## General

### What is Aurora?

Aurora is an AI-powered root cause analysis tool that helps Site Reliability Engineers investigate and resolve incidents. It uses LLM agents to query your cloud infrastructure and observability tools using natural language.

### Is Aurora open source?

Yes. Aurora is licensed under Apache License 2.0. The source code is available at [github.com/arvo-ai/aurora](https://github.com/arvo-ai/aurora).

### Do I need cloud provider accounts?

**No.** Aurora works without any cloud provider accounts. You only need an LLM API key to get started. Connectors are optional.

## Setup

### What are the system requirements?

- Docker and Docker Compose >= 28.x
- 8GB+ RAM recommended
- Any OS that runs Docker (macOS, Linux, Windows)

### How long does setup take?

About 5 minutes for basic setup. The quickstart guide walks you through:
1. Clone repository
2. Run `make init`
3. Add LLM API key
4. Run `make prod-prebuilt` (or `make prod-local` to build from source)

### Which LLM provider should I use?

**OpenRouter** is the shortest path: one key, many models. Direct keys also work for OpenAI, Anthropic, Google Gemini, Vertex AI, and AWS Bedrock. Ollama runs models on your own machine when the deployment cannot call out. See [LLM providers](/docs/integrations/llm-providers).

### Can I run Aurora without Docker?

Docker is the supported deployment method. Running without Docker requires manually setting up all services (PostgreSQL, Redis, Vault, etc.).

## Features

### What can I ask Aurora?

Examples:
- "Why did the API latency spike at 3am?"
- "Show me all errors from the payment service in the last hour"
- "What changed in our Kubernetes deployments this week?"
- "Summarize the alerts from PagerDuty today"

### Which cloud providers are supported?

AWS, Microsoft Azure, Google Cloud, OVH, Scaleway, Cloudflare, and Fly.io. On-prem machines connect over SSH, including through Tailscale. The [connector catalog](/docs/integrations/connectors) is the current list.

### Which observability and incident tools are supported?

Monitoring: Datadog, Grafana, New Relic, Splunk, Elastic, Dynatrace, Coroot, ThousandEyes, Netdata, Sentry, and CloudWatch.

Incident management: PagerDuty, OpsGenie / JSM, incident.io, ServiceNow, and BigPanda.

Chat: Slack, Microsoft Teams, and Google Chat. Docs and tickets: Confluence, Jira, Notion, and SharePoint. Code: GitHub, GitLab, and Bitbucket. CI/CD: Jenkins, CloudBees, and Spinnaker.

### Can I use Aurora with Kubernetes?

Yes. Install the in-cluster kubectl agent, or upload a kubeconfig for a cluster that is not on a connected cloud. See the [kubectl agent documentation](https://github.com/arvo-ai/aurora/blob/main/kubectl-agent/README.md).

### What are Actions and Monitor?

**Actions** are background agent sessions: a postmortem when an incident resolves, a weekly alert-gap or right-sizing audit, a nightly memory cleanup, or instructions you write. **Monitor** is fleet runs, SRE timings, tool usage, living artifacts, and an audit log. Both are described in [Product](/docs/product).

## Security

### Where are my credentials stored?

Credentials are stored in the secrets backend, HashiCorp Vault by default or AWS Secrets Manager when `SECRETS_BACKEND=aws_secrets_manager`. The database only stores references to that backend.

### Is my data sent to external services?

Only when you explicitly query cloud providers or LLM services:
- Cloud queries go to your configured providers
- Investigation prompts go to your LLM provider
- No telemetry or analytics are collected

### Can I self-host everything?

Yes. Aurora runs on your infrastructure. Ollama keeps model calls on that host. Bedrock and Vertex use public endpoints unless you add AWS PrivateLink or Private Service Connect. With `LLM_PROVIDER_MODE=ollama`, a model id that does not start with `ollama/` is sent to that model's own provider when its credentials are set.

## Development

### How do I contribute?

See the [Contributing Guide](https://github.com/arvo-ai/aurora/blob/main/CONTRIBUTING.md). The basic flow:
1. Fork the repository
2. Create a feature branch
3. Make changes
4. Submit a pull request

### Where do I report bugs?

Open an issue at [github.com/arvo-ai/aurora/issues](https://github.com/arvo-ai/aurora/issues) with:
- Clear description
- Steps to reproduce
- Error logs
- Environment details

### How do I request features?

Open an issue with the `enhancement` label describing:
- Use case
- Proposed solution
- Alternatives considered

## Troubleshooting

### Aurora won't start

See the [Troubleshooting Guide](/docs/troubleshooting). Common causes:
- Missing `.env` file (run `make init`)
- Port conflicts
- Docker not running

### "Vault token not set"

After first startup, you need to extract the root token:
```bash
docker logs vault-init 2>&1 | grep "Root Token:"
```
Add it to `.env` as `VAULT_TOKEN` and restart.

### Investigations are slow

LLM latency depends on the model, how many tools the investigation calls, and the provider. A smaller model (Haiku, a Flash or mini model) is enough for narrow questions. Background RCA can be pinned to a cheaper model with cost optimization. See [LLM providers](/docs/integrations/llm-providers).

## Pricing

### Is Aurora free?

Aurora itself is free and open source. You'll pay for:
- **LLM API usage** - Based on tokens processed
- **Cloud provider costs** - If using cloud connectors
- **Infrastructure** - If deploying to cloud (optional)

### How much does the LLM cost?

Depends on usage. Rough estimates:
- Simple query: $0.01-0.05
- Full investigation: $0.10-0.50
- Complex RCA: $0.50-2.00

OpenRouter shows per-request costs for transparency.
