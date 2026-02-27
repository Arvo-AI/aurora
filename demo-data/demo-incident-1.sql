-- Demo Incident 1 postmortem: Database connection pool exhausted - payment-service
-- The incident itself lives in aurora_db.sql; this adds the postmortem (idempotent).

-- Ensure resolved status on existing installs
UPDATE incidents SET status = 'resolved' WHERE id = 'a867c4b3-f09c-4a4f-a2bc-1c390d967b37' AND status != 'resolved';

INSERT INTO postmortems (incident_id, user_id, content, generated_at, updated_at)
VALUES (
  'a867c4b3-f09c-4a4f-a2bc-1c390d967b37',
  'ab209180-626b-4601-8042-9f6328d03ae9',
  $postmortem_1$# Postmortem: Database connection pool exhausted - payment-service on product-api-cluster

**Date:** 2026-02-12 14:36 UTC
**Duration:** 2m
**Severity:** medium
**Service:** product-api-cluster
**Source:** pagerduty

## Summary
On February 12, 2026 at 14:36 UTC, the payment-service on product-api-cluster experienced database connection pool exhaustion, causing payment processing degradation. The incident was traced to a database connection leak introduced in commit 7d6f976c on February 5, 2026, which moved connection cleanup from a `finally` block to a conditional statement inside the `try` block, preventing connections from being released in error scenarios and when the `amount` parameter was null.

## Timeline
- **14:36:00** - Alert triggered: Database connection pool exhausted on payment-service
- **14:37:06** - Investigation began; relevant runbook for database connection issues identified
- **14:37:22** - OpenTelemetry traces discovered showing payment processing activity; recent commit from February 5th identified
- **14:37:41** - Correlated alert found: high database query latency, suggesting connection leak
- **14:38:07** - Critical finding: Connection leak identified in payment-service code
- **14:38:11** - Root cause isolated to conditional `client.release()` on line 61-65 that skips release when `amount` is null
- **14:38:25** - Root cause confirmed by comparing original code (commit 34d3d327) with buggy code (commit 7d6f976c)
- **14:39:15** - Fix proposed: Move `client.release()` back to `finally` block

## Root Cause
Commit 7d6f976c titled "Improving speed of checkout for successful payments" by Olivier Trudeau on February 5, 2026 introduced a database connection leak. The commit moved the `client.release()` call from a `finally` block (which always executes) to inside the `try` block with a conditional check `if (amount != null)`. This created two critical failure modes: (1) if an error occurs in the try block before reaching the conditional release statement, the connection is never returned to the pool, and (2) requests where `amount` is null or undefined never release their connections. The original code correctly placed connection cleanup in the `finally` block, ensuring connections were always released regardless of success, failure, or early returns. With only 5 connections in the pool and a single replica handling all traffic, even a small number of leaked connections from failed requests or null-amount health checks rapidly exhausted the pool, causing subsequent requests to timeout waiting for available connections.

## Impact
The incident affected payment processing capabilities on the product-api-cluster for approximately 2 minutes. The medium severity classification indicates degraded service rather than complete outage, with intermittent connection pool exhaustion where some payment requests succeeded while others timed out waiting for available database connections. Checkout operations were impacted during this period, potentially affecting customer transactions and revenue.

## Resolution
A code fix was proposed to move `client.release()` back to the `finally` block to ensure database connections are always released regardless of execution path. The fix addresses the connection leak by guaranteeing cleanup in all scenarios: successful requests, failed requests, null amount values, and exceptions.

## Action Items
- [ ] Deploy fix to move `client.release()` back to the `finally` block in payment-service index.js
- [ ] Review and rollback commit 7d6f976c if immediate fix deployment is not feasible
- [ ] Scale payment-service from 1 to 3 replicas to distribute connection pool load (15 total connections)
- [ ] Implement connection pool metrics monitoring (active connections, idle connections, waiting requests) with alerting thresholds
- [ ] Query OpenTelemetry spans around 14:36 UTC to identify specific failed transactions and quantify customer impact
- [ ] Add automated tests to verify database connections are released in all code paths (success, failure, null parameters)
- [ ] Review connection pool configuration: assess whether 5 max connections per replica is sufficient for current traffic patterns
- [ ] Conduct code review of the February 5th commit to identify if similar patterns exist elsewhere in the codebase
- [ ] Update deployment process to require database connection handling review for changes affecting data access layers

## Lessons Learned
**What Went Well:**
- The incident was detected quickly through automated alerting
- Correlated alerts (connection pool exhaustion + high query latency) provided clear diagnostic signals
- OpenTelemetry traces and commit history enabled rapid root cause identification
- Investigation timeline from alert to root cause confirmation was under 3 minutes

**What Went Wrong:**
- Code review process did not catch the removal of connection cleanup from the `finally` block
- Moving connection release logic from `finally` to conditional code violated fundamental resource management patterns
- No automated tests verified connection release behavior in error scenarios
- Single replica configuration with limited connection pool (5 connections) created a fragile system with no redundancy
- Connection pool metrics were not being monitored, preventing early detection of the leak

**Where We Got Lucky:**
- The incident duration was only 2 minutes, limiting customer impact
- The issue manifested quickly after deployment (7 days) rather than remaining dormant

**Prevention Measures:**
- Establish code review guidelines requiring that database connections, file handles, and other resources must always be released in `finally` blocks or using try-with-resources patterns
- Implement connection pool metrics and monitoring as a standard requirement for all database-connected services
- Add integration tests that verify resource cleanup under failure conditions, not just happy path scenarios
- Require minimum 2 replicas for production services to provide redundancy during resource exhaustion scenarios
- Consider implementing connection leak detection tooling in development and staging environments$postmortem_1$,
  '2026-02-27 01:27:37',
  '2026-02-27 01:27:37'
)
ON CONFLICT (incident_id) DO NOTHING;
