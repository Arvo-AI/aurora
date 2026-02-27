-- Demo Incident (Jenkins + Bitbucket): Connection pool saturation - checkout-service
-- Source: PagerDuty | Tools: jenkins_rca, bitbucket_repos, bitbucket_branches,
--         bitbucket_pull_requests, bitbucket_pipelines
-- Root cause: PR #89 reduced maxPoolSize from 50 to 8, deployed via Jenkins build #247

DO $$
DECLARE
  v_incident_id UUID := 'd2e3f4a5-b6c7-4d8e-9f0a-1b2c3d4e5f67';
  v_chat_session_id VARCHAR(50) := 'd2e3f4a6-c7d8-4e9f-a0b1-2c3d4e5f6a78';
  v_demo_user_id VARCHAR(255) := 'ab209180-626b-4601-8042-9f6328d03ae9';
  v_deploy_event_id INTEGER;
  v_incident_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM incidents WHERE id = v_incident_id) INTO v_incident_exists;
  IF v_incident_exists THEN
    RAISE NOTICE 'Demo incident (Jenkins+Bitbucket) already exists, skipping.';
    RETURN;
  END IF;

  RAISE NOTICE 'Inserting demo incident (Jenkins+Bitbucket)...';

  -- 1. Insert deployment event (successful deploy that introduced the bug)
  INSERT INTO jenkins_deployment_events (
    user_id, event_type, service, environment, result,
    build_number, build_url, commit_sha, branch, repository,
    deployer, duration_ms, job_name, trace_id, span_id,
    payload, received_at, created_at
  ) VALUES (
    v_demo_user_id, 'deployment', 'checkout-service', 'production', 'SUCCESS',
    247, 'https://jenkins.internal.acme.io/job/checkout-service-deploy/247/',
    'e4f8a21c3b9d', 'main', 'acme-team/checkout-service',
    'oliviertrudeau', 48200, 'checkout-service-deploy', NULL, NULL,
    '{"git": {"branch": "main", "commit_sha": "e4f8a21c3b9d"}, "result": "SUCCESS", "service": "checkout-service", "build_url": "https://jenkins.internal.acme.io/job/checkout-service-deploy/247/", "build_number": 247}',
    '2026-02-25T09:12:45.000000', '2026-02-25T09:12:45.000000'
  ) RETURNING id INTO v_deploy_event_id;

  -- 2. Insert main incident record
  INSERT INTO incidents (
    id, user_id, source_type, source_alert_id, status, severity,
    alert_title, alert_service, alert_environment, aurora_status,
    aurora_summary, aurora_chat_session_id, started_at, analyzed_at,
    active_tab, created_at, updated_at, alert_metadata,
    correlated_alert_count, affected_services, visualization_code,
    visualization_updated_at
  ) VALUES (
    v_incident_id, v_demo_user_id, 'pagerduty', v_deploy_event_id,
    'resolved', 'critical',
    'Connection pool saturation causing transaction timeouts - checkout-service on payments-cluster',
    'payments-cluster', 'production', 'complete',
    '', v_chat_session_id::uuid,
    '2026-02-25 09:18:33', '2026-02-25 09:32:00',
    'thoughts', '2026-02-25 09:18:33', '2026-02-25 09:32:00',
    jsonb_build_object(
      'is_demo', true,
      'urgency', 'high',
      'priority', 'P1',
      'incidentId', 'Q3RTXYP8MN2V51',
      'escalation_policy', 'Payments On-Call',
      'service_name', 'checkout-service',
      'raw_payload', '{"incident":{"id":"Q3RTXYP8MN2V51","type":"incident","title":"Connection pool saturation causing transaction timeouts - checkout-service on payments-cluster","status":"triggered","urgency":"high","service":{"id":"PSVC001","summary":"checkout-service"},"escalation_policy":{"id":"PESC001","summary":"Payments On-Call"},"teams":[{"id":"PTEAM01","summary":"Payments"}],"priority":{"id":"PPRI01","summary":"P1"},"created_at":"2026-02-25T09:18:33Z","updated_at":"2026-02-25T09:18:33Z"}}'
    ),
    2,
    ARRAY['checkout-service', 'api-gateway', 'postgres-payments'],
    '', '2026-02-25 09:32:00'
  );

  -- 3. Insert correlated alerts
  INSERT INTO incident_alerts (user_id, incident_id, source_type, source_alert_id, alert_title, alert_service, alert_severity, correlation_strategy, correlation_score, correlation_details, alert_metadata, received_at) VALUES
  (v_demo_user_id, v_incident_id, 'pagerduty', v_deploy_event_id, 'Transaction timeout rate exceeded 15% - checkout-service on payments-cluster', 'checkout-service', 'critical', 'primary', 1.0, '{}', '{"tags":["env:production","service:checkout-service","cluster:payments-cluster"]}'::jsonb, '2026-02-25 09:18:33'),
  (v_demo_user_id, v_incident_id, 'pagerduty', v_deploy_event_id + 10000, 'API gateway upstream latency p99 > 5s - payments endpoint', 'api-gateway', 'high', 'time_proximity', 0.92, '{"reason":"API gateway latency spike correlates with checkout-service timeout surge, same 2-minute window"}'::jsonb, '{"tags":["env:production","service:api-gateway","endpoint:/v1/payments"]}'::jsonb, '2026-02-25 09:19:10'),
  (v_demo_user_id, v_incident_id, 'pagerduty', v_deploy_event_id + 10001, 'PostgreSQL active connections at 100% capacity - payments-db', 'postgres-payments', 'warning', 'topology', 0.88, '{"reason":"payments-db connection saturation started within 30s of checkout-service pool exhaustion"}'::jsonb, '{"tags":["env:production","service:postgres-payments","pool:checkout-service"]}'::jsonb, '2026-02-25 09:19:45');

  RAISE NOTICE 'Demo incident (Jenkins+Bitbucket) core record inserted.';
END $$;

-- Thoughts, citations, suggestions
DO $$
DECLARE
  v_incident_id UUID := 'd2e3f4a5-b6c7-4d8e-9f0a-1b2c3d4e5f67';
  v_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM incident_thoughts WHERE incident_id = v_incident_id) INTO v_exists;
  IF v_exists THEN
    RAISE NOTICE 'Demo incident (Jenkins+Bitbucket) thoughts already exist, skipping.';
    RETURN;
  END IF;

  INSERT INTO incident_thoughts (incident_id, timestamp, content, thought_type) VALUES
  (v_incident_id, '2026-02-25 09:18:40', 'PagerDuty alert triggered for checkout-service on payments-cluster. Transaction timeout rate exceeded 15%. Checking Jenkins for recent deployments that may correlate.', 'analysis'),
  (v_incident_id, '2026-02-25 09:19:00', 'Jenkins shows checkout-service-deploy build #247 completed successfully 6 minutes before the alert. Timing is suspicious. Investigating build details.', 'analysis'),
  (v_incident_id, '2026-02-25 09:19:30', 'Build #247 was triggered by a webhook from Bitbucket after merging PR #89 by oliviertrudeau. Commit e4f8a21c deployed to production. Need to check what this commit changed.', 'analysis'),
  (v_incident_id, '2026-02-25 09:20:00', 'Jenkins pipeline stages show all 5 stages passed: Checkout, Build, Test, Deploy, Verify. The deployment itself succeeded but the issue manifested afterward under load.', 'analysis'),
  (v_incident_id, '2026-02-25 09:21:00', 'Build logs confirm the Docker image checkout-service:e4f8a21c was pushed and rolled out via kubectl. The deploy completed at 09:12:45, alert fired at 09:18:33 -- 6 minute lag.', 'analysis'),
  (v_incident_id, '2026-02-25 09:22:00', 'Checking Bitbucket commit history on main branch. Commit e4f8a21c is the merge commit for PR #89 titled "Optimize DB connections for cost savings".', 'analysis'),
  (v_incident_id, '2026-02-25 09:23:00', 'PR #89 was authored by oliviertrudeau and merged with 0 reviewer comments. The description mentions reducing connection pool size to lower RDS costs.', 'analysis'),
  (v_incident_id, '2026-02-25 09:24:00', 'PR diff shows maxPoolSize changed from 50 to 8 in src/config/db.ts. This is a 84% reduction in available connections. Under production load this would saturate immediately.', 'analysis'),
  (v_incident_id, '2026-02-25 09:25:00', 'Confirmed: the pool size reduction is the root cause. checkout-service handles ~400 req/s during peak. With 8 connections and avg query time of 25ms, max throughput is only 320 req/s before pool starvation.', 'analysis'),
  (v_incident_id, '2026-02-25 09:26:00', 'Current file at HEAD shows maxPoolSize: 8, connectionTimeoutMillis: 30000. The 30s timeout means requests queue for up to 30 seconds before failing, explaining the PagerDuty timeout alerts.', 'analysis'),
  (v_incident_id, '2026-02-25 09:27:00', 'Checking Bitbucket pipelines for the deploy pipeline run that corresponds to this build. Pipeline #412 ran the full CI/CD cycle and passed all checks.', 'analysis'),
  (v_incident_id, '2026-02-25 09:28:00', 'Pipeline step logs show unit tests passed but there are no load tests in the pipeline. The pool reduction was never tested under realistic traffic.', 'analysis'),
  (v_incident_id, '2026-02-25 09:29:00', 'Searching Bitbucket for other services with similar pool configuration. Found 2 other services using the same db.ts pattern -- risk of same issue if similar PRs are merged.', 'analysis'),
  (v_incident_id, '2026-02-25 09:30:00', 'Cascading impact confirmed: api-gateway reports p99 latency > 5s on /v1/payments endpoint. postgres-payments shows all connection slots consumed. Both are downstream effects of the pool starvation.', 'analysis'),
  (v_incident_id, '2026-02-25 09:31:00', 'Immediate remediation: revert the pool size to 50 connections via config patch and trigger a redeployment. Long-term: add pool sizing guardrails and load testing to CI/CD.', 'analysis'),
  (v_incident_id, '2026-02-25 09:32:00', 'Investigation complete. Root cause is PR #89 reducing maxPoolSize from 50 to 8. Fix prepared: restore pool size + add connection pool monitoring.', 'analysis');

  RAISE NOTICE 'Demo incident (Jenkins+Bitbucket) thoughts inserted.';

  -- Citations
  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '1', 'Jenkins', 'jenkins_rca: recent_deployments for checkout-service', '{
  "deployments": [
    {
      "id": 42,
      "service": "checkout-service",
      "environment": "production",
      "result": "SUCCESS",
      "build_number": 247,
      "build_url": "https://jenkins.internal.acme.io/job/checkout-service-deploy/247/",
      "commit_sha": "e4f8a21c3b9d",
      "branch": "main",
      "repository": "acme-team/checkout-service",
      "deployer": "oliviertrudeau",
      "duration_ms": 48200,
      "job_name": "checkout-service-deploy",
      "received_at": "2026-02-25T09:12:45.000000"
    },
    {
      "id": 38,
      "service": "checkout-service",
      "environment": "production",
      "result": "SUCCESS",
      "build_number": 246,
      "build_url": "https://jenkins.internal.acme.io/job/checkout-service-deploy/246/",
      "commit_sha": "b7c2d9e1f4a3",
      "branch": "main",
      "deployer": "mike.rodriguez",
      "received_at": "2026-02-24T15:22:10.000000"
    }
  ],
  "count": 2
}', '2026-02-25 09:18:40');

  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '2', 'Jenkins', 'jenkins_rca: build_detail for checkout-service-deploy #247', '{
  "result": "SUCCESS",
  "timestamp": 1772046765000,
  "duration": 48200,
  "building": false,
  "displayName": "#247",
  "changeSets": [
    {
      "kind": "git",
      "items": [
        {
          "commitId": "e4f8a21c3b9d",
          "author": {"fullName": "Olivier Trudeau"},
          "msg": "Merge pull request #89 from feature/optimize-db-connections",
          "timestamp": "2026-02-25T09:10:22+00:00"
        }
      ]
    }
  ],
  "causes": [
    {
      "_class": "com.atlassian.bitbucket.webhooks.PushCause",
      "shortDescription": "Started by Bitbucket push by oliviertrudeau"
    }
  ]
}', '2026-02-25 09:19:00');

  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '3', 'Jenkins', 'jenkins_rca: pipeline_stages for checkout-service-deploy #247', '{
  "id": "247",
  "name": "#247",
  "status": "SUCCESS",
  "durationMillis": 48200,
  "stages": [
    {"id": "10", "name": "Checkout", "status": "SUCCESS", "durationMillis": 3200},
    {"id": "20", "name": "Build & Push Image", "status": "SUCCESS", "durationMillis": 18400},
    {"id": "30", "name": "Run Tests", "status": "SUCCESS", "durationMillis": 12100},
    {"id": "40", "name": "Deploy to Production", "status": "SUCCESS", "durationMillis": 8700},
    {"id": "50", "name": "Post-Deploy Verify", "status": "SUCCESS", "durationMillis": 5800}
  ]
}', '2026-02-25 09:19:30');

  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '4', 'Jenkins', 'jenkins_rca: build_logs for checkout-service-deploy #247', '{
  "console": "Started by Bitbucket push by oliviertrudeau\n[Pipeline] Start of Pipeline\n[Pipeline] stage (Checkout)\n+ git checkout main\nAlready on ''main''\n+ git pull origin main\nFrom bitbucket.org:acme-team/checkout-service\n   b7c2d9e..e4f8a21  main -> origin/main\nHEAD is now at e4f8a21c Merge pull request #89 from feature/optimize-db-connections\n[Pipeline] stage (Build & Push Image)\n+ docker build -t registry.acme.io/checkout-service:e4f8a21c .\nSuccessfully built 9a3f2b1c\nSuccessfully tagged registry.acme.io/checkout-service:e4f8a21c\n+ docker push registry.acme.io/checkout-service:e4f8a21c\nPushed: registry.acme.io/checkout-service:e4f8a21c\n[Pipeline] stage (Run Tests)\n+ npm test\n\n> checkout-service@2.14.0 test\n> jest --coverage\n\nPASS src/config/__tests__/db.test.ts\nPASS src/handlers/__tests__/checkout.test.ts\nPASS src/handlers/__tests__/payment.test.ts\nTest Suites: 3 passed, 3 total\nTests: 47 passed, 47 total\nCoverage: 82.4%\n[Pipeline] stage (Deploy to Production)\n+ kubectl set image deployment/checkout-service checkout-service=registry.acme.io/checkout-service:e4f8a21c -n payments\ndeployment.apps/checkout-service image updated\n+ kubectl rollout status deployment/checkout-service -n payments --timeout=120s\ndeployment ''checkout-service'' successfully rolled out\n[Pipeline] stage (Post-Deploy Verify)\n+ curl -sf http://checkout-service.payments.svc:3000/health\n{\"status\":\"ok\",\"version\":\"2.14.0-e4f8a21c\"}\nFinished: SUCCESS"
}', '2026-02-25 09:21:00');

  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '5', 'Bitbucket', 'bitbucket: list_commits on main branch', '{
  "success": true,
  "commits": [
    {"hash": "e4f8a21c3b9d", "message": "Merge pull request #89 from feature/optimize-db-connections\n\nOptimize DB connections for cost savings", "author": "Olivier Trudeau <oliviertrudeau@acme.io>", "date": "2026-02-25T09:10:22+00:00"},
    {"hash": "b7c2d9e1f4a3", "message": "fix: handle null response in payment callback", "author": "Mike Rodriguez <mike.rodriguez@acme.io>", "date": "2026-02-24T15:20:44+00:00"},
    {"hash": "91a3c5d7e8f2", "message": "chore: bump stripe-sdk to 14.2.0", "author": "Mike Rodriguez <mike.rodriguez@acme.io>", "date": "2026-02-24T11:05:33+00:00"}
  ],
  "count": 3,
  "branch": "main"
}', '2026-02-25 09:22:00');

  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '6', 'Bitbucket', 'bitbucket: list_prs (MERGED) for checkout-service', '{
  "success": true,
  "pull_requests": [
    {"id": 89, "title": "Optimize DB connections for cost savings", "state": "MERGED", "author": "oliviertrudeau", "source_branch": "feature/optimize-db-connections", "dest_branch": "main", "created_on": "2026-02-25T08:45:00+00:00", "updated_on": "2026-02-25T09:10:22+00:00", "comment_count": 0},
    {"id": 87, "title": "fix: handle null response in payment callback", "state": "MERGED", "author": "mike.rodriguez", "source_branch": "fix/payment-callback-null", "dest_branch": "main", "created_on": "2026-02-24T14:30:00+00:00", "updated_on": "2026-02-24T15:20:44+00:00", "comment_count": 2}
  ],
  "count": 2,
  "state": "MERGED"
}', '2026-02-25 09:23:00');

  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '7', 'Bitbucket', 'bitbucket: get_pr_diff for PR #89', '{
  "success": true,
  "diff": "diff --git a/src/config/db.ts b/src/config/db.ts\nindex 4a2c8f1..e9b3d72 100644\n--- a/src/config/db.ts\n+++ b/src/config/db.ts\n@@ -12,7 +12,7 @@ export const poolConfig: PoolConfig = {\n   host: process.env.DB_HOST || ''localhost'',\n   port: parseInt(process.env.DB_PORT || ''5432''),\n   database: process.env.DB_NAME || ''payments'',\n-  max: parseInt(process.env.MAX_POOL_SIZE || ''50''),\n+  max: parseInt(process.env.MAX_POOL_SIZE || ''8''),\n   connectionTimeoutMillis: 30000,\n   idleTimeoutMillis: 10000,\n   allowExitOnIdle: false,\n",\n  "pr_id": 89
}', '2026-02-25 09:24:00');

  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '8', 'Bitbucket', 'bitbucket: get_file_contents src/config/db.ts', '{
  "content": "import { PoolConfig } from ''pg'';\nimport { logger } from ''../utils/logger'';\n\nexport const poolConfig: PoolConfig = {\n  host: process.env.DB_HOST || ''localhost'',\n  port: parseInt(process.env.DB_PORT || ''5432''),\n  database: process.env.DB_NAME || ''payments'',\n  user: process.env.DB_USER || ''checkout_svc'',\n  password: process.env.DB_PASSWORD,\n  max: parseInt(process.env.MAX_POOL_SIZE || ''8''),\n  connectionTimeoutMillis: 30000,\n  idleTimeoutMillis: 10000,\n  allowExitOnIdle: false,\n};\n\nlogger.info(''DB pool configured'', { max: poolConfig.max });",\n  "path": "src/config/db.ts",\n  "commit": "e4f8a21c3b9d"
}', '2026-02-25 09:25:00');

  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '9', 'Bitbucket', 'bitbucket: search_code "maxPoolSize OR max:" in checkout-service', '{
  "success": true,
  "results": [
    {"file": "src/config/db.ts", "lines": [10], "content": "  max: parseInt(process.env.MAX_POOL_SIZE || ''8''),"},
    {"file": "src/config/db.test.ts", "lines": [15], "content": "  expect(poolConfig.max).toBe(8);"}
  ]
}', '2026-02-25 09:29:00');

  INSERT INTO incident_citations (incident_id, citation_key, tool_name, command, output, executed_at) VALUES
  (v_incident_id, '10', 'Bitbucket', 'bitbucket: get_step_log for pipeline #412 deploy step', '{
  "success": true,
  "log": "Step: Deploy to production\n+ kubectl set image deployment/checkout-service checkout-service=registry.acme.io/checkout-service:e4f8a21c -n payments\ndeployment.apps/checkout-service image updated\n+ kubectl rollout status deployment/checkout-service -n payments --timeout=120s\nWaiting for deployment ''checkout-service'' rollout to finish: 1 of 3 updated replicas are available...\nWaiting for deployment ''checkout-service'' rollout to finish: 2 of 3 updated replicas are available...\ndeployment ''checkout-service'' successfully rolled out",
  "pipeline_uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "step_uuid": "deploy-step-001"
}', '2026-02-25 09:27:00');

  RAISE NOTICE 'Demo incident (Jenkins+Bitbucket) citations inserted.';

  -- Suggestions
  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id,
   'Rollback checkout-service to previous build #246',
   'Immediately revert to the last known-good deployment (build #246, commit b7c2d9e1) which had maxPoolSize=50. This restores connection capacity and stops the transaction timeouts.',
   'mitigation', 'low',
   'kubectl set image deployment/checkout-service checkout-service=registry.acme.io/checkout-service:b7c2d9e1 -n payments && kubectl rollout status deployment/checkout-service -n payments --timeout=120s',
   '2026-02-25 09:30:00');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id,
   'Verify checkout-service and api-gateway recovery after rollback',
   'After rolling back, confirm that transaction timeout rate drops below 1% and api-gateway p99 latency returns to baseline (<500ms).',
   'diagnostic', 'safe',
   'kubectl top pods -l app=checkout-service -n payments && kubectl logs -l app=checkout-service -n payments --tail=20 --since=5m | grep -c "pool exhausted"',
   '2026-02-25 09:31:00');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id,
   'Restore connection pool size to 50 in db.ts',
   'PR #89 reduced maxPoolSize from 50 to 8 without load testing. Restore the original value of 50 which was stable under production load of ~400 req/s. Update the env var MAX_POOL_SIZE or revert the code change.',
   'mitigation', 'low',
   'kubectl set env deployment/checkout-service MAX_POOL_SIZE=50 -n payments && kubectl rollout restart deployment/checkout-service -n payments',
   '2026-02-25 09:31:30');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id,
   'Add connection pool health monitoring',
   'Expose pool.totalCount, pool.idleCount, and pool.waitingCount as Prometheus metrics. Alert when pool utilization exceeds 80% to detect exhaustion before it causes timeouts.',
   'mitigation', 'safe',
   NULL,
   '2026-02-25 09:32:00');

  INSERT INTO incident_suggestions (incident_id, title, description, type, risk, command, created_at) VALUES
  (v_incident_id,
   'Add load testing stage to Jenkins deployment pipeline',
   'The current pipeline runs unit tests but no load tests. Add a stage that simulates production traffic levels before deploying to production to catch pool sizing regressions.',
   'mitigation', 'safe', NULL,
   '2026-02-25 09:32:00');

  RAISE NOTICE 'Demo incident (Jenkins+Bitbucket) suggestions inserted.';
END $$;

-- Chat session
DO $$
DECLARE
  v_incident_id UUID := 'd2e3f4a5-b6c7-4d8e-9f0a-1b2c3d4e5f67';
  v_chat_session_id VARCHAR(50) := 'd2e3f4a6-c7d8-4e9f-a0b1-2c3d4e5f6a78';
  v_demo_user_id VARCHAR(255) := 'ab209180-626b-4601-8042-9f6328d03ae9';
  v_exists BOOLEAN;
BEGIN
  SELECT EXISTS(SELECT 1 FROM chat_sessions WHERE id = v_chat_session_id) INTO v_exists;
  IF v_exists THEN
    RAISE NOTICE 'Demo incident (Jenkins+Bitbucket) chat session already exists, skipping.';
    RETURN;
  END IF;

  INSERT INTO chat_sessions (id, user_id, title, messages, created_at, updated_at, is_active, status, incident_id) VALUES
  (v_chat_session_id, v_demo_user_id,
   'RCA: Connection pool saturation - checkout-service',
   ('[
     {"sender":"user","text":"# ROOT CAUSE ANALYSIS REQUIRED - PAGERDUTY ALERT\n\n## ALERT DETAILS:\n- **Title**: Connection pool saturation causing transaction timeouts - checkout-service on payments-cluster\n- **Status**: Triggered\n- **Source**: PagerDuty\n- **Priority**: P1\n- **Urgency**: High\n- **Service**: checkout-service\n- **Cluster**: payments-cluster\n- **Environment**: production\n\n## CONNECTED PROVIDERS:\nYou have access to: jenkins, bitbucket\n\n## BITBUCKET REPOSITORY CONTEXT:\n- **Connected Repository**: acme-team/checkout-service\n- **Branch**: main\n\n## JENKINS CI/CD INTEGRATION:\nJenkins CI is connected. Use the `jenkins_rca` tool to investigate CI/CD activity.\n\n### INVESTIGATION INSTRUCTIONS:\nConduct a thorough Root Cause Analysis using Jenkins and Bitbucket to correlate recent deployments with the incident.","message_number":1,"isCompleted":true},
     {"sender":"bot","text":"","message_number":2,"isCompleted":true,"run_id":"lc_run--demo-jb-0001","toolCalls":[{"id":"tc_jb_001","tool_name":"jenkins_rca","input":"{\"action\": \"recent_deployments\", \"service\": \"checkout-service\", \"time_window_hours\": 4}","output":"{\"deployments\": [{\"id\": 42, \"service\": \"checkout-service\", \"environment\": \"production\", \"result\": \"SUCCESS\", \"build_number\": 247, \"build_url\": \"https://jenkins.internal.acme.io/job/checkout-service-deploy/247/\", \"commit_sha\": \"e4f8a21c3b9d\", \"branch\": \"main\", \"repository\": \"acme-team/checkout-service\", \"deployer\": \"oliviertrudeau\", \"duration_ms\": 48200, \"job_name\": \"checkout-service-deploy\", \"received_at\": \"2026-02-25T09:12:45.000000\"}, {\"id\": 38, \"service\": \"checkout-service\", \"environment\": \"production\", \"result\": \"SUCCESS\", \"build_number\": 246, \"build_url\": \"https://jenkins.internal.acme.io/job/checkout-service-deploy/246/\", \"commit_sha\": \"b7c2d9e1f4a3\", \"branch\": \"main\", \"deployer\": \"mike.rodriguez\", \"received_at\": \"2026-02-24T15:22:10.000000\"}], \"count\": 2}","status":"completed","timestamp":"2026-02-25T09:18:40.000000","command":"jenkins_rca: recent_deployments"}]},
     {"sender":"bot","text":"Jenkins shows two recent deployments for checkout-service. Build #247 completed successfully just 6 minutes before the PagerDuty alert fired [1]. This tight correlation between deploy and incident onset is a strong signal. Let me examine the build details and pipeline stages to understand what was deployed.","message_number":3,"isCompleted":true,"run_id":"lc_run--demo-jb-0002","toolCalls":[{"id":"tc_jb_002","tool_name":"jenkins_rca","input":"{\"action\": \"build_detail\", \"job_path\": \"checkout-service-deploy\", \"build_number\": 247}","output":"{\"result\": \"SUCCESS\", \"timestamp\": 1772046765000, \"duration\": 48200, \"building\": false, \"displayName\": \"#247\", \"changeSets\": [{\"kind\": \"git\", \"items\": [{\"commitId\": \"e4f8a21c3b9d\", \"author\": {\"fullName\": \"Olivier Trudeau\"}, \"msg\": \"Merge pull request #89 from feature/optimize-db-connections\", \"timestamp\": \"2026-02-25T09:10:22+00:00\"}]}], \"causes\": [{\"_class\": \"com.atlassian.bitbucket.webhooks.PushCause\", \"shortDescription\": \"Started by Bitbucket push by oliviertrudeau\"}]}","status":"completed","timestamp":"2026-02-25T09:19:00.000000","command":"jenkins_rca: build_detail #247"},{"id":"tc_jb_003","tool_name":"jenkins_rca","input":"{\"action\": \"pipeline_stages\", \"job_path\": \"checkout-service-deploy\", \"build_number\": 247}","output":"{\"id\": \"247\", \"name\": \"#247\", \"status\": \"SUCCESS\", \"durationMillis\": 48200, \"stages\": [{\"id\": \"10\", \"name\": \"Checkout\", \"status\": \"SUCCESS\", \"durationMillis\": 3200}, {\"id\": \"20\", \"name\": \"Build & Push Image\", \"status\": \"SUCCESS\", \"durationMillis\": 18400}, {\"id\": \"30\", \"name\": \"Run Tests\", \"status\": \"SUCCESS\", \"durationMillis\": 12100}, {\"id\": \"40\", \"name\": \"Deploy to Production\", \"status\": \"SUCCESS\", \"durationMillis\": 8700}, {\"id\": \"50\", \"name\": \"Post-Deploy Verify\", \"status\": \"SUCCESS\", \"durationMillis\": 5800}]}","status":"completed","timestamp":"2026-02-25T09:19:30.000000","command":"jenkins_rca: pipeline_stages #247"}]},
     {"sender":"bot","text":"Build #247 was triggered by a Bitbucket push from oliviertrudeau, deploying commit `e4f8a21c3b9d` [2]. The commit message is **\"Merge pull request #89 from feature/optimize-db-connections\"** -- this is about database connection changes, which directly relates to our pool saturation alert. All 5 pipeline stages passed [3], so the deployment itself was healthy. Let me check the build logs for details and trace the commit in Bitbucket.","message_number":4,"isCompleted":true,"run_id":"lc_run--demo-jb-0003","toolCalls":[{"id":"tc_jb_004","tool_name":"jenkins_rca","input":"{\"action\": \"build_logs\", \"job_path\": \"checkout-service-deploy\", \"build_number\": 247}","output":"{\"console\": \"Started by Bitbucket push by oliviertrudeau\\n[Pipeline] Start of Pipeline\\n[Pipeline] stage (Checkout)\\n+ git checkout main\\nAlready on ''main''\\n+ git pull origin main\\nFrom bitbucket.org:acme-team/checkout-service\\n   b7c2d9e..e4f8a21  main -> origin/main\\nHEAD is now at e4f8a21c Merge pull request #89 from feature/optimize-db-connections\\n[Pipeline] stage (Build & Push Image)\\n+ docker build -t registry.acme.io/checkout-service:e4f8a21c .\\nSuccessfully built 9a3f2b1c\\nSuccessfully tagged registry.acme.io/checkout-service:e4f8a21c\\n+ docker push registry.acme.io/checkout-service:e4f8a21c\\nPushed: registry.acme.io/checkout-service:e4f8a21c\\n[Pipeline] stage (Run Tests)\\n+ npm test\\n\\n> checkout-service@2.14.0 test\\n> jest --coverage\\n\\nPASS src/config/__tests__/db.test.ts\\nPASS src/handlers/__tests__/checkout.test.ts\\nPASS src/handlers/__tests__/payment.test.ts\\nTest Suites: 3 passed, 3 total\\nTests: 47 passed, 47 total\\nCoverage: 82.4%\\n[Pipeline] stage (Deploy to Production)\\n+ kubectl set image deployment/checkout-service checkout-service=registry.acme.io/checkout-service:e4f8a21c -n payments\\ndeployment.apps/checkout-service image updated\\n+ kubectl rollout status deployment/checkout-service -n payments --timeout=120s\\ndeployment ''checkout-service'' successfully rolled out\\n[Pipeline] stage (Post-Deploy Verify)\\n+ curl -sf http://checkout-service.payments.svc:3000/health\\n{\\\"status\\\":\\\"ok\\\",\\\"version\\\":\\\"2.14.0-e4f8a21c\\\"}\\nFinished: SUCCESS\"}","status":"completed","timestamp":"2026-02-25T09:21:00.000000","command":"jenkins_rca: build_logs #247"},{"id":"tc_jb_005","tool_name":"bitbucket_branches","input":"{\"action\": \"list_commits\", \"branch\": \"main\"}","output":"{\"success\": true, \"commits\": [{\"hash\": \"e4f8a21c3b9d\", \"message\": \"Merge pull request #89 from feature/optimize-db-connections\\n\\nOptimize DB connections for cost savings\", \"author\": \"Olivier Trudeau <oliviertrudeau@acme.io>\", \"date\": \"2026-02-25T09:10:22+00:00\"}, {\"hash\": \"b7c2d9e1f4a3\", \"message\": \"fix: handle null response in payment callback\", \"author\": \"Mike Rodriguez <mike.rodriguez@acme.io>\", \"date\": \"2026-02-24T15:20:44+00:00\"}, {\"hash\": \"91a3c5d7e8f2\", \"message\": \"chore: bump stripe-sdk to 14.2.0\", \"author\": \"Mike Rodriguez <mike.rodriguez@acme.io>\", \"date\": \"2026-02-24T11:05:33+00:00\"}], \"count\": 3, \"branch\": \"main\"}","status":"completed","timestamp":"2026-02-25T09:22:00.000000","command":"bitbucket: list_commits"}]},
     {"sender":"bot","text":"The build logs confirm Docker image `checkout-service:e4f8a21c` was deployed via kubectl rollout [4]. Bitbucket commit history shows commit `e4f8a21c3b9d` is the merge of PR #89: **\"Optimize DB connections for cost savings\"** [5]. This is almost certainly the change that caused the pool saturation. Let me pull up the PR details and the actual code diff.","message_number":5,"isCompleted":true,"run_id":"lc_run--demo-jb-0004","toolCalls":[{"id":"tc_jb_006","tool_name":"bitbucket_pull_requests","input":"{\"action\": \"list_prs\", \"state\": \"MERGED\"}","output":"{\"success\": true, \"pull_requests\": [{\"id\": 89, \"title\": \"Optimize DB connections for cost savings\", \"state\": \"MERGED\", \"author\": \"oliviertrudeau\", \"source_branch\": \"feature/optimize-db-connections\", \"dest_branch\": \"main\", \"created_on\": \"2026-02-25T08:45:00+00:00\", \"updated_on\": \"2026-02-25T09:10:22+00:00\", \"comment_count\": 0}, {\"id\": 87, \"title\": \"fix: handle null response in payment callback\", \"state\": \"MERGED\", \"author\": \"mike.rodriguez\", \"source_branch\": \"fix/payment-callback-null\", \"dest_branch\": \"main\", \"created_on\": \"2026-02-24T14:30:00+00:00\", \"updated_on\": \"2026-02-24T15:20:44+00:00\", \"comment_count\": 2}], \"count\": 2, \"state\": \"MERGED\"}","status":"completed","timestamp":"2026-02-25T09:23:00.000000","command":"bitbucket: list_prs (MERGED)"},{"id":"tc_jb_007","tool_name":"bitbucket_pull_requests","input":"{\"action\": \"get_pr_diff\", \"pr_id\": 89}","output":"{\"success\": true, \"diff\": \"diff --git a/src/config/db.ts b/src/config/db.ts\\nindex 4a2c8f1..e9b3d72 100644\\n--- a/src/config/db.ts\\n+++ b/src/config/db.ts\\n@@ -12,7 +12,7 @@ export const poolConfig: PoolConfig = {\\n   host: process.env.DB_HOST || ''localhost'',\\n   port: parseInt(process.env.DB_PORT || ''5432''),\\n   database: process.env.DB_NAME || ''payments'',\\n-  max: parseInt(process.env.MAX_POOL_SIZE || ''50''),\\n+  max: parseInt(process.env.MAX_POOL_SIZE || ''8''),\\n   connectionTimeoutMillis: 30000,\\n   idleTimeoutMillis: 10000,\\n   allowExitOnIdle: false,\\n\", \"pr_id\": 89}","status":"completed","timestamp":"2026-02-25T09:24:00.000000","command":"bitbucket: get_pr_diff PR #89"}]},
     {"sender":"bot","text":"**Found it.** PR #89 [6] was merged with **zero code review comments** -- it went straight through. The diff [7] shows a single-line change in `src/config/db.ts`:\n\n```\n-  max: parseInt(process.env.MAX_POOL_SIZE || ''50''),\n+  max: parseInt(process.env.MAX_POOL_SIZE || ''8''),\n```\n\nThe default connection pool size was reduced from **50 to 8**. Under checkout-service''s production load of ~400 req/s with an average query time of 25ms, 8 connections can handle at most 320 req/s before saturating. Let me verify the current file state and check for similar patterns.","message_number":6,"isCompleted":true,"run_id":"lc_run--demo-jb-0005","toolCalls":[{"id":"tc_jb_008","tool_name":"bitbucket_repos","input":"{\"action\": \"get_file_contents\", \"path\": \"src/config/db.ts\"}","output":"{\"content\": \"import { PoolConfig } from ''pg'';\\nimport { logger } from ''../utils/logger'';\\n\\nexport const poolConfig: PoolConfig = {\\n  host: process.env.DB_HOST || ''localhost'',\\n  port: parseInt(process.env.DB_PORT || ''5432''),\\n  database: process.env.DB_NAME || ''payments'',\\n  user: process.env.DB_USER || ''checkout_svc'',\\n  password: process.env.DB_PASSWORD,\\n  max: parseInt(process.env.MAX_POOL_SIZE || ''8''),\\n  connectionTimeoutMillis: 30000,\\n  idleTimeoutMillis: 10000,\\n  allowExitOnIdle: false,\\n};\\n\\nlogger.info(''DB pool configured'', { max: poolConfig.max });\", \"path\": \"src/config/db.ts\", \"commit\": \"e4f8a21c3b9d\"}","status":"completed","timestamp":"2026-02-25T09:25:00.000000","command":"bitbucket: get_file_contents src/config/db.ts"},{"id":"tc_jb_009","tool_name":"bitbucket_repos","input":"{\"action\": \"search_code\", \"query\": \"MAX_POOL_SIZE\"}","output":"{\"success\": true, \"results\": [{\"file\": \"src/config/db.ts\", \"lines\": [10], \"content\": \"  max: parseInt(process.env.MAX_POOL_SIZE || ''8''),\"}, {\"file\": \"src/config/db.test.ts\", \"lines\": [15], \"content\": \"  expect(poolConfig.max).toBe(8);\"}]}","status":"completed","timestamp":"2026-02-25T09:29:00.000000","command":"bitbucket: search_code MAX_POOL_SIZE"},{"id":"tc_jb_010","tool_name":"bitbucket_pipelines","input":"{\"action\": \"list_pipelines\"}","output":"{\"success\": true, \"pipelines\": [{\"uuid\": \"a1b2c3d4-e5f6-7890-abcd-ef1234567890\", \"state\": \"COMPLETED\", \"target_branch\": \"main\", \"creator\": \"oliviertrudeau\", \"created_on\": \"2026-02-25T09:10:30+00:00\", \"completed_on\": \"2026-02-25T09:11:45+00:00\", \"build_number\": 412}, {\"uuid\": \"b2c3d4e5-f6a7-8901-bcde-f12345678901\", \"state\": \"COMPLETED\", \"target_branch\": \"main\", \"creator\": \"mike.rodriguez\", \"created_on\": \"2026-02-24T15:21:00+00:00\", \"completed_on\": \"2026-02-24T15:22:05+00:00\", \"build_number\": 411}], \"count\": 2}","status":"completed","timestamp":"2026-02-25T09:27:00.000000","command":"bitbucket: list_pipelines"}]},
     {"sender":"bot","text":"Confirmed at HEAD [8]: `maxPoolSize` is now 8. The test file was also updated to expect 8, meaning the unit tests passed despite the dangerous change [9]. The Bitbucket pipeline #412 ran the CI checks which all passed [10] -- but there are no load tests in the pipeline, so the pool reduction was never validated under realistic traffic.\n\n**Cascading impact**: The pool starvation in checkout-service is causing transaction requests to queue for up to 30 seconds (the `connectionTimeoutMillis` value), which triggers timeout alerts on the api-gateway and drives postgres-payments to 100% connection capacity as the limited connections hold longer.\n\nLet me now provide the full analysis and remediation plan.","message_number":7,"isCompleted":true,"run_id":"lc_run--demo-jb-0006","toolCalls":[]},
     {"sender":"bot","text":"## Root Cause Analysis Complete\n\n**Root Cause**: PR #89 (\"Optimize DB connections for cost savings\") merged by oliviertrudeau reduced the checkout-service connection pool `maxPoolSize` from **50 to 8** in `src/config/db.ts` [7, 8]. The change was deployed via Jenkins build #247 (commit `e4f8a21c3b9d`) at 09:12 UTC [1, 4]. Under production load of ~400 req/s, the 8-connection pool saturated within 6 minutes, causing transaction timeouts that cascaded to the api-gateway and postgres-payments.\n\n**Evidence Chain**:\n1. Jenkins deployment #247 at 09:12:45 → PagerDuty alert at 09:18:33 (6-minute lag) [1]\n2. Build triggered by Bitbucket merge of PR #89 by oliviertrudeau [2]\n3. All pipeline stages passed including tests — but no load tests exist [3, 10]\n4. PR diff shows single-line change: `max: 50 → max: 8` [7]\n5. PR merged with 0 reviewer comments [6]\n6. File at HEAD confirms pool size is 8 [8]\n\n**Impact**: checkout-service transaction timeout rate exceeded 15%, api-gateway p99 latency spiked above 5s on /v1/payments, postgres-payments connections at 100% capacity.\n\n**Remediation**:\n1. **Immediate**: Roll back to build #246 (commit `b7c2d9e1`) or set `MAX_POOL_SIZE=50` env var and restart pods\n2. **Permanent**: Revert PR #89, restore pool size to 50, add connection pool monitoring\n3. **Prevention**: Add load testing stage to Jenkins pipeline, require code review approval for config changes","message_number":8,"isCompleted":true,"run_id":"lc_run--demo-jb-0007","toolCalls":[]}
   ]')::jsonb,
   '2026-02-25 09:18:33', '2026-02-25 09:32:00', true, 'completed', v_incident_id);

  -- Fix tool call IDs and run_ids for proper rendering
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

  RAISE NOTICE 'Demo incident (Jenkins+Bitbucket) chat session inserted.';
END $$;

-- Update summary
DO $$
DECLARE
  v_incident_id UUID := 'd2e3f4a5-b6c7-4d8e-9f0a-1b2c3d4e5f67';
BEGIN

  UPDATE incidents SET aurora_summary = 'On February 25, 2026 at 09:18 UTC, the **checkout-service** on **payments-cluster** began experiencing connection pool saturation, causing transaction timeout rates to exceed 15% [1]. The incident triggered a P1 PagerDuty alert and lasted 14 minutes. Cascading failures affected the api-gateway (p99 latency > 5s on `/v1/payments`) and postgres-payments (connections at 100% capacity) [1].

The root cause was Bitbucket PR #89 (''Optimize DB connections for cost savings''), authored by oliviertrudeau and merged with zero code reviews [6, 7]. The PR reduced the connection pool `maxPoolSize` from 50 to 8 in `src/config/db.ts` [7, 8]. Jenkins build #247 deployed commit `e4f8a21c3b9d` containing this change at 09:12 UTC -- 6 minutes before the alert fired [1, 2, 4]. Under production load of ~400 req/s with 25ms average query time, 8 connections sustain at most 320 req/s, falling 20% short of demand [8]. The pool saturated, queuing requests for the 30-second connection timeout and triggering cascading failures upstream.

The deployment passed all 5 Jenkins pipeline stages and all unit tests in both the Bitbucket pipeline (#412) and Jenkins pipeline (#247) [3, 10]. However, no load tests exist in either pipeline, so the pool reduction was never validated under realistic traffic. The incident was resolved by rolling back to build #246 (commit `b7c2d9e1`) which restored `maxPoolSize=50`.

## Suggested Next Steps

- **Revert PR #89 and restore pool size**: Change `maxPoolSize` default back to 50 in `src/config/db.ts` and add connection pool monitoring metrics [8, 9].
- **Add load testing to Jenkins pipeline**: The current pipeline runs unit tests only. Add a stage simulating production traffic to catch capacity regressions before deploy [3, 10].
- **Enforce code review for config changes**: PR #89 merged with 0 reviewer comments. Require mandatory approval for database configuration changes [6].
- **Improve post-deploy verification**: The health check only validates HTTP 200; add pool utilization checks under synthetic load [4].'
  WHERE id = v_incident_id;

  RAISE NOTICE 'Demo incident (Jenkins+Bitbucket) summary updated.';
END $$;

-- Update visualization
DO $$
DECLARE
  v_incident_id UUID := 'd2e3f4a5-b6c7-4d8e-9f0a-1b2c3d4e5f67';
BEGIN

  UPDATE incidents SET visualization_code = '{
  "nodes": [
    {"id": "checkout-svc", "label": "checkout-service", "type": "service", "status": "degraded", "parentId": null},
    {"id": "postgres-payments", "label": "postgres-payments", "type": "database", "status": "degraded", "parentId": null},
    {"id": "api-gateway", "label": "api-gateway", "type": "service", "status": "degraded", "parentId": null},
    {"id": "conn-pool", "label": "Connection Pool (max:8)", "type": "event", "status": "failed", "parentId": "checkout-svc"},
    {"id": "pr-89", "label": "PR #89: Reduce pool 50 to 8", "type": "deployment", "status": "failed", "parentId": null},
    {"id": "build-247", "label": "Jenkins Build #247", "type": "deployment", "status": "healthy", "parentId": null},
    {"id": "timeout-alert", "label": "Transaction Timeout >15%", "type": "alert", "status": "failed", "parentId": null},
    {"id": "rollback", "label": "Rollback to Build #246", "type": "deployment", "status": "healthy", "parentId": null}
  ],
  "edges": [
    {"source": "pr-89", "target": "build-247", "label": "merged and deployed", "type": "causation"},
    {"source": "build-247", "target": "conn-pool", "label": "deployed maxPoolSize=8", "type": "causation"},
    {"source": "conn-pool", "target": "checkout-svc", "label": "pool saturated under load", "type": "causation"},
    {"source": "checkout-svc", "target": "api-gateway", "label": "timeout cascades upstream", "type": "causation"},
    {"source": "checkout-svc", "target": "postgres-payments", "label": "connections held longer", "type": "causation"},
    {"source": "checkout-svc", "target": "timeout-alert", "label": "triggered PagerDuty P1", "type": "causation"},
    {"source": "rollback", "target": "conn-pool", "label": "restores pool to 50", "type": "resolution"}
  ],
  "rootCauseId": "pr-89",
  "affectedIds": ["conn-pool", "checkout-svc", "api-gateway", "postgres-payments", "timeout-alert", "build-247"],
  "version": 5,
  "updatedAt": "2026-02-25T09:32:00.000000+00:00"
}'
  WHERE id = v_incident_id;

  RAISE NOTICE 'Demo incident (Jenkins+Bitbucket) visualization updated.';
END $$;

-- Postmortem
INSERT INTO postmortems (incident_id, user_id, content, generated_at, updated_at)
VALUES (
  'd2e3f4a5-b6c7-4d8e-9f0a-1b2c3d4e5f67',
  'ab209180-626b-4601-8042-9f6328d03ae9',
  $postmortem_jb$# Postmortem: Connection pool saturation causing transaction timeouts - checkout-service

**Date:** 2026-02-25 09:18 UTC
**Duration:** 14m
**Severity:** critical
**Service:** checkout-service
**Source:** pagerduty

## Summary
The checkout-service experienced connection pool exhaustion after a deployment reduced the PostgreSQL connection pool maxPoolSize from 50 to 8. Under production load of approximately 400 requests per second, the reduced pool saturated within 6 minutes, causing transaction timeouts that cascaded to the api-gateway and postgres-payments services. The change was introduced via Bitbucket PR #89 and deployed through Jenkins build #247.

## Timeline
- **09:10:22** - PR #89 ("Optimize DB connections for cost savings") merged by oliviertrudeau on Bitbucket with zero code reviews
- **09:10:30** - Bitbucket pipeline #412 triggered, CI checks pass (no load tests in pipeline)
- **09:12:45** - Jenkins build #247 deploys commit e4f8a21c3b9d to production; all 5 pipeline stages pass
- **09:12:50** - Post-deploy health check returns 200 OK (health endpoint does not validate pool capacity)
- **09:15:00** - Connection pool begins saturating as peak traffic exceeds 320 req/s throughput ceiling
- **09:18:33** - PagerDuty P1 alert fires: transaction timeout rate exceeded 15% on checkout-service
- **09:19:10** - Correlated alert: api-gateway p99 latency exceeds 5 seconds on /v1/payments endpoint
- **09:19:45** - Correlated alert: postgres-payments active connections at 100% capacity
- **09:22:00** - Investigation traces deployment to PR #89 via Jenkins build logs and Bitbucket commit history
- **09:24:00** - Root cause identified: PR diff shows maxPoolSize changed from 50 to 8 in src/config/db.ts
- **09:30:00** - Rollback initiated to build #246 (commit b7c2d9e1 with maxPoolSize=50)
- **09:32:00** - Service recovered; transaction timeout rate below 1%, api-gateway latency normalized

## Root Cause
Bitbucket PR #89 reduced the default connection pool maxPoolSize from 50 to 8 in `checkout-service/src/config/db.ts`. The change was intended as a cost optimization to reduce idle database connections. However, the checkout-service handles approximately 400 requests per second during peak hours, and with an average query duration of 25ms, 8 connections can sustain a maximum theoretical throughput of only 320 req/s.

The change was merged without code review (0 comments on the PR) and passed all CI checks in both Bitbucket Pipelines and Jenkins because the test suite only includes unit tests with no load testing. The post-deploy health check only validates that the application responds to HTTP requests, not that the connection pool can handle production-level concurrency.

## Impact
- **checkout-service**: Transaction timeout rate exceeded 15% for 14 minutes; approximately 50,400 requests experienced timeouts (400 req/s x 14 min x 15%)
- **api-gateway**: p99 latency on /v1/payments endpoint exceeded 5 seconds, degrading all payment-related API calls
- **postgres-payments**: Connection utilization reached 100%, risking impact to other services sharing the database
- **Customer impact**: Payment processing failures and elevated checkout abandonment during the 14-minute incident window

## Resolution
The incident was resolved by rolling back checkout-service to Jenkins build #246 (commit b7c2d9e1) which had the original maxPoolSize of 50. Service health was restored within 2 minutes of the rollback, with transaction timeout rates dropping below 1%.

## Action Items
- [ ] Revert PR #89 and restore maxPoolSize default to 50 in src/config/db.ts
- [ ] Add connection pool utilization metrics (totalCount, idleCount, waitingCount) as Prometheus gauges with alerting at 80% utilization
- [ ] Add load testing stage to Jenkins deployment pipeline that simulates production traffic levels before deploying
- [ ] Enforce mandatory code review approval for changes to database configuration files via Bitbucket branch permissions
- [ ] Update post-deploy health check to validate connection pool capacity under synthetic load
- [ ] Document connection pool sizing guidelines based on expected request rate and query duration

## Lessons Learned
- **Cost optimizations require load testing**: Reducing connection pool size without validating under production traffic levels can cause immediate outages. Configuration changes affecting capacity must be tested under realistic load.
- **Zero-review merges are risky for config changes**: PR #89 was merged with no code reviews. Branch permissions should require at least one approval for changes to configuration files.
- **Health checks must validate capacity, not just availability**: The post-deploy health check returned 200 OK but did not test whether the service could handle production concurrency. Health checks should include pool utilization checks.
- **CI pipelines need load testing stages**: Both the Bitbucket pipeline and Jenkins pipeline ran only unit tests. Adding a load testing stage would have caught the throughput regression before deployment.$postmortem_jb$,
  '2026-02-25 09:35:00',
  '2026-02-25 09:35:00'
)
ON CONFLICT (incident_id) DO NOTHING;
