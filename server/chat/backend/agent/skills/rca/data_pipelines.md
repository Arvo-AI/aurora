---
id: data_pipelines
name: Data Pipeline RCA (dbt / Airflow / CI logs)
category: rca_source
connection_check:
  method: always
index: "Debugging ladder for failed dbt models/tests, Airflow tasks and pipeline logs"
rca_priority: 1
metadata:
  author: aurora
  version: "1.0"
---

## Data Pipeline Investigation

The failure evidence came with the alert: dbt artifacts, an Airflow failure payload or a pasted log, already redacted. A deterministic pre-classifier has tagged it with a category, the rule that fired and the lines that triggered it. Treat that as a hypothesis. Confirm it from the evidence or overturn it and say why.

Nothing new needs to be connected. Never ask the user to connect a tool, and never try to reach Airflow, dbt Cloud or the warehouse with credentials you were not given.

### The debugging ladder (work top to bottom, stop when the cause is proven)

1. **Which node failed.** Name the primary failing node (dbt `unique_id` or Airflow `dag.task`) and quote the exact error line. If several nodes failed, decide which one is the origin and which are knock-on failures (a skipped or failed child of a failed parent is not a second root cause).
2. **First failure or repeat.** Read the History and Previous run lines. A first failure right after a change points at the change. A repeat points at something persistent (data, quota, capacity).
3. **Upstream lineage.** Check the "First suspicious ancestor" and the direct parents. For a missing column or relation, find which upstream model or source used to provide it and what it provides now. The fix often belongs upstream of the node that crashed.
4. **Compiled SQL.** Find the exact expression that fails (the column reference, the cast, the join key). Full compiled SQL and the full log are in the stored payload: `get_alert_field('evidence.failing_nodes.0.compiled_sql')`, `get_alert_field('evidence.log_excerpt')`, `get_alert_field('evidence.lineage')`.
5. **Recent changes to that model.** If a "Recent changes" block is present, read it first: it is the git history the user attached. Otherwise, only if GitHub/GitLab/Bitbucket is already connected, look up recent commits touching the failing file and its parents (read-only). Do not guess at changes you cannot see.
6. **Only for `ingestion_upstream` or `platform`:** if Elastic, Datadog, Splunk, GCP, AWS, Azure or Kubernetes is already connected, query those logs and metrics around the failure time (worker OOM events, node pressure, API error rates). If none is connected, say which signal would confirm the hypothesis instead of guessing.

### By category

- **schema_drift**: name the column or relation that disappeared or changed type, the upstream node that changed it, and every downstream model that still expects the old shape (the blast radius). The fix is either to update the references or to restore or alias the column upstream. Say which, and why.
- **execution**: find the failing expression in the compiled SQL (bad cast, divide by zero, syntax, permissions). For a bad cast, say which column and value shape breaks it and whether the fix is in the SQL (`try_cast`, filtering) or in the data.
- **data_test**: state which test, on which column of which model, and how many rows failed. Decide honestly whether this is **expected variation in the data** (a new valid value, a late or duplicate row in the source) or a **bug in a transformation** (a join that fans out, a filter that drops keys, a column that became null after a change). If the evidence cannot tell them apart, say so and name the query that would (for example, select the failing rows).
- **ingestion_upstream**: say which upstream API or source failed and how (429 rate limit, quota, expired or invalid token, 403, timeout). Suggest the fix on the pipeline side (backoff and retry, a lower request rate, spreading schedules, rotating the credential in the secret store). Never read or print the credential.
- **platform**: say which resource ran out or was unavailable (memory, disk, warehouse, lock) and whether the task's own work caused it (a large unfiltered query, a cartesian join) or the platform did.
- **unknown**: say what is missing and the single most useful piece of evidence to attach next time.

### Rules

- Read-only. Investigate only. Never write to a repository, ticket, chat channel or warehouse.
- Quote evidence verbatim. Do not invent model names, columns, commits or files that are not in the evidence or tool output.
- Prefer the smallest correct fix, and point to the file where it belongs.
