---
sidebar_position: 4
---

# CloudBees

Aurora integrates with [CloudBees](https://www.cloudbees.com/) to provide CI/CD deployment visibility, automated incident correlation, and root cause analysis across your entire Jenkins infrastructure.

## What You Get

| Capability | Description |
|------------|-------------|
| **Deployment event tracking** | Build completions from any controller are correlated with alerts automatically |
| **RCA with build context** | Auto-generated RCA includes pipeline stages, build logs, test results, and changeset |
| **Cross-controller visibility** | Query deployments across all managed controllers via Operations Center |
| **Feature flag correlation** | Identify if a feature flag toggle caused an incident (Feature Management) |
| **Webhook-triggered RCA** | Automatically investigate failed deployments when they happen |

## Connection Modes

Aurora supports three ways to connect CloudBees:

### Single Controller

Direct connection to one CloudBees CI (Jenkins) controller. Best for teams with a single CI instance.

**You'll need:**
- Controller URL (e.g., `https://jenkins.company.com`)
- Username with API token permissions
- API Token (generated in your profile → Security → API Token)

### Operations Center

Connect to your CloudBees Operations Center to automatically discover and manage all controllers from one connection. Best for enterprises with multiple Jenkins instances.

**You'll need:**
- Operations Center URL (e.g., `https://cjoc.company.com`)
- Username with OC-level API token permissions
- API Token (generated at the Operations Center level)

After connecting, Aurora will discover all managed controllers and can query builds across all of them during incident investigation.

### Personal Access Token (PAT)

Platform-level authentication for organizations using CloudBees Platform tokens. PAT mode supports Operations Center discovery, cross-controller builds, and Feature Management — the same capabilities as OC mode. It does not support single-controller-only workflows.

**You'll need:**
- Platform URL (e.g., `https://your-org.cloudbees.io`)
- Personal Access Token (generated in Profile → Personal access tokens)

---

## Setup

1. Navigate to **Connectors** in Aurora
2. Find **CloudBees** under the CI/CD category
3. Select your connection mode
4. Enter your credentials
5. Click **Connect**

For Operations Center mode, Aurora will automatically discover your managed controllers after connecting.

---

## Webhook Setup (Deployment Tracking)

To track deployments in real-time, add a webhook to your Jenkinsfile post-build step. After connecting, Aurora provides:

- A unique webhook URL for your account
- Ready-to-use Jenkinsfile snippets (Basic, OpenTelemetry, and cURL variants)
- HMAC-SHA256 signing for webhook security

Copy the webhook URL and Jenkinsfile snippet from the connected view in Aurora.

---

## RCA Actions

During incident investigation, Aurora's AI agent can use these CloudBees-specific actions:

### Standard (all modes)

- `recent_deployments` — Recent build events tracked via webhook
- `build_detail` — Changeset, causes, and build metadata
- `pipeline_stages` — Stage-level breakdown with durations
- `stage_log` — Per-stage console output
- `build_logs` — Full build console output
- `test_results` — JUnit test report data
- `blue_ocean_run` — Blue Ocean pipeline run data
- `blue_ocean_steps` — Step-level pipeline data

### Enterprise (Operations Center)

- `controller_list` — All managed controllers and their status
- `cross_controller_deployments` — Recent builds across ALL controllers
- `flag_changes` — Recent feature flag toggles (requires Feature Management)

---

## Feature Flag Correlation (Optional)

If your organization uses CloudBees Feature Management, you can optionally provide a Feature Management API token during setup. This enables Aurora to check if a feature flag was toggled before an incident occurred — a common root cause that's otherwise hard to spot.

This is configured in the "Feature flag correlation" section during Operations Center setup.

---

## Auto-trigger RCA

When enabled (default), Aurora automatically starts an investigation when a deployment webhook reports a failure. Configure this in the CloudBees connector settings under "RCA Settings."
