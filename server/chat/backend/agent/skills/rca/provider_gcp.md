---
id: provider_gcp
name: GCP RCA Investigation
category: rca_provider
connection_check:
  method: provider_in_preference
index: "GCP/GKE and BigQuery investigation commands"
rca_priority: 5
metadata:
  author: aurora
  version: "1.0"
---

## GCP/GKE Investigation

- Check cluster status: `cloud_exec('gcp', 'container clusters list')`
- **IMPORTANT**: Get cluster credentials first: `cloud_exec('gcp', 'container clusters get-credentials CLUSTER_NAME --region=REGION')`
- Get pod details: `cloud_exec('gcp', 'kubectl get pods -n NAMESPACE -o wide')`
- Describe problematic pods: `cloud_exec('gcp', 'kubectl describe pod POD_NAME -n NAMESPACE')`
- Check pod logs: `cloud_exec('gcp', 'kubectl logs POD_NAME -n NAMESPACE --since=1h')`
- Check pod metrics: `cloud_exec('gcp', 'kubectl top pod POD_NAME -n NAMESPACE')`
- Check events: `cloud_exec('gcp', 'kubectl get events -n NAMESPACE --sort-by=.lastTimestamp')`
- Check node health: `cloud_exec('gcp', 'kubectl describe node NODE_NAME')`
- Query Stackdriver logs: `cloud_exec('gcp', 'logging read "resource.type=k8s_container" --limit=50 --freshness=1h')`
- Check deployments: `cloud_exec('gcp', 'kubectl get deployments -n NAMESPACE')`
- Check services: `cloud_exec('gcp', 'kubectl get svc -n NAMESPACE')`
- Check HPA: `cloud_exec('gcp', 'kubectl get hpa -n NAMESPACE')`
- Check PVC status: `cloud_exec('gcp', 'kubectl get pvc -n NAMESPACE')`

## BigQuery

When the question is whether data landed, a load job failed, or a table is empty, start with metadata. Do not scan the table first.

- List datasets: `cloud_exec('gcp', 'bq ls')`
- Table metadata (row count, last modified, partitioning): `cloud_exec('gcp', 'bq show --format=prettyjson DATASET.TABLE')`
- Recent jobs: `cloud_exec('gcp', 'bq ls -j -a -n 20')`
- Read rows only when the contents matter: `cloud_exec('gcp', "bq query --nouse_legacy_sql 'SELECT ...'")`. One quoted `SELECT` or `WITH`, with a partition or date filter and a `LIMIT`. Ask mode blocks write tokens it can see; it does not parse SQL inside the quotes.
- `bq query` and `bq wait` run for 300 seconds. Pass `timeout` (seconds) only when the query needs longer, up to 600. The investigation ends at INVESTIGATION_TIMEOUT_SECONDS (default 3600), and a command that runs until then is killed before its BigQuery job can be cancelled.
- A query with no `--maximum_bytes_billed` is capped at 10 GB, and one with no `--max_rows` returns 100 rows. Set either flag to raise it. The ceiling is 1 TiB and 1,000 rows. A cap error means narrow the filter.
- A 403 means the connected identity cannot read BigQuery (`roles/bigquery.dataViewer` and `roles/bigquery.jobUser`). Say that. Do not treat it as proof the data is missing.
