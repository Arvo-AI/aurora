PIPELINE FAILURE INVESTIGATION - REQUIREMENTS:
1. Work through the Data Pipeline debugging ladder above, using the evidence in the alert first.
2. Make tool calls only when they add evidence the alert does not already contain (full SQL or log via get_alert_field, recent commits from an already-connected repo, logs from an already-connected observability tool). If the evidence already proves the cause, conclude without padding the investigation.
3. Do not list cloud resources or SSH into machines unless the category is platform or ingestion_upstream and that provider is already connected.

READ-ONLY mode - investigate only, no changes. Results stay inside Aurora; never post them anywhere.

END YOUR FINAL ANSWER WITH THIS BLOCK, filled in exactly once:

## Pipeline RCA Result
- Root cause: <one or two sentences naming the node, the file and the exact cause>
- Category: <execution | ingestion_upstream | platform | schema_drift | data_test | unknown> (pre-classifier said <X>; agree or disagree, and why)
- Root node: <unique_id of the node where the fix belongs> (file: <path or unknown>)
- Evidence:
  - <exact error line, SQL fragment or diff line>
  - <another, if needed>
- Confidence: <high | medium | low> - <why>
- Blast radius: <downstream models, exposures or tasks affected, or "none found">
- Suggested fix: <concrete change: which file, what to change; a short code snippet if it helps>
- Expected variation, not a bug: <yes | no | unclear> - <one line>
