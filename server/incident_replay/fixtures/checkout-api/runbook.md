# checkout-api — Incident Response Runbook

## Service
`checkout-api` (payments namespace). Public-facing checkout endpoint `/v1/checkout`.
Depends on `payments-db` (PostgreSQL). Deployed via Helm (rolling).

## Triage checklist
1. Confirm the alert window against the `http_5xx_rate` and `p99_latency_ms` series.
2. Check whether a deployment landed shortly before the first error.
3. Inspect application logs for the dominant error class in the window.
4. Compare saturated resources (CPU, memory, database connections) against the
   pre-incident baseline.
5. If a deployment is implicated, compare its config diff against the previous
   revision before recommending a rollback.

## Known failure modes (non-exhaustive)
- Application regression in a newly deployed image.
- Database failover or replication lag.
- Downstream dependency latency causing request pile-up.
- Resource limit or connection budget misconfiguration.
- Traffic spike beyond provisioned capacity.

## Escalation
SEV-2 → page the payments on-call. SEV-1 → page SRE lead.
