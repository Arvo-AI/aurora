# Findings about Aurora from lab runs

Things the lab showed about Aurora itself: bugs, gaps, good and bad behavior.
Each entry links the run that showed it. These feed engineering posts and
Aurora issues. Nothing here is fixed by editing the lab to hide it.

## 2026-10-02, smoke-01 (01-worker-oom)

**Read-only `kubectl rollout history` is blocked by the default command policy.**
Aurora tried `kubectl rollout history deployment/worker -n shop` to see what
changed and got "Command blocked by organization policy: Mutating kubectl
operations". The deny rule in `server/utils/auth/command_policy.py` (line 704 in
`observability_only`, line 790 in `standard_ops`, Aurora `87d22baf`) matches
the whole `rollout` verb, so `rollout history` and `rollout status`, both
read-only, are denied with the mutating `rollout restart` and `rollout undo`.
Aurora worked around it by diffing ReplicaSets, but "what changed" is the
first question in most incidents. Fix: deny only `rollout (restart|undo|pause|resume)`
and allow `rollout (history|status)`.

**No explicit fix in the final report.** The RCA was correct (OOMKilled,
BATCH_SIZE 500 to 2500 against a 192Mi limit, with the memory math) but did not
recommend a remediation. The judge flagged it. (Wrong lead: the report never
holds fixes by design; they are in `suggestions`. See the correction at the end.)

**Honest "Not checked" section.** Aurora listed what it could not verify
(metrics-server absent, rollout history blocked, previous logs unavailable)
instead of guessing. Good behavior worth showing in the lab report.

Run: 120 s, 14 tool calls, 0.38 USD, model `bedrock/us.anthropic.claude-sonnet-5`.

## 2026-10-02, v0-run1 (all 10 scenarios, one run each)

**Image provenance unverified; superseded by v0-run2.** v0-run1 ran on the shared
`aurora_server:latest` tag (container image `ef856b3d`), which another stack on the
same machine also builds and has since retagged. The server code is bind-mounted
from the lab worktree (87d22baf), so the Python code was right, but the image
(dependencies, MCP binaries) cannot be tied to a commit. All three runs in one
shared org, so they were also not independent (see below).

Judge verdicts (not yet reviewed by a person): 9 correct, 1 partial (09).
Total 2,213 s, 190 tool calls, 6.31 USD. Without 09: median about 150 s and 0.40 USD.

**09: Aurora found the cause, then lost it in the report.** By step 10 Aurora had
run `get pvc,pv`, `get storageclass` and `get pvc redis-data -o yaml`, and at step 19
it wrote to its own memory "Redis PVC references non-existent StorageClass". It then
made 25 `write_artifact` calls to its "Incident Index". Many had no `content`
argument and never finished. Others look like the model debugging the tool
("placeholder", "test2", a lorem ipsum string). That took 12 of the 15 minutes and
2 of the 2.61 USD. The report's root cause never names the PVC or the StorageClass
("a dependency the cluster could not satisfy"), says the redis pod is "running,
not ready" (it was Pending), and lists the PVC under "Not checked" because it had
"no numbered evidence". The investigation notes and suggested next steps do name
the PVC and `fast-ssd`; graded on that full output the run is correct. So the
investigation was right but the report was not. The defect is in the artifact tool and in how the
summary picks up evidence, not in the reasoning.

**Runs are not independent.** Aurora keeps an "Incident Index" artifact and
memory across incidents in the same org, and its first step lists recent
incidents. In 10 it ruled out "the worker BATCH_SIZE/OOM crash mechanism (seen in
prior incidents)". For v1, each scenario needs a fresh org (or wiped memory and
artifacts), so that one run cannot leak into the next.

**No remediation section.** In 10 of 10 reports the fix was implied or appeared
only under "Not checked". (Wrong: the report prompt forbids fixes and Aurora puts
them in a separate suggestions list, which the lab did not read. See the
correction at the end.)

**`rollout history` blocked again** in 05, 09 and 10. In 05 Aurora repeated the policy's
wording and called `rollout history` "a mutating operation" in the report.

**Inferences go a little past the evidence.** Aurora often reads a difference
between `last-applied-configuration` and the live spec as "an unreviewed
imperative edit". In this lab that was true, but Aurora states it more firmly
than the evidence supports. In 08 it guessed the cause was a missing `m` suffix.

## 2026-10-02, v0-run2 and v1-run1 (fresh org per run, 3 lanes, 2 repeats)

Judge verdicts, later reviewed by a person (see REPORT.md). v0-run2: 20/20 correct. v1-run1:
14 correct, 4 partial, 0 wrong. Second-model review agrees except v0-run2/07 r1
(partial: the selector value app=api-v2 is never named in the root cause).

**Stops at the alerted fault and calls the rest healthy (12, both runs).** With a
worker OOM and a frontend Service selecting no pods, Aurora found the OOM and wrote
"Other workloads in the `shop` namespace (api, frontend) remained healthy and
unaffected" with pod status as its only evidence. It never ran `get svc` or
`get endpoints`. A "healthy" claim should need a traffic-level check.

**Policy blocks `kubectl exec` for reads (19 r1).** `kubectl exec <pod> -- cat
/etc/nginx/conf.d/default.conf` is denied as "Mutating kubectl operations". That
is defensible for `exec` in general, but it left Aurora unable to compare the live
config of two pods, and it did not fall back to pod age and the ConfigMap
resourceVersion, which were already in its output.

**`rollout history` blocked in 26 of 38 runs** (16 of 20 in v0-run2, 10 of 18 in v1-run1).

**Fix stated in 10 of 38 reports.** (Wrong measure: fixes live in `suggestions`;
34 of 38 were acceptable. See the correction at the end.)

**Small unsupported claims in most v1 reports**, none in the root cause: "both
cluster nodes" (three exist), "pulled successfully" with no Pulled event, a worker
that "processed Redis-backed batches" (it never touches redis), a trend from one
sample. List in each `grade.json` under `reviewer_ai`.

**Prediscovery costs more than the investigations.** Each kubeconfig upload
schedules a prediscovery agent run 600 s later (`utils/auth/token_management.py:551`).
30 completed runs cost $29.70 per `llm_usage_tracking` ($30.38 counting the runs
killed part-way), against $14.53 for 38 RCAs. They
also share the 4 celery slots with RCAs and delay them.

## 2026-10-02, fix branch lab/rollout-readonly (see patches/NOTES.md)

**4096 output-token cap on Bedrock.** The agent never sets `max_tokens`
(`chat/backend/agent/providers/bedrock_provider.py:180-197`), so Converse defaults
to 4096. In v0-run1/09, 19 agent calls over about 11 minutes had exactly 4096
output tokens, between a few short ones: the model was rewriting a whole artifact in one call and its arguments
were cut off. pydantic rejected the call before the tool ran, the model saw only
"content: Field required", and the step stayed "running". Patch 0002 makes the
tool explain the cut-off; the cap itself is not changed.

**Summary uses only the last 30 citations** (`chat/background/summarization.py:210`),
and artifact/memory write receipts counted as citations, which is how 09's PVC
evidence fell out of the report. Patch 0003 excludes write receipts; the window
is not changed.

**`rollout history`/`status` allowed** on the fix branch (patch 0001). Not
changed: the Ask-mode classifier (`utils/security/read_only_classifier.py:47`)
still treats `rollout` as a write verb on the cloud kubectl path.

**`prediscovery_enabled` does not stop the on-upload run.** The preference is
checked only by the periodic scheduler (`chat/background/prediscovery_task.py:37`).
The run scheduled by a kubeconfig upload (`utils/auth/token_management.py:551`)
ignores it, so a user who turned prediscovery off still pays for one run per
cluster connection.

## 2026-10-02, correction: fixes were never graded

grade.py read only `aurora-rca.md`. Aurora's summary prompt forbids
recommendations (`chat/background/summarization.py:151`) and writes fixes to a
separate "Suggested Next Steps" list (`suggestions` in incident.json, from
`chat/background/recommender.py`). So "no remediation section" above, and "28 of
38 proposed no acceptable fix" in the first REPORT.md, were lab bugs, not Aurora
behavior. `runner/grade_fixes.py` grades the suggestions: in v0-run2 and v1-run1,
34 of 38 runs were acceptable, 2 partial (both 12 runs fixed only the OOM) and 2 had
no suggestions. In v0-fix-run1, 6 of 6 were acceptable.

The diagnosis was also graded on the report only. `runner/grade_full.py`
regraded every run on the report, investigation notes, suggestions and last chat
message. No v0-run2 or v1-run1 verdict moved; the v1 partials (11 r2, both 12,
19 r1) are misses in all of it. v0-run1/09 and v0-fix-run1/05 r2 went from
partial to correct. The `reviewer_ai` notes saying "No fix is stated" come from
the same report-only reading and are wrong.
