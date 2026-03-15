-- Demo Incident: UPI callback timeout cascade causing recharge transaction failures
-- Source: Grafana | Cloud: AWS/EKS (ap-south-1) | Tools: kubectl, Splunk, GitHub, Web Search
-- Company context: FreeCharge - Digital payments platform (recharges, bill payments, UPI, wallets)

DO $$
DECLARE
  v_incident_id UUID := 'e5f6a7b8-c9d0-4e1f-2a3b-4c5d6e7f8901';
  v_chat_session_id VARCHAR(50) := 'f6a7b8c9-d0e1-4f2a-3b4c-5d6e7f8a9012';
  v_demo_user_id VARCHAR(255) := 'ab209180-626b-4601-8042-9f6328d03ae9';
  v_grafana_alert_id INTEGER;
  v_incident_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM incidents WHERE id = v_incident_id) INTO v_incident_exists;
  IF v_incident_exists THEN
    RAISE NOTICE 'Demo incident (FreeCharge) already exists, skipping.';
    RETURN;
  END IF;

  RAISE NOTICE 'Inserting demo incident (FreeCharge)...';

  -- 1. Insert grafana_alerts source record
  INSERT INTO grafana_alerts (user_id, alert_uid, alert_title, alert_state, rule_name, rule_url, dashboard_url, panel_url, payload, received_at)
  VALUES (
    v_demo_user_id,
    'grafana-demo-fc-001',
    'Elevated transaction failure rate on recharge-service',
    'alerting',
    'Transaction Failure Rate > 10%',
    'https://grafana.internal/alerting/grafana-demo-fc-001/view',
    'https://grafana.internal/d/payments-overview/payments-platform-overview',
    'https://grafana.internal/d/payments-overview/payments-platform-overview?viewPanel=7',
    '{
      "title": "Elevated transaction failure rate on recharge-service",
      "state": "alerting",
      "ruleUrl": "https://grafana.internal/alerting/grafana-demo-fc-001/view",
      "message": "Transaction failure rate for recharge-service has exceeded 10% threshold. Current rate: 34%. UPI callback processing timeouts detected. Wallet-service pending transaction queue growing.",
      "evalMatches": [{"metric": "transaction_failure_rate", "value": 34.2, "tags": {"service": "recharge-service", "namespace": "payments", "payment_method": "upi"}}],
      "tags": {"env": "production", "service": "recharge-service", "namespace": "payments", "cluster": "eks-prod-ap-south-1", "team": "payments-platform", "severity": "critical"}
    }'::jsonb,
    '2026-02-26 10:15:00'
  ) RETURNING id INTO v_grafana_alert_id;

  -- 2. Insert main incident record
  INSERT INTO incidents (
    id, user_id, source_type, source_alert_id, status, severity,
    alert_title, alert_service, alert_environment, aurora_status,
    aurora_summary, aurora_chat_session_id, started_at, analyzed_at,
    active_tab, created_at, updated_at, alert_metadata,
    correlated_alert_count, affected_services, visualization_code,
    visualization_updated_at
  ) VALUES (
    v_incident_id, v_demo_user_id, 'grafana', v_grafana_alert_id,
    'resolved', 'critical',
    'Elevated transaction failure rate on recharge-service',
    'recharge-service', 'production', 'complete',
    '', v_chat_session_id::uuid,
    '2026-02-26 10:15:00', '2026-02-26 10:35:00',
    'thoughts', '2026-02-26 10:15:00', '2026-02-26 10:35:00',
    jsonb_build_object(
      'is_demo', true,
      'grafana_alert_uid', 'grafana-demo-fc-001',
      'incidentId', 'grafana-demo-fc-001',
      'source_url', 'https://grafana.internal/alerting/grafana-demo-fc-001/view'
    ),
    3,
    ARRAY['recharge-service', 'wallet-service', 'upi-callback-handler', 'payment-gateway'],
    '', '2026-02-26 10:35:00'
  );

  -- 3. Update summary
  UPDATE incidents SET aurora_summary = '# Incident Report: UPI Callback Timeout Cascade on Recharge Service

## Summary

On February 26, 2026 at 10:15 IST, Grafana alerting detected a critical spike in transaction failure rates on the recharge-service in the EKS production cluster (eks-prod-ap-south-1). The failure rate surged to 34%, primarily affecting UPI-based mobile recharge transactions. Users reported "Transaction Failed" errors and "money deducted but recharge not completed" complaints.

AWS infrastructure checks confirmed the EKS cluster and all 6 EC2 nodes (m5.2xlarge across 3 AZs) were healthy [1, 2]. However, the ALB target group showed all 4 recharge-service targets in an unhealthy state with 503 health check failures [3]. CloudWatch metrics revealed 5,421 cumulative 5xx errors in 30 minutes with exponential growth starting at 09:50 [4], confirming the issue was at the application layer, not infrastructure.

Investigation revealed that all 25 Gunicorn workers on each recharge-service pod were blocked on synchronous HTTP calls to the NPCI UPI gateway, which was experiencing intermittent latency spikes (3-5s response times vs normal ~200ms) [7, 8]. Splunk log analysis confirmed thousands of UPI callback timeout errors and a saturated worker pool [9, 10]. The root cause was traced to PR #234 ("Add retry logic for UPI callback processing"), merged 2 days earlier, which introduced synchronous retries (3 attempts x 10s timeout = up to 30s blocking per request) directly in the main request handler [13, 14]. Under normal load (~60 req/s), the 25-worker pool saturated within minutes when NPCI latency spiked, as each worker was blocked for up to 30 seconds.

The cascading effect impacted the wallet-service: pending debits from initiated but unconfirmed UPI transactions piled up, with the pending transaction queue growing to 2,847 entries [12]. Users saw their wallet balance deducted but received no recharge confirmation, generating a surge in customer support tickets.

The deployment was rolled back to the pre-PR #234 version to immediately restore service [16]. A code fix was submitted via PR #236 to make UPI callback processing asynchronous via SQS, with a circuit breaker pattern for NPCI gateway calls.

## Suggested Next Steps

- **Implement async UPI callback processing**: Move UPI callback handling to an SQS queue with dedicated consumer workers, decoupling it from the main request thread pool.
- **Add circuit breaker for NPCI gateway**: Implement a circuit breaker (50% error threshold, 30s cooldown) to fail fast when the NPCI gateway is degraded, preventing worker pool saturation.
- **Add timeout enforcement**: Set a hard 2-second timeout on all external payment gateway HTTP calls, with immediate fallback to async retry.
- **Deploy pending transaction reconciliation job**: Create a Celery task to automatically reconcile wallet debits against UPI confirmation status, preventing stale pending transactions.
- **Load test UPI callback path under degraded NPCI conditions**: Simulate NPCI latency spikes in staging to validate the async architecture handles graceful degradation.'
  WHERE id = v_incident_id;

  -- 4. Update visualization
  UPDATE incidents SET visualization_code = '{
  "nodes": [
    {"id": "recharge-svc", "label": "recharge-service", "type": "service", "status": "failed", "parentId": null},
    {"id": "wallet-svc", "label": "wallet-service", "type": "service", "status": "degraded", "parentId": null},
    {"id": "upi-handler", "label": "upi-callback-handler", "type": "service", "status": "failed", "parentId": "recharge-svc"},
    {"id": "npci-gateway", "label": "NPCI UPI Gateway", "type": "service", "status": "degraded", "parentId": null},
    {"id": "payment-gw", "label": "payment-gateway", "type": "service", "status": "degraded", "parentId": null},
    {"id": "alb", "label": "ALB (payments-prod)", "type": "service", "status": "degraded", "parentId": null},
    {"id": "pr-234", "label": "PR #234 Deploy", "type": "deployment", "status": "failed", "parentId": null},
    {"id": "npci-latency", "label": "NPCI Latency Spike", "type": "event", "status": "failed", "parentId": null},
    {"id": "alert-txn-fail", "label": "Transaction Failure Alert", "type": "alert", "status": "failed", "parentId": null},
    {"id": "alert-wallet-queue", "label": "Wallet Pending Queue Alert", "type": "alert", "status": "failed", "parentId": null},
    {"id": "alert-alb-5xx", "label": "ALB 5xx Spike Alert", "type": "alert", "status": "failed", "parentId": null},
    {"id": "worker-pool", "label": "Gunicorn Workers (25)", "type": "metric", "status": "failed", "parentId": "recharge-svc"}
  ],
  "edges": [
    {"source": "pr-234", "target": "upi-handler", "label": "added synchronous retry logic", "type": "causation"},
    {"source": "npci-latency", "target": "upi-handler", "label": "3-5s response times", "type": "causation"},
    {"source": "upi-handler", "target": "worker-pool", "label": "blocks workers for 30s per request", "type": "causation"},
    {"source": "worker-pool", "target": "recharge-svc", "label": "all 25 workers exhausted", "type": "causation"},
    {"source": "recharge-svc", "target": "alb", "label": "readiness probe fails", "type": "causation"},
    {"source": "alb", "target": "alert-alb-5xx", "label": "5xx errors spike", "type": "causation"},
    {"source": "recharge-svc", "target": "alert-txn-fail", "label": "34% failure rate", "type": "causation"},
    {"source": "recharge-svc", "target": "wallet-svc", "label": "pending debits unresolved", "type": "dependency"},
    {"source": "wallet-svc", "target": "alert-wallet-queue", "label": "2847 pending transactions", "type": "causation"},
    {"source": "recharge-svc", "target": "payment-gw", "label": "upstream dependency", "type": "dependency"}
  ],
  "rootCauseId": "pr-234",
  "affectedIds": ["upi-handler", "worker-pool", "recharge-svc", "wallet-svc", "alb", "payment-gw", "alert-txn-fail", "alert-wallet-queue", "alert-alb-5xx"],
  "version": 5,
  "updatedAt": "2026-02-26T10:35:00.000000+00:00"
}'
  WHERE id = v_incident_id;

  -- 5. Insert correlated alerts
  INSERT INTO incident_alerts (user_id, incident_id, source_type, source_alert_id, alert_title, alert_service, alert_severity, correlation_strategy, correlation_score, correlation_details, alert_metadata, received_at) VALUES
  (v_demo_user_id, v_incident_id, 'grafana', v_grafana_alert_id, 'Elevated transaction failure rate on recharge-service', 'recharge-service', 'critical', 'primary', 1.0, '{}', '{"tags":["env:production","service:recharge-service","payment_method:upi"]}'::jsonb, '2026-02-26 10:15:00'),
  (v_demo_user_id, v_incident_id, 'grafana', v_grafana_alert_id + 1, 'Pending transaction queue growing on wallet-service', 'wallet-service', 'high', 'service_dependency', 0.92, '{"reason":"Wallet-service pending debit queue growing due to unconfirmed UPI transactions from recharge-service failures"}'::jsonb, '{"tags":["env:production","service:wallet-service","queue:pending_debits"]}'::jsonb, '2026-02-26 10:18:00'),
  (v_demo_user_id, v_incident_id, 'grafana', v_grafana_alert_id + 2, 'ALB 5xx error spike on payments-prod target group', 'payments-alb', 'high', 'time_proximity', 0.88, '{"reason":"ALB 5xx errors spike coincides with recharge-service readiness probe failures and worker pool exhaustion"}'::jsonb, '{"tags":["env:production","service:payments-alb","target_group:recharge-tg"]}'::jsonb, '2026-02-26 10:16:30');

  RAISE NOTICE 'Demo incident (FreeCharge) summary, viz, alerts inserted.';

END $$;

-- Thoughts, citations, suggestions
DO $$
DECLARE
  v_incident_id UUID := 'e5f6a7b8-c9d0-4e1f-2a3b-4c5d6e7f8901';
  v_already_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM incident_thoughts WHERE incident_id = v_incident_id) INTO v_already_exists;
  IF v_already_exists THEN
    RAISE NOTICE 'Demo incident (FreeCharge) thoughts/citations already exist, skipping.';
    RETURN;
  END IF;

  -- 6. Insert incident thoughts
  INSERT INTO incident_thoughts (incident_id, timestamp, content, thought_type) VALUES
  (v_incident_id, '2026-02-26 10:15:30', 'Starting investigation of elevated transaction failure rate (34%) on recharge-service in EKS production cluster (ap-south-1). This is a critical payment service -- user transactions are failing. Let me first check the AWS infrastructure health.', 'analysis'),
  (v_incident_id, '2026-02-26 10:15:35', 'Checking EKS cluster status and EC2 node health for the payments-prod cluster in ap-south-1.', 'analysis'),
  (v_incident_id, '2026-02-26 10:15:40', 'EKS cluster is ACTIVE and all 6 EC2 nodes are in Running state with healthy status checks. No instance-level issues -- infrastructure layer is fine.', 'analysis'),
  (v_incident_id, '2026-02-26 10:15:45', 'Checking ALB target group health for the recharge-service targets and CloudWatch metrics for 5xx error patterns.', 'analysis'),
  (v_incident_id, '2026-02-26 10:15:50', 'ALB target group shows 3 of 4 targets in "unhealthy" state (health check failing with 503). CloudWatch confirms a sharp 5xx spike starting at 09:58 -- HTTPCode_Target_5XX_Count went from 0 to 2,341 in 15 minutes. The infrastructure is healthy but the application targets are failing. Let me check the pods.', 'analysis'),
  (v_incident_id, '2026-02-26 10:15:55', 'Checking pod health for recharge-service. Need to determine if pods are crashing or if this is a downstream dependency issue.', 'analysis'),
  (v_incident_id, '2026-02-26 10:16:00', 'All 4 recharge-service pods are Running but readiness probes are failing (0/1 READY). HPA scaled a 4th pod but it hit the same issue. This is not a crash or capacity problem -- something is blocking the application.', 'analysis'),
  (v_incident_id, '2026-02-26 10:16:10', 'Checking Kubernetes events for the payments namespace to understand the timeline of failures.', 'analysis'),
  (v_incident_id, '2026-02-26 10:16:20', 'Events confirm ALB has deregistered targets as unhealthy. Let me check pod logs to identify what is blocking the workers.', 'analysis'),
  (v_incident_id, '2026-02-26 10:16:30', 'Logs show repeated UPI callback timeout errors: "NPCI gateway response exceeded 10000ms". Workers are stuck waiting on the external NPCI UPI gateway. The timeout is 10 seconds and there appear to be retries.', 'analysis'),
  (v_incident_id, '2026-02-26 10:16:40', 'Querying Splunk for a broader view of the timeout pattern and worker pool status across all pods.', 'analysis'),
  (v_incident_id, '2026-02-26 10:16:50', 'Splunk confirms: 1,247 UPI callback timeout events in the last 30 minutes. Worker pool utilization is at 25/25 (100%) on all pods. The recharge-service is completely saturated.', 'analysis'),
  (v_incident_id, '2026-02-26 10:17:00', 'Checking for the cascading impact on wallet-service. If UPI debits are initiated but never confirmed, the wallet-service will have a growing queue of pending transactions.', 'analysis'),
  (v_incident_id, '2026-02-26 10:17:10', 'Confirmed: wallet-service has 2,847 pending debit transactions that were initiated but never received a confirmation callback. This is the "money deducted but recharge not done" scenario.', 'analysis'),
  (v_incident_id, '2026-02-26 10:17:20', 'The UPI callback handler is blocking with synchronous retries. This seems like a recent code change -- let me check GitHub for recent deployments to the recharge-service.', 'analysis'),
  (v_incident_id, '2026-02-26 10:17:30', 'Found it. PR #234 "Add retry logic for UPI callback processing" was merged 2 days ago. Let me examine the diff.', 'analysis'),
  (v_incident_id, '2026-02-26 10:17:40', 'PR #234 diff confirms the root cause: a synchronous retry loop was added to the UPI callback handler -- 3 retries with a 10-second timeout each (up to 30s blocking per request). Under normal conditions this works fine, but when NPCI is slow, every worker gets stuck for 30 seconds.', 'analysis'),
  (v_incident_id, '2026-02-26 10:17:50', 'With 25 workers and requests arriving at ~60/s, the pool saturates in under a minute when NPCI response times exceed 1-2 seconds. The retry logic amplifies the problem -- without retries, only 1 worker would be blocked per slow request; with 3 retries, each slow request holds a worker 3x longer.', 'analysis'),
  (v_incident_id, '2026-02-26 10:18:00', 'Searching web for UPI payment callback best practices and circuit breaker patterns for external payment gateway integration.', 'analysis'),
  (v_incident_id, '2026-02-26 10:18:10', 'Best practice is clear: UPI callbacks should be processed asynchronously via a message queue (SQS/Kafka). Synchronous retries in the request path are an anti-pattern for payment gateways. A circuit breaker should fail-fast when the NPCI gateway is degraded.', 'analysis'),
  (v_incident_id, '2026-02-26 10:18:20', 'Immediate action: roll back the recharge-service deployment to the version before PR #234 to restore service.', 'analysis'),
  (v_incident_id, '2026-02-26 10:18:30', 'Rollback successful. Worker pool utilization is dropping. Transaction success rate is recovering.', 'analysis'),
  (v_incident_id, '2026-02-26 10:18:40', 'Creating a code fix: replace synchronous UPI callback retries with async SQS-based processing and add a circuit breaker for the NPCI gateway.', 'analysis'),
  (v_incident_id, '2026-02-26 10:18:50', 'PR #236 created with async UPI callback processing via SQS and circuit breaker (pybreaker, 50% failure threshold, 30s reset timeout, 2s hard timeout on NPCI calls).', 'analysis'),
  (v_incident_id, '2026-02-26 10:19:00', 'Investigation complete. Root cause was synchronous retry logic in UPI callback handler (PR #234) that blocked worker threads when NPCI gateway was slow. Rolled back and submitted async fix as PR #236.', 'analysis');

  -- 7. Insert citations (AWS infrastructure checks first, then kubectl, then Splunk/GitHub)
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '1', 'Cloud CLI', 'aws eks describe-cluster --name payments-prod --region ap-south-1 --query "cluster.{status:status,version:version,endpoint:endpoint,platformVersion:platformVersion}"', '{
    "status": "ACTIVE",
    "version": "1.29",
    "endpoint": "https://A1B2C3D4E5.gr7.ap-south-1.eks.amazonaws.com",
    "platformVersion": "eks.8"
}

EKS cluster payments-prod is ACTIVE and healthy. Kubernetes version 1.29 on platform eks.8.', '2026-02-26 10:15:35'),
  (v_incident_id, '2', 'Cloud CLI', 'aws ec2 describe-instances --filters "Name=tag:eks:cluster-name,Values=payments-prod" "Name=instance-state-name,Values=running" --query "Reservations[].Instances[].{InstanceId:InstanceId,Type:InstanceType,AZ:Placement.AvailabilityZone,State:State.Name}" --output table --region ap-south-1', '--------------------------------------------------------------
|                    DescribeInstances                       |
+----------------+----------------+------------------+-------+
|       AZ       |  InstanceId    |      Type        | State |
+----------------+----------------+------------------+-------+
| ap-south-1a    | i-0a1b2c3d4e   | m5.2xlarge       |running|
| ap-south-1a    | i-0b2c3d4e5f   | m5.2xlarge       |running|
| ap-south-1b    | i-0c3d4e5f6g   | m5.2xlarge       |running|
| ap-south-1b    | i-0d4e5f6g7h   | m5.2xlarge       |running|
| ap-south-1c    | i-0e5f6g7h8i   | m5.2xlarge       |running|
| ap-south-1c    | i-0f6g7h8i9j   | m5.2xlarge       |running|
+----------------+----------------+------------------+-------+

All 6 EC2 nodes across 3 AZs are running. System and instance status checks passing. No infrastructure-level issues.', '2026-02-26 10:15:38'),
  (v_incident_id, '3', 'Cloud CLI', 'aws elbv2 describe-target-health --target-group-arn arn:aws:elasticloadbalancing:ap-south-1:891377104296:targetgroup/recharge-tg/a1b2c3d4e5f6g7h8 --region ap-south-1', '{
    "TargetHealthDescriptions": [
        {"Target": {"Id": "10.0.12.41", "Port": 8080}, "TargetHealth": {"State": "unhealthy", "Reason": "Target.ResponseCodeMismatch", "Description": "Health checks failed with these codes: [503]"}},
        {"Target": {"Id": "10.0.13.22", "Port": 8080}, "TargetHealth": {"State": "unhealthy", "Reason": "Target.ResponseCodeMismatch", "Description": "Health checks failed with these codes: [503]"}},
        {"Target": {"Id": "10.0.14.15", "Port": 8080}, "TargetHealth": {"State": "unhealthy", "Reason": "Target.ResponseCodeMismatch", "Description": "Health checks failed with these codes: [503]"}},
        {"Target": {"Id": "10.0.12.67", "Port": 8080}, "TargetHealth": {"State": "unhealthy", "Reason": "Target.ResponseCodeMismatch", "Description": "Health checks failed with these codes: [503]"}}
    ]
}

All 4 ALB targets are unhealthy -- health checks returning 503. The application is alive but unable to serve requests.', '2026-02-26 10:15:42'),
  (v_incident_id, '4', 'Cloud CLI', 'aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB --metric-name HTTPCode_Target_5XX_Count --dimensions Name=TargetGroup,Value=targetgroup/recharge-tg/a1b2c3d4e5f6g7h8 Name=LoadBalancer,Value=app/payments-alb/9z8y7x6w5v4u3t2 --start-time 2026-02-26T09:45:00Z --end-time 2026-02-26T10:15:00Z --period 300 --statistics Sum --region ap-south-1', 'Timestamp                 Sum
2026-02-26T09:45:00Z      0
2026-02-26T09:50:00Z      12
2026-02-26T09:55:00Z      87
2026-02-26T10:00:00Z      456
2026-02-26T10:05:00Z      891
2026-02-26T10:10:00Z      1,634
2026-02-26T10:15:00Z      2,341

CloudWatch confirms sharp 5xx escalation starting at 09:50, with exponential growth. Total 5,421 5xx errors in 30 minutes. Infrastructure is healthy -- issue is at the application layer.', '2026-02-26 10:15:48'),
  (v_incident_id, '5', 'Cloud CLI', 'kubectl get pods -n payments -l app=recharge-service -o wide', 'NAME                                  READY   STATUS    RESTARTS   AGE    IP             NODE
recharge-svc-7f8e6d5c4-ab2k9        0/1     Running   0          6h     10.0.12.41     ip-10-0-12-41.ap-south-1.compute.internal
recharge-svc-7f8e6d5c4-cd4m1        0/1     Running   0          6h     10.0.13.22     ip-10-0-13-22.ap-south-1.compute.internal
recharge-svc-7f8e6d5c4-ef6n3        0/1     Running   0          6h     10.0.14.15     ip-10-0-14-15.ap-south-1.compute.internal
recharge-svc-7f8e6d5c4-gh8p5        0/1     Running   0          22m    10.0.12.67     ip-10-0-12-67.ap-south-1.compute.internal

Note: All pods Running but READY 0/1 -- readiness probes are failing. 4th pod was scaled up by HPA 22 minutes ago.', '2026-02-26 10:15:55'),
  (v_incident_id, '6', 'Cloud CLI', 'kubectl get events -n payments --sort-by=.lastTimestamp --field-selector reason!=Pulled,reason!=Created,reason!=Started | tail -20', 'LAST SEEN   TYPE      REASON              OBJECT                                     MESSAGE
1m          Warning   Unhealthy           pod/recharge-svc-7f8e6d5c4-ab2k9          Readiness probe failed: HTTP probe failed with statuscode: 503
1m          Warning   Unhealthy           pod/recharge-svc-7f8e6d5c4-cd4m1          Readiness probe failed: HTTP probe failed with statuscode: 503
1m          Warning   Unhealthy           pod/recharge-svc-7f8e6d5c4-ef6n3          Readiness probe failed: HTTP probe failed with statuscode: 503
1m          Warning   Unhealthy           pod/recharge-svc-7f8e6d5c4-gh8p5          Readiness probe failed: HTTP probe failed with statuscode: 503
3m          Normal    SuccessfulRescale   horizontalpodautoscaler/recharge-svc-hpa  New size: 4; reason: cpu resource utilization above target
5m          Warning   BackOff             pod/recharge-svc-7f8e6d5c4-gh8p5          Back-off restarting failed container
22m         Normal    Scheduled           pod/recharge-svc-7f8e6d5c4-gh8p5          Successfully assigned payments/recharge-svc-7f8e6d5c4-gh8p5 to ip-10-0-12-67.ap-south-1.compute.internal
25m         Warning   FailedTarget        targetgroupbinding/recharge-tg            Deregistered target 10.0.12.41:8080 (unhealthy)
25m         Warning   FailedTarget        targetgroupbinding/recharge-tg            Deregistered target 10.0.13.22:8080 (unhealthy)
25m         Warning   FailedTarget        targetgroupbinding/recharge-tg            Deregistered target 10.0.14.15:8080 (unhealthy)', '2026-02-26 10:16:05'),
  (v_incident_id, '7', 'Cloud CLI', 'kubectl logs recharge-svc-7f8e6d5c4-ab2k9 -n payments --tail=30', '{"level":"info","service":"recharge-service","message":"Processing UPI recharge request","txn_id":"TXN-2026022610-4821","user_id":"usr_fc_8k2m1","amount":299,"operator":"airtel","timestamp":"2026-02-26T10:14:52.103Z"}
{"level":"info","service":"recharge-service","message":"UPI payment initiated via NPCI gateway","txn_id":"TXN-2026022610-4821","upi_ref":"UPI26022610154821","timestamp":"2026-02-26T10:14:52.341Z"}
{"level":"warn","service":"recharge-service","message":"UPI callback timeout: NPCI gateway response exceeded 10000ms (attempt 1/3)","txn_id":"TXN-2026022610-4821","gateway":"npci-upi-prod.npci.org.in","elapsed_ms":10002,"timestamp":"2026-02-26T10:15:02.345Z"}
{"level":"warn","service":"recharge-service","message":"UPI callback timeout: NPCI gateway response exceeded 10000ms (attempt 2/3)","txn_id":"TXN-2026022610-4821","gateway":"npci-upi-prod.npci.org.in","elapsed_ms":10001,"timestamp":"2026-02-26T10:15:12.348Z"}
{"level":"error","service":"recharge-service","message":"UPI callback timeout: NPCI gateway response exceeded 10000ms (attempt 3/3) - giving up","txn_id":"TXN-2026022610-4821","gateway":"npci-upi-prod.npci.org.in","elapsed_ms":10003,"timestamp":"2026-02-26T10:15:22.351Z"}
{"level":"error","service":"recharge-service","message":"Transaction failed: UPI callback confirmation timeout after 3 retries","txn_id":"TXN-2026022610-4821","total_elapsed_ms":30210,"status":"FAILED","timestamp":"2026-02-26T10:15:22.355Z"}
{"level":"error","service":"recharge-service","message":"Worker pool exhausted: 25/25 workers busy, request queued","queue_depth":47,"timestamp":"2026-02-26T10:15:23.001Z"}
{"level":"error","service":"recharge-service","message":"Worker pool exhausted: 25/25 workers busy, request queued","queue_depth":52,"timestamp":"2026-02-26T10:15:24.103Z"}
{"level":"warn","service":"recharge-service","message":"Health check endpoint returning 503: worker pool saturated","active_workers":25,"max_workers":25,"timestamp":"2026-02-26T10:15:25.002Z"}
{"level":"error","service":"recharge-service","message":"Transaction failed: UPI callback confirmation timeout after 3 retries","txn_id":"TXN-2026022610-4834","total_elapsed_ms":30185,"status":"FAILED","timestamp":"2026-02-26T10:15:28.912Z"}', '2026-02-26 10:16:25'),
  (v_incident_id, '8', 'Cloud CLI', 'kubectl logs recharge-svc-7f8e6d5c4-ab2k9 -n payments --since=30m | grep -c "Worker pool exhausted"', '312', '2026-02-26 10:16:30');

  -- More citations
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '9', 'Splunk Search', 'index=kubernetes namespace=payments sourcetype=recharge-service "UPI callback timeout" | timechart span=5m count', 'Time                  count
2026-02-26T09:45:00   0
2026-02-26T09:50:00   2
2026-02-26T09:55:00   8
2026-02-26T10:00:00   67
2026-02-26T10:05:00   198
2026-02-26T10:10:00   412
2026-02-26T10:15:00   560

Trend: Exponential growth starting ~09:55 when NPCI gateway latency increased. By 10:15, timeout rate is saturating the worker pool.', '2026-02-26 10:16:50'),
  (v_incident_id, '10', 'Splunk Search', 'index=kubernetes namespace=payments sourcetype=recharge-service "Worker pool exhausted" | stats count by host', 'host                                     count
recharge-svc-7f8e6d5c4-ab2k9            312
recharge-svc-7f8e6d5c4-cd4m1            298
recharge-svc-7f8e6d5c4-ef6n3            305
recharge-svc-7f8e6d5c4-gh8p5            187

Total: 1,102 worker pool exhaustion events across all pods in the last 30 minutes. Pod gh8p5 has fewer because it was scaled up by HPA 22 minutes ago (and hit the same issue immediately).', '2026-02-26 10:17:00'),
  (v_incident_id, '11', 'Cloud CLI', 'kubectl exec -n payments recharge-svc-7f8e6d5c4-ab2k9 -- python -c "import requests; r=requests.get(''http://localhost:8080/metrics''); print(r.text)" | grep -E "gunicorn|worker|active"', 'gunicorn_workers_total 25
gunicorn_workers_active 25
gunicorn_requests_queued 64
gunicorn_request_duration_seconds_bucket{le="1.0"} 2841
gunicorn_request_duration_seconds_bucket{le="10.0"} 3102
gunicorn_request_duration_seconds_bucket{le="30.0"} 4587
gunicorn_request_duration_seconds_bucket{le="+Inf"} 4590

Note: 25/25 workers active (100% utilization). 64 requests in queue. Request duration distribution shows a spike at the 30s bucket -- consistent with 3x10s retry timeout.', '2026-02-26 10:17:05'),
  (v_incident_id, '12', 'Splunk Search', 'index=kubernetes namespace=payments sourcetype=wallet-service "pending debit" status=PENDING | stats count', 'count
2847

2,847 wallet debit transactions in PENDING status with no UPI confirmation callback received. These represent the "money deducted but recharge not done" user complaints.', '2026-02-26 10:17:15'),
  (v_incident_id, '13', 'MCP: GitHub', 'github_rca: recent commits on recharge-service', '{
  "commits": [
    {"sha": "a8c2f34", "message": "Merge PR #234: Add retry logic for UPI callback processing", "author": "priya.dev", "date": "2026-02-24T11:30:00Z"},
    {"sha": "d1e7b92", "message": "Fix DTH recharge operator code mapping", "author": "rahul.eng", "date": "2026-02-23T15:20:00Z"},
    {"sha": "f3a9c56", "message": "Update Grafana dashboard queries for payment metrics", "author": "amit.sre", "date": "2026-02-22T09:45:00Z"}
  ],
  "timeline_correlation": "Commit a8c2f34 (PR #234) deployed 2 days before issue start. CRITICAL correlation -- UPI callback retry logic was added."
}', '2026-02-26 10:17:35'),
  (v_incident_id, '14', 'MCP: GitHub', 'github_rca: diff for PR #234', '{
  "status": "success",
  "action": "diff",
  "files_changed": 2,
  "additions": 42,
  "deletions": 8,
  "key_changes": [
    {
      "file": "services/recharge-service/app/handlers/upi_callback.py",
      "diff": "  def process_upi_callback(self, txn_id: str, upi_ref: str) -> dict:\n-     response = self.npci_client.verify_payment(upi_ref)\n-     if response.status_code == 200:\n-         return self._confirm_recharge(txn_id, response.json())\n-     raise UPIVerificationError(f\"NPCI returned {response.status_code}\")\n+     # Add retry logic for reliability (3 attempts)\n+     max_retries = 3\n+     timeout_seconds = 10\n+     for attempt in range(1, max_retries + 1):\n+         try:\n+             response = self.npci_client.verify_payment(\n+                 upi_ref, timeout=timeout_seconds\n+             )\n+             if response.status_code == 200:\n+                 return self._confirm_recharge(txn_id, response.json())\n+             logger.warning(\n+                 f\"NPCI returned {response.status_code}, attempt {attempt}/{max_retries}\"\n+             )\n+         except requests.Timeout:\n+             logger.warning(\n+                 f\"UPI callback timeout: NPCI gateway response exceeded {timeout_seconds*1000}ms \"\n+                 f\"(attempt {attempt}/{max_retries})\",\n+                 extra={\"txn_id\": txn_id, \"gateway\": NPCI_GATEWAY_HOST}\n+             )\n+         if attempt < max_retries:\n+             continue\n+     raise UPIVerificationError(\"UPI callback confirmation timeout after 3 retries\")"
    },
    {
      "file": "services/recharge-service/app/config.py",
      "diff": "+ NPCI_CALLBACK_TIMEOUT = int(os.getenv(\"NPCI_CALLBACK_TIMEOUT\", \"10\"))\n+ NPCI_CALLBACK_MAX_RETRIES = int(os.getenv(\"NPCI_CALLBACK_MAX_RETRIES\", \"3\"))"
    }
  ],
  "pr_description": "Add retry logic to UPI callback processing for improved reliability. When NPCI gateway is temporarily unavailable, retries up to 3 times with 10s timeout per attempt. This fixes occasional single-request failures during NPCI maintenance windows.",
  "reviewers": ["rahul.eng"],
  "review_comments": "Approved by rahul.eng: Looks good, retries will help with the intermittent NPCI timeouts we have been seeing."
}', '2026-02-26 10:17:45'),
  (v_incident_id, '15', 'Web Search', 'UPI payment callback async processing best practices circuit breaker pattern India fintech', 'Search results summary:

1. **NPCI UPI Technical Guidelines** (npci.org.in): "UPI callback responses should be processed asynchronously. Merchant systems must not block the primary transaction thread waiting for callback confirmation. Implement a separate callback consumer with idempotent processing."

2. **Circuit Breaker Pattern for Payment Gateways** (martinfowler.com): "Payment gateway integrations should use circuit breaker pattern with: (a) failure threshold (50%), (b) timeout window (30s), (c) half-open state for gradual recovery. Never use synchronous retries in the request handling thread."

3. **Building Resilient UPI Integrations** (razorpay.com/blog): "Process UPI status callbacks via a message queue (SQS/Kafka). Use a separate worker pool for callback verification. Set hard 2-second timeouts on NPCI gateway calls. Implement a reconciliation job that periodically checks pending transactions against NPCI status API."', '2026-02-26 10:18:10'),
  (v_incident_id, '16', 'Cloud CLI', 'kubectl rollout undo deployment/recharge-svc -n payments', 'deployment.apps/recharge-svc rolled back to revision 14

Rollback to pre-PR #234 deployment (revision 14) initiated. Previous version does not have the synchronous retry logic.', '2026-02-26 10:18:25');

  -- 8. Insert suggestions
  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id, 'Rollback recharge-service to pre-PR #234 deployment', 'Immediately roll back the recharge-service deployment to the previous version that does not include the synchronous UPI callback retry logic. This will restore normal worker pool behavior and stop the cascading transaction failures.', 'mitigation', 'safe', 'kubectl rollout undo deployment/recharge-svc -n payments', '2026-02-26 10:18:30'),
  (v_incident_id, 'Reconcile pending wallet transactions', 'Run the wallet reconciliation job to resolve the 2,847 pending debit transactions. This will check each transaction against NPCI UPI status API and either confirm the recharge or refund the wallet debit.', 'mitigation', 'low', 'kubectl exec -n payments deploy/wallet-service -- python manage.py reconcile_pending_debits --status=PENDING --older-than=30m', '2026-02-26 10:18:35'),
  (v_incident_id, 'Verify rollback health and worker pool recovery', 'After rollback, verify that recharge-service worker pool utilization has returned to normal and ALB targets are healthy.', 'diagnostic', 'safe', 'kubectl get pods -n payments -l app=recharge-service && kubectl exec -n payments deploy/recharge-svc -- curl -s localhost:8080/metrics | grep gunicorn_workers', '2026-02-26 10:18:40');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, file_path, original_content, suggested_content, repository, pr_url, pr_number, created_branch, created_at) VALUES
  (v_incident_id,
   'Fix recharge-service: Async UPI callback processing with circuit breaker',
   'Replace the synchronous UPI callback retry logic with async processing via SQS. Add a circuit breaker for NPCI gateway calls (pybreaker, 50% failure threshold, 30s reset timeout) and enforce a 2-second hard timeout. This decouples callback verification from the main request thread, preventing worker pool saturation during NPCI latency spikes.',
   'fix', 'low', NULL,
   'services/recharge-service/app/handlers/upi_callback.py',
   E'def process_upi_callback(self, txn_id: str, upi_ref: str) -> dict:\n    # Add retry logic for reliability (3 attempts)\n    max_retries = 3\n    timeout_seconds = 10\n    for attempt in range(1, max_retries + 1):\n        try:\n            response = self.npci_client.verify_payment(\n                upi_ref, timeout=timeout_seconds\n            )\n            if response.status_code == 200:\n                return self._confirm_recharge(txn_id, response.json())\n            logger.warning(\n                f\"NPCI returned {response.status_code}, attempt {attempt}/{max_retries}\"\n            )\n        except requests.Timeout:\n            logger.warning(\n                f\"UPI callback timeout: NPCI gateway response exceeded {timeout_seconds*1000}ms \"\n                f\"(attempt {attempt}/{max_retries})\",\n                extra={\"txn_id\": txn_id, \"gateway\": NPCI_GATEWAY_HOST}\n            )\n        if attempt < max_retries:\n            continue\n    raise UPIVerificationError(\"UPI callback confirmation timeout after 3 retries\")',
   E'import pybreaker\n\nnpci_circuit_breaker = pybreaker.CircuitBreaker(\n    fail_max=5,\n    reset_timeout=30,\n    name=\"npci_gateway\"\n)\n\ndef process_upi_callback(self, txn_id: str, upi_ref: str) -> dict:\n    \"\"\"Initiate UPI callback verification asynchronously via SQS.\"\"\"\n    # Enqueue for async processing -- do not block the request thread\n    sqs_client.send_message(\n        QueueUrl=UPI_CALLBACK_QUEUE_URL,\n        MessageBody=json.dumps({\n            \"txn_id\": txn_id,\n            \"upi_ref\": upi_ref,\n            \"enqueued_at\": datetime.utcnow().isoformat()\n        }),\n        MessageGroupId=txn_id\n    )\n    logger.info(f\"UPI callback queued for async processing\", extra={\"txn_id\": txn_id})\n    return {\"status\": \"PROCESSING\", \"txn_id\": txn_id}\n\n@npci_circuit_breaker\ndef _verify_upi_payment(self, upi_ref: str) -> dict:\n    \"\"\"Verify UPI payment with NPCI gateway (called by SQS consumer).\"\"\"\n    response = self.npci_client.verify_payment(upi_ref, timeout=2)\n    if response.status_code == 200:\n        return response.json()\n    raise UPIVerificationError(f\"NPCI returned {response.status_code}\")',
   'freecharge/payments-platform',
   'https://github.com/freecharge/payments-platform/pull/236',
   236,
   'fix/aurora-async-upi-callback',
   '2026-02-26 10:18:50');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id, 'Add NPCI gateway latency monitoring alert', 'Create a Grafana alert for NPCI UPI gateway response time p95 > 1 second. This enables early detection of NPCI degradation before it cascades to transaction failures, allowing the team to proactively enable fallback paths.', 'mitigation', 'safe', NULL, '2026-02-26 10:19:00');

END $$;

-- 9. Chat session (separate block for readability)
DO $$
DECLARE
  v_incident_id UUID := 'e5f6a7b8-c9d0-4e1f-2a3b-4c5d6e7f8901';
  v_chat_session_id VARCHAR(50) := 'f6a7b8c9-d0e1-4f2a-3b4c-5d6e7f8a9012';
  v_demo_user_id VARCHAR(255) := 'ab209180-626b-4601-8042-9f6328d03ae9';
  v_already_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM chat_sessions WHERE id = v_chat_session_id) INTO v_already_exists;
  IF v_already_exists THEN
    RAISE NOTICE 'Demo incident (FreeCharge) chat session already exists, skipping.';
    RETURN;
  END IF;

  INSERT INTO chat_sessions (id, user_id, title, messages, created_at, updated_at, is_active, status, incident_id) VALUES
  (v_chat_session_id, v_demo_user_id,
   'RCA: Elevated transaction failure rate on recharge-service',
   ('[
     {"sender":"user","text":"# ROOT CAUSE ANALYSIS REQUIRED - GRAFANA ALERT\n\n## ALERT DETAILS:\n- **Title**: Elevated transaction failure rate on recharge-service\n- **Status**: alerting\n- **Source**: grafana\n- **Rule**: Transaction Failure Rate > 10% (current: 34%)\n- **Impact**: UPI-based recharge transactions failing, wallet pending queue growing\n\n## CONNECTED PROVIDERS:\nYou have access to: aws, grafana, github, github_repo_selection, splunk\n\n## GITHUB REPOSITORY CONTEXT:\n- **Connected Repository**: freecharge/payments-platform\n- **Branch**: main\n\n### INVESTIGATION INSTRUCTIONS:\nConduct a thorough Root Cause Analysis. Focus on the UPI payment callback processing path and identify why transaction failure rate has spiked to 34%.","message_number":1,"isCompleted":true},
     {"sender":"bot","text":"","message_number":2,"isCompleted":true,"run_id":"lc_run--demofc-0001","toolCalls":[{"id":"tc_demofc_001","tool_name":"cloud_exec","input":"{\"command\": \"aws eks describe-cluster --name payments-prod --region ap-south-1 --query \\\"cluster.{status:status,version:version,endpoint:endpoint,platformVersion:platformVersion}\\\"\", \"provider\": \"aws\"}","output":"{\n    \"status\": \"ACTIVE\",\n    \"version\": \"1.29\",\n    \"endpoint\": \"https://A1B2C3D4E5.gr7.ap-south-1.eks.amazonaws.com\",\n    \"platformVersion\": \"eks.8\"\n}\n\nEKS cluster payments-prod is ACTIVE and healthy. Kubernetes version 1.29 on platform eks.8.","status":"completed","timestamp":"2026-02-26T10:15:35.000000"},{"id":"tc_demofc_002","tool_name":"cloud_exec","input":"{\"command\": \"aws ec2 describe-instances --filters \\\"Name=tag:eks:cluster-name,Values=payments-prod\\\" \\\"Name=instance-state-name,Values=running\\\" --query \\\"Reservations[].Instances[].{InstanceId:InstanceId,Type:InstanceType,AZ:Placement.AvailabilityZone,State:State.Name}\\\" --output table --region ap-south-1\", \"provider\": \"aws\"}","output":"-------------------------------------------------------------\n|                    DescribeInstances                      |\n+----------------+----------------+------------------+------+\n|       AZ       |  InstanceId    |      Type        |State |\n+----------------+----------------+------------------+------+\n| ap-south-1a    | i-0a1b2c3d4e   | m5.2xlarge       |running|\n| ap-south-1a    | i-0b2c3d4e5f   | m5.2xlarge       |running|\n| ap-south-1b    | i-0c3d4e5f6g   | m5.2xlarge       |running|\n| ap-south-1b    | i-0d4e5f6g7h   | m5.2xlarge       |running|\n| ap-south-1c    | i-0e5f6g7h8i   | m5.2xlarge       |running|\n| ap-south-1c    | i-0f6g7h8i9j   | m5.2xlarge       |running|\n+----------------+----------------+------------------+------+\n\nAll 6 EC2 nodes across 3 AZs are running. No infrastructure-level issues.","status":"completed","timestamp":"2026-02-26T10:15:38.000000"},{"id":"tc_demofc_003","tool_name":"cloud_exec","input":"{\"command\": \"aws elbv2 describe-target-health --target-group-arn arn:aws:elasticloadbalancing:ap-south-1:891377104296:targetgroup/recharge-tg/a1b2c3d4e5f6g7h8 --region ap-south-1\", \"provider\": \"aws\"}","output":"{\n    \"TargetHealthDescriptions\": [\n        {\"Target\": {\"Id\": \"10.0.12.41\", \"Port\": 8080}, \"TargetHealth\": {\"State\": \"unhealthy\", \"Reason\": \"Target.ResponseCodeMismatch\", \"Description\": \"Health checks failed with these codes: [503]\"}},\n        {\"Target\": {\"Id\": \"10.0.13.22\", \"Port\": 8080}, \"TargetHealth\": {\"State\": \"unhealthy\", \"Reason\": \"Target.ResponseCodeMismatch\", \"Description\": \"Health checks failed with these codes: [503]\"}},\n        {\"Target\": {\"Id\": \"10.0.14.15\", \"Port\": 8080}, \"TargetHealth\": {\"State\": \"unhealthy\", \"Reason\": \"Target.ResponseCodeMismatch\", \"Description\": \"Health checks failed with these codes: [503]\"}},\n        {\"Target\": {\"Id\": \"10.0.12.67\", \"Port\": 8080}, \"TargetHealth\": {\"State\": \"unhealthy\", \"Reason\": \"Target.ResponseCodeMismatch\", \"Description\": \"Health checks failed with these codes: [503]\"}}\n    ]\n}\n\nAll 4 ALB targets are unhealthy -- health checks returning 503.","status":"completed","timestamp":"2026-02-26T10:15:42.000000"},{"id":"tc_demofc_004","tool_name":"cloud_exec","input":"{\"command\": \"aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB --metric-name HTTPCode_Target_5XX_Count --dimensions Name=TargetGroup,Value=targetgroup/recharge-tg/a1b2c3d4e5f6g7h8 --start-time 2026-02-26T09:45:00Z --end-time 2026-02-26T10:15:00Z --period 300 --statistics Sum --region ap-south-1\", \"provider\": \"aws\"}","output":"Timestamp                 Sum\n2026-02-26T09:45:00Z      0\n2026-02-26T09:50:00Z      12\n2026-02-26T09:55:00Z      87\n2026-02-26T10:00:00Z      456\n2026-02-26T10:05:00Z      891\n2026-02-26T10:10:00Z      1,634\n2026-02-26T10:15:00Z      2,341\n\nCloudWatch confirms sharp 5xx escalation starting at 09:50, total 5,421 errors in 30 minutes.","status":"completed","timestamp":"2026-02-26T10:15:48.000000"}]},
     {"sender":"bot","text":"**AWS infrastructure is healthy** -- EKS cluster is ACTIVE, all 6 EC2 nodes (m5.2xlarge across 3 AZs in ap-south-1) are running with passing status checks. However, the **ALB target group tells a different story**: all 4 recharge-service targets are `unhealthy` with 503 health check failures. CloudWatch shows 5xx errors growing exponentially from 0 to 2,341 in 30 minutes. The problem is at the application layer, not infrastructure. Let me check the pods and application logs.","message_number":3,"isCompleted":true,"run_id":"lc_run--demofc-0002","toolCalls":[{"id":"tc_demofc_005","tool_name":"cloud_exec","input":"{\"command\": \"kubectl get pods -n payments -l app=recharge-service -o wide\", \"provider\": \"aws\"}","output":"NAME                                  READY   STATUS    RESTARTS   AGE    IP             NODE\nrecharge-svc-7f8e6d5c4-ab2k9        0/1     Running   0          6h     10.0.12.41     ip-10-0-12-41.ap-south-1.compute.internal\nrecharge-svc-7f8e6d5c4-cd4m1        0/1     Running   0          6h     10.0.13.22     ip-10-0-13-22.ap-south-1.compute.internal\nrecharge-svc-7f8e6d5c4-ef6n3        0/1     Running   0          6h     10.0.14.15     ip-10-0-14-15.ap-south-1.compute.internal\nrecharge-svc-7f8e6d5c4-gh8p5        0/1     Running   0          22m    10.0.12.67     ip-10-0-12-67.ap-south-1.compute.internal\n\nNote: All pods Running but READY 0/1 -- readiness probes are failing. 4th pod was scaled up by HPA 22 minutes ago.","status":"completed","timestamp":"2026-02-26T10:15:55.000000"},{"id":"tc_demofc_006","tool_name":"cloud_exec","input":"{\"command\": \"kubectl get events -n payments --sort-by=.lastTimestamp --field-selector reason!=Pulled,reason!=Created,reason!=Started | tail -20\", \"provider\": \"aws\"}","output":"LAST SEEN   TYPE      REASON              OBJECT                                     MESSAGE\n1m          Warning   Unhealthy           pod/recharge-svc-7f8e6d5c4-ab2k9          Readiness probe failed: HTTP probe failed with statuscode: 503\n1m          Warning   Unhealthy           pod/recharge-svc-7f8e6d5c4-cd4m1          Readiness probe failed: HTTP probe failed with statuscode: 503\n1m          Warning   Unhealthy           pod/recharge-svc-7f8e6d5c4-ef6n3          Readiness probe failed: HTTP probe failed with statuscode: 503\n3m          Normal    SuccessfulRescale   horizontalpodautoscaler/recharge-svc-hpa  New size: 4; reason: cpu resource utilization above target\n25m         Warning   FailedTarget        targetgroupbinding/recharge-tg            Deregistered target 10.0.12.41:8080 (unhealthy)\n25m         Warning   FailedTarget        targetgroupbinding/recharge-tg            Deregistered target 10.0.13.22:8080 (unhealthy)","status":"completed","timestamp":"2026-02-26T10:16:05.000000"}]},
     {"sender":"bot","text":"All 4 recharge-service pods are Running but have **0/1 READY** -- readiness probes are returning 503. The HPA scaled up a 4th pod 22 minutes ago but it hit the same issue immediately. This confirms it is not a crash or capacity problem -- something is blocking the application from serving requests. Let me check the pod logs.","message_number":4,"isCompleted":true,"run_id":"lc_run--demofc-0003","toolCalls":[{"id":"tc_demofc_007","tool_name":"cloud_exec","input":"{\"command\": \"kubectl logs recharge-svc-7f8e6d5c4-ab2k9 -n payments --tail=30\", \"provider\": \"aws\"}","output":"{\"level\":\"info\",\"service\":\"recharge-service\",\"message\":\"Processing UPI recharge request\",\"txn_id\":\"TXN-2026022610-4821\",\"user_id\":\"usr_fc_8k2m1\",\"amount\":299,\"operator\":\"airtel\",\"timestamp\":\"2026-02-26T10:14:52.103Z\"}\n{\"level\":\"warn\",\"service\":\"recharge-service\",\"message\":\"UPI callback timeout: NPCI gateway response exceeded 10000ms (attempt 1/3)\",\"txn_id\":\"TXN-2026022610-4821\",\"gateway\":\"npci-upi-prod.npci.org.in\",\"elapsed_ms\":10002,\"timestamp\":\"2026-02-26T10:15:02.345Z\"}\n{\"level\":\"warn\",\"service\":\"recharge-service\",\"message\":\"UPI callback timeout: NPCI gateway response exceeded 10000ms (attempt 2/3)\",\"txn_id\":\"TXN-2026022610-4821\",\"gateway\":\"npci-upi-prod.npci.org.in\",\"elapsed_ms\":10001,\"timestamp\":\"2026-02-26T10:15:12.348Z\"}\n{\"level\":\"error\",\"service\":\"recharge-service\",\"message\":\"UPI callback timeout: NPCI gateway response exceeded 10000ms (attempt 3/3) - giving up\",\"txn_id\":\"TXN-2026022610-4821\",\"gateway\":\"npci-upi-prod.npci.org.in\",\"elapsed_ms\":10003,\"timestamp\":\"2026-02-26T10:15:22.351Z\"}\n{\"level\":\"error\",\"service\":\"recharge-service\",\"message\":\"Transaction failed: UPI callback confirmation timeout after 3 retries\",\"txn_id\":\"TXN-2026022610-4821\",\"total_elapsed_ms\":30210,\"status\":\"FAILED\",\"timestamp\":\"2026-02-26T10:15:22.355Z\"}\n{\"level\":\"error\",\"service\":\"recharge-service\",\"message\":\"Worker pool exhausted: 25/25 workers busy, request queued\",\"queue_depth\":47,\"timestamp\":\"2026-02-26T10:15:23.001Z\"}","status":"completed","timestamp":"2026-02-26T10:16:25.000000"},{"id":"tc_demofc_008","tool_name":"cloud_exec","input":"{\"command\": \"kubectl logs recharge-svc-7f8e6d5c4-ab2k9 -n payments --since=30m | grep -c \\\"Worker pool exhausted\\\"\", \"provider\": \"aws\"}","output":"312","status":"completed","timestamp":"2026-02-26T10:16:30.000000"}]},
     {"sender":"bot","text":"**Found the bottleneck.** The logs show a clear pattern: each UPI callback verification is timing out after 10 seconds, retrying 3 times (30 seconds total per request). The NPCI gateway at `npci-upi-prod.npci.org.in` is responding slowly. With 25 Gunicorn workers all blocked for 30 seconds each, the entire worker pool is saturated -- 312 pool exhaustion events in just 30 minutes. The health check returns 503 because no workers are available to handle it.\n\nLet me check Splunk for the full scope and the downstream impact on wallet-service.","message_number":5,"isCompleted":true,"run_id":"lc_run--demofc-0004","toolCalls":[{"id":"tc_demofc_009","tool_name":"search_splunk","input":"{\"query\": \"index=kubernetes namespace=payments sourcetype=recharge-service \\\"UPI callback timeout\\\" | timechart span=5m count\"}","output":"Time                  count\n2026-02-26T09:45:00   0\n2026-02-26T09:50:00   2\n2026-02-26T09:55:00   8\n2026-02-26T10:00:00   67\n2026-02-26T10:05:00   198\n2026-02-26T10:10:00   412\n2026-02-26T10:15:00   560\n\nTrend: Exponential growth starting ~09:55 when NPCI gateway latency increased.","status":"completed","timestamp":"2026-02-26T10:16:50.000000"},{"id":"tc_demofc_010","tool_name":"search_splunk","input":"{\"query\": \"index=kubernetes namespace=payments sourcetype=recharge-service \\\"Worker pool exhausted\\\" | stats count by host\"}","output":"host                                     count\nrecharge-svc-7f8e6d5c4-ab2k9            312\nrecharge-svc-7f8e6d5c4-cd4m1            298\nrecharge-svc-7f8e6d5c4-ef6n3            305\nrecharge-svc-7f8e6d5c4-gh8p5            187\n\nTotal: 1,102 worker pool exhaustion events. Pod gh8p5 has fewer because HPA scaled it up 22 minutes ago.","status":"completed","timestamp":"2026-02-26T10:17:00.000000"},{"id":"tc_demofc_011","tool_name":"search_splunk","input":"{\"query\": \"index=kubernetes namespace=payments sourcetype=wallet-service \\\"pending debit\\\" status=PENDING | stats count\"}","output":"count\n2847\n\n2,847 wallet debit transactions in PENDING status with no UPI confirmation callback received.","status":"completed","timestamp":"2026-02-26T10:17:15.000000"}]},
     {"sender":"bot","text":"The situation is severe:\n\n- **1,247 UPI callback timeouts** in 30 minutes, growing exponentially\n- **1,102 worker pool exhaustion events** across all 4 pods\n- **2,847 pending wallet transactions** -- money deducted from user wallets but recharges not completed\n\nThe exponential growth started around 09:55 when NPCI gateway latency increased. But the *real* problem is why a slow external gateway is taking down the entire service. Synchronous retries with 30s total timeout in the request handler is an anti-pattern. Let me check GitHub for recent code changes to the UPI callback path.","message_number":6,"isCompleted":true,"run_id":"lc_run--demofc-0005","toolCalls":[{"id":"tc_demofc_012","tool_name":"github_rca","input":"{\"action\": \"commits\", \"repo\": \"freecharge/payments-platform\"}","output":"{\"commits\": [{\"sha\": \"a8c2f34\", \"message\": \"Merge PR #234: Add retry logic for UPI callback processing\", \"author\": \"priya.dev\", \"date\": \"2026-02-24T11:30:00Z\"}, {\"sha\": \"d1e7b92\", \"message\": \"Fix DTH recharge operator code mapping\", \"author\": \"rahul.eng\", \"date\": \"2026-02-23T15:20:00Z\"}, {\"sha\": \"f3a9c56\", \"message\": \"Update Grafana dashboard queries for payment metrics\", \"author\": \"amit.sre\", \"date\": \"2026-02-22T09:45:00Z\"}], \"timeline_correlation\": \"Commit a8c2f34 (PR #234) deployed 2 days before issue start. CRITICAL correlation.\"}","status":"completed","timestamp":"2026-02-26T10:17:35.000000"},{"id":"tc_demofc_013","tool_name":"github_rca","input":"{\"action\": \"diff\", \"repo\": \"freecharge/payments-platform\", \"sha\": \"a8c2f34\"}","output":"{\"status\": \"success\", \"action\": \"diff\", \"files_changed\": 2, \"additions\": 42, \"deletions\": 8, \"key_changes\": [{\"file\": \"services/recharge-service/app/handlers/upi_callback.py\", \"diff\": \"  def process_upi_callback(self, txn_id: str, upi_ref: str) -> dict:\\n-     response = self.npci_client.verify_payment(upi_ref)\\n-     if response.status_code == 200:\\n-         return self._confirm_recharge(txn_id, response.json())\\n-     raise UPIVerificationError(f\\\"NPCI returned {response.status_code}\\\")\\n+     # Add retry logic for reliability (3 attempts)\\n+     max_retries = 3\\n+     timeout_seconds = 10\\n+     for attempt in range(1, max_retries + 1):\\n+         try:\\n+             response = self.npci_client.verify_payment(\\n+                 upi_ref, timeout=timeout_seconds\\n+             )\\n+             if response.status_code == 200:\\n+                 return self._confirm_recharge(txn_id, response.json())\\n+         except requests.Timeout:\\n+             logger.warning(f\\\"UPI callback timeout (attempt {attempt}/{max_retries})\\\")\\n+         if attempt < max_retries:\\n+             continue\\n+     raise UPIVerificationError(\\\"UPI callback confirmation timeout after 3 retries\\\")\"}, {\"file\": \"services/recharge-service/app/config.py\", \"diff\": \"+ NPCI_CALLBACK_TIMEOUT = int(os.getenv(\\\"NPCI_CALLBACK_TIMEOUT\\\", \\\"10\\\"))\\n+ NPCI_CALLBACK_MAX_RETRIES = int(os.getenv(\\\"NPCI_CALLBACK_MAX_RETRIES\\\", \\\"3\\\"))\"}], \"pr_description\": \"Add retry logic to UPI callback processing for improved reliability.\", \"reviewers\": [\"rahul.eng\"], \"review_comments\": \"Approved: retries will help with intermittent NPCI timeouts.\"}","status":"completed","timestamp":"2026-02-26T10:17:45.000000"}]},
     {"sender":"bot","text":"**Root cause confirmed.** PR #234 (\"Add retry logic for UPI callback processing\"), merged 2 days ago by priya.dev, introduced **synchronous retries** in the UPI callback handler:\n\n- **Before**: Single NPCI call with no timeout -- fast fail on error\n- **After**: 3 retries x 10s timeout = **up to 30 seconds blocking per request**\n\nThe intent was good (handle intermittent NPCI timeouts), but the implementation is a critical anti-pattern: synchronous retries in the request handler thread. With 25 workers and ~60 req/s, the pool saturates in under a minute when NPCI latency exceeds 1-2 seconds. The reviewer approved it without catching the thread-blocking issue.\n\nLet me search for best practices and apply a fix.","message_number":7,"isCompleted":true,"run_id":"lc_run--demofc-0006","toolCalls":[{"id":"tc_demofc_014","tool_name":"web_search","input":"{\"query\": \"UPI payment callback async processing best practices circuit breaker pattern India fintech\"}","output":"1. NPCI UPI Technical Guidelines (npci.org.in): UPI callback responses should be processed asynchronously. Never block the primary transaction thread waiting for callback confirmation.\n\n2. Circuit Breaker Pattern for Payment Gateways (martinfowler.com): Use circuit breaker with 50% failure threshold, 30s reset, half-open recovery. Never use synchronous retries in request handling thread.\n\n3. Building Resilient UPI Integrations (razorpay.com/blog): Process UPI callbacks via SQS/Kafka. Set hard 2s timeouts. Implement reconciliation job for pending transactions.","status":"completed","timestamp":"2026-02-26T10:18:10.000000"},{"id":"tc_demofc_015","tool_name":"cloud_exec","input":"{\"command\": \"kubectl rollout undo deployment/recharge-svc -n payments\", \"provider\": \"aws\"}","output":"deployment.apps/recharge-svc rolled back to revision 14\n\nRollback to pre-PR #234 deployment initiated.","status":"completed","timestamp":"2026-02-26T10:18:25.000000"},{"id":"tc_demofc_016","tool_name":"github_fix","input":"{\"repo\": \"freecharge/payments-platform\", \"file_path\": \"services/recharge-service/app/handlers/upi_callback.py\", \"description\": \"Replace synchronous UPI callback retries with async SQS processing and circuit breaker\"}","output":"Fix suggestion created: Replace synchronous retry loop with SQS-based async processing. Add pybreaker circuit breaker for NPCI gateway calls (50% threshold, 30s reset, 2s hard timeout). Decouple callback verification from main request thread.","status":"completed","timestamp":"2026-02-26T10:18:40.000000"},{"id":"tc_demofc_017","tool_name":"github_apply_fix","input":"{\"repo\": \"freecharge/payments-platform\", \"branch\": \"fix/aurora-async-upi-callback\", \"title\": \"Fix: Async UPI callback processing with circuit breaker\"}","output":"Pull request created successfully.\n- Branch: fix/aurora-async-upi-callback\n- PR #236: https://github.com/freecharge/payments-platform/pull/236\n- Changes: services/recharge-service/app/handlers/upi_callback.py","status":"completed","timestamp":"2026-02-26T10:18:50.000000"}]},
     {"sender":"bot","text":"Investigation complete. Here is a summary of findings and actions taken:\n\n**Root Cause**: PR #234 (\"Add retry logic for UPI callback processing\", merged 2 days ago) introduced synchronous retries (3 attempts x 10s timeout) in the UPI callback handler. When the NPCI UPI gateway experienced latency spikes (~3-5s response times), every Gunicorn worker got blocked for up to 30 seconds per request. With 25 workers and ~60 req/s incoming, the entire worker pool saturated within a minute. New requests queued up, readiness probes failed (503), ALB deregistered all targets, and the transaction failure rate spiked to 34%. The cascading effect left 2,847 wallet debit transactions in a pending state (\"money deducted but recharge not done\").\n\n**Actions Taken**:\n1. Rolled back recharge-service deployment to pre-PR #234 version (revision 14) to immediately restore transaction processing\n2. Created PR #236 with an async UPI callback architecture: callback verification is enqueued to SQS for separate worker processing, with a pybreaker circuit breaker on NPCI gateway calls (50% failure threshold, 30s reset, 2s hard timeout)\n\n**Recommended Follow-ups**:\n- Run the wallet reconciliation job to resolve 2,847 pending transactions\n- Add Grafana alert for NPCI gateway p95 latency > 1s for early detection\n- Load test the async callback path under simulated NPCI degradation\n- Review all external payment gateway integrations for synchronous blocking patterns\n- Implement a periodic reconciliation Celery task for pending UPI transactions","message_number":8,"isCompleted":true,"run_id":"lc_run--demofc-0007","toolCalls":[]}
   ]')::jsonb,
   '2026-02-26 10:15:00', '2026-02-26 10:35:00', true, 'completed', v_incident_id);

  RAISE NOTICE 'Demo incident (FreeCharge) chat session inserted.';

  -- Fix tool call IDs, run_ids, and command display fields
  UPDATE chat_sessions SET messages = (
    SELECT jsonb_agg(
      CASE
        WHEN msg->'toolCalls' IS NOT NULL AND jsonb_typeof(msg->'toolCalls') = 'array' AND jsonb_array_length(msg->'toolCalls') > 0 THEN
          jsonb_set(msg, '{toolCalls}', (
            SELECT jsonb_agg(
              CASE
                WHEN updated_tc->>'tool_name' = 'cloud_exec' THEN
                  updated_tc || jsonb_build_object('command', (updated_tc->>'input')::jsonb->>'command')
                WHEN updated_tc->>'tool_name' = 'search_splunk' THEN
                  updated_tc || jsonb_build_object('command', (updated_tc->>'input')::jsonb->>'query')
                WHEN updated_tc->>'tool_name' = 'github_rca' THEN
                  updated_tc || jsonb_build_object('command', 'github: ' || COALESCE((updated_tc->>'input')::jsonb->>'action', 'rca'))
                WHEN updated_tc->>'tool_name' = 'web_search' THEN
                  updated_tc || jsonb_build_object('command', (updated_tc->>'input')::jsonb->>'query')
                WHEN updated_tc->>'tool_name' = 'github_fix' THEN
                  updated_tc || jsonb_build_object('command', 'Suggest code fix')
                WHEN updated_tc->>'tool_name' = 'github_apply_fix' THEN
                  updated_tc || jsonb_build_object('command', 'Create pull request')
                ELSE updated_tc
              END
            )
            FROM (
              SELECT jsonb_set(
                jsonb_set(tc, '{id}', to_jsonb('toolu_vrtx_' || replace(gen_random_uuid()::text, '-', ''))),
                '{run_id}', to_jsonb('lc_run--' || gen_random_uuid()::text)
              ) as updated_tc
              FROM jsonb_array_elements(msg->'toolCalls') as tc
            ) sub
          ))
        ELSE msg
      END
    )
    FROM jsonb_array_elements(messages) as msg
  )
  WHERE id = v_chat_session_id;

  RAISE NOTICE 'Demo incident (FreeCharge) fully inserted.';

END $$;

-- Ensure resolved status on existing installs
UPDATE incidents SET status = 'resolved' WHERE id = 'e5f6a7b8-c9d0-4e1f-2a3b-4c5d6e7f8901' AND status != 'resolved';

-- Postmortem for FreeCharge incident (idempotent)
INSERT INTO postmortems (incident_id, user_id, content, generated_at, updated_at)
VALUES (
  'e5f6a7b8-c9d0-4e1f-2a3b-4c5d6e7f8901',
  'ab209180-626b-4601-8042-9f6328d03ae9',
  $postmortem_fc$# Postmortem: Elevated Transaction Failure Rate on Recharge Service

**Date:** 2026-02-26 10:15 IST
**Duration:** 20m
**Severity:** critical
**Service:** recharge-service
**Source:** grafana

## Summary
The recharge-service experienced a 34% transaction failure rate primarily affecting UPI-based mobile recharge transactions. Investigation revealed that a recent code change (PR #234) introduced synchronous retry logic for UPI callback processing with a 30-second total blocking time per request. When the NPCI UPI gateway experienced intermittent latency spikes, all 25 Gunicorn workers became blocked waiting for NPCI responses, causing complete worker pool saturation. The cascading effect resulted in 2,847 pending wallet debit transactions where users had money deducted but recharges were not completed.

## Timeline
- **10:15:00** - Grafana alert fires: transaction failure rate exceeds 10% threshold (current: 34%)
- **10:15:45** - Investigation started. All 4 recharge-service pods Running but 0/1 READY (readiness probes failing with 503)
- **10:16:00** - Kubernetes events show ALB has deregistered all recharge-service targets as unhealthy. HPA scaled to 4 pods but new pod hit same issue
- **10:16:25** - Pod logs reveal UPI callback timeout pattern: 3 retries x 10s timeout = 30s blocking per request against NPCI gateway
- **10:16:50** - Splunk confirms 1,247 UPI callback timeouts in 30 minutes with exponential growth since 09:55
- **10:17:00** - Worker pool exhaustion confirmed: 1,102 events across all pods, 25/25 workers busy on every pod
- **10:17:15** - Downstream impact: 2,847 pending wallet debit transactions (money deducted, recharge not completed)
- **10:17:45** - Root cause identified: PR #234 added synchronous retry logic to UPI callback handler, merged 2 days ago
- **10:18:10** - Web search confirms synchronous retries in payment request handlers is a known anti-pattern
- **10:18:25** - Deployment rolled back to pre-PR #234 version. Worker pool recovering
- **10:18:50** - PR #236 created with async SQS-based callback processing and circuit breaker

## Root Cause
PR #234 ("Add retry logic for UPI callback processing") was merged 2 days prior to add resilience against intermittent NPCI gateway timeouts. The implementation added synchronous retries (3 attempts with 10-second timeouts) directly in the main request handler thread. Under normal NPCI performance (~200ms responses), this had no visible impact. However, when the NPCI UPI gateway experienced latency spikes (3-5 second response times), each request held a Gunicorn worker for up to 30 seconds. With 25 workers per pod and ~60 incoming requests per second, the worker pool saturated in under a minute. The synchronous retry approach amplified the problem -- without retries, only one worker per slow request would be blocked; with 3 retries, each slow request held a worker 3x longer.

## Impact
- **34% transaction failure rate** on UPI-based recharge transactions for approximately 20 minutes
- **2,847 pending wallet transactions** where user wallet balances were deducted but recharge confirmations were never received ("money deducted but recharge not done")
- **100% worker pool saturation** across all 4 pods, rendering the recharge-service completely unresponsive
- **ALB target deregistration** causing all incoming traffic to receive 5xx errors
- **Surge in customer support tickets** related to failed recharges and pending wallet debits
- Estimated ~3,400 failed recharge transactions during the incident window

## Resolution
The incident was resolved by rolling back the recharge-service deployment to the version prior to PR #234 (revision 14), which removed the synchronous retry logic. Worker pool utilization returned to normal within 2 minutes of rollback. A proper fix was submitted via PR #236, implementing async UPI callback processing via SQS with a pybreaker circuit breaker for NPCI gateway calls (50% failure threshold, 30s reset timeout, 2-second hard timeout on NPCI calls).

## Action Items
- [ ] Review and merge PR #236 to implement async UPI callback processing with circuit breaker
- [ ] Run wallet reconciliation job to resolve 2,847 pending debit transactions
- [ ] Add Grafana alert for NPCI UPI gateway p95 response time > 1 second for early degradation detection
- [ ] Audit all external payment gateway integrations (NPCI, Razorpay, bank APIs) for synchronous blocking patterns in request handlers
- [ ] Implement a periodic Celery reconciliation task that automatically resolves pending UPI transactions by checking NPCI status API
- [ ] Load test the recharge-service under simulated NPCI degradation in staging (3-5s latency, 50% timeout rate)
- [ ] Update code review guidelines to flag synchronous external API calls with retry loops in request handler threads

## Lessons Learned
This incident demonstrates the critical difference between "retry for resilience" and "retry that causes cascading failure." The developer's intent was correct -- handling intermittent NPCI timeouts -- but synchronous retries in the request thread turned a minor external dependency issue into a total service outage. Key takeaways: (1) External payment gateway calls must always be asynchronous or have sub-second hard timeouts, (2) circuit breakers should be mandatory for all external payment API integrations, (3) load testing must include degraded-dependency scenarios, not just high-traffic scenarios, (4) code reviews for payment-critical paths should specifically check for thread-blocking patterns. The 2-day gap between deployment and incident highlights that the issue only manifests under NPCI latency conditions, which underscores the need for chaos engineering and dependency failure simulation in the testing pipeline.$postmortem_fc$,
  '2026-02-27 02:15:00',
  '2026-02-27 02:15:00'
)
ON CONFLICT (incident_id) DO NOTHING;
