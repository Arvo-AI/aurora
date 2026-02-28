-- Demo Incident (Dynatrace + BigPanda + ThousandEyes): API gateway degradation
-- Source: BigPanda | Tools: query_dynatrace, thousandeyes_*
-- Root cause: Memory leak on api-gateway host causing GC pauses, compounded by
--             ISP packet loss on path to payment processor detected by ThousandEyes

DO $$
DECLARE
  v_incident_id UUID := 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';
  v_chat_session_id VARCHAR(50) := 'a1b2c3d5-f6a7-4901-bcde-f12345678901';
  v_demo_user_id VARCHAR(255) := 'ab209180-626b-4601-8042-9f6328d03ae9';
  v_bp_event_id INTEGER;
  v_incident_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM incidents WHERE id = v_incident_id) INTO v_incident_exists;
  IF v_incident_exists THEN
    RAISE NOTICE 'Demo incident (DT+BP+TE) already exists, skipping.';
    RETURN;
  END IF;

  RAISE NOTICE 'Inserting demo incident (DT+BP+TE)...';

  -- 1. BigPanda event (correlated incident from BigPanda)
  INSERT INTO bigpanda_events (
    user_id, event_type, incident_id, incident_title, incident_status,
    incident_severity, primary_property, secondary_property, source_system,
    child_alert_count, payload, received_at, created_at
  ) VALUES (
    v_demo_user_id, 'incident', 'BP-INC-20260226-0042',
    'API gateway error rate spike with payment path degradation',
    'active', 'critical', 'api-gateway', 'error_rate',
    'dynatrace', 3,
    '{"incident": {"id": "BP-INC-20260226-0042", "status": "active", "severity": "critical", "environments": ["production"], "incident_tags": {"service": "api-gateway", "cluster": "prod-us-east", "team": "platform-engineering"}, "alerts": [{"description": "Service api-gateway error rate exceeded 12% threshold", "source_system": "dynatrace", "primary_property": "api-gateway", "secondary_property": "error_rate", "status": "active", "severity": "critical", "condition_name": "Error Rate > 10%"}, {"description": "HTTP test to payments.processor.com failing with packet loss > 8%", "source_system": "thousandeyes", "primary_property": "payments.processor.com", "secondary_property": "packet_loss", "status": "active", "severity": "warning", "condition_name": "Packet Loss > 5%"}, {"description": "Host memory utilization at 94% on api-gateway-prod-01", "source_system": "dynatrace", "primary_property": "api-gateway-prod-01", "secondary_property": "memory", "status": "active", "severity": "warning", "condition_name": "Memory > 90%"}]}}'::jsonb,
    '2026-02-26T14:32:00.000000', '2026-02-26T14:32:00.000000'
  ) RETURNING id INTO v_bp_event_id;

  -- 2. Main incident record
  INSERT INTO incidents (
    id, user_id, source_type, source_alert_id, status, severity,
    alert_title, alert_service, alert_environment, aurora_status,
    aurora_summary, aurora_chat_session_id, started_at, analyzed_at,
    active_tab, created_at, updated_at, alert_metadata,
    correlated_alert_count, affected_services, visualization_code,
    visualization_updated_at
  ) VALUES (
    v_incident_id, v_demo_user_id, 'bigpanda', v_bp_event_id,
    'resolved', 'critical',
    'API gateway error rate spike with payment path degradation - prod-us-east',
    'api-gateway', 'production', 'complete',
    '', v_chat_session_id::uuid,
    '2026-02-26 14:32:00', '2026-02-26 14:58:00',
    'thoughts', '2026-02-26 14:32:00', '2026-02-26 14:58:00',
    jsonb_build_object(
      'is_demo', true,
      'environments', ARRAY['production'],
      'incident_tags', '{"service": "api-gateway", "cluster": "prod-us-east", "team": "platform-engineering"}'::jsonb,
      'childAlerts', '[{"description": "Service api-gateway error rate exceeded 12% threshold", "source_system": "dynatrace", "primary_property": "api-gateway", "severity": "critical"}, {"description": "HTTP test to payments.processor.com failing with packet loss > 8%", "source_system": "thousandeyes", "primary_property": "payments.processor.com", "severity": "warning"}, {"description": "Host memory utilization at 94% on api-gateway-prod-01", "source_system": "dynatrace", "primary_property": "api-gateway-prod-01", "severity": "warning"}]'::jsonb
    ),
    2,
    ARRAY['api-gateway', 'payment-service', 'api-gateway-prod-01'],
    '', '2026-02-26 14:58:00'
  );

  -- 3. Correlated alerts
  INSERT INTO incident_alerts (user_id, incident_id, source_type, source_alert_id, alert_title, alert_service, alert_severity, correlation_strategy, correlation_score, correlation_details, alert_metadata, received_at) VALUES
  (v_demo_user_id, v_incident_id, 'bigpanda', v_bp_event_id, 'API gateway error rate spike with payment path degradation - prod-us-east', 'api-gateway', 'critical', 'primary', 1.0, '{}', '{"is_demo": true, "source_system": "dynatrace", "condition_name": "Error Rate > 10%"}'::jsonb, '2026-02-26 14:32:00'),
  (v_demo_user_id, v_incident_id, 'bigpanda', v_bp_event_id + 10000, 'ThousandEyes: HTTP test to payments.processor.com packet loss > 8%', 'payments.processor.com', 'warning', 'time_proximity', 0.91, '{"reason":"Network degradation to payment processor detected within 90s of api-gateway error rate spike"}'::jsonb, '{"is_demo": true, "source_system": "thousandeyes", "condition_name": "Packet Loss > 5%"}'::jsonb, '2026-02-26 14:33:30'),
  (v_demo_user_id, v_incident_id, 'bigpanda', v_bp_event_id + 10001, 'Dynatrace: Host memory at 94% on api-gateway-prod-01', 'api-gateway-prod-01', 'warning', 'topology', 0.87, '{"reason":"Host memory saturation on api-gateway node correlates with service error rate increase"}'::jsonb, '{"is_demo": true, "source_system": "dynatrace", "condition_name": "Memory > 90%"}'::jsonb, '2026-02-26 14:34:15');

  RAISE NOTICE 'Demo incident (DT+BP+TE) core record inserted.';
END $$;

-- Block 2: Thoughts
DO $$
DECLARE
  v_incident_id UUID := 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';
  v_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM incident_thoughts WHERE incident_id = v_incident_id) INTO v_exists;
  IF v_exists THEN
    RAISE NOTICE 'Demo incident (DT+BP+TE) thoughts already exist, skipping.';
    RETURN;
  END IF;

  INSERT INTO incident_thoughts (incident_id, timestamp, content, thought_type) VALUES
  (v_incident_id, '2026-02-26 14:32:30', 'BigPanda correlated incident received: 3 child alerts -- Dynatrace error rate spike on api-gateway, ThousandEyes packet loss to payments.processor.com, and Dynatrace high memory on api-gateway-prod-01. Starting investigation with Dynatrace problems API.', 'analysis'),
  (v_incident_id, '2026-02-26 14:33:00', 'Dynatrace confirms an active problem on the api-gateway service: error rate at 14.2%, started at 14:28 UTC. The problem is auto-detected and classified as a service-level degradation. Pulling Dynatrace entity data for the host.', 'analysis'),
  (v_incident_id, '2026-02-26 14:34:00', 'Dynatrace entity details for HOST-9A3F1B2C show api-gateway-prod-01 running at 94% memory utilization with JVM heap at 91%. GC pause time has increased from 20ms baseline to 480ms. This strongly suggests a memory leak causing stop-the-world garbage collection pauses.', 'analysis'),
  (v_incident_id, '2026-02-26 14:35:00', 'Querying Dynatrace logs for api-gateway-prod-01 to identify the memory leak source. Filtering for OutOfMemory, GC, and heap-related log entries around the incident window.', 'analysis'),
  (v_incident_id, '2026-02-26 14:36:00', 'Dynatrace logs show repeated GC overhead warnings starting at 14:26 -- "GC overhead limit exceeded: 94% of time spent in GC, recovering < 2% heap". Also found: "java.lang.OutOfMemoryError: Java heap space" at 14:31. The application is exhausting its heap well before the alert fired.', 'analysis'),
  (v_incident_id, '2026-02-26 14:37:00', 'Now pivoting to ThousandEyes to understand the network dimension of this incident. BigPanda correlated a packet loss alert to payments.processor.com. Listing active ThousandEyes tests targeting that endpoint.', 'analysis'),
  (v_incident_id, '2026-02-26 14:38:00', 'ThousandEyes has 3 active tests to payments.processor.com: HTTP server test (ID: 28451), network path trace (ID: 28452), and page load test (ID: 28453). Pulling recent results for the HTTP server test.', 'analysis'),
  (v_incident_id, '2026-02-26 14:39:30', 'ThousandEyes HTTP test results show response time jumped from 120ms baseline to 2,400ms at 14:30 UTC, with 8.3% packet loss on the network path. The degradation correlates with the Dynatrace error rate spike but the root causes appear to be independent.', 'analysis'),
  (v_incident_id, '2026-02-26 14:40:30', 'ThousandEyes path visualization shows packet loss concentrated on the ISP hop between AT&T peering point (ae-12.r01.nycmny17.us.bb.gin.ntt.net) and the payment processor datacenter. This is a transit provider issue, not within our infrastructure.', 'analysis'),
  (v_incident_id, '2026-02-26 14:41:30', 'Checking ThousandEyes alerts for broader context. Found 2 active alerts: the HTTP test failure to payments.processor.com and a BGP route instability alert on the same path prefix (203.0.113.0/24).', 'analysis'),
  (v_incident_id, '2026-02-26 14:43:00', 'ThousandEyes agent details show the issue is observed from 4 of 6 cloud agents (NYC, IAD, ORD, ATL) but NOT from SFO or LAX agents. This confirms it is an East Coast ISP transit issue, not a destination-side problem.', 'analysis'),
  (v_incident_id, '2026-02-26 14:44:00', 'Pulling Dynatrace metrics for api-gateway service-level response time to quantify the combined impact. The p99 latency went from 250ms to 8.2 seconds, with a 14.2% error rate. This matches the two compounding factors: local GC pauses + remote payment path degradation.', 'analysis'),
  (v_incident_id, '2026-02-26 14:45:00', 'Two distinct root causes identified. Primary: Memory leak on api-gateway-prod-01 causing GC pauses and local request failures. Secondary: ISP packet loss on East Coast transit path to payments.processor.com causing timeout on payment-related API calls. These are independent failures that BigPanda correctly correlated based on timing.', 'analysis'),
  (v_incident_id, '2026-02-26 14:47:00', 'Recommended immediate actions: (1) Restart api-gateway-prod-01 to clear the memory leak and restore heap, (2) Failover payment traffic to West Coast path to avoid the ISP transit issue, (3) Open a ticket with NTT Communications for the peering point packet loss.', 'analysis'),
  (v_incident_id, '2026-02-26 14:50:00', 'Long-term fixes: Investigate the api-gateway memory leak root cause (likely the request caching module based on heap dump analysis), add JVM heap monitoring alerts at 80% threshold, and configure ThousandEyes BGP alerting to proactively detect route instability before packet loss manifests.', 'analysis'),
  (v_incident_id, '2026-02-26 14:53:00', 'Investigation complete. BigPanda event correlation successfully grouped two independent failures into a single incident. Aurora cross-referenced Dynatrace deep monitoring with ThousandEyes network visibility to isolate both root causes and recommend targeted remediation.', 'analysis');

  RAISE NOTICE 'Demo incident (DT+BP+TE) thoughts inserted.';
END $$;

-- Block 3: Citations (4 Dynatrace + 5 ThousandEyes)
DO $$
DECLARE
  v_incident_id UUID := 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';
  v_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM incident_citations WHERE incident_id = v_incident_id) INTO v_exists;
  IF v_exists THEN
    RAISE NOTICE 'Demo incident (DT+BP+TE) citations already exist, skipping.';
    RETURN;
  END IF;

  -- Citation 1: Dynatrace problems
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at)
  VALUES (v_incident_id, '1', 'Dynatrace', 'query_dynatrace: problems for api-gateway', '{
  "resource_type": "problems",
  "count": 1,
  "total": 1,
  "results": [
    {
      "problemId": "P-2026022614280001",
      "displayId": "P-1842",
      "title": "Error rate increase on api-gateway",
      "impactLevel": "SERVICE",
      "severityLevel": "ERROR",
      "status": "OPEN",
      "startTime": 1740579480000,
      "endTime": -1,
      "impactedEntities": [
        {"entityId": {"id": "SERVICE-4B2E9F1A3C7D8E0F", "type": "SERVICE"}, "name": "api-gateway"}
      ],
      "rootCauseEntity": {
        "entityId": {"id": "HOST-9A3F1B2C4D5E6F70", "type": "HOST"},
        "name": "api-gateway-prod-01"
      },
      "affectedEntities": [
        {"entityId": {"id": "SERVICE-4B2E9F1A3C7D8E0F", "type": "SERVICE"}, "name": "api-gateway"},
        {"entityId": {"id": "SERVICE-7C8D9E0F1A2B3C4D", "type": "SERVICE"}, "name": "payment-service"}
      ],
      "managementZones": [{"id": "2481039812", "name": "Production"}],
      "evidenceDetails": {
        "totalCount": 3,
        "details": [
          {"evidenceType": "METRIC", "displayName": "Error rate", "entity": {"name": "api-gateway"}, "data": {"currentValue": 14.2, "threshold": 5.0, "unit": "PERCENT"}},
          {"evidenceType": "METRIC", "displayName": "GC pause time", "entity": {"name": "api-gateway-prod-01"}, "data": {"currentValue": 480, "threshold": 100, "unit": "MILLISECOND"}},
          {"evidenceType": "METRIC", "displayName": "Memory usage", "entity": {"name": "api-gateway-prod-01"}, "data": {"currentValue": 94.1, "threshold": 90.0, "unit": "PERCENT"}}
        ]
      }
    }
  ],
  "success": true,
  "time_range": "now-2h to now"
}', '2026-02-26 14:33:00');

  -- Citation 2: Dynatrace entity details (host)
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at)
  VALUES (v_incident_id, '2', 'Dynatrace', 'query_dynatrace: entities HOST-9A3F1B2C', '{
  "resource_type": "entities",
  "count": 1,
  "total": 1,
  "results": [
    {
      "entityId": "HOST-9A3F1B2C4D5E6F70",
      "displayName": "api-gateway-prod-01",
      "type": "HOST",
      "tags": [{"key": "environment", "value": "production"}, {"key": "service", "value": "api-gateway"}, {"key": "cluster", "value": "prod-us-east"}],
      "managementZones": [{"id": "2481039812", "name": "Production"}],
      "properties": {
        "osType": "LINUX",
        "osVersion": "Ubuntu 22.04.3 LTS",
        "cpuCores": 8,
        "memoryTotal": 34359738368,
        "memoryUsed": 32290025472,
        "jvmHeapMax": 17179869184,
        "jvmHeapUsed": 15654042828,
        "gcPauseTime": 480,
        "gcPauseTimeBaseline": 20,
        "ipAddresses": ["10.0.42.18"],
        "cloudType": "AWS",
        "instanceType": "m5.2xlarge"
      }
    }
  ],
  "success": true,
  "time_range": "now-24h to now"
}', '2026-02-26 14:34:00');

  -- Citation 3: Dynatrace logs
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at)
  VALUES (v_incident_id, '3', 'Dynatrace', 'query_dynatrace: logs api-gateway-prod-01 GC|OutOfMemory', '{
  "resource_type": "logs",
  "count": 6,
  "total": 6,
  "results": [
    {"timestamp": "2026-02-26T14:26:12.441Z", "content": "WARN  [gc-monitor] GC overhead limit approaching: 87% of time spent in GC", "loglevel": "WARN", "host": "api-gateway-prod-01"},
    {"timestamp": "2026-02-26T14:27:45.102Z", "content": "WARN  [gc-monitor] GC overhead limit approaching: 91% of time spent in GC, recovering < 5% heap", "loglevel": "WARN", "host": "api-gateway-prod-01"},
    {"timestamp": "2026-02-26T14:28:33.887Z", "content": "ERROR [gc-monitor] GC overhead limit exceeded: 94% of time spent in GC, recovering < 2% heap", "loglevel": "ERROR", "host": "api-gateway-prod-01"},
    {"timestamp": "2026-02-26T14:29:18.224Z", "content": "ERROR [http-handler] Request processing failed: java.lang.OutOfMemoryError: GC overhead limit exceeded", "loglevel": "ERROR", "host": "api-gateway-prod-01"},
    {"timestamp": "2026-02-26T14:31:02.556Z", "content": "FATAL [main] java.lang.OutOfMemoryError: Java heap space at com.gateway.cache.RequestCacheManager.put(RequestCacheManager.java:142)", "loglevel": "FATAL", "host": "api-gateway-prod-01"},
    {"timestamp": "2026-02-26T14:31:04.112Z", "content": "ERROR [health-check] Service health check failed: heap utilization 97%, response latency 8241ms", "loglevel": "ERROR", "host": "api-gateway-prod-01"}
  ],
  "success": true,
  "time_range": "2026-02-26T14:20:00Z to 2026-02-26T14:35:00Z"
}', '2026-02-26 14:36:00');

  -- Citation 4: Dynatrace metrics (service-level response time)
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at)
  VALUES (v_incident_id, '4', 'Dynatrace', 'query_dynatrace: metrics builtin:service.response.time for api-gateway', '{
  "resource_type": "metrics",
  "count": 8,
  "total": 8,
  "results": [
    {"timestamp": "2026-02-26T14:20:00Z", "value": 245.3, "unit": "MILLISECOND", "dimensions": {"dt.entity.service": "api-gateway"}},
    {"timestamp": "2026-02-26T14:25:00Z", "value": 312.7, "unit": "MILLISECOND", "dimensions": {"dt.entity.service": "api-gateway"}},
    {"timestamp": "2026-02-26T14:28:00Z", "value": 1842.1, "unit": "MILLISECOND", "dimensions": {"dt.entity.service": "api-gateway"}},
    {"timestamp": "2026-02-26T14:30:00Z", "value": 4215.8, "unit": "MILLISECOND", "dimensions": {"dt.entity.service": "api-gateway"}},
    {"timestamp": "2026-02-26T14:32:00Z", "value": 8241.3, "unit": "MILLISECOND", "dimensions": {"dt.entity.service": "api-gateway"}},
    {"timestamp": "2026-02-26T14:35:00Z", "value": 7832.6, "unit": "MILLISECOND", "dimensions": {"dt.entity.service": "api-gateway"}},
    {"timestamp": "2026-02-26T14:40:00Z", "value": 3124.5, "unit": "MILLISECOND", "dimensions": {"dt.entity.service": "api-gateway"}},
    {"timestamp": "2026-02-26T14:45:00Z", "value": 487.2, "unit": "MILLISECOND", "dimensions": {"dt.entity.service": "api-gateway"}}
  ],
  "success": true,
  "time_range": "2026-02-26T14:20:00Z to 2026-02-26T14:45:00Z",
  "metric_key": "builtin:service.response.time",
  "aggregation": "P99"
}', '2026-02-26 14:44:00');

  RAISE NOTICE 'Demo incident (DT+BP+TE) citations 1-4 (Dynatrace) inserted.';
END $$;

-- Block 4: ThousandEyes citations (5-9)
DO $$
DECLARE
  v_incident_id UUID := 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';
  v_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM incident_citations WHERE incident_id = v_incident_id AND citation_key = '5') INTO v_exists;
  IF v_exists THEN
    RAISE NOTICE 'Demo incident (DT+BP+TE) TE citations already exist, skipping.';
    RETURN;
  END IF;

  -- Citation 5: ThousandEyes list tests
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at)
  VALUES (v_incident_id, '5', 'thousandeyes_list_tests', 'thousandeyes_list_tests: payments.processor.com', '{
  "tests": [
    {"testId": 28451, "testName": "Payment Processor - HTTP Server", "type": "http-server", "url": "https://payments.processor.com/api/v2/health", "interval": 120, "enabled": true, "alertsEnabled": true, "savedEvent": false, "agents": [{"agentId": 14201, "agentName": "New York, NY"}, {"agentId": 14202, "agentName": "Washington, DC"}, {"agentId": 14203, "agentName": "Chicago, IL"}, {"agentId": 14204, "agentName": "Atlanta, GA"}, {"agentId": 14205, "agentName": "San Francisco, CA"}, {"agentId": 14206, "agentName": "Los Angeles, CA"}]},
    {"testId": 28452, "testName": "Payment Processor - Network Path Trace", "type": "network", "server": "payments.processor.com", "port": 443, "protocol": "TCP", "interval": 120, "enabled": true, "alertsEnabled": true},
    {"testId": 28453, "testName": "Payment Processor - Page Load", "type": "page-load", "url": "https://payments.processor.com/status", "interval": 300, "enabled": true, "alertsEnabled": true}
  ],
  "count": 3
}', '2026-02-26 14:37:30');

  -- Citation 6: ThousandEyes HTTP test results
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at)
  VALUES (v_incident_id, '6', 'thousandeyes_get_test_results', 'thousandeyes_get_test_results: test 28451 (HTTP Server)', '{
  "test": {"testId": 28451, "testName": "Payment Processor - HTTP Server"},
  "results": [
    {"agentId": 14201, "agentName": "New York, NY", "roundId": 1740580200, "responseTime": 2412, "totalTime": 2834, "connectTime": 342, "dnsTime": 12, "errorType": "None", "responseCode": 200, "throughput": 1240, "loss": 8.3, "latency": 124.5, "jitter": 42.1},
    {"agentId": 14202, "agentName": "Washington, DC", "roundId": 1740580200, "responseTime": 2198, "totalTime": 2654, "connectTime": 298, "dnsTime": 8, "errorType": "None", "responseCode": 200, "throughput": 1380, "loss": 7.1, "latency": 118.2, "jitter": 38.4},
    {"agentId": 14203, "agentName": "Chicago, IL", "roundId": 1740580200, "responseTime": 1987, "totalTime": 2342, "connectTime": 267, "dnsTime": 11, "errorType": "None", "responseCode": 200, "throughput": 1560, "loss": 6.8, "latency": 102.8, "jitter": 31.2},
    {"agentId": 14204, "agentName": "Atlanta, GA", "roundId": 1740580200, "responseTime": 2087, "totalTime": 2498, "connectTime": 285, "dnsTime": 9, "errorType": "None", "responseCode": 200, "throughput": 1420, "loss": 7.5, "latency": 112.3, "jitter": 36.7},
    {"agentId": 14205, "agentName": "San Francisco, CA", "roundId": 1740580200, "responseTime": 134, "totalTime": 198, "connectTime": 42, "dnsTime": 7, "errorType": "None", "responseCode": 200, "throughput": 8420, "loss": 0.0, "latency": 18.4, "jitter": 2.1},
    {"agentId": 14206, "agentName": "Los Angeles, CA", "roundId": 1740580200, "responseTime": 128, "totalTime": 187, "connectTime": 38, "dnsTime": 6, "errorType": "None", "responseCode": 200, "throughput": 8640, "loss": 0.0, "latency": 16.2, "jitter": 1.8}
  ],
  "baseline": {"responseTime": 120, "loss": 0.1, "latency": 15.8}
}', '2026-02-26 14:39:30');

  -- Citation 7: ThousandEyes path visualization (network test results)
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at)
  VALUES (v_incident_id, '7', 'thousandeyes_get_test_results', 'thousandeyes_get_test_results: test 28452 (Network Path Trace)', '{
  "test": {"testId": 28452, "testName": "Payment Processor - Network Path Trace"},
  "results": [
    {
      "agentId": 14201, "agentName": "New York, NY",
      "pathTraces": [
        {"hop": 1, "ipAddress": "10.0.1.1", "prefix": "10.0.0.0/8", "rdns": "gw.internal.aurora.net", "responseTime": 1, "loss": 0.0},
        {"hop": 2, "ipAddress": "198.51.100.1", "prefix": "198.51.100.0/24", "rdns": "be-101.cr1.nyc1.aurora.net", "responseTime": 3, "loss": 0.0},
        {"hop": 3, "ipAddress": "198.51.100.5", "prefix": "198.51.100.0/24", "rdns": "ae-2.r01.nycmny01.us.bb.att.net", "responseTime": 8, "loss": 0.0},
        {"hop": 4, "ipAddress": "192.0.2.42", "prefix": "192.0.2.0/24", "rdns": "ae-12.r01.nycmny17.us.bb.gin.ntt.net", "responseTime": 124, "loss": 8.3, "mpls": "L=3, E=0"},
        {"hop": 5, "ipAddress": "203.0.113.10", "prefix": "203.0.113.0/24", "rdns": "payments-edge-01.processor.com", "responseTime": 128, "loss": 8.3}
      ]
    }
  ],
  "summary": {"pathLength": 5, "lossHop": 4, "lossNode": "ae-12.r01.nycmny17.us.bb.gin.ntt.net", "lossPercent": 8.3}
}', '2026-02-26 14:40:30');

  -- Citation 8: ThousandEyes alerts
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at)
  VALUES (v_incident_id, '8', 'thousandeyes_get_alerts', 'thousandeyes_get_alerts: active alerts', '{
  "alerts": [
    {
      "alertId": 98201,
      "testId": 28451,
      "testName": "Payment Processor - HTTP Server",
      "ruleId": 4401,
      "ruleName": "HTTP Response Time > 2000ms",
      "type": "http-server",
      "severity": "MAJOR",
      "state": "ACTIVE",
      "startTime": "2026-02-26T14:30:00Z",
      "violationCount": 4,
      "agents": [
        {"agentId": 14201, "agentName": "New York, NY", "active": true, "metricsAtStart": {"responseTime": 2412, "loss": 8.3}},
        {"agentId": 14202, "agentName": "Washington, DC", "active": true, "metricsAtStart": {"responseTime": 2198, "loss": 7.1}},
        {"agentId": 14203, "agentName": "Chicago, IL", "active": true, "metricsAtStart": {"responseTime": 1987, "loss": 6.8}},
        {"agentId": 14204, "agentName": "Atlanta, GA", "active": true, "metricsAtStart": {"responseTime": 2087, "loss": 7.5}}
      ]
    },
    {
      "alertId": 98202,
      "testId": 28452,
      "testName": "Payment Processor - Network Path Trace",
      "ruleId": 4402,
      "ruleName": "BGP Route Instability",
      "type": "bgp",
      "severity": "WARNING",
      "state": "ACTIVE",
      "startTime": "2026-02-26T14:28:00Z",
      "violationCount": 1,
      "details": {"prefix": "203.0.113.0/24", "instabilityType": "path_change", "affectedPeers": 3}
    }
  ],
  "count": 2
}', '2026-02-26 14:41:30');

  -- Citation 9: ThousandEyes agents
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at)
  VALUES (v_incident_id, '9', 'thousandeyes_get_agents', 'thousandeyes_get_agents: cloud agents for test 28451', '{
  "agents": [
    {"agentId": 14201, "agentName": "New York, NY", "agentType": "Cloud", "location": "New York, NY, US", "countryId": "US", "ipAddresses": ["38.140.30.18"], "testResults": {"responseTime": 2412, "loss": 8.3, "status": "DEGRADED"}},
    {"agentId": 14202, "agentName": "Washington, DC", "agentType": "Cloud", "location": "Washington, DC, US", "countryId": "US", "ipAddresses": ["38.140.31.22"], "testResults": {"responseTime": 2198, "loss": 7.1, "status": "DEGRADED"}},
    {"agentId": 14203, "agentName": "Chicago, IL", "agentType": "Cloud", "location": "Chicago, IL, US", "countryId": "US", "ipAddresses": ["38.140.32.14"], "testResults": {"responseTime": 1987, "loss": 6.8, "status": "DEGRADED"}},
    {"agentId": 14204, "agentName": "Atlanta, GA", "agentType": "Cloud", "location": "Atlanta, GA, US", "countryId": "US", "ipAddresses": ["38.140.33.08"], "testResults": {"responseTime": 2087, "loss": 7.5, "status": "DEGRADED"}},
    {"agentId": 14205, "agentName": "San Francisco, CA", "agentType": "Cloud", "location": "San Francisco, CA, US", "countryId": "US", "ipAddresses": ["38.140.34.11"], "testResults": {"responseTime": 134, "loss": 0.0, "status": "HEALTHY"}},
    {"agentId": 14206, "agentName": "Los Angeles, CA", "agentType": "Cloud", "location": "Los Angeles, CA, US", "countryId": "US", "ipAddresses": ["38.140.35.06"], "testResults": {"responseTime": 128, "loss": 0.0, "status": "HEALTHY"}}
  ],
  "summary": {"total": 6, "degraded": 4, "healthy": 2, "pattern": "East Coast agents degraded, West Coast agents healthy"}
}', '2026-02-26 14:43:00');

  RAISE NOTICE 'Demo incident (DT+BP+TE) citations 5-9 (ThousandEyes) inserted.';
END $$;

-- Block 5: Suggestions
DO $$
DECLARE
  v_incident_id UUID := 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';
  v_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM incident_suggestions WHERE incident_id = v_incident_id) INTO v_exists;
  IF v_exists THEN
    RAISE NOTICE 'Demo incident (DT+BP+TE) suggestions already exist, skipping.';
    RETURN;
  END IF;

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id,
   'Restart api-gateway-prod-01 to clear memory leak',
   'The JVM heap is at 97% with GC overhead at 94%. A rolling restart will reclaim heap memory and restore normal request processing. This is the fastest path to restoring service health.',
   'mitigation', 'low',
   'ssh api-gateway-prod-01 "sudo systemctl restart api-gateway && sleep 5 && curl -s localhost:8080/health | jq .status"',
   '2026-02-26 14:50:00');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id,
   'Verify api-gateway recovery after restart',
   'After restarting the service, confirm that heap utilization drops below 60%, GC pause time returns to the 20ms baseline, and error rate drops below 1%.',
   'diagnostic', 'safe',
   'curl -s "https://dynatrace.example.com/api/v2/metrics/query?metricSelector=builtin:host.mem.usage&entitySelector=entityId(HOST-9A3F1B2C4D5E6F70)&from=now-15m" -H "Authorization: Api-Token $DT_TOKEN" | jq ".result[0].data[0].values[-1]"',
   '2026-02-26 14:50:30');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id,
   'Failover payment traffic to West Coast path',
   'ThousandEyes confirms the East Coast ISP transit path (NTT peering point) has 8.3% packet loss while West Coast agents show zero loss. Route payment API traffic through the us-west-2 region to bypass the degraded path.',
   'mitigation', 'medium',
   NULL,
   '2026-02-26 14:51:00');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id,
   'Add JVM heap monitoring alert at 80% threshold',
   'The memory leak went undetected until 94% utilization. Configure a Dynatrace custom alert to trigger at 80% JVM heap usage, giving the team time to investigate before GC pauses impact users.',
   'mitigation', 'safe',
   NULL,
   '2026-02-26 14:51:30');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id,
   'Investigate RequestCacheManager memory leak',
   'The OutOfMemoryError stack trace points to RequestCacheManager.put() as the allocation site. Review the caching eviction policy -- the cache may be growing unbounded under high request volume.',
   'diagnostic', 'safe',
   NULL,
   '2026-02-26 14:52:00');

  RAISE NOTICE 'Demo incident (DT+BP+TE) suggestions inserted.';
END $$;

-- Block 6: Chat session
DO $$
DECLARE
  v_incident_id UUID := 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';
  v_chat_session_id VARCHAR(50) := 'a1b2c3d5-f6a7-4901-bcde-f12345678901';
  v_demo_user_id VARCHAR(255) := 'ab209180-626b-4601-8042-9f6328d03ae9';
  v_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM chat_sessions WHERE id = v_chat_session_id) INTO v_exists;
  IF v_exists THEN
    RAISE NOTICE 'Demo incident (DT+BP+TE) chat session already exists, skipping.';
    RETURN;
  END IF;

  INSERT INTO chat_sessions (id, user_id, title, messages, created_at, updated_at, is_active, status, incident_id) VALUES
  (v_chat_session_id, v_demo_user_id,
   'RCA: API gateway degradation with payment path issues',
   ('[
     {"sender":"user","text":"# ROOT CAUSE ANALYSIS REQUIRED - BIGPANDA CORRELATED INCIDENT\n\n## ALERT DETAILS:\n- **Title**: API gateway error rate spike with payment path degradation - prod-us-east\n- **Status**: Active\n- **Source**: BigPanda (Event Correlation)\n- **Severity**: Critical\n- **Service**: api-gateway\n- **Cluster**: prod-us-east\n- **Environment**: production\n\n## CORRELATED CHILD ALERTS (3):\n1. **Dynatrace**: Service api-gateway error rate exceeded 12% threshold (critical)\n2. **ThousandEyes**: HTTP test to payments.processor.com failing with packet loss > 8% (warning)\n3. **Dynatrace**: Host memory utilization at 94% on api-gateway-prod-01 (warning)\n\n## CONNECTED PROVIDERS:\nYou have access to: dynatrace, thousandeyes\n\n### INVESTIGATION INSTRUCTIONS:\nConduct a thorough Root Cause Analysis. Use Dynatrace to investigate the application-level issues (error rates, host metrics, logs) and ThousandEyes to investigate network path degradation to the payment processor. Determine whether these are related or independent failures.","message_number":1,"isCompleted":true},
     {"sender":"bot","text":"","message_number":2,"isCompleted":true,"run_id":"lc_run--demo-dt-0001","toolCalls":[{"id":"tc_dt_001","tool_name":"query_dynatrace","input":"{\"resource_type\": \"problems\", \"query\": \"api-gateway\", \"time_from\": \"now-2h\", \"time_to\": \"now\"}","output":"{\"resource_type\": \"problems\", \"count\": 1, \"total\": 1, \"results\": [{\"problemId\": \"P-2026022614280001\", \"displayId\": \"P-1842\", \"title\": \"Error rate increase on api-gateway\", \"impactLevel\": \"SERVICE\", \"severityLevel\": \"ERROR\", \"status\": \"OPEN\", \"startTime\": 1740579480000, \"rootCauseEntity\": {\"entityId\": {\"id\": \"HOST-9A3F1B2C4D5E6F70\", \"type\": \"HOST\"}, \"name\": \"api-gateway-prod-01\"}, \"evidenceDetails\": {\"totalCount\": 3, \"details\": [{\"evidenceType\": \"METRIC\", \"displayName\": \"Error rate\", \"data\": {\"currentValue\": 14.2, \"threshold\": 5.0, \"unit\": \"PERCENT\"}}, {\"evidenceType\": \"METRIC\", \"displayName\": \"GC pause time\", \"data\": {\"currentValue\": 480, \"threshold\": 100, \"unit\": \"MILLISECOND\"}}, {\"evidenceType\": \"METRIC\", \"displayName\": \"Memory usage\", \"data\": {\"currentValue\": 94.1, \"threshold\": 90.0, \"unit\": \"PERCENT\"}}]}}], \"success\": true}","status":"completed","timestamp":"2026-02-26T14:33:00.000000","command":"query_dynatrace: problems"}]},
     {"sender":"bot","text":"Dynatrace confirms an active problem on api-gateway with a 14.2% error rate [1]. The root cause entity is identified as HOST-9A3F1B2C (api-gateway-prod-01) with three evidence markers: high error rate, elevated GC pause time (480ms vs 100ms threshold), and memory at 94%. Let me pull the host entity details and logs.","message_number":3,"isCompleted":true,"run_id":"lc_run--demo-dt-0002","toolCalls":[{"id":"tc_dt_002","tool_name":"query_dynatrace","input":"{\"resource_type\": \"entities\", \"query\": \"entityId(HOST-9A3F1B2C4D5E6F70)\"}","output":"{\"resource_type\": \"entities\", \"count\": 1, \"results\": [{\"entityId\": \"HOST-9A3F1B2C4D5E6F70\", \"displayName\": \"api-gateway-prod-01\", \"type\": \"HOST\", \"properties\": {\"memoryTotal\": 34359738368, \"memoryUsed\": 32290025472, \"jvmHeapMax\": 17179869184, \"jvmHeapUsed\": 15654042828, \"gcPauseTime\": 480, \"gcPauseTimeBaseline\": 20, \"cloudType\": \"AWS\", \"instanceType\": \"m5.2xlarge\"}}], \"success\": true}","status":"completed","timestamp":"2026-02-26T14:34:00.000000","command":"query_dynatrace: entities HOST-9A3F1B2C"},{"id":"tc_dt_003","tool_name":"query_dynatrace","input":"{\"resource_type\": \"logs\", \"query\": \"host:api-gateway-prod-01 AND (GC OR OutOfMemory OR heap)\", \"time_from\": \"2026-02-26T14:20:00Z\", \"time_to\": \"2026-02-26T14:35:00Z\", \"limit\": 10}","output":"{\"resource_type\": \"logs\", \"count\": 6, \"results\": [{\"timestamp\": \"2026-02-26T14:26:12.441Z\", \"content\": \"WARN  [gc-monitor] GC overhead limit approaching: 87% of time spent in GC\", \"loglevel\": \"WARN\"}, {\"timestamp\": \"2026-02-26T14:27:45.102Z\", \"content\": \"WARN  [gc-monitor] GC overhead limit approaching: 91% of time spent in GC, recovering < 5% heap\", \"loglevel\": \"WARN\"}, {\"timestamp\": \"2026-02-26T14:28:33.887Z\", \"content\": \"ERROR [gc-monitor] GC overhead limit exceeded: 94% of time spent in GC, recovering < 2% heap\", \"loglevel\": \"ERROR\"}, {\"timestamp\": \"2026-02-26T14:29:18.224Z\", \"content\": \"ERROR [http-handler] Request processing failed: java.lang.OutOfMemoryError: GC overhead limit exceeded\", \"loglevel\": \"ERROR\"}, {\"timestamp\": \"2026-02-26T14:31:02.556Z\", \"content\": \"FATAL [main] java.lang.OutOfMemoryError: Java heap space at com.gateway.cache.RequestCacheManager.put(RequestCacheManager.java:142)\", \"loglevel\": \"FATAL\"}, {\"timestamp\": \"2026-02-26T14:31:04.112Z\", \"content\": \"ERROR [health-check] Service health check failed: heap utilization 97%, response latency 8241ms\", \"loglevel\": \"ERROR\"}], \"success\": true}","status":"completed","timestamp":"2026-02-26T14:36:00.000000","command":"query_dynatrace: logs"}]},
     {"sender":"bot","text":"The host is an AWS m5.2xlarge with 32GB RAM [2]. JVM heap is at 91% (15.6GB of 17.2GB). GC pause time has spiked from a 20ms baseline to 480ms -- a 24x increase. The Dynatrace logs [3] tell the full story: GC overhead warnings started at 14:26, escalating from 87% to 94% of CPU time spent in garbage collection. By 14:31, a fatal OutOfMemoryError occurred in `RequestCacheManager.put()` -- this points to the request caching module as the memory leak source.\n\nNow let me investigate the network dimension. BigPanda correlated a ThousandEyes packet loss alert to payments.processor.com.","message_number":4,"isCompleted":true,"run_id":"lc_run--demo-dt-0003","toolCalls":[{"id":"tc_te_001","tool_name":"thousandeyes_list_tests","input":"{\"test_name_filter\": \"payment\"}","output":"{\"tests\": [{\"testId\": 28451, \"testName\": \"Payment Processor - HTTP Server\", \"type\": \"http-server\", \"url\": \"https://payments.processor.com/api/v2/health\", \"interval\": 120, \"enabled\": true, \"agents\": [{\"agentId\": 14201, \"agentName\": \"New York, NY\"}, {\"agentId\": 14202, \"agentName\": \"Washington, DC\"}, {\"agentId\": 14203, \"agentName\": \"Chicago, IL\"}, {\"agentId\": 14204, \"agentName\": \"Atlanta, GA\"}, {\"agentId\": 14205, \"agentName\": \"San Francisco, CA\"}, {\"agentId\": 14206, \"agentName\": \"Los Angeles, CA\"}]}, {\"testId\": 28452, \"testName\": \"Payment Processor - Network Path Trace\", \"type\": \"network\", \"server\": \"payments.processor.com\", \"port\": 443}, {\"testId\": 28453, \"testName\": \"Payment Processor - Page Load\", \"type\": \"page-load\", \"url\": \"https://payments.processor.com/status\"}], \"count\": 3}","status":"completed","timestamp":"2026-02-26T14:37:30.000000","command":"thousandeyes_list_tests: payment"}]},
     {"sender":"bot","text":"ThousandEyes has 3 tests monitoring payments.processor.com [5]. Let me pull the HTTP server test results and the network path trace to understand the packet loss.","message_number":5,"isCompleted":true,"run_id":"lc_run--demo-dt-0004","toolCalls":[{"id":"tc_te_002","tool_name":"thousandeyes_get_test_results","input":"{\"test_id\": 28451}","output":"{\"test\": {\"testId\": 28451, \"testName\": \"Payment Processor - HTTP Server\"}, \"results\": [{\"agentId\": 14201, \"agentName\": \"New York, NY\", \"responseTime\": 2412, \"loss\": 8.3, \"latency\": 124.5}, {\"agentId\": 14202, \"agentName\": \"Washington, DC\", \"responseTime\": 2198, \"loss\": 7.1, \"latency\": 118.2}, {\"agentId\": 14203, \"agentName\": \"Chicago, IL\", \"responseTime\": 1987, \"loss\": 6.8, \"latency\": 102.8}, {\"agentId\": 14204, \"agentName\": \"Atlanta, GA\", \"responseTime\": 2087, \"loss\": 7.5, \"latency\": 112.3}, {\"agentId\": 14205, \"agentName\": \"San Francisco, CA\", \"responseTime\": 134, \"loss\": 0.0, \"latency\": 18.4}, {\"agentId\": 14206, \"agentName\": \"Los Angeles, CA\", \"responseTime\": 128, \"loss\": 0.0, \"latency\": 16.2}], \"baseline\": {\"responseTime\": 120, \"loss\": 0.1}}","status":"completed","timestamp":"2026-02-26T14:39:30.000000","command":"thousandeyes_get_test_results: test 28451"},{"id":"tc_te_003","tool_name":"thousandeyes_get_test_results","input":"{\"test_id\": 28452}","output":"{\"test\": {\"testId\": 28452, \"testName\": \"Payment Processor - Network Path Trace\"}, \"results\": [{\"agentId\": 14201, \"agentName\": \"New York, NY\", \"pathTraces\": [{\"hop\": 1, \"ipAddress\": \"10.0.1.1\", \"rdns\": \"gw.internal.aurora.net\", \"loss\": 0.0}, {\"hop\": 2, \"ipAddress\": \"198.51.100.1\", \"rdns\": \"be-101.cr1.nyc1.aurora.net\", \"loss\": 0.0}, {\"hop\": 3, \"ipAddress\": \"198.51.100.5\", \"rdns\": \"ae-2.r01.nycmny01.us.bb.att.net\", \"loss\": 0.0}, {\"hop\": 4, \"ipAddress\": \"192.0.2.42\", \"rdns\": \"ae-12.r01.nycmny17.us.bb.gin.ntt.net\", \"responseTime\": 124, \"loss\": 8.3}, {\"hop\": 5, \"ipAddress\": \"203.0.113.10\", \"rdns\": \"payments-edge-01.processor.com\", \"loss\": 8.3}]}], \"summary\": {\"lossHop\": 4, \"lossNode\": \"ae-12.r01.nycmny17.us.bb.gin.ntt.net\", \"lossPercent\": 8.3}}","status":"completed","timestamp":"2026-02-26T14:40:30.000000","command":"thousandeyes_get_test_results: test 28452"}]},
     {"sender":"bot","text":"Clear geographic pattern in ThousandEyes [6]: all 4 East Coast agents (NYC, DC, Chicago, Atlanta) show response times of 1,987-2,412ms with 6.8-8.3% packet loss, while both West Coast agents (SF, LA) are healthy at 128-134ms with 0% loss. The baseline response time is 120ms.\n\nThe network path trace [7] pinpoints the loss at hop 4: `ae-12.r01.nycmny17.us.bb.gin.ntt.net` -- an NTT Communications peering point in New York. This is an ISP transit issue, not within our infrastructure or the payment processor. Let me check ThousandEyes alerts and agent status for confirmation.","message_number":6,"isCompleted":true,"run_id":"lc_run--demo-dt-0005","toolCalls":[{"id":"tc_te_004","tool_name":"thousandeyes_get_alerts","input":"{}","output":"{\"alerts\": [{\"alertId\": 98201, \"testName\": \"Payment Processor - HTTP Server\", \"ruleName\": \"HTTP Response Time > 2000ms\", \"severity\": \"MAJOR\", \"state\": \"ACTIVE\", \"startTime\": \"2026-02-26T14:30:00Z\", \"violationCount\": 4, \"agents\": [{\"agentName\": \"New York, NY\", \"active\": true}, {\"agentName\": \"Washington, DC\", \"active\": true}, {\"agentName\": \"Chicago, IL\", \"active\": true}, {\"agentName\": \"Atlanta, GA\", \"active\": true}]}, {\"alertId\": 98202, \"testName\": \"Payment Processor - Network Path Trace\", \"ruleName\": \"BGP Route Instability\", \"severity\": \"WARNING\", \"state\": \"ACTIVE\", \"startTime\": \"2026-02-26T14:28:00Z\", \"details\": {\"prefix\": \"203.0.113.0/24\", \"instabilityType\": \"path_change\", \"affectedPeers\": 3}}], \"count\": 2}","status":"completed","timestamp":"2026-02-26T14:41:30.000000","command":"thousandeyes_get_alerts"},{"id":"tc_te_005","tool_name":"thousandeyes_get_agents","input":"{\"test_id\": 28451}","output":"{\"agents\": [{\"agentId\": 14201, \"agentName\": \"New York, NY\", \"location\": \"New York, NY, US\", \"testResults\": {\"responseTime\": 2412, \"loss\": 8.3, \"status\": \"DEGRADED\"}}, {\"agentId\": 14202, \"agentName\": \"Washington, DC\", \"testResults\": {\"responseTime\": 2198, \"loss\": 7.1, \"status\": \"DEGRADED\"}}, {\"agentId\": 14203, \"agentName\": \"Chicago, IL\", \"testResults\": {\"responseTime\": 1987, \"loss\": 6.8, \"status\": \"DEGRADED\"}}, {\"agentId\": 14204, \"agentName\": \"Atlanta, GA\", \"testResults\": {\"responseTime\": 2087, \"loss\": 7.5, \"status\": \"DEGRADED\"}}, {\"agentId\": 14205, \"agentName\": \"San Francisco, CA\", \"testResults\": {\"responseTime\": 134, \"loss\": 0.0, \"status\": \"HEALTHY\"}}, {\"agentId\": 14206, \"agentName\": \"Los Angeles, CA\", \"testResults\": {\"responseTime\": 128, \"loss\": 0.0, \"status\": \"HEALTHY\"}}], \"summary\": {\"total\": 6, \"degraded\": 4, \"healthy\": 2, \"pattern\": \"East Coast agents degraded, West Coast agents healthy\"}}","status":"completed","timestamp":"2026-02-26T14:43:00.000000","command":"thousandeyes_get_agents"}]},
     {"sender":"bot","text":"ThousandEyes alerts confirm two active issues [8]: an HTTP response time alert with 4 violating East Coast agents, and a BGP route instability alert on prefix 203.0.113.0/24 (the payment processor network). The agent breakdown [9] shows 4 degraded / 2 healthy, confirming the East Coast pattern. Let me pull Dynatrace service-level metrics to quantify the combined impact.","message_number":7,"isCompleted":true,"run_id":"lc_run--demo-dt-0006","toolCalls":[{"id":"tc_dt_004","tool_name":"query_dynatrace","input":"{\"resource_type\": \"metrics\", \"query\": \"builtin:service.response.time:percentile(99):filter(eq(dt.entity.service,SERVICE-4B2E9F1A3C7D8E0F))\", \"time_from\": \"2026-02-26T14:20:00Z\", \"time_to\": \"2026-02-26T14:45:00Z\"}","output":"{\"resource_type\": \"metrics\", \"count\": 8, \"results\": [{\"timestamp\": \"2026-02-26T14:20:00Z\", \"value\": 245.3, \"unit\": \"MILLISECOND\"}, {\"timestamp\": \"2026-02-26T14:25:00Z\", \"value\": 312.7}, {\"timestamp\": \"2026-02-26T14:28:00Z\", \"value\": 1842.1}, {\"timestamp\": \"2026-02-26T14:30:00Z\", \"value\": 4215.8}, {\"timestamp\": \"2026-02-26T14:32:00Z\", \"value\": 8241.3}, {\"timestamp\": \"2026-02-26T14:35:00Z\", \"value\": 7832.6}, {\"timestamp\": \"2026-02-26T14:40:00Z\", \"value\": 3124.5}, {\"timestamp\": \"2026-02-26T14:45:00Z\", \"value\": 487.2}], \"success\": true, \"metric_key\": \"builtin:service.response.time\", \"aggregation\": \"P99\"}","status":"completed","timestamp":"2026-02-26T14:44:00.000000","command":"query_dynatrace: metrics response.time"}]},
     {"sender":"bot","text":"## Root Cause Analysis Complete\n\n**Two independent root causes identified**, correctly correlated by BigPanda based on timing overlap:\n\n### Root Cause 1 (Primary): Memory Leak on api-gateway-prod-01\nA memory leak in the `RequestCacheManager` module caused JVM heap exhaustion on api-gateway-prod-01 [2, 3]. GC overhead escalated from baseline 20ms to 480ms, with 94% of CPU time spent in garbage collection. This caused request processing failures (14.2% error rate) and p99 latency spiking from 245ms to 8,241ms [1, 4].\n\n### Root Cause 2 (Secondary): ISP Transit Packet Loss\nAn NTT Communications peering point (`ae-12.r01.nycmny17.us.bb.gin.ntt.net`) in New York is dropping 8.3% of packets on the path to payments.processor.com [6, 7]. This affects all East Coast agents (NYC, DC, Chicago, Atlanta) but NOT West Coast agents (SF, LA), confirming it is a transit provider issue [8, 9]. A BGP route instability alert on prefix 203.0.113.0/24 corroborates the network-level disruption [8].\n\n### Combined Impact\nDynatrace metrics show api-gateway p99 latency peaked at 8.2 seconds at 14:32 UTC [4]. The memory leak caused local request failures while the ISP packet loss caused payment-related API calls to timeout independently. Together, these produced the critical error rate spike that BigPanda correlated.\n\n**Remediation:**\n1. **Immediate**: Restart api-gateway-prod-01 to clear the memory leak and restore heap\n2. **Immediate**: Failover payment traffic to West Coast path to bypass ISP transit issue\n3. **Short-term**: Open a ticket with NTT Communications for the peering point packet loss\n4. **Long-term**: Investigate RequestCacheManager eviction policy, add JVM heap alerts at 80% threshold, configure ThousandEyes BGP alerting for proactive detection","message_number":8,"isCompleted":true,"run_id":"lc_run--demo-dt-0007","toolCalls":[]}
   ]')::jsonb,
   '2026-02-26 14:32:00', '2026-02-26 14:53:00', true, 'completed', v_incident_id);

  -- Fix tool call IDs for proper rendering
  UPDATE chat_sessions SET messages = (
    SELECT jsonb_agg(
      CASE
        WHEN msg->'toolCalls' IS NOT NULL AND jsonb_typeof(msg->'toolCalls') = 'array' AND jsonb_array_length(msg->'toolCalls') > 0 THEN
          jsonb_set(msg, '{toolCalls}', (
            SELECT jsonb_agg(updated_tc)
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

  RAISE NOTICE 'Demo incident (DT+BP+TE) chat session inserted.';
END $$;

-- Block 7: Summary update
DO $$
DECLARE
  v_incident_id UUID := 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';
BEGIN

  UPDATE incidents SET aurora_summary = 'On February 26, 2026 at 14:28 UTC, the **api-gateway** service in **prod-us-east** experienced a critical degradation with error rates reaching 14.2% and p99 latency spiking from 245ms to 8,241ms [1, 4]. BigPanda event correlation grouped three child alerts from Dynatrace and ThousandEyes into a single correlated incident, identifying simultaneous application-level failures and network path degradation affecting the payment processing path.

The primary root cause was a memory leak in the `RequestCacheManager` module on host api-gateway-prod-01, which drove JVM heap utilization to 97% and GC pause time from a 20ms baseline to 480ms [2, 3]. Dynatrace logs show GC overhead warnings beginning at 14:26, escalating to a fatal `OutOfMemoryError` at 14:31 in `RequestCacheManager.put()` [3]. Independently, ThousandEyes detected 8.3% packet loss on the East Coast ISP transit path to payments.processor.com, localized to an NTT Communications peering point (`ae-12.r01.nycmny17.us.bb.gin.ntt.net`) [6, 7]. West Coast agents (San Francisco, Los Angeles) showed zero packet loss, confirming this was a regional transit issue rather than a destination-side problem [9].

The combination of local GC pauses and remote payment path degradation produced the critical error rate spike. Dynatrace service metrics confirm p99 latency peaked at 14:32 UTC before gradually recovering [4]. ThousandEyes BGP monitoring also detected route instability on the payment processor prefix (203.0.113.0/24), corroborating the network-level disruption [8].

## Suggested Next Steps

- **Restart api-gateway-prod-01 and investigate RequestCacheManager**: The OutOfMemoryError stack trace at `RequestCacheManager.put()` indicates unbounded cache growth. Review the eviction policy and add a max-size bound [2, 3].
- **Failover payment traffic to West Coast path**: ThousandEyes confirms West Coast agents have zero packet loss and normal response times (128-134ms). Route payment API traffic through us-west-2 until the ISP transit issue resolves [6, 9].
- **Add JVM heap monitoring at 80% threshold**: The memory leak went undetected until 94% utilization. A Dynatrace custom alert at 80% heap usage would provide early warning [1, 2].
- **Open a ticket with NTT Communications**: The packet loss is concentrated at the NTT peering point in New York. Engage the ISP to investigate and resolve the transit degradation [7, 8].'
  WHERE id = v_incident_id;

  RAISE NOTICE 'Demo incident (DT+BP+TE) summary updated.';
END $$;

-- Block 8: Visualization
DO $$
DECLARE
  v_incident_id UUID := 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';
BEGIN

  UPDATE incidents SET visualization_code = '{
  "nodes": [
    {"id": "api-gateway", "label": "api-gateway", "type": "service", "status": "degraded", "parentId": null},
    {"id": "api-gw-host", "label": "api-gateway-prod-01", "type": "host", "status": "failed", "parentId": "api-gateway"},
    {"id": "memory-leak", "label": "RequestCacheManager OOM", "type": "event", "status": "failed", "parentId": "api-gw-host"},
    {"id": "payment-svc", "label": "payment-service", "type": "service", "status": "degraded", "parentId": null},
    {"id": "payment-proc", "label": "payments.processor.com", "type": "external", "status": "degraded", "parentId": null},
    {"id": "ntt-peering", "label": "NTT Peering (nycmny17)", "type": "network", "status": "failed", "parentId": null},
    {"id": "bigpanda", "label": "BigPanda Correlation", "type": "alert", "status": "degraded", "parentId": null},
    {"id": "restart", "label": "Restart api-gateway-prod-01", "type": "deployment", "status": "healthy", "parentId": null}
  ],
  "edges": [
    {"source": "memory-leak", "target": "api-gw-host", "label": "JVM heap 97%, GC 480ms", "type": "causation"},
    {"source": "api-gw-host", "target": "api-gateway", "label": "14.2% error rate", "type": "causation"},
    {"source": "ntt-peering", "target": "payment-proc", "label": "8.3% packet loss", "type": "causation"},
    {"source": "payment-proc", "target": "payment-svc", "label": "2400ms response time", "type": "causation"},
    {"source": "payment-svc", "target": "api-gateway", "label": "payment timeouts", "type": "causation"},
    {"source": "api-gateway", "target": "bigpanda", "label": "correlated alerts", "type": "causation"},
    {"source": "restart", "target": "memory-leak", "label": "clears heap", "type": "resolution"}
  ],
  "rootCauseId": "memory-leak",
  "affectedIds": ["api-gw-host", "api-gateway", "payment-svc", "payment-proc", "ntt-peering", "bigpanda"],
  "version": 5,
  "updatedAt": "2026-02-26T14:53:00.000000+00:00"
}'
  WHERE id = v_incident_id;

  RAISE NOTICE 'Demo incident (DT+BP+TE) visualization updated.';
END $$;

-- Block 9: Postmortem
INSERT INTO postmortems (incident_id, user_id, content, generated_at, updated_at)
VALUES (
  'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
  'ab209180-626b-4601-8042-9f6328d03ae9',
  $postmortem_dt$# Postmortem: API gateway error rate spike with payment path degradation - prod-us-east

**Date:** 2026-02-26 14:28 UTC
**Duration:** 25m
**Severity:** critical
**Service:** api-gateway
**Source:** bigpanda

## Summary
The api-gateway service in prod-us-east experienced a critical degradation caused by two independent failures that overlapped in time. A memory leak in the RequestCacheManager module exhausted JVM heap on api-gateway-prod-01, causing GC pauses up to 480ms and a 14.2% error rate. Simultaneously, an ISP transit issue at an NTT Communications peering point in New York caused 8.3% packet loss on the network path to the payment processor. BigPanda correctly correlated these alerts into a single incident.

## Timeline
- **14:26:12** - GC overhead warnings begin on api-gateway-prod-01 (87% of CPU time in GC)
- **14:27:45** - GC overhead escalates to 91%, recovering less than 5% heap per cycle
- **14:28:00** - Dynatrace auto-detects problem P-1842: error rate increase on api-gateway
- **14:28:33** - GC overhead limit exceeded: 94% of time in GC, less than 2% heap recovered
- **14:29:18** - First OutOfMemoryError in HTTP request handler
- **14:30:00** - ThousandEyes HTTP test to payments.processor.com shows response time spike to 2,400ms with 8.3% packet loss from East Coast agents
- **14:31:02** - Fatal OutOfMemoryError at RequestCacheManager.put() (line 142)
- **14:32:00** - BigPanda correlates 3 child alerts (Dynatrace error rate, ThousandEyes packet loss, Dynatrace host memory) into a single incident
- **14:33:30** - Aurora RCA investigation begins using Dynatrace and ThousandEyes
- **14:40:30** - ThousandEyes path trace isolates packet loss to NTT peering point ae-12.r01.nycmny17.us.bb.gin.ntt.net
- **14:43:00** - ThousandEyes agent analysis confirms East Coast degradation only; West Coast agents healthy
- **14:45:00** - Both root causes identified: memory leak (local) and ISP transit loss (external)
- **14:50:00** - api-gateway-prod-01 restarted; heap utilization drops to normal
- **14:53:00** - Service fully recovered; error rate below 1%, latency back to baseline

## Root Cause
Two independent failures combined to produce the critical incident:

**Primary - Memory Leak:** The RequestCacheManager module in api-gateway had an unbounded cache that grew without eviction under sustained request volume. JVM heap on api-gateway-prod-01 reached 97% (15.6GB of 17.2GB), causing GC pause time to spike from a 20ms baseline to 480ms. This resulted in request processing failures and p99 latency increasing from 245ms to 8,241ms.

**Secondary - ISP Transit Packet Loss:** An NTT Communications peering point (ae-12.r01.nycmny17.us.bb.gin.ntt.net) in New York began dropping 8.3% of packets on the path to payments.processor.com. This affected all East Coast ThousandEyes agents (New York, Washington DC, Chicago, Atlanta) but not West Coast agents (San Francisco, Los Angeles). BGP route instability on prefix 203.0.113.0/24 was also detected, suggesting a routing change triggered the packet loss.

## Impact
- **api-gateway**: 14.2% error rate, p99 latency peaked at 8,241ms (from 245ms baseline) for approximately 25 minutes
- **payment-service**: Payment API calls from East Coast clients experienced 2,400ms response times (from 120ms baseline) with 8.3% failure rate
- **Customer impact**: Users experienced slow page loads and intermittent payment failures during the incident window
- **Correlation**: BigPanda grouped 3 alerts from 2 monitoring systems into 1 incident, reducing alert noise and accelerating triage

## Resolution
The memory leak was resolved by restarting api-gateway-prod-01, which cleared the JVM heap and restored normal GC behavior. The ISP transit issue was mitigated by failover of payment traffic to a West Coast path. A ticket was opened with NTT Communications for the peering point packet loss.

## Action Items
- [ ] Investigate and fix the RequestCacheManager eviction policy to prevent unbounded cache growth
- [ ] Add JVM heap monitoring alert at 80% threshold in Dynatrace to catch memory leaks earlier
- [ ] Configure automatic payment traffic failover when ThousandEyes detects East Coast path degradation
- [ ] Add ThousandEyes BGP alerting for the payment processor prefix to detect route instability proactively
- [ ] Review api-gateway heap allocation (currently 16GB) and consider increasing or adding horizontal scaling

## Lessons Learned
- **BigPanda event correlation accelerated triage**: By grouping alerts from Dynatrace and ThousandEyes into a single incident, BigPanda eliminated the need to manually correlate 3 separate alerts from 2 different monitoring tools. This saved significant triage time and immediately surfaced the multi-dimensional nature of the failure.
- **Independent failures can compound**: The memory leak and ISP transit issue were unrelated but their timing overlap made the user experience significantly worse than either failure alone. Monitoring systems should surface compound impact, not just individual alert severity.
- **ThousandEyes geographic agent analysis is critical for ISP issues**: The ability to compare East Coast vs West Coast agent results immediately isolated the packet loss as a regional transit problem rather than a destination-side issue, avoiding a misdirected investigation into the payment processor infrastructure.
- **JVM heap monitoring needs lower thresholds**: The memory leak was not detected until 94% utilization. An 80% threshold would have provided 10-15 minutes of additional lead time for investigation before GC pauses began impacting users.$postmortem_dt$,
  '2026-02-26 14:58:00',
  '2026-02-26 14:58:00'
)
ON CONFLICT (incident_id) DO NOTHING;
