-- Demo Incident 4: Intermittent 503s from expired Istio mTLS certificates
-- Source: Netdata | Cloud: Azure/AKS | Tools: kubectl, Splunk, GitHub, Web Search, Knowledge Base

DO $$
DECLARE
  v_incident_id UUID := 'f1a2b3c4-d5e6-4f7a-8b9c-0d1e2f3a4b5c';
  v_chat_session_id VARCHAR(50) := 'a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d';
  v_demo_user_id VARCHAR(255) := 'ab209180-626b-4601-8042-9f6328d03ae9';
  v_netdata_alert_id INTEGER;
  v_incident_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM incidents WHERE id = v_incident_id) INTO v_incident_exists;
  IF v_incident_exists THEN
    RAISE NOTICE 'Demo incident 4 already exists, skipping.';
    RETURN;
  END IF;

  RAISE NOTICE 'Inserting demo incident 4...';

  -- 1. Insert netdata_alerts source record
  INSERT INTO netdata_alerts (user_id, alert_name, alert_status, host, chart, payload, received_at)
  VALUES (
    v_demo_user_id,
    'http_503_error_rate',
    'CRITICAL',
    'aks-payment-node-1.westus2.cloudapp.azure.com',
    'web_log.response_codes',
    '{
      "name": "http_503_error_rate",
      "status": "CRITICAL",
      "host": "aks-payment-node-1.westus2.cloudapp.azure.com",
      "chart": "web_log.response_codes",
      "family": "response_codes",
      "value": 31.2,
      "units": "%",
      "info": "HTTP 503 error rate has exceeded 10% threshold. Current: 31.2% of requests to payment-gateway are returning 503 Service Unavailable. Pattern shows intermittent failures, not total outage.",
      "duration": "2m 15s",
      "alarm_id": "netdata-demo-004",
      "date": 1739548800,
      "recipient": "aurora",
      "source": "http://aks-payment-node-1.westus2.cloudapp.azure.com:19999/#menu_web_log_submenu_response_codes",
      "roles": "sysadmin devops",
      "tags": "env=production service=payment-gateway cloud=azure namespace=payments cluster=aks-prod-westus2"
    }'::jsonb,
    '2026-02-15 09:00:00'
  ) RETURNING id INTO v_netdata_alert_id;

  -- 2. Insert main incident record
  INSERT INTO incidents (
    id, user_id, source_type, source_alert_id, status, severity,
    alert_title, alert_service, alert_environment, aurora_status,
    aurora_summary, aurora_chat_session_id, started_at, analyzed_at,
    active_tab, created_at, updated_at, alert_metadata,
    correlated_alert_count, affected_services, visualization_code,
    visualization_updated_at
  ) VALUES (
    v_incident_id, v_demo_user_id, 'netdata', v_netdata_alert_id,
    'analyzed', 'high',
    'Intermittent 503 errors on payment-gateway service',
    'payment-gateway', 'production', 'complete',
    '', v_chat_session_id::uuid,
    '2026-02-15 09:00:00', '2026-02-15 09:18:00',
    'thoughts', '2026-02-15 09:00:00', '2026-02-15 09:18:00',
    jsonb_build_object(
      'is_demo', true,
      'netdata_alert_id', 'netdata-demo-004',
      'incidentId', 'netdata-demo-004',
      'source_url', 'http://aks-payment-node-1.westus2.cloudapp.azure.com:19999/#menu_web_log_submenu_response_codes'
    ),
    2,
    ARRAY['payment-gateway', 'istio-ingress', 'cert-manager'],
    '', '2026-02-15 09:18:00'
  );

  -- 3. Update summary
  UPDATE incidents SET aurora_summary = '# Incident Report: Intermittent 503 Errors from Expired Istio mTLS Certificates

## Summary

On February 15, 2026 at 09:00 UTC, Netdata monitoring detected a 31% HTTP 503 error rate on the payment-gateway service running in the Azure AKS production cluster (aks-prod-westus2). The failures were intermittent rather than total: approximately 3 out of 10 requests succeeded while 7 failed [1, 2]. Kubernetes endpoint inspection revealed only 2 of 5 payment-gateway pods were marked Ready, with the 3 NotReady pods being the ones created 3 days ago while the 2 Ready pods were created just 22 hours ago [3]. Logs from the older pods showed repeated TLS handshake failures with "certificate has expired" errors [4].

Investigation revealed the root cause was a failed Istio certificate rotation. Inspection of the mTLS certificates showed the payment-gateway workload certificate expired on February 13, 2026 at 14:23 UTC [5, 6]. Pods created before expiration had cached the expired certificate and continued attempting to use it (causing all their TLS handshakes to fail). Pods created after expiration received Istio''s self-signed fallback certificate and worked correctly. The cert-manager deployment responsible for automatic certificate rotation had been stuck in CrashLoopBackOff since February 10 due to OOM with a 256Mi memory limit [7]. Splunk logs confirmed TLS errors were isolated to the 3 older pods [9]. Web search identified this as a documented Istio failure mode when cert-manager is unhealthy [8].

The impact was significant: 70% of payment requests returned 503 errors, causing customer payment failures and support escalations. Two correlated Netdata alerts fired for the 503 error rate and TLS handshake failures. The issue was resolved by increasing cert-manager memory limits to 512Mi and restarting it [10], manually triggering Istio certificate rotation for all workloads [11], and performing a rolling restart of the payment-gateway deployment to pick up the new certificates [12]. All pods became healthy and the 503 error rate dropped to 0%.

## Suggested Next Steps

- **Monitor cert-manager health proactively**: Add Prometheus alerting on cert-manager pod health, memory usage, and certificate processing queue depth
- **Implement certificate expiration monitoring**: Create alerts for all Istio workload certificates when expiration is within 7 days, providing time to investigate before outages
- **Increase cert-manager resource limits**: The 256Mi memory limit was insufficient for the cluster size; permanently increase to 512Mi or 1Gi
- **Add automated certificate audit**: Implement a daily CronJob that checks all workload certificates and reports any expired or expiring within 14 days

## Code Fix Available

- **Update cert-manager deployment: Increase memory limits** to prevent OOM crashes that break automatic certificate rotation. The fix increases limits from 256Mi to 512Mi and adds proactive monitoring.'
  WHERE id = v_incident_id;

  -- 4. Update visualization
  UPDATE incidents SET visualization_code = '{
  "nodes": [
    {"id": "payment-gateway", "label": "payment-gateway", "type": "service", "status": "degraded", "parentId": null},
    {"id": "istio-ingress", "label": "istio-ingress", "type": "service", "status": "degraded", "parentId": null},
    {"id": "cert-manager", "label": "cert-manager", "type": "service", "status": "failed", "parentId": null},
    {"id": "istio-mtls", "label": "Istio mTLS", "type": "service", "status": "degraded", "parentId": null},
    {"id": "old-pods", "label": "Old Pods (expired cert)", "type": "metric", "status": "failed", "parentId": "payment-gateway"},
    {"id": "new-pods", "label": "New Pods (self-signed)", "type": "metric", "status": "healthy", "parentId": "payment-gateway"},
    {"id": "certificate", "label": "payment-gateway-cert", "type": "event", "status": "failed", "parentId": null},
    {"id": "cert-expiry", "label": "Certificate Expired", "type": "event", "status": "failed", "parentId": null},
    {"id": "alert-503", "label": "503 Error Alert", "type": "alert", "status": "failed", "parentId": null},
    {"id": "alert-tls", "label": "TLS Handshake Alert", "type": "alert", "status": "failed", "parentId": null}
  ],
  "edges": [
    {"source": "cert-manager", "target": "certificate", "label": "failed to rotate", "type": "causation"},
    {"source": "certificate", "target": "cert-expiry", "label": "expired Feb 13", "type": "causation"},
    {"source": "cert-expiry", "target": "old-pods", "label": "cached expired cert", "type": "causation"},
    {"source": "old-pods", "target": "istio-mtls", "label": "TLS handshake failure", "type": "causation"},
    {"source": "istio-mtls", "target": "payment-gateway", "label": "503 on 70% of requests", "type": "causation"},
    {"source": "payment-gateway", "target": "istio-ingress", "label": "propagates 503", "type": "causation"},
    {"source": "istio-ingress", "target": "alert-503", "label": "triggered alert", "type": "causation"},
    {"source": "istio-mtls", "target": "alert-tls", "label": "triggered alert", "type": "causation"}
  ],
  "rootCauseId": "cert-manager",
  "affectedIds": ["certificate", "cert-expiry", "old-pods", "istio-mtls", "payment-gateway", "istio-ingress", "alert-503", "alert-tls"],
  "version": 5,
  "updatedAt": "2026-02-15T09:18:00.000000+00:00"
}'
  WHERE id = v_incident_id;

  -- 5. Insert correlated alerts
  INSERT INTO incident_alerts (user_id, incident_id, source_type, source_alert_id, alert_title, alert_service, alert_severity, correlation_strategy, correlation_score, correlation_details, alert_metadata, received_at) VALUES
  (v_demo_user_id, v_incident_id, 'netdata', v_netdata_alert_id, 'High 503 error rate on payment-gateway', 'payment-gateway', 'critical', 'primary', 1.0, '{}', '{"tags":["env:production","service:payment-gateway","cloud:azure"]}'::jsonb, '2026-02-15 09:00:00'),
  (v_demo_user_id, v_incident_id, 'netdata', v_netdata_alert_id + 1, 'TLS handshake failures in istio-proxy logs', 'istio-ingress', 'warning', 'time_proximity', 0.96, '{"reason":"TLS errors coincide with 503 spikes, both started at same timestamp"}'::jsonb, '{"tags":["env:production","service:istio","namespace:istio-system"]}'::jsonb, '2026-02-15 09:00:30');

  RAISE NOTICE 'Demo incident 4 summary, viz, alerts inserted.';

END $$;

-- Thoughts section
DO $$
DECLARE
  v_incident_id UUID := 'f1a2b3c4-d5e6-4f7a-8b9c-0d1e2f3a4b5c';
BEGIN

  -- 6. Insert incident thoughts
  INSERT INTO incident_thoughts (incident_id, timestamp, content, thought_type) VALUES
  (v_incident_id, '2026-02-15 09:00:15', 'Starting investigation of 503 errors on payment-gateway in Azure AKS production cluster.', 'analysis'),
  (v_incident_id, '2026-02-15 09:00:20', 'Searching knowledge base for Azure AKS 503 troubleshooting and service mesh debugging.', 'analysis'),
  (v_incident_id, '2026-02-15 09:00:25', 'Found a service mesh troubleshooting runbook covering Istio, certificate issues, and intermittent failures.', 'analysis'),
  (v_incident_id, '2026-02-15 09:00:35', 'Checking payment-gateway pod status. Need to see if pods are crashing or if this is a networking/service mesh issue.', 'analysis'),
  (v_incident_id, '2026-02-15 09:00:45', 'All 5 payment-gateway pods are Running with 0 restarts. This rules out application crashes. The 30% failure rate suggests load balancer distributing across healthy and unhealthy endpoints.', 'analysis'),
  (v_incident_id, '2026-02-15 09:00:55', 'Checking pod ages. If some pods are old and some new, there could be a configuration drift issue.', 'analysis'),
  (v_incident_id, '2026-02-15 09:01:05', 'Clear pattern: 3 pods created 3+ days ago, 2 pods created yesterday. The age split matches the 30% success rate (2 out of 5 pods working).', 'analysis'),
  (v_incident_id, '2026-02-15 09:01:15', 'Checking logs from an old pod first to see what error it is reporting.', 'analysis'),
  (v_incident_id, '2026-02-15 09:01:25', 'Logs show repeated TLS handshake failures: "tls: failed to verify certificate: x509: certificate has expired". The istio-proxy sidecar is rejecting connections.', 'analysis'),
  (v_incident_id, '2026-02-15 09:01:35', 'This is an Istio mTLS certificate issue. The pattern makes sense: old pods have an expired cert, new pods have a fresh one.', 'analysis'),
  (v_incident_id, '2026-02-15 09:01:45', 'Inspecting Istio certificates for payment-gateway workload.', 'analysis'),
  (v_incident_id, '2026-02-15 09:01:55', 'Certificate inspection confirms: payment-gateway cert expired on February 13, 2026 at 14:23 UTC. That was 2 days ago, matching when the old pods would have cached it.', 'analysis'),
  (v_incident_id, '2026-02-15 09:02:05', 'Istio cert-manager is supposed to auto-rotate certificates. Let me check if cert-manager is healthy.', 'analysis'),
  (v_incident_id, '2026-02-15 09:02:15', 'cert-manager deployment shows 0/1 ready replicas. The pod has been in CrashLoopBackOff since February 10 with OOMKilled status.', 'analysis'),
  (v_incident_id, '2026-02-15 09:02:25', 'Root cause identified: cert-manager crashed 5 days ago and never recovered. Without cert-manager, certificates were not rotated. payment-gateway cert expired on Feb 13.', 'analysis'),
  (v_incident_id, '2026-02-15 09:02:35', 'Pods created before expiration cached the old cert. Pods created after expiration received the Istio fallback self-signed certificate, which works but triggers warnings.', 'analysis'),
  (v_incident_id, '2026-02-15 09:02:45', 'Querying Splunk for TLS errors across other services to see if this is isolated or cluster-wide.', 'analysis'),
  (v_incident_id, '2026-02-15 09:02:55', 'Splunk shows TLS errors only on payment-gateway. Other services renewed their certificates before Feb 10 when cert-manager was still healthy.', 'analysis'),
  (v_incident_id, '2026-02-15 09:03:05', 'Checking GitHub for recent cert-manager or Istio configuration changes.', 'analysis'),
  (v_incident_id, '2026-02-15 09:03:15', 'No recent changes to cert-manager config. The OOM is likely from organic growth in certificate count, not a config change.', 'analysis'),
  (v_incident_id, '2026-02-15 09:03:25', 'Searching web for Istio certificate expiration recovery procedures.', 'analysis'),
  (v_incident_id, '2026-02-15 09:03:35', 'Best practice: Fix cert-manager first, then manually trigger cert rotation, then rolling restart workloads.', 'analysis'),
  (v_incident_id, '2026-02-15 09:03:45', 'Increasing cert-manager memory limit from 256Mi to 512Mi and restarting deployment.', 'analysis'),
  (v_incident_id, '2026-02-15 09:03:55', 'cert-manager is now healthy. Manually triggering certificate renewal for payment-gateway.', 'analysis'),
  (v_incident_id, '2026-02-15 09:04:05', 'Certificate renewed successfully. Now performing rolling restart of payment-gateway to pick up new certificate.', 'analysis'),
  (v_incident_id, '2026-02-15 09:04:15', 'All payment-gateway pods restarted with new certificates. 503 error rate dropped to 0%.', 'analysis'),
  (v_incident_id, '2026-02-15 09:04:25', 'Investigation complete. Fixed cert-manager OOM, rotated certificate, restarted pods.', 'analysis');

  -- 7. Insert citations
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '1', 'Cloud CLI', 'kubectl get pods -n payments -l app=payment-gateway -o wide', 'NAME                                READY   STATUS    RESTARTS   AGE   IP            NODE
payment-gateway-7c8d9e1f2-ab3cd    2/2     Running   0          3d    10.244.1.12   aks-agentpool-1
payment-gateway-7c8d9e1f2-ef4gh    2/2     Running   0          3d    10.244.2.23   aks-agentpool-2
payment-gateway-7c8d9e1f2-ij5kl    2/2     Running   0          3d    10.244.1.34   aks-agentpool-1
payment-gateway-7c8d9e1f2-mn6op    2/2     Running   0          22h   10.244.3.45   aks-agentpool-3
payment-gateway-7c8d9e1f2-qr7st    2/2     Running   0          22h   10.244.2.56   aks-agentpool-2', '2026-02-15 09:00:40'),
  (v_incident_id, '2', 'Cloud CLI', 'kubectl get endpoints payment-gateway -n payments -o json', '{
  "subsets": [{
    "addresses": [
      {"ip": "10.244.3.45", "nodeName": "aks-agentpool-3", "targetRef": {"name": "payment-gateway-7c8d9e1f2-mn6op"}},
      {"ip": "10.244.2.56", "nodeName": "aks-agentpool-2", "targetRef": {"name": "payment-gateway-7c8d9e1f2-qr7st"}}
    ],
    "notReadyAddresses": [
      {"ip": "10.244.1.12", "nodeName": "aks-agentpool-1", "targetRef": {"name": "payment-gateway-7c8d9e1f2-ab3cd"}},
      {"ip": "10.244.2.23", "nodeName": "aks-agentpool-2", "targetRef": {"name": "payment-gateway-7c8d9e1f2-ef4gh"}},
      {"ip": "10.244.1.34", "nodeName": "aks-agentpool-1", "targetRef": {"name": "payment-gateway-7c8d9e1f2-ij5kl"}}
    ]
  }]
}

Note: 2 ready, 3 not ready. The 2 ready pods are the newer ones (22h old).', '2026-02-15 09:00:50'),
  (v_incident_id, '3', 'Cloud CLI', 'kubectl get pods -n payments -l app=payment-gateway --sort-by=''.metadata.creationTimestamp''', 'NAME                                CREATED
payment-gateway-7c8d9e1f2-ab3cd    2026-02-12T06:15:23Z (3d old)
payment-gateway-7c8d9e1f2-ef4gh    2026-02-12T06:15:23Z (3d old)
payment-gateway-7c8d9e1f2-ij5kl    2026-02-12T06:15:23Z (3d old)
payment-gateway-7c8d9e1f2-mn6op    2026-02-14T11:22:10Z (22h old)
payment-gateway-7c8d9e1f2-qr7st    2026-02-14T11:22:10Z (22h old)', '2026-02-15 09:01:00'),
  (v_incident_id, '4', 'Cloud CLI', 'kubectl logs payment-gateway-7c8d9e1f2-ab3cd -n payments -c istio-proxy --tail=20', '[2026-02-15T09:00:12.456Z] "GET /api/charge HTTP/1.1" 503 - tls_error 0 152 25 - "-" "Go-http-client/1.1" "a7b8c9d0-e1f2-4a3b-8c7d-9e0f1a2b3c4d" "payment-gateway.payments.svc.cluster.local" "-"
[error] TLS handshake failed: x509: certificate has expired
[2026-02-15T09:00:12.512Z] "POST /api/refund HTTP/1.1" 503 - tls_error 0 178 31 - "-" "axios/0.21.1" "b8c9d0e1-f2a3-4b5c-9d8e-0f1a2b3c4d5e" "payment-gateway.payments.svc.cluster.local" "-"
[error] TLS handshake failed: x509: certificate has expired
[2026-02-15T09:00:12.623Z] "GET /healthz HTTP/1.1" 200 - via_upstream 2 0 1 1 "-" "kube-probe/1.28" "-" "localhost" "127.0.0.1:8080"', '2026-02-15 09:01:20'),
  (v_incident_id, '5', 'Cloud CLI', 'kubectl exec payment-gateway-7c8d9e1f2-ab3cd -n payments -c istio-proxy -- openssl x509 -in /etc/certs/cert-chain.pem -noout -dates -subject', 'subject=O=cluster.local,CN=payment-gateway.payments.svc.cluster.local
notBefore=Jan 14 14:23:15 2026 GMT
notAfter=Feb 13 14:23:15 2026 GMT

Certificate expired 2 days ago.', '2026-02-15 09:01:50'),
  (v_incident_id, '6', 'Cloud CLI', 'kubectl exec payment-gateway-7c8d9e1f2-mn6op -n payments -c istio-proxy -- openssl x509 -in /etc/certs/cert-chain.pem -noout -dates -subject', 'subject=O=cluster.local,CN=payment-gateway.payments.svc.cluster.local
notBefore=Feb 14 11:22:45 2026 GMT
notAfter=Mar 16 11:22:45 2026 GMT

Certificate is valid (self-signed fallback from Istio)', '2026-02-15 09:01:58');

  -- More citations  
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '7', 'Cloud CLI', 'kubectl get deployment cert-manager -n cert-manager', 'NAME           READY   UP-TO-DATE   AVAILABLE   AGE
cert-manager   0/1     1            0           45d

kubectl get pods -n cert-manager
NAME                           READY   STATUS             RESTARTS      AGE
cert-manager-7f8c9d6e5-xyz12   0/1     CrashLoopBackOff   1247 (5m)     5d

kubectl logs cert-manager-7f8c9d6e5-xyz12 -n cert-manager --tail=5
FATAL: out of memory: cannot allocate memory
OOMKilled

Last successful run: Feb 10, 2026 03:15 UTC', '2026-02-15 09:02:10'),
  (v_incident_id, '8', 'Web Search', 'Istio certificate expiration cert-manager failure recovery', 'Search results summary:

1. **Istio Certificate Management** (istio.io/docs): "When cert-manager fails, Istio will not rotate workload certificates. Expired certificates cause TLS handshake failures. Pods created after expiration receive self-signed fallback certs."

2. **Cert-Manager OOM Issues** (cert-manager.io/troubleshooting): "cert-manager memory usage grows with certificate count. Default 256Mi limit may be insufficient for large clusters (>500 certs). Increase to 512Mi or 1Gi."

3. **Manual Certificate Rotation** (istio.io/operations): "To manually rotate certificates: 1) Fix cert-manager, 2) Run istioctl experimental cert rotate, 3) Restart workloads to pick up new certs."', '2026-02-15 09:03:30'),
  (v_incident_id, '9', 'Splunk Search', 'index=kubernetes namespace=payments "TLS" OR "x509" OR "certificate" | stats count by pod', 'Pod                              Count
payment-gateway-7c8d9e1f2-ab3cd  1247
payment-gateway-7c8d9e1f2-ef4gh  1183
payment-gateway-7c8d9e1f2-ij5kl  1205
payment-gateway-7c8d9e1f2-mn6op  0
payment-gateway-7c8d9e1f2-qr7st  0

Only the 3 old pods show TLS errors. New pods have zero errors.', '2026-02-15 09:02:50'),
  (v_incident_id, '10', 'Cloud CLI', 'kubectl set resources deployment cert-manager -n cert-manager --limits=memory=512Mi && kubectl rollout restart deployment/cert-manager -n cert-manager', 'deployment.apps/cert-manager resource limits updated
deployment.apps/cert-manager restarted

Waiting for deployment rollout...
deployment "cert-manager" successfully rolled out
cert-manager-7f8c9d6e5-abc34   1/1     Running   0          15s', '2026-02-15 09:03:50'),
  (v_incident_id, '11', 'Cloud CLI', 'kubectl exec -n istio-system deploy/istiod -- pilot-discovery request POST /debug/force-rotate-cert', 'Certificate rotation triggered for all workloads.
payment-gateway.payments: certificate renewed (expires Mar 17, 2026)
Status: Success', '2026-02-15 09:04:00'),
  (v_incident_id, '12', 'Cloud CLI', 'kubectl rollout restart deployment/payment-gateway -n payments && kubectl rollout status deployment/payment-gateway -n payments', 'deployment.apps/payment-gateway restarted
Waiting for deployment "payment-gateway" rollout to finish: 2 out of 5 new replicas have been updated...
Waiting for deployment "payment-gateway" rollout to finish: 3 out of 5 new replicas have been updated...
Waiting for deployment "payment-gateway" rollout to finish: 4 out of 5 new replicas have been updated...
Waiting for deployment "payment-gateway" rollout to finish: 5 out of 5 new replicas have been updated...
deployment "payment-gateway" successfully rolled out

All 5 pods now healthy with valid certificates. 503 error rate: 0%.', '2026-02-15 09:04:10'),
  (v_incident_id, '13', 'Knowledge Base', 'Search: Istio mTLS certificate rotation service mesh troubleshooting', 'Found: "Service Mesh Certificate Management Runbook"

Key steps:
1. Check workload certificate expiration dates with openssl x509
2. Verify cert-manager is healthy and processing certificate requests
3. Inspect Istio certificate rotation logs
4. For expired certs: fix cert-manager first, then manual rotation, then restart workloads
5. Add monitoring for certificate expiration (alert 7 days before)

Tags: istio, service-mesh, mtls, certificates, cert-manager, kubernetes', '2026-02-15 09:00:25');

  -- 8. Insert suggestions (including code fix)
  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id, 'Verify all workload certificates are valid', 'Audit all Istio workload certificates cluster-wide to ensure no other services have expired or expiring certificates after the cert-manager outage.', 'diagnostic', 'safe', 'kubectl get secret -A -o json | jq ''.items[] | select(.metadata.annotations."cert-manager.io/certificate-name" != null) | {namespace: .metadata.namespace, name: .metadata.name, cert: (.data."tls.crt" | @base64d)}'' | while read cert; do echo "$cert" | openssl x509 -noout -dates; done', '2026-02-15 09:04:20'),
  (v_incident_id, 'Monitor cert-manager health', 'Add Prometheus alerting on cert-manager pod health and memory usage to detect future cert-manager failures before they cause certificate expiration.', 'mitigation', 'safe', NULL, '2026-02-15 09:04:25'),
  (v_incident_id, 'Add certificate expiration monitoring', 'Create alerts for all Istio workload certificates when they are within 7 days of expiration, providing time to investigate before outages occur.', 'mitigation', 'safe', NULL, '2026-02-15 09:04:35');

  -- Insert code fix suggestion with deployment yaml
  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, file_path, original_content, suggested_content, repository, pr_url, pr_number, created_branch, created_at) VALUES
  (v_incident_id,
   'Fix cert-manager deployment: Increase memory limits to prevent OOM crashes',
   'The cert-manager deployment crashed with OOM due to insufficient memory limits (256Mi). Increase to 512Mi to handle the certificate load in this cluster and add resource requests for proper scheduling. This prevents future certificate rotation failures.',
   'fix', 'low', NULL,
   'k8s/cert-manager/deployment.yaml',
   E'apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: cert-manager\n  namespace: cert-manager\nspec:\n  replicas: 1\n  selector:\n    matchLabels:\n      app: cert-manager\n  template:\n    metadata:\n      labels:\n        app: cert-manager\n    spec:\n      containers:\n      - name: cert-manager\n        image: quay.io/jetstack/cert-manager-controller:v1.14.0\n        args:\n        - --v=2\n        - --cluster-resource-namespace=$(POD_NAMESPACE)\n        - --leader-election-namespace=kube-system\n        resources:\n          limits:\n            memory: 256Mi\n          requests:\n            cpu: 100m\n            memory: 128Mi',
   E'apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: cert-manager\n  namespace: cert-manager\nspec:\n  replicas: 1\n  selector:\n    matchLabels:\n      app: cert-manager\n  template:\n    metadata:\n      labels:\n        app: cert-manager\n    spec:\n      containers:\n      - name: cert-manager\n        image: quay.io/jetstack/cert-manager-controller:v1.14.0\n        args:\n        - --v=2\n        - --cluster-resource-namespace=$(POD_NAMESPACE)\n        - --leader-election-namespace=kube-system\n        resources:\n          limits:\n            memory: 512Mi  # Increased from 256Mi\n          requests:\n            cpu: 100m\n            memory: 256Mi  # Increased from 128Mi',
   'acme-corp/platform',
   'https://github.com/acme-corp/platform/pull/320',
   320,
   'fix/aurora-cert-manager-memory',
   '2026-02-15 09:04:30');

END $$;

-- 9. Chat session
DO $$
DECLARE
  v_incident_id UUID := 'f1a2b3c4-d5e6-4f7a-8b9c-0d1e2f3a4b5c';
  v_chat_session_id VARCHAR(50) := 'a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d';
  v_demo_user_id VARCHAR(255) := 'ab209180-626b-4601-8042-9f6328d03ae9';
BEGIN

  INSERT INTO chat_sessions (id, user_id, title, messages, created_at, updated_at, is_active, status, incident_id) VALUES
  (v_chat_session_id, v_demo_user_id,
   'RCA: Intermittent 503 errors on payment-gateway service',
   ('[
     {"sender":"user","text":"# ROOT CAUSE ANALYSIS REQUIRED - NETDATA ALERT\n\n## ALERT DETAILS:\n- **Title**: Intermittent 503 errors on payment-gateway service\n- **Status**: CRITICAL\n- **Source**: netdata\n- **Host**: aks-payment-node-1.westus2.cloudapp.azure.com\n- **Value**: 31.2% of requests returning 503\n- **Info**: Pattern shows intermittent failures, not total outage\n\n## CONNECTED PROVIDERS:\nYou have access to: azure, github, github_repo_selection, splunk\n\n## GITHUB REPOSITORY CONTEXT:\n- **Connected Repository**: acme-corp/platform\n- **Branch**: main\n\n### INVESTIGATION INSTRUCTIONS:\nConduct a thorough Root Cause Analysis. The intermittent nature (30% failure) is unusual - investigate why some requests succeed while others fail.","message_number":1,"isCompleted":true},
     {"sender":"bot","text":"","message_number":2,"isCompleted":true,"run_id":"lc_run--demo4-0001","toolCalls":[{"id":"tc_demo4_001","tool_name":"knowledge_base_search","input":"{\"query\": \"Azure AKS 503 service mesh Istio certificate troubleshooting intermittent\", \"limit\": 5}","output":"Found: Service Mesh Certificate Management Runbook. Key steps: Check workload certificate expiration, verify cert-manager health, inspect Istio certificate rotation logs. Common cause of intermittent failures: pod-level certificate drift.","status":"completed","timestamp":"2026-02-15T09:00:25.000000"},{"id":"tc_demo4_002","tool_name":"cloud_exec","input":"{\"command\": \"kubectl get pods -n payments -l app=payment-gateway -o wide\", \"provider\": \"azure\"}","output":"NAME                                READY   STATUS    RESTARTS   AGE   IP            NODE\npayment-gateway-7c8d9e1f2-ab3cd    2/2     Running   0          3d    10.244.1.12   aks-agentpool-1\npayment-gateway-7c8d9e1f2-ef4gh    2/2     Running   0          3d    10.244.2.23   aks-agentpool-2\npayment-gateway-7c8d9e1f2-ij5kl    2/2     Running   0          3d    10.244.1.34   aks-agentpool-1\npayment-gateway-7c8d9e1f2-mn6op    2/2     Running   0          22h   10.244.3.45   aks-agentpool-3\npayment-gateway-7c8d9e1f2-qr7st    2/2     Running   0          22h   10.244.2.56   aks-agentpool-2","status":"completed","timestamp":"2026-02-15T09:00:40.000000"},{"id":"tc_demo4_003","tool_name":"cloud_exec","input":"{\"command\": \"kubectl get endpoints payment-gateway -n payments -o json\", \"provider\": \"azure\"}","output":"{\"subsets\": [{\"addresses\": [{\"ip\": \"10.244.3.45\"}, {\"ip\": \"10.244.2.56\"}], \"notReadyAddresses\": [{\"ip\": \"10.244.1.12\"}, {\"ip\": \"10.244.2.23\"}, {\"ip\": \"10.244.1.34\"}]}]}","status":"completed","timestamp":"2026-02-15T09:00:50.000000"}]},
     {"sender":"bot","text":"Found a service mesh certificate runbook [13]. All 5 payment-gateway pods are Running, but the endpoint check reveals only 2 pods are Ready while 3 are NotReady [2]. This 2-out-of-5 pattern matches the ~30% success rate. The 2 ready pods are the newer ones (22h old), the 3 not-ready pods are 3 days old [1]. Let me check logs from an old pod to see why it is failing.","message_number":3,"isCompleted":true,"run_id":"lc_run--demo4-0002","toolCalls":[{"id":"tc_demo4_004","tool_name":"cloud_exec","input":"{\"command\": \"kubectl get pods -n payments -l app=payment-gateway --sort-by=''.metadata.creationTimestamp''\", \"provider\": \"azure\"}","output":"NAME                                CREATED\npayment-gateway-7c8d9e1f2-ab3cd    2026-02-12T06:15:23Z (3d old)\npayment-gateway-7c8d9e1f2-ef4gh    2026-02-12T06:15:23Z (3d old)\npayment-gateway-7c8d9e1f2-ij5kl    2026-02-12T06:15:23Z (3d old)\npayment-gateway-7c8d9e1f2-mn6op    2026-02-14T11:22:10Z (22h old)\npayment-gateway-7c8d9e1f2-qr7st    2026-02-14T11:22:10Z (22h old)","status":"completed","timestamp":"2026-02-15T09:01:00.000000"},{"id":"tc_demo4_005","tool_name":"cloud_exec","input":"{\"command\": \"kubectl logs payment-gateway-7c8d9e1f2-ab3cd -n payments -c istio-proxy --tail=20\", \"provider\": \"azure\"}","output":"[2026-02-15T09:00:12.456Z] \"GET /api/charge HTTP/1.1\" 503 - tls_error 0 152 25\n[error] TLS handshake failed: x509: certificate has expired\n[2026-02-15T09:00:12.512Z] \"POST /api/refund HTTP/1.1\" 503 - tls_error 0 178 31\n[error] TLS handshake failed: x509: certificate has expired","status":"completed","timestamp":"2026-02-15T09:01:20.000000"}]},
     {"sender":"bot","text":"Pod age confirms the split: 3 pods are 3 days old, 2 pods are 22 hours old [3]. Logs from an old pod show TLS handshake failures with \"certificate has expired\" errors [4]. This is an Istio mTLS certificate issue. The old pods have an expired certificate. Let me inspect the actual certificate.","message_number":4,"isCompleted":true,"run_id":"lc_run--demo4-0003","toolCalls":[{"id":"tc_demo4_006","tool_name":"cloud_exec","input":"{\"command\": \"kubectl exec payment-gateway-7c8d9e1f2-ab3cd -n payments -c istio-proxy -- openssl x509 -in /etc/certs/cert-chain.pem -noout -dates -subject\", \"provider\": \"azure\"}","output":"subject=O=cluster.local,CN=payment-gateway.payments.svc.cluster.local\nnotBefore=Jan 14 14:23:15 2026 GMT\nnotAfter=Feb 13 14:23:15 2026 GMT\n\nCertificate expired 2 days ago.","status":"completed","timestamp":"2026-02-15T09:01:50.000000"},{"id":"tc_demo4_007","tool_name":"cloud_exec","input":"{\"command\": \"kubectl exec payment-gateway-7c8d9e1f2-mn6op -n payments -c istio-proxy -- openssl x509 -in /etc/certs/cert-chain.pem -noout -dates -subject\", \"provider\": \"azure\"}","output":"subject=O=cluster.local,CN=payment-gateway.payments.svc.cluster.local\nnotBefore=Feb 14 11:22:45 2026 GMT\nnotAfter=Mar 16 11:22:45 2026 GMT\n\nCertificate is valid (self-signed fallback from Istio)","status":"completed","timestamp":"2026-02-15T09:01:58.000000"}]},
     {"sender":"bot","text":"**Certificate expiration confirmed.** Old pod has a certificate that expired on Feb 13 (2 days ago) [5]. New pod has a valid certificate issued on Feb 14 [6]. Istio cert-manager is supposed to auto-rotate certificates before expiration. Let me check if cert-manager is healthy.","message_number":5,"isCompleted":true,"run_id":"lc_run--demo4-0004","toolCalls":[{"id":"tc_demo4_008","tool_name":"cloud_exec","input":"{\"command\": \"kubectl get deployment cert-manager -n cert-manager\", \"provider\": \"azure\"}","output":"NAME           READY   UP-TO-DATE   AVAILABLE   AGE\ncert-manager   0/1     1            0           45d\n\nkubectl get pods -n cert-manager:\nNAME                           READY   STATUS             RESTARTS      AGE\ncert-manager-7f8c9d6e5-xyz12   0/1     CrashLoopBackOff   1247 (5m)     5d\n\nLogs: FATAL: out of memory: cannot allocate memory, OOMKilled\nLast successful run: Feb 10, 2026 03:15 UTC","status":"completed","timestamp":"2026-02-15T09:02:10.000000"},{"id":"tc_demo4_009","tool_name":"search_splunk","input":"{\"query\": \"index=kubernetes namespace=payments \\\"TLS\\\" OR \\\"x509\\\" OR \\\"certificate\\\" | stats count by pod\"}","output":"Pod                              Count\npayment-gateway-7c8d9e1f2-ab3cd  1247\npayment-gateway-7c8d9e1f2-ef4gh  1183\npayment-gateway-7c8d9e1f2-ij5kl  1205\npayment-gateway-7c8d9e1f2-mn6op  0\npayment-gateway-7c8d9e1f2-qr7st  0","status":"completed","timestamp":"2026-02-15T09:02:50.000000"}]},
     {"sender":"bot","text":"**Root cause identified.** cert-manager has been in CrashLoopBackOff for 5 days due to OOM (out of memory) [7]. Without cert-manager, Istio certificates were not rotated. The payment-gateway certificate expired on Feb 13. Pods created before expiration cached the expired cert and continue using it (causing TLS failures). Pods created after expiration received the Istio self-signed fallback cert and work correctly. Splunk confirms only the 3 old pods have TLS errors [9]. Let me search for best practices and apply a fix.","message_number":6,"isCompleted":true,"run_id":"lc_run--demo4-0005","toolCalls":[{"id":"tc_demo4_010","tool_name":"web_search","input":"{\"query\": \"Istio certificate expiration cert-manager failure recovery\"}","output":"1. Istio Certificate Management (istio.io): When cert-manager fails, Istio will not rotate workload certificates. Expired certificates cause TLS handshake failures.\n\n2. Cert-Manager OOM Issues (cert-manager.io): cert-manager memory usage grows with certificate count. Default 256Mi limit may be insufficient. Increase to 512Mi or 1Gi.\n\n3. Manual Certificate Rotation (istio.io/operations): To recover: 1) Fix cert-manager, 2) Run istioctl cert rotate, 3) Restart workloads.","status":"completed","timestamp":"2026-02-15T09:03:30.000000"},{"id":"tc_demo4_011","tool_name":"cloud_exec","input":"{\"command\": \"kubectl set resources deployment cert-manager -n cert-manager --limits=memory=512Mi && kubectl rollout restart deployment/cert-manager -n cert-manager\", \"provider\": \"azure\"}","output":"deployment.apps/cert-manager resource limits updated\ndeployment.apps/cert-manager restarted\n\nWaiting for deployment rollout...\ndeployment \"cert-manager\" successfully rolled out\ncert-manager-7f8c9d6e5-abc34   1/1     Running   0          15s","status":"completed","timestamp":"2026-02-15T09:03:50.000000"},{"id":"tc_demo4_012","tool_name":"cloud_exec","input":"{\"command\": \"kubectl exec -n istio-system deploy/istiod -- pilot-discovery request POST /debug/force-rotate-cert\", \"provider\": \"azure\"}","output":"Certificate rotation triggered for all workloads.\npayment-gateway.payments: certificate renewed (expires Mar 17, 2026)\nStatus: Success","status":"completed","timestamp":"2026-02-15T09:04:00.000000"},{"id":"tc_demo4_013","tool_name":"cloud_exec","input":"{\"command\": \"kubectl rollout restart deployment/payment-gateway -n payments && kubectl rollout status deployment/payment-gateway -n payments\", \"provider\": \"azure\"}","output":"deployment.apps/payment-gateway restarted\nWaiting for deployment rollout...\ndeployment \"payment-gateway\" successfully rolled out\n\nAll 5 pods now healthy with valid certificates. 503 error rate: 0%.","status":"completed","timestamp":"2026-02-15T09:04:10.000000"}]},
     {"sender":"bot","text":"Investigation complete. Here is a summary:\n\n**Root Cause**: cert-manager crashed 5 days ago with OOM (256Mi limit too low). Without cert-manager, Istio workload certificates were not rotated. The payment-gateway certificate expired on Feb 13. Pods created before expiration cached the expired cert (causing 503 TLS errors). Pods created after expiration received Istio fallback self-signed certs (working correctly). This caused 30% of requests to succeed (2 new pods) and 70% to fail (3 old pods).\n\n**Actions Taken**:\n1. Increased cert-manager memory limit from 256Mi to 512Mi and restarted [10]\n2. Manually triggered Istio certificate rotation for all workloads [11]\n3. Performed rolling restart of payment-gateway deployment to pick up new certificates [12]\n4. All pods now healthy, 503 error rate dropped to 0%\n\n**Recommended Follow-ups**:\n- Add monitoring for cert-manager health and memory usage\n- Create alerts for certificate expiration (7 days before)\n- Audit all cluster certificates to ensure none expired during the cert-manager outage\n- Document manual certificate rotation procedure for emergencies","message_number":7,"isCompleted":true,"run_id":"lc_run--demo4-0006","toolCalls":[]}
   ]')::jsonb,
   '2026-02-15 09:00:00', '2026-02-15 09:18:00', true, 'completed', v_incident_id);

  RAISE NOTICE 'Demo incident 4 chat session inserted.';

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
                WHEN updated_tc->>'tool_name' = 'knowledge_base_search' THEN
                  updated_tc || jsonb_build_object('command', 'Knowledge Base')
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

  RAISE NOTICE 'Demo incident 4 fully inserted.';

END $$;
