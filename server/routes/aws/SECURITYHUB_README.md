# AWS Security Hub & Audit Integration

This feature bridges **AWS Security Hub** (including Amazon GuardDuty, Macie, Inspector, and IAM Access Analyzer) directly into the Aurora platform. It enables DevSecOps and SRE teams to natively ingest, track, and triage AWS security audits directly within Aurora's overarching incident management ecosystem.

## How This Enables AWS Security Auditing

AWS Security Hub centralizes hundreds of security findings across massive cloud environments. The Aurora integration automatically funnels these complex audit events via **Amazon EventBridge webhooks**. 

When a finding (e.g., *Public S3 Bucket detected by Macie* or *Cryptocurrency mining detected by GuardDuty*) triggers in AWS:
1. It is seamlessly pushed to Aurora in real-time.
2. Aurora inherently maps the finding to the exact tenant (`org_id`).
3. An **Agentic AI Triage** layer automatically intercepts the raw JSON finding, summarizes the true risk, and drafts a suggested remediation plan.
4. The audit is safely materialized in Aurora's database, waiting for a human engineer to review and approve the fix natively in the UI.

This prevents engineers from repeatedly logging into the AWS Console to hunt down audits and instead democratizes security visibility.

---

## Codebase Blueprint: What Was Built

To ensure highly reliable and secure AWS auditing capabilities, the following architectures were introduced into the codebase:

### 1. Database & Persistence (`server/utils/db/db_utils.py`)
Introduced the `aws_security_findings` schema.
* **Storage**: Secures critical finding identifiers, severity labels, and the original raw JSON payload.
* **AI Telemetry**: Houses the computed `ai_summary`, `ai_risk_level`, and `ai_suggested_fix`.
* **Idempotency**: AWS EventBridge guarantees "at-least-once" delivery, meaning it often sends duplicate events. For this reason, a strict `UNIQUE(org_id, finding_id)` constraint was created.

### 2. High-Performance Webhook Endpoint (`server/routes/aws/securityhub_routes.py`)
Introduced the `POST /aws/securityhub/webhook/<org_id>` listener.
* **API Security**: The endpoint parses an inbound `x-api-key` header mapped statically to the `user_tokens` table. It utilizes `hmac` constant-time verification to safeguard your platform against timing-based cryptographic attacks by rogue agents attempting to forge audits.
* **Dev/Prod Isolation**: Ensures the local `DEV_SECURITYHUB_API_KEY` cannot silently bypass production checks if inadvertently left inside `.env`.
* **Prometheus Observability**: Embedded `Counter` and `Histogram` metrics to natively track `aws_securityhub_events_received_total` and latency, enabling real-time Grafana observation of the integration's health.

### 3. Asynchronous Worker & AI Triage (`server/routes/aws/tasks.py`)
Routing logic immediately offloads processing into Celery via `@shared_task def process_securityhub_finding`.
* **Type-Safe Extraction**: Rigorously validates finding topologies before parsing elements `get("Id")` and `get("Severity")` to prevent upstream schema changes from crashing the worker threads.
* **Postgres UPSERT**: Rather than failing entirely when AWS updates an audit (e.g., marking a finding as "RESOLVED"), the worker issues an `ON CONFLICT DO UPDATE SET` query. The database flawlessly mutates the finding inline.
* **Failed Retry Semantics**: Transient database locks raise explicit errors to the message broker, seamlessly allowing Celery to retry inserting the audit. 

### 4. Dependency Injection (`server/requirements.txt`)
Added `prometheus-client>=0.20.0` securely to backend deployment dependencies to resolve local uninstalled instances ensuring native execution upon container rebuilds.

---

## Setting Up Your AWS Environment

To actively feed audits into this system:

1. **Obtain your Webhook Details**
   - Provide your Aurora App URL and append your destination: `https://<YOUR-DOMAIN>/api/v1/aws/securityhub/webhook/<YOUR_ORG_ID>`
   - Map a generated API key into your specific tenant (`user_tokens` row where provider is `aws_securityhub`).
2. **AWS API Destination Setup**
   - Navigate to Amazon EventBridge -> **API Destinations**.
   - Create a new connection and set your API Key under "Authorization header" (Header name: `x-api-key`).
3. **AWS EventBridge Rule**
   - Instruct EventBridge to capture audits utilizing a catch-all pattern matching: 
     ```json
     { "source": ["aws.securityhub"] }
     ```
   - Set the destination to the Aurora API Destination created above.

Your Aurora application is now actively serving as the brain of your AWS Security tracking network!

![WhatsApp Image 2026-04-11 at 1 27 00 PM](https://github.com/user-attachments/assets/f7eec989-5f26-405b-9d02-07750d40af22)
