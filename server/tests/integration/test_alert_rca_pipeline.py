"""Run with the server image, a disposable initialized aurora_test DB and an isolated
Redis broker: ALERT_PIPELINE_INTEGRATION=1 python -m unittest tests.integration.test_alert_rca_pipeline.
The worker, webhook signatures, SQL, RCA prompt, summarizer and notifier are real.
The agent's evidence, model summary, provider credentials and Slack HTTP are fixtures.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4


@unittest.skipUnless(os.getenv("ALERT_PIPELINE_INTEGRATION") == "1", "requires disposable Postgres and Redis")
class AlertPipeline(unittest.TestCase):
    def test_report_uses_alert_observations_even_with_tool_citations(self):
        from chat.background.citation_extractor import Citation
        from chat.background.summarization import _build_summary_prompt_with_chat

        readings = "Input voltage 0 V, battery charge 79%, battery voltage 26.2 V"
        probe = Citation(1, "lookup", "read inventory", "No device telemetry", None, "test")
        for citations in ([], [probe]):
            with self.subTest(has_tool_evidence=bool(citations)):
                prompt = _build_summary_prompt_with_chat(
                    "cloudwatch", "UPS alert", "critical", "terminal", None,
                    investigation_transcript="No independent device access",
                    citations=citations, source_observations=readings,
                )
                self.assertIn(readings, prompt)

    def test_from_signed_webhook_to_source_reply(self):
        self.assertEqual(os.environ["POSTGRES_DB"], "aurora_test")
        from celery.contrib.testing.worker import start_worker
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from flask import Flask
        from celery_config import celery_app, _prewarm_ready
        from utils.db.connection_pool import db_pool
        from routes.aws import cloudwatch_routes as aws
        from routes.sentry import sentry_routes as sentry
        from tasks.slack_source_reply import retry_due_source_replies
        from utils.cache.redis_client import get_redis_client
        with patch.dict(os.environ, INTERNAL_API_SECRET="integration-test-only"):
            from main_chatbot import process_workflow_async

        org, user = str(uuid4()), str(uuid4())
        def sql(query, params=()):
            with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
                cur.execute(query, params)
                result = cur.fetchall() if cur.description else []
                conn.commit()
                return result

        sql("INSERT INTO organizations(id,name,slug) VALUES (%s,'RCA test',%s)", (org, org))
        sql("INSERT INTO users(id,email,password_hash,org_id) VALUES (%s,%s,'unused',%s)",
            (user, user + "@example.test", org))
        sql("INSERT INTO slack_channels(user_id,org_id,channel_id,team_id,is_member) VALUES "
            "(%s,%s,'C-alert','T-test',true),(%s,%s,'C-sentry','T-test',true)", (user, org, user, org))
        application = Flask(__name__)
        application.register_blueprint(aws.cloudwatch_bp)
        application.register_blueprint(sentry.sentry_bp, url_prefix="/sentry")
        http = application.test_client()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        topic = "arn:aws:sns:us-west-2:111111111111:alerts"
        def aws_post(body, subject=None, message_id=None, expected_status=200):
            envelope = dict(Type="Notification", Timestamp=ts, TopicArn=topic,
                            MessageId=message_id or str(uuid4()), SignatureVersion="2",
                            SigningCertURL="https://sns.us-west-2.amazonaws.com/test.pem",
                            Message=body if isinstance(body, str) else json.dumps(body))
            if subject:
                envelope["Subject"] = subject
            envelope["Signature"] = base64.b64encode(key.sign(
                aws._build_sns_string_to_sign(envelope).encode(), padding.PKCS1v15(), hashes.SHA256()
            )).decode()
            response = http.post(f"/aws/cloudwatch/webhook/{user}", json=envelope)
            self.assertEqual(response.status_code, expected_status)
            return response.json

        def sentry_post(payload=None, resource="event_alert"):
            body = json.dumps(payload or {"action": "triggered", "data": {"event": {"event_id": "c"*32,
                              "web_url": "https://example.sentry.io/issues/447/events/" + "c"*32, "title": "Error in checkout",
                              "project": {"slug": "checkout"}, "level": "error"}}}).encode()
            self.assertEqual(http.post(f"/sentry/webhook/{user}", data=body, headers={
                "Sentry-Hook-Resource": resource,
                "Sentry-Hook-Signature": hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
            }).status_code, 202)

        findings = "Evidence from the test sensor shows the service lost power."
        contexts, replies, messages = [], [], {"C-alert": [], "C-sentry": [], "C-configured": []}
        def add_post(channel, app_id, title, detail):
            parent = f"{int(time.time())}.{len(messages[channel])+1:06d}"
            messages[channel].append(dict(ts=parent, app_id=app_id, team="T-test", text=title,
                                          attachments=[{"text": detail}]))
            return parent

        async def investigate(wf, state, socket, owner, incident_id):
            contexts.append((state.rca_context["source"], state.question))
            if "QA stream failure" in state.question:
                async def broken_stream(state):
                    yield "token", "Starting investigation"
                    raise RuntimeError("simulated stream failure")
                wf.stream = broken_stream
                return await process_workflow_async(wf, state, socket, owner, incident_id)
            sql("UPDATE chat_sessions SET messages=%s WHERE id=%s", (json.dumps([
                {"sender": "assistant", "text": findings}]), state.session_id))
            return True

        post_attempts = []
        fail_post = [True]
        def slack_http(client, method, endpoint, data=None, **kwargs):
            if endpoint == "users.conversations":
                return {"ok": True, "channels": [{"id": channel} for channel in messages]}
            if endpoint == "conversations.history":
                return {"ok": True, "messages": list(messages[data["channel"]])}
            if endpoint == "chat.postMessage":
                post_attempts.append(dict(data))
                if fail_post and fail_post.pop():
                    raise ValueError("simulated Slack transport failure")
                self.assertIn(findings, json.dumps(data["blocks"]))
                self.assertTrue(data["client_msg_id"])
                self.assertTrue(data["thread_ts"])
                replies.append((data["channel"], data.get("thread_ts")))
                return {"ok": True, "ts": str(time.time()), "message": {"thread_ts": data.get("thread_ts")}}
            raise AssertionError(endpoint)

        def wait_for(predicate, description):
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(.2)
            self.fail(f"Timed out waiting for {description}; contexts={len(contexts)}, replies={replies}")

        with ExitStack() as stack:
            def replace(path, **kw):
                return stack.enter_context(patch(path, **kw))
            slack_creds = {"access_token": "local-fixture", "team_id": "T-test"}
            replace("tasks.slack_source_reply.get_credentials_from_db", return_value=slack_creds)
            replace("utils.auth.stateless_auth.get_credentials_from_db", side_effect=lambda u,p: slack_creds if p=="slack" else None)
            replace("utils.notifications.dispatcher.get_org_preference",
                    side_effect=lambda org, key, default=None: key in {"slack_investigation_start_notifications", "slack_investigation_complete_notifications"})
            replace("connectors.slack_connector.client.SlackClient._make_request", new=slack_http)
            replace("utils.notifications.slack_notification_service._get_incidents_channel_id", return_value="C-configured")
            routing = replace("utils.notifications.team_routing.trigger_team_routing_agent")
            replace("utils.notifications.dispatcher._has_teams_connected", return_value=False)
            replace("services.correlation.recurrence_agent.run_recurrence_check", return_value={})
            replace("services.memory.incident_index.append_incident_line")
            replace("services.memory.incident_index.generate_root_cause_synopsis", return_value=findings)
            replace("services.memory.collector.extract_memories_from_session.delay")
            replace("services.actions.executor.dispatch_on_incident_actions")
            replace("chat.background.task._get_worker_agent")
            replace("chat.backend.agent.workflow.Workflow", side_effect=lambda *args: SimpleNamespace(_last_state=None))
            stack.enter_context(patch.dict(sys.modules, main_chatbot=SimpleNamespace(process_workflow_async=investigate)))
            summarize = replace("chat.background.summarization.tracked_invoke", return_value=SimpleNamespace(content=findings))
            replace("chat.background.summarization.create_chat_model")
            replace("chat.background.summarization._fetch_fix_suggestions", return_value=[])
            replace("chat.background.task._get_connected_integrations", return_value={})
            replace("chat.background.rca_prompt_builder.get_user_providers", return_value=[])
            replace("routes.aws.cloudwatch_routes._get_sns_certificate", return_value=SimpleNamespace(public_key=key.public_key))
            saved_connection = {}
            read_connection = replace("routes.aws.cloudwatch_routes.get_token_data",
                                      side_effect=lambda *args, **kwargs: dict(saved_connection))
            save_connection = replace("routes.aws.cloudwatch_routes.store_tokens_in_db",
                                      side_effect=lambda owner, data, provider: saved_connection.update(data))
            connection_status = replace("routes.aws.cloudwatch_routes._has_cloudwatch_row", return_value=(False, False))
            replace("routes.sentry.sentry_routes._get_stored_sentry_credentials", return_value={"client_secret":"test-secret"})
            replace("services.detection.runner._org_representatives", return_value=[(user, org)])
            queue = "alert-qa-" + org
            celery_app.conf.update(task_default_queue=queue, task_routes={})
            celery_app.amqp.flush_routes()
            _prewarm_ready.set()
            # Exercise the real Redis broker, task registry, and worker. Tasks
            # unrelated to delivery (e.g. visualizations) are ancillary.
            stack.enter_context(start_worker(celery_app, pool="solo", perform_ping_check=False,
                                             queues=(queue,), loglevel="INFO", shutdown_timeout=30))
            expected = []
            for n in range(2):
                title = f"[CRITICAL] fault {n} on terminal 42"
                body = f"Terminal: 42\nSeverity: critical\nSignal value: {n}"
                custom = dict(version="1.0", source="custom", content=dict(title=title, description=body))
                expected.append(("C-alert", add_post("C-alert", "A6L22LZNH", title, body)))
                identity = str(uuid4())
                aws_post(custom, message_id=identity)
                if n == 0:
                    aws_post(custom, message_id=identity)
            plain = "Client: terminal-43\nPower lost"
            aws_post(plain, subject="[V2] CRITICAL offline")
            alarm = dict(AlarmName="critical CPU", AlarmArn="arn:aws:cloudwatch:region:account:alarm:cpu",
                         NewStateValue="ALARM", NewStateReason="CPU above threshold",
                         Trigger={"Dimensions":[{"name":"InstanceId", "value":"i-test"}]})
            expected.append(("C-alert", add_post("C-alert","A6L22LZNH", alarm["AlarmName"],alarm["AlarmArn"])))
            aws_post(alarm)
            expected.append(("C-sentry", add_post("C-sentry","A011MFBJEUU","checkout","https://test.sentry.io/issues/447/")))
            sentry_post()
            sentry_post()
            wait_for(lambda: len(contexts) == 5, "five investigations within the shared limit")
            self.assertEqual(sorted(x[0] for x in contexts), ["cloudwatch"]*4 + ["sentry"])
            self.assertTrue(all(question for _, question in contexts))
            wait_for(lambda: len(replies) == 3, "three immediate replies")
            expected.append(("C-alert", add_post("C-alert","A6L22LZNH","[V2] CRITICAL offline",plain)))
            sql("UPDATE incidents SET slack_source_reply_due=NOW() WHERE org_id=%s AND slack_source_reply_due IS NOT NULL", (org,))
            retry_due_source_replies.delay()
            wait_for(lambda: sorted(replies)==sorted(expected), "late and retried replies")
            aws_post(dict(alarm, NewStateValue="OK"))
            wait_for(lambda: sql("SELECT count(*) FROM incidents WHERE org_id=%s AND status='resolved'",(org,))==[(1,)], "CloudWatch OK")
            self.assertEqual(sql("SELECT count(*) FROM incidents WHERE org_id=%s AND slack_source_reply_ts IS NOT NULL",
                                 (org,)), [(5,)])

            # Both connectors still ingest alerts when the user's five slots are spent.
            aws_post("Client: terminal-99\nPower lost", subject="Rate-limited SNS")
            sentry_post({"action": "created", "data": {"issue": {
                "id": "899", "title": "Rate-limited Sentry",
            }}}, resource="issue")
            retry_due_source_replies.delay().get(timeout=20, disable_sync_subtasks=False)
            self.assertEqual(sql("SELECT alert_title, rca_celery_task_id, slack_source_reply_ts FROM incidents "
                                 "WHERE org_id=%s AND alert_title IN ('Rate-limited SNS','Rate-limited Sentry') "
                                 "ORDER BY alert_title", (org,)),
                             [("Rate-limited SNS", None, None), ("Rate-limited Sentry", None, None)])
            self.assertEqual(sql("SELECT count(*) FROM chat_sessions WHERE org_id=%s AND incident_id IS NOT NULL",
                                 (org,)), [(5,)])
            self.assertEqual(len(contexts), 5)
            # Begin a fresh rate-limit window for the remaining delivery scenarios.
            get_redis_client().delete(f"background_chat_rate_limit:{user}")

            # Some SNS topics have no Slack subscriber. Exhaust the lookup delay
            # without sleeping for minutes. An unmatched alert stays quiet.
            aws_post("Service: power\nBattery offline", subject="Battery offline")
            wait_for(lambda: sql("SELECT count(*) FROM incidents WHERE org_id=%s AND alert_title='Battery offline' "
                                 "AND slack_source_reply_attempts=1", (org,)) == [(1,)], "unmatched delivery")
            sql("UPDATE incidents SET slack_source_reply_due=NOW(), slack_source_reply_attempts=3 "
                "WHERE org_id=%s AND alert_title='Battery offline'", (org,))
            retry_due_source_replies.delay()
            wait_for(lambda: sql("SELECT slack_source_reply_ts, slack_source_reply_due FROM incidents "
                                 "WHERE org_id=%s AND alert_title='Battery offline'", (org,)) == [(None, None)],
                     "exhausted lookup without a standalone message")
            self.assertCountEqual(replies, expected)
            for kind in ("incident_initial_summary", "incident_rca_summary"):
                prompts = [call.args[1][0].content for call in summarize.call_args_list
                           if call.kwargs.get("request_type") == kind]
                self.assertTrue(any("Signal value: 0" in prompt for prompt in prompts), kind)
            aws_post({"unrecognized": "object"})
            aws_post(dict(alarm, NewStateValue="INSUFFICIENT_DATA"))
            wait_for(lambda: sql("SELECT count(*) FROM cloudwatch_alarms WHERE org_id=%s AND state_value IN "
                                 "('', 'INSUFFICIENT_DATA')", (org,)) == [(2,)], "non-firing audit rows")
            self.assertEqual(sql("SELECT count(*) FROM incidents WHERE org_id=%s", (org,)), [(8,)])


            # A v1 worker can still execute its original upsert while v2 workers
            # store immutable occurrences. An exact replay of a legacy event is inert.
            legacy = {"action": "created", "data": {"issue": {"id": "445", "title": "Earlier incident"}}}
            sql("INSERT INTO sentry_events (org_id,user_id,issue_id,action,resource,payload,received_at) "
                "VALUES (%s,%s,'445','created','issue',%s,NOW()) ON CONFLICT (org_id,issue_id,action) "
                "WHERE issue_id IS NOT NULL DO UPDATE SET payload=EXCLUDED.payload", (org,user,json.dumps(legacy)))
            from routes.sentry.tasks import process_sentry_event
            process_sentry_event.delay(legacy, {"resource": "issue"}, user).get(timeout=20, disable_sync_subtasks=False)
            self.assertEqual(sql("SELECT count(*) FROM incidents WHERE org_id=%s", (org,)), [(8,)])

            parent = add_post("C-alert", "A6L22LZNH", "QA stream failure", "Signal unavailable")
            aws_post("Signal unavailable", subject="QA stream failure")
            def failed_run():
                return sql(
                    "SELECT i.aurora_status, s.status, i.slack_source_reply_ts FROM incidents i "
                    "JOIN chat_sessions s ON s.id=i.aurora_chat_session_id::text "
                    "WHERE i.org_id=%s AND i.alert_title='QA stream failure'", (org,)
                ) == [("error", "failed", None)]
            wait_for(failed_run, "failed workflow recorded without a completed RCA")
            retry_due_source_replies.delay().get(timeout=20, disable_sync_subtasks=False)
            self.assertNotIn(("C-alert", parent), replies)
            self.assertCountEqual(replies, expected)

            title = "QA truncated RCA"
            parent = add_post("C-alert", "A6L22LZNH", title, "Signal unavailable")
            report_attempts = []
            def generate_report(model, messages, *, request_type, **kwargs):
                if request_type == "incident_rca_summary":
                    report_attempts.append(request_type)
                    self.assertFalse(any(p.get("thread_ts") == parent for p in post_attempts))
                    if len(report_attempts) == 1:
                        return SimpleNamespace(content="The cause is", response_metadata={"stopReason": "max_tokens"})
                return SimpleNamespace(content=findings, response_metadata={"stopReason": "end_turn"})

            summarize.side_effect = generate_report
            aws_post("Signal unavailable", subject=title)
            expected.append(("C-alert", parent))
            wait_for(lambda: sorted(replies) == sorted(expected), "only the finished report posted after regeneration")
            self.assertEqual(len(report_attempts), 2)
            self.assertTrue(all(p.get("thread_ts") and findings in json.dumps(p.get("blocks"))
                                for p in post_attempts), "Every outbound Slack post must be a threaded final report")
            # The Sentry issue subscription sends both creation and later
            # lifecycle changes. A resolution can also arrive without its creation.
            issue = {"id": "629", "title": "Synthetic checkout exception", "level": "error",
                     "project": {"slug": "qa-store"}, "permalink": "https://test.sentry.io/issues/629/"}
            parent = add_post("C-sentry", "A011MFBJEUU", issue["title"], issue["permalink"])
            expected.append(("C-sentry", parent))
            before = len(contexts)
            for action in ("created", "created", "resolved"):
                sentry_post({"action": action, "data": {"issue": issue}}, resource="issue")
            sentry_post({"action": "resolved", "data": {"issue": dict(issue, id="630")}}, resource="issue")
            wait_for(lambda: sorted(replies) == sorted(expected), "one final reply under the Sentry issue")
            retry_due_source_replies.delay().get(timeout=20, disable_sync_subtasks=False)
            self.assertEqual(sql("SELECT action, count(*) FROM sentry_events WHERE org_id=%s "
                                 "AND group_id IN ('629','630') GROUP BY action ORDER BY action", (org,)),
                             [("created", 1), ("resolved", 2)])
            self.assertEqual(len(contexts), before + 1)
            self.assertCountEqual(replies, expected)
            completed = sql("SELECT id FROM incidents WHERE org_id=%s AND aurora_status='complete' "
                            "AND rca_unfinished_reason IS NULL", (org,))
            self.assertCountEqual(
                [call.args[1]["incident_id"] for call in routing.call_args_list],
                [str(incident_id) for (incident_id,) in completed],
            )

            # The configured main channel has no local registry row or description.
            # Verify completed incidents against Slack's actual bot memberships.
            from utils.notifications.dispatcher import notify_investigation_completed
            for source, app_id, url, metadata in (
                ("pagerduty", "A1FKYAUUX", "https://acme.pagerduty.com/incidents/P001",
                 {"incidentId": "P001"}),
                ("incidentio", "A01DEGPUHHC", "https://app.incident.io/acme/incidents/42",
                 {"permalink": "https://app.incident.io/acme/incidents/42"}),
            ):
                parent = add_post("C-configured", app_id, "API is down", url)
                [(incident_id,)] = sql(
                    "INSERT INTO incidents (org_id,user_id,source_type,source_alert_id,alert_title,"
                    "started_at,aurora_status,aurora_summary,alert_metadata) "
                    "VALUES (%s,%s,%s,42,'API is down',NOW(),'complete',%s,%s) RETURNING id",
                    (org, user, source, findings, json.dumps(metadata)),
                )
                for _ in range(2):
                    notify_investigation_completed(user, str(incident_id))
                expected.append(("C-configured", parent))
                wait_for(lambda: sorted(replies) == sorted(expected), f"{source} RCA under its main-channel alert")
                [(target, reply_ts)] = sql(
                    "SELECT slack_source_target,slack_source_reply_ts FROM incidents WHERE id=%s", (incident_id,)
                )
                self.assertEqual((target["channel"], target["ts"]), ("C-configured", parent))
                self.assertTrue(reply_ts)

            self.assertEqual(saved_connection, {"approved_topic_arn": topic})
            save_connection.assert_called_once()
            connection_status.return_value = (True, False)
            self.assertEqual(aws_post("Disabled webhook")["reason"], "connection_disabled")
            save_connection.assert_called_once()
            connection_status.return_value = (True, True)
            read_connection.side_effect = OSError("test store unavailable")
            aws_post("Retry after storage recovery", expected_status=503)
            save_connection.assert_called_once()
