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

**No.** Aurora works without any cloud provider accounts. You only need an LLM API key (OpenRouter, OpenAI, or Anthropic) to get started. Cloud connectors are optional.

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

We recommend **OpenRouter** because:
- Single API key for multiple models
- Pay-per-token (no monthly commitment)
- Easy to switch between models

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

- Google Cloud Platform (GCP)
- Amazon Web Services (AWS)
- Microsoft Azure
- OVH (multi-region)

### Which observability tools are supported?

- Datadog
- Grafana
- PagerDuty
- Netdata

### Can I use Aurora with Kubernetes?

Yes. Aurora includes a kubectl agent that runs inside your cluster. See the [kubectl agent documentation](https://github.com/arvo-ai/aurora/blob/main/kubectl-agent/README.md).

## Security

### Where are my credentials stored?

All credentials are stored in HashiCorp Vault, not in the database. The database only stores references to Vault paths.

### Is my data sent to external services?

Only when you explicitly query cloud providers or LLM services:
- Cloud queries go to your configured providers
- Investigation prompts go to your LLM provider
- No telemetry or analytics are collected

### Can I self-host everything?

Yes. Aurora runs entirely on your infrastructure. You can even use self-hosted LLMs (via OpenRouter-compatible APIs).

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

LLM latency depends on:
- Model choice (GPT-4 is slower than GPT-3.5)
- Query complexity
- Provider load

Try using faster models for simple queries.

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
