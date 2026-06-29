<div align="center">

<img src=".github/assets/aurora-banner.gif" alt="Aurora — The open-source AI agent that investigates your incidents for you" width="100%" />

<a href="https://github.com/Arvo-AI/aurora/stargazers"><img src="https://img.shields.io/github/stars/Arvo-AI/aurora?style=for-the-badge&logo=github&color=181717" alt="Stars" /></a>&nbsp;
<a href="https://github.com/Arvo-AI/aurora/releases/latest"><img src="https://img.shields.io/github/v/release/Arvo-AI/aurora?style=for-the-badge&label=version&color=2ea44f" alt="Version" /></a>&nbsp;
<a href="https://github.com/Arvo-AI/aurora/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue?style=for-the-badge" alt="License" /></a>&nbsp;
<a href="https://discord.com/invite/ccbN4FwHxM"><img src="https://img.shields.io/badge/Discord-Join_us-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord" /></a>&nbsp;
<a href="https://cal.com/arvo-ai"><img src="https://img.shields.io/badge/Book_a_Demo-FF6B4A?style=for-the-badge&logo=googlecalendar&logoColor=white" alt="Book a Demo" /></a>&nbsp;
<a href="https://aurora-ai.net"><img src="https://img.shields.io/badge/Try_it_Live-aurora--ai.net-8B5CF6?style=for-the-badge&logo=rocket&logoColor=white" alt="Try Aurora Live" /></a>

<br />

<a href="https://aurora-ai.net">Try Aurora Live</a>&nbsp;&nbsp;&bull;&nbsp;&nbsp;<a href="#quick-start">Quick Start</a>&nbsp;&nbsp;&bull;&nbsp;&nbsp;<a href="https://arvo-ai.github.io/aurora/">Documentation</a>&nbsp;&nbsp;&bull;&nbsp;&nbsp;<a href="#integrations">Integrations</a>&nbsp;&nbsp;&bull;&nbsp;&nbsp;<a href="https://www.arvoai.ca">Website</a>&nbsp;&nbsp;&bull;&nbsp;&nbsp;<a href="CHANGELOG.md">Changelog</a>

</div>

---

## What's New

- **Artifacts** — Persistent agent-maintained documents in Monitor, continuously updated as investigations progress
- **Actions** — Automated post-RCA workflows (generate postmortems, open fix PRs, notify Slack) triggered on investigation completion
- **AWS Bedrock Support** — Use Claude, Titan, and other Bedrock models via IAM auth
- **Fly.io Connector** — Investigate incidents on Fly.io infrastructure
- **CloudBees Enterprise** — Operations Center + Feature Management connector
- **Kubeconfig Upload** — Connect on-prem Kubernetes clusters without a cloud provider
- **CloudWatch Alarm Webhooks** — Ingest AWS CloudWatch alarms directly as incidents
- **Extensibility Hooks** — Gate LLM calls, enforce seat limits, and customize behavior with lifecycle hooks

See the full [CHANGELOG](CHANGELOG.md) for all releases.

---

## Why Aurora?

When an alert fires at 3 AM, your on-call engineer spends 30-60 minutes doing the same thing every time: checking dashboards, running kubectl commands, reading logs, correlating deployments, and searching Slack history.

**Aurora does all of that autonomously.** It receives the alert, spins up AI agents that investigate across your entire stack, and delivers a structured RCA by the time you open your laptop.

| Without Aurora | With Aurora |
|:---|:---|
| Engineer paged, context-switches | Alert auto-triaged in background |
| 30-60 min manual investigation | AI agents investigate in parallel |
| Knowledge siloed in individuals | Investigation reasoning captured |
| Postmortem written days later | Postmortem auto-generated |
| Same failure, different engineer | Knowledge base grows over time |

<div align="center">

<a href="https://www.loom.com/share/8082df350ea64a928f7fadbf811c5138">
  <img src=".github/assets/aurora-demo.gif" alt="Aurora Demo — AI agent investigating a cloud incident" width="100%" />
</a>

<sub>Click to watch the full demo</sub>

</div>

---

## Features

<table>
<tr>
<td width="50%" valign="top">

### Agentic Investigation

AI agents dynamically select from 30+ tools. They run `kubectl`, `aws`, `az`, and `gcloud` in **sandboxed Kubernetes pods**, query logs, check deployments, and correlate data — all autonomously.

</td>
<td width="50%">

<img src=".github/assets/ai-investigation.png" alt="AI agent investigating" width="100%" />

</td>
</tr>
<tr>
<td width="50%" valign="top">

### AI Code Fixes

Aurora doesn't just find root cause — it suggests fixes and can generate pull requests with the remediation.

</td>
<td width="50%">

<img src=".github/assets/pr-suggestion.png" alt="PR suggestion" width="100%" />

</td>
</tr>
<tr>
<td width="50%" valign="top">

### Incident Dashboard

Ingest alerts from PagerDuty, Datadog, Grafana, New Relic, OpsGenie, incident.io and more. Every alert auto-triggers a background investigation.

</td>
<td width="50%">

<img src=".github/assets/incidents-dashboard.png" alt="Incidents dashboard" width="100%" />

</td>
</tr>
<tr>
<td width="50%" valign="top">

### Auto-Generated Postmortems

Detailed reports with timeline, root cause, impact assessment, and remediation steps. Export directly to Confluence, Notion, or SharePoint.

</td>
<td width="50%">

<img src=".github/assets/postmortem-report.png" alt="Postmortem report" width="100%" />

</td>
</tr>
<tr>
<td width="50%" valign="top">

### Infrastructure Knowledge Graph

Visualize your entire infrastructure as a dependency graph. When an incident occurs, Aurora traces blast radius across services and providers.

</td>
<td width="50%">

<img src=".github/assets/infrastructure-graph.png" alt="Infrastructure graph" width="100%" />

</td>
</tr>
</table>

**More capabilities:** Knowledge Base RAG &bull; Multi-Cloud (AWS, Azure, GCP, OVH, Scaleway, Cloudflare) &bull; Any LLM (OpenAI, Anthropic, Gemini, Vertex AI, OpenRouter, Ollama) &bull; Terraform/IaC Analysis &bull; MCP Server (Cursor, Claude Desktop, Windsurf) &bull; Org-level Command Policies &bull; SigmaHQ Guardrails &bull; NeMo Input Rail

---

## Quick Start

Get Aurora running locally in under 5 minutes:

```bash
git clone https://github.com/arvo-ai/aurora.git && cd aurora

make init                # Generate secure secrets
nano .env                # Add your LLM API key (OpenRouter, OpenAI, etc.)
make prod-prebuilt       # Pull prebuilt images and start
```

Open **http://localhost:3000**. The first user to register becomes admin.

> [!TIP]
> Aurora works without any cloud provider accounts. The LLM API key is the only external requirement. Connectors are optional.

<details>
<summary><strong>Vault setup (required after first start)</strong></summary>
<br />

```bash
# Get the auto-generated root token
docker logs vault-init 2>&1 | grep "Root Token:"

# Add it to .env
echo "VAULT_TOKEN=hvs.your-token-here" >> .env

# Restart to connect services to Vault
make down && make prod-prebuilt
```

</details>

<details>
<summary><strong>Pin a specific version</strong></summary>
<br />

```bash
make prod-prebuilt VERSION=v1.2.3
```

</details>

<details>
<summary><strong>Build from source</strong></summary>
<br />

```bash
make prod-local
```

</details>

---

## Deploy

| Method | Best for |
|--------|----------|
| `make prod-prebuilt` | Local evaluation, single-node |
| [Helm chart](https://arvo-ai.github.io/aurora/docs/deployment/kubernetes) | Production Kubernetes (GKE, EKS, AKS) |
| [Air-tight bundle](https://arvo-ai.github.io/aurora/docs/deployment/vm-deployment#secure-deployment-air-tight) | Air-gapped / restricted networks |

### Kubernetes (Helm)

```bash
helm repo add aurora https://raw.githubusercontent.com/Arvo-AI/aurora/gh-pages
helm repo update
helm show values aurora/aurora-oss > my-values.yaml
# Edit my-values.yaml, then:
helm install aurora-oss aurora/aurora-oss -n aurora --create-namespace -f my-values.yaml
```

Also available via OCI: `oci://ghcr.io/arvo-ai/charts/aurora-oss`

---

## Integrations

<div align="center">

![PagerDuty](https://img.shields.io/badge/PagerDuty-06AC38?style=flat-square&logo=pagerduty&logoColor=white)
![Datadog](https://img.shields.io/badge/Datadog-632CA6?style=flat-square&logo=datadog&logoColor=white)
![Grafana](https://img.shields.io/badge/Grafana-F46800?style=flat-square&logo=grafana&logoColor=white)
![New Relic](https://img.shields.io/badge/New_Relic-1CE783?style=flat-square&logo=newrelic&logoColor=white)
![OpsGenie](https://img.shields.io/badge/OpsGenie-0052CC?style=flat-square&logo=opsgenie&logoColor=white)
![Dynatrace](https://img.shields.io/badge/Dynatrace-1496FF?style=flat-square&logo=dynatrace&logoColor=white)
![incident.io](https://img.shields.io/badge/incident.io-FF4785?style=flat-square&logoColor=white)
![AWS](https://img.shields.io/badge/AWS-232F3E?style=flat-square&logo=amazonwebservices&logoColor=white)
![Azure](https://img.shields.io/badge/Azure-0078D4?style=flat-square&logo=microsoftazure&logoColor=white)
![GCP](https://img.shields.io/badge/GCP-4285F4?style=flat-square&logo=googlecloud&logoColor=white)
![Cloudflare](https://img.shields.io/badge/Cloudflare-F38020?style=flat-square&logo=cloudflare&logoColor=white)
![Kubernetes](https://img.shields.io/badge/Kubernetes-326CE5?style=flat-square&logo=kubernetes&logoColor=white)
![Terraform](https://img.shields.io/badge/Terraform-844FBA?style=flat-square&logo=terraform&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat-square&logo=docker&logoColor=white)
![GitHub](https://img.shields.io/badge/GitHub-181717?style=flat-square&logo=github&logoColor=white)
![Jenkins](https://img.shields.io/badge/Jenkins-D24939?style=flat-square&logo=jenkins&logoColor=white)
![Slack](https://img.shields.io/badge/Slack-4A154B?style=flat-square&logo=slack&logoColor=white)
![Google Chat](https://img.shields.io/badge/Google_Chat-34A853?style=flat-square&logo=googlechat&logoColor=white)
![Jira](https://img.shields.io/badge/Jira-0052CC?style=flat-square&logo=jira&logoColor=white)
![Confluence](https://img.shields.io/badge/Confluence-172B4D?style=flat-square&logo=confluence&logoColor=white)
![Notion](https://img.shields.io/badge/Notion-000000?style=flat-square&logo=notion&logoColor=white)
![SharePoint](https://img.shields.io/badge/SharePoint-0078D4?style=flat-square&logo=microsoftsharepoint&logoColor=white)
![Bitbucket](https://img.shields.io/badge/Bitbucket-0052CC?style=flat-square&logo=bitbucket&logoColor=white)
![Splunk](https://img.shields.io/badge/Splunk-000000?style=flat-square&logo=splunk&logoColor=white)
![Fly.io](https://img.shields.io/badge/Fly.io-7B36ED?style=flat-square&logo=flydotio&logoColor=white)
![CloudBees](https://img.shields.io/badge/CloudBees-1997B5?style=flat-square&logoColor=white)
![Tailscale](https://img.shields.io/badge/Tailscale-242424?style=flat-square&logo=tailscale&logoColor=white)

**LLMs:** OpenAI &bull; Anthropic &bull; Google Gemini &bull; Vertex AI &bull; AWS Bedrock &bull; OpenRouter &bull; Ollama (air-gapped)

</div>

---

## Architecture

```
aurora/
├── server/      # Python API (Flask), Celery workers, LangGraph agents
├── client/      # Next.js frontend
├── deploy/      # Helm chart, Docker Compose, deployment scripts
├── config/      # Default configuration
├── scripts/     # CLI utilities
└── website/     # Documentation (Docusaurus)
```

| Layer | Stack |
|-------|-------|
| AI Orchestration | LangGraph, 30+ tool definitions |
| Backend | Python, Flask, Celery |
| Frontend | Next.js, TypeScript |
| Graph DB | Memgraph |
| Vector Store | Weaviate |
| Secrets | HashiCorp Vault, AWS Secrets Manager |
| Storage | PostgreSQL, Redis, S3-compatible |

---

## Security

- **Sandboxed execution** — Agent commands run in isolated Kubernetes pods with NetworkPolicy, not on your control plane
- **RBAC** — Three roles (Admin, Editor, Viewer) enforced at API and UI layers via Casbin
- **Closed registration** — First user is admin; all others are invited
- **SigmaHQ guardrails** — 37 threat detection signatures on agent command execution
- **NeMo input rail** — Prompt injection detection on every turn
- **No telemetry** — Zero data sent to Arvo AI. Fully self-hosted.

---

## Data Privacy

Aurora is **100% self-hosted**. Your incident data never leaves your infrastructure.

- All data on your infrastructure (Docker or Kubernetes)
- No telemetry or tracking to Arvo AI
- Secrets encrypted at rest in Vault or AWS Secrets Manager
- LLM calls go directly from your infra to your chosen provider
- Use Ollama for fully air-gapped operation

---

## Community

<div align="center">

<a href="https://discord.com/invite/ccbN4FwHxM"><img src="https://img.shields.io/badge/Discord-Join_the_community-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord" /></a>&nbsp;&nbsp;
<a href="https://cal.com/arvo-ai"><img src="https://img.shields.io/badge/Book_a_Demo-FF6B4A?style=for-the-badge&logo=googlecalendar&logoColor=white" alt="Book a Demo" /></a>

</div>

<br />

- **[GitHub Issues](https://github.com/Arvo-AI/aurora/issues)** — Bug reports and feature requests
- **[GitHub Discussions](https://github.com/Arvo-AI/aurora/discussions)** — Ideas and Q&A
- **[Documentation](https://arvo-ai.github.io/aurora/)** — Full deployment and configuration guides
- **[Blog](https://www.arvoai.ca/blog)** — SRE best practices, incident management guides
- **[Contributing](CONTRIBUTING.md)** — We welcome PRs! Read the guide first.

---

## License

[Apache License 2.0](LICENSE) — free forever, no per-seat or per-incident pricing.

---

<div align="center">
<br />
<strong>If Aurora helps your team, <a href="https://github.com/Arvo-AI/aurora">star us on GitHub</a></strong>
<br /><br />
<a href="https://github.com/Arvo-AI/aurora/stargazers"><img src="https://img.shields.io/github/stars/Arvo-AI/aurora?style=for-the-badge&logo=github&color=181717" alt="Stars" /></a>
<br /><br />
</div>
