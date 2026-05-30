---
sidebar_position: 6
---

# GCP Log PII Redaction

Aurora can read logs from Google Cloud Logging with automatic PII filtering. A redaction pipeline deployed within your GCP project strips personally identifiable information before Aurora has access to the data. Aurora's service account is granted read permission only to the redacted output; it never sees, receives, or processes raw log data.

Log entries with no PII pass through unchanged. Entries containing PII have sensitive values replaced with type labels (e.g., `[EMAIL_ADDRESS]`, `[US_SOCIAL_SECURITY_NUMBER]`).

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Your GCP Project                                               │
│                                                                 │
│  Application ──► Cloud Logging ──► Log Router Sink              │
│                                          │                      │
│                                          ▼                      │
│                                     Pub/Sub Topic               │
│                                          │                      │
│                                          ▼                      │
│                                   Dataflow Pipeline             │
│                                          │                      │
│                                          ▼                      │
│                                      DLP API                    │
│                                          │                      │
│                                          ▼                      │
│                                 Redacted Log Bucket             │
│                                          │                      │
└──────────────────────────────────────────┼──────────────────────┘
                                           │
                                           ▼
                                        Aurora
                                   (reads only here)
```

Every component runs inside your GCP project. The DLP API identifies and removes PII before the redacted entries are written to the destination bucket. Aurora has read access exclusively to that destination. There is no path by which Aurora receives unredacted data.

The DLP API uses machine learning classifiers to detect PII. With default settings (`ALL_BASIC` mode), it automatically identifies 50+ sensitive data types including email addresses, phone numbers, social security numbers, credit card numbers, IP addresses, physical addresses, dates of birth, person names, and driver's license numbers. No explicit configuration of which types to scan for is required.

## Setup

All commands run in the target GCP project. Set `PROJECT_ID` before starting:

```bash
export PROJECT_ID="your-project-id"
```

### 1. Enable APIs

```bash
gcloud services enable dlp.googleapis.com --project=$PROJECT_ID
gcloud services enable pubsub.googleapis.com --project=$PROJECT_ID
gcloud services enable dataflow.googleapis.com --project=$PROJECT_ID
```

### 2. Create Pub/Sub topic and subscription

```bash
gcloud pubsub topics create aurora-log-redaction \
  --project=$PROJECT_ID

gcloud pubsub subscriptions create aurora-log-redaction-sub \
  --topic=aurora-log-redaction \
  --project=$PROJECT_ID \
  --ack-deadline=60
```

### 3. Create the Log Router Sink

```bash
gcloud logging sinks create aurora-redaction-sink \
  pubsub.googleapis.com/projects/$PROJECT_ID/topics/aurora-log-redaction \
  --project=$PROJECT_ID \
  --log-filter='resource.type="gce_instance" OR resource.type="k8s_container"'
```

Adjust `--log-filter` to match the log sources you want to redact. Omitting the filter routes all logs (higher volume, higher cost).

The command outputs a service account ID. Grant it publish access:

```bash
gcloud pubsub topics add-iam-policy-binding aurora-log-redaction \
  --project=$PROJECT_ID \
  --member="serviceAccount:<SERVICE_ACCOUNT_FROM_OUTPUT>" \
  --role="roles/pubsub.publisher"
```

### 4. Create destination for redacted logs

```bash
gcloud logging buckets create aurora-redacted \
  --location=global \
  --project=$PROJECT_ID \
  --retention-days=30
```

### 5. Create service account for Dataflow

```bash
gcloud iam service-accounts create aurora-dataflow-redaction \
  --project=$PROJECT_ID \
  --display-name="Aurora DLP Redaction Pipeline"

SA=aurora-dataflow-redaction@$PROJECT_ID.iam.gserviceaccount.com

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/dataflow.worker"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/pubsub.subscriber"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/dlp.user"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/logging.logWriter"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/storage.objectAdmin"
```

### 6. Create temp storage bucket for Dataflow

```bash
gsutil mb -p $PROJECT_ID -l us-central1 \
  gs://$PROJECT_ID-aurora-dataflow-temp
```

### 7. Deploy the redaction pipeline

This pipeline is adapted from Google's open source reference implementation ([source](https://github.com/GoogleCloudPlatform/python-docs-samples/blob/main/logging/redaction/log_redaction_final.py), Apache 2.0 license) with the inspection config set to detect all PII types.

Install dependencies:

```bash
pip install 'apache-beam[gcp]' google-cloud-dlp google-cloud-logging
```

Save as `log_redaction_pipeline.py`:

```python
from __future__ import annotations
import argparse, json, logging

from apache_beam import (
    CombineFn, CombineGlobally, DoFn, io, ParDo, Pipeline, WindowInto,
)
from apache_beam.error import PipelineError
from apache_beam.options.pipeline_options import (
    GoogleCloudOptions, PipelineOptions,
)
from apache_beam.transforms.window import FixedWindows
from google.cloud import dlp_v2, logging_v2

INSPECT_CFG = {"min_likelihood": "POSSIBLE"}

REDACTION_CFG = {
    "info_type_transformations": {
        "transformations": [{
            "primitive_transformation": {
                "replace_with_info_type_config": {}
            }
        }]
    }
}


class PayloadAsJson(DoFn):
    def process(self, element):
        yield json.loads(element.decode("utf-8"))


class BatchPayloads(CombineFn):
    def create_accumulator(self):
        return []

    def add_input(self, accumulator, input):
        accumulator.append(input)
        return accumulator

    def merge_accumulators(self, accumulators):
        return [i for a in accumulators for i in a]

    def extract_output(self, accumulator):
        return accumulator


class LogRedaction(DoFn):
    def __init__(self, region, project_id):
        self.project_id = project_id
        self.region = region
        self.dlp_client = None

    def _log_to_row(self, entry):
        payload = entry.get("textPayload", "")
        return {"values": [{"string_value": payload}]}

    def setup(self):
        if self.dlp_client:
            return
        self.dlp_client = dlp_v2.DlpServiceClient()
        if not self.dlp_client:
            raise PipelineError("Cannot create DLP client")

    def process(self, logs):
        if not logs:
            return
        table = {
            "table": {
                "headers": [{"name": "textPayload"}],
                "rows": list(map(self._log_to_row, logs)),
            }
        }
        response = self.dlp_client.deidentify_content(
            request={
                "parent": f"projects/{self.project_id}/locations/{self.region}",
                "inspect_config": INSPECT_CFG,
                "deidentify_config": REDACTION_CFG,
                "item": table,
            }
        )
        for idx, log in enumerate(logs):
            log["textPayload"] = (
                response.item.table.rows[idx].values[0].string_value
            )
        yield logs


class IngestLogs(DoFn):
    def __init__(self, destination_log_name):
        self.destination_log_name = destination_log_name
        self.logger = None

    def _replace_log_name(self, entry):
        entry["logName"] = self.logger.name
        return entry

    def setup(self):
        if self.logger:
            return
        client = logging_v2.Client()
        if not client:
            raise PipelineError("Cannot create Logging client")
        self.logger = client.logger(self.destination_log_name)

    def process(self, element):
        if self.logger:
            logs = list(map(self._replace_log_name, element))
            self.logger.client.logging_api.write_entries(logs)
            yield logs


def run(pubsub_subscription, destination_log_name,
        window_size, pipeline_args=None):
    pipeline_options = PipelineOptions(
        pipeline_args, streaming=True, save_main_session=True
    )
    region = "us-central1"
    try:
        region = pipeline_options.view_as(GoogleCloudOptions).region
    except AttributeError:
        pass

    pipeline = Pipeline(options=pipeline_options)
    _ = (
        pipeline
        | "Read from Pub/Sub"
        >> io.ReadFromPubSub(subscription=pubsub_subscription)
        | "Parse JSON"
        >> ParDo(PayloadAsJson())
        | "Window"
        >> WindowInto(FixedWindows(window_size))
        | "Batch"
        >> CombineGlobally(BatchPayloads()).without_defaults()
        | "Redact PII"
        >> ParDo(
            LogRedaction(region, destination_log_name.split("/")[1])
        )
        | "Write redacted logs"
        >> ParDo(IngestLogs(destination_log_name))
    )
    pipeline.run()


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--pubsub_subscription")
    parser.add_argument("--destination_log_name")
    parser.add_argument("--window_size", type=float, default=60.0)
    known_args, pipeline_args = parser.parse_known_args()
    run(
        known_args.pubsub_subscription,
        known_args.destination_log_name,
        known_args.window_size,
        pipeline_args,
    )
```

Deploy to Dataflow:

```bash
SA=aurora-dataflow-redaction@$PROJECT_ID.iam.gserviceaccount.com
SUB=projects/$PROJECT_ID/subscriptions/aurora-log-redaction-sub
DEST=projects/$PROJECT_ID/logs/aurora-redacted

python log_redaction_pipeline.py \
  --pubsub_subscription=$SUB \
  --destination_log_name=$DEST \
  --window_size=60 \
  --runner=DataflowRunner \
  --project=$PROJECT_ID \
  --region=us-central1 \
  --temp_location=gs://$PROJECT_ID-aurora-dataflow-temp/tmp \
  --service_account_email=$SA \
  --num_workers=1 \
  --max_num_workers=3
```

Once deployed, the job appears in the GCP console under **Dataflow > Jobs** as a streaming pipeline. It runs continuously until manually stopped.

### 8. Grant Aurora read access to redacted logs only

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:aurora@aurora-saas-prod.iam.gserviceaccount.com" \
  --role="roles/logging.viewer" \
  --condition='expression=resource.name.endsWith("aurora-redacted"),
    title=redacted-bucket-only'
```

Aurora's service account should not receive the Logs Field Accessor role, ensuring restricted fields in the original bucket remain hidden even if queried directly.

## Verification

Write a test log entry containing known PII:

```bash
gcloud logging write test-log \
  "ERROR: User john@example.com from IP 10.0.0.1, SSN 123-45-6789" \
  --project=$PROJECT_ID --severity=ERROR
```

After 10-15 seconds, the redacted output appears:

```
ERROR: User [EMAIL_ADDRESS] from IP [IP_ADDRESS], SSN [US_SOCIAL_SECURITY_NUMBER]
```

## Scope

The sink can be created at project, folder, or organization level. An org-level aggregated sink captures logs from all projects in one pass:

```bash
gcloud logging sinks create aurora-redaction-sink \
  pubsub.googleapis.com/projects/$PROJECT_ID/topics/aurora-log-redaction \
  --organization=$ORG_ID
```

This requires `roles/logging.configWriter` at the organization level.

## Alternatives

### Field-level access controls (no pipeline)

Cloud Logging supports hiding specific `LogEntry` fields from principals without the Logs Field Accessor role.

```bash
gcloud logging buckets update _Default \
  --location=global --project=$PROJECT_ID \
  --restricted-fields="jsonPayload.email,jsonPayload.ip,jsonPayload.ssn"
```

Aurora, without the field accessor role, cannot see those fields. This works well for structured JSON logs where PII is isolated in known fields. It does not work for unstructured `textPayload` where PII is embedded in free text (the entire field would be hidden, not just the PII substring). Limited to 20 restricted fields per bucket.

### Skip GCP logs (use an external log source)

If your primary logging is in an external observability platform rather than GCP (e.g., Datadog, Splunk, New Relic, Elastic), the DLP pipeline above is unnecessary. Instead, grant Aurora a GCP service account with infrastructure visibility but no log access, and connect Aurora to your observability platform separately for log-based investigation.

This approach provides significant RCA capability from GCP (pod status, events, metrics, deployments) while keeping all log data access through your external platform's built-in PII filtering.

#### Recommended roles

```bash
SA=aurora@aurora-saas-prod.iam.gserviceaccount.com

# Infrastructure visibility (pods, nodes, deployments, events — no pod logs)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/container.viewer"

# Metrics, alerts, dashboards, uptime checks
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/monitoring.viewer"
```

#### What Aurora can do with these roles

| Capability | Example |
|---|---|
| Detect crash loops | Pod status shows CrashLoopBackoff, restart count |
| Identify OOM kills | Kubernetes events show OOMKilled reason |
| Find bad deployments | Rollout history shows recent changes, replica counts |
| Check resource pressure | Metrics show CPU/memory spikes at alert time |
| See scheduling failures | Events show FailedScheduling, image pull errors |
| Check node health | Node conditions, capacity, allocatable resources |
| Review networking | Service endpoints, ingress configs, HPA state |
| Alert context | What fired, thresholds, notification channels |

#### What Aurora cannot do (by design)

| Blocked | Why |
|---|---|
| Read pod logs (`kubectl logs`) | `container.pods.getLogs` is not in `container.viewer` |
| Read Cloud Logging entries | No `logging.*` permissions granted |
| Read traces | No `cloudtrace.*` permissions |
| Read error reports | No `errorreporting.*` permissions |
| Access Cloud Storage | No `storage.*` permissions |

Pod logs and application-level log data should be accessed through your external observability platform, which handles PII filtering natively (e.g., Datadog Sensitive Data Scanner, Splunk Data Masking, Elastic Field Redaction).
