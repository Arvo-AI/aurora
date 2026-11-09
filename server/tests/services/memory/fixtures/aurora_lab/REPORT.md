# Aurora lab report, v1 (2026-10-02)

We broke a small Kubernetes app on purpose, gave Aurora the alert and read-only access to the cluster, and
scored its root cause analysis against the fault we injected. Arvo builds
Aurora, and Arvo wrote these scenarios. Read the numbers with that in mind.

Every verdict here started from an LLM judge, was checked by a second model,
and was then reviewed by a person at Arvo, who agreed with the full-output
verdicts below (`human_verdict` in each grade.json). The superseded v0-run1 was
not reviewed.

## Results

| Run | Scenarios | Runs | Correct | Partial | Wrong | Aurora cost |
|---|---|---|---|---|---|---|
| v0-run2 | 01-10 | 20 | 20 | 0 | 0 | $6.44 |
| v1-run1 | 11-19 | 18 | 14 | 4 | 0 | $8.09 |
| v0-fix-run1 | 05, 09, 10 on the fix branch | 6 | 6 | 0 | 0 | $2.24 |

The judge read only the RCA text (the incident's `summary`). Aurora also shows
the user investigation notes, a "Suggested Next Steps" list and the agent's last
chat message. `runner/grade_full.py` regraded every run on all of that
(`full_output` in each grade.json). No v0-run2 or v1-run1 verdict changed: the 4
v1 partials are misses in everything Aurora showed, not only in the report. Two
partials outside those runs became correct: v0-fix-run1/05 r2 (so the fix run is
6/6) and the superseded v0-run1/09.

The second model (AI review) read all 44 runs and disagreed with the judge
twice, once in each direction: v0-run2/07 r1 is partial in its view, and
v0-fix-run1/05 r2 is correct (details below). The AI review also read only the
RCA text, so its "No fix is stated" notes in grade.json are wrong; see
`fix_from_suggestions` for the fixes.

An answer counts as correct when it meets every `must_identify` item in the
scenario and asserts none of the listed wrong answers. Partial means some items
were met. A false claim outside the listed wrong answers does not make a run
wrong: both 12 runs are partial even though they said a broken Service was
healthy. Stating a fix is not required for "correct". Aurora puts fixes in a separate
"Suggested Next Steps" list (the incident's `suggestions`), not in the RCA text.
Graded by `runner/grade_fixes.py`, the suggestions held an acceptable fix in 34 of
38 runs. Both 12 runs were partial (they fixed only the OOM) and 2 runs had no
suggestions (v0-run2/04 r1, v0-run2/09 r2). The first version of this report said
28 of 38 runs proposed no acceptable fix. That was wrong: grade.py only read the
RCA text and never saw the suggestions list.

Per-run tables: `runs/v0-run2/report.md`, `runs/v1-run1/report.md`,
`runs/v0-fix-run1/report.md`.

### v1-run1 by scenario

| Scenario | Correct | Median time | Median cost |
|---|---|---|---|
| 11 api Service targetPort wrong, worker logs noisy but harmless | 1/2 | 340s | $0.51 |
| 12 two faults: worker OOM plus frontend Service selecting no pods; alert covers only the worker | 0/2 | 344s | $0.34 |
| 13 NetworkPolicy blocks api to redis | 2/2 | 323s | $0.61 |
| 14 ResourceQuota blocks the rollout's surge pod | 2/2 | 126s | $0.32 |
| 15 stray Deployment's pods carry app=api, 1 in 3 requests fail | 2/2 | 146s | $0.47 |
| 16 dnsConfig search domains for the wrong cluster domain | 2/2 | 172s | $0.48 |
| 17 slow memory leak in worker code, logs clean | 2/2 | 144s | $0.25 |
| 18 init container waits for a Service that does not exist | 2/2 | 159s | $0.44 |
| 19 ConfigMap edited, only one of two frontend pods restarted | 1/2 | 266s | $0.61 |

## Failures

**12, both runs: Aurora found the alerted fault and declared everything else
healthy without checking.** Both runs found the OOMKilled worker and the
BATCH_SIZE change. Neither ran `get svc` or `get endpoints`, and neither looked
at the load generator, which was getting errors from the frontend. r1's 18 tool
calls were pods, describe, logs, events, ReplicaSets, the worker Deployment,
a ConfigMap, a blocked `rollout history`, `top nodes` and nodes. Then the report
said:

> Other workloads in the `shop` namespace (api, frontend) remained healthy and unaffected [5, 14].

r2 said the same: "Other workloads in the namespace (api, frontend pods) were
running normally and unaffected [4]." Pod status was the only evidence for
both claims. A real second outage went unreported, and the report said it was
not there.

**19 r1: the right clue, the wrong conclusion.** Aurora read the frontend-nginx
ConfigMap, saw `server api:8000` against a Service on 8080, and tied it to the
failing pod. It did not work out that only the recreated pod had loaded the
edited ConfigMap. It tried to read the live config in the pod and the policy
blocked it:

> $ kubectl exec frontend-76d98597b9-g5zrg -n shop -c frontend -- cat /etc/nginx/conf.d/default.conf
> Command blocked by organization policy: Mutating kubectl operations

So the report left it open: "This discrepancy was not fully reconciled, the
live nginx configuration inside the running pod could not be inspected." Its own
`get pods` output had the clue it needed: the failing pod was 9 seconds younger
than the healthy one. r2 got it right with 28 tool calls.

**11 r2: graded partial for not checking the red herring.** Aurora found the
targetPort 9090 against the container's 8080. The worker's noisy errors are in
the scenario to tempt a wrong answer. Aurora never looked at the worker: "the
redis, worker, and loadgen deployments ... were not examined". The judge and
the AI review both marked "the worker's errors are not the cause" as partial.
You could argue this run deserves "correct", since it was not misled. We kept
the rubric as written and report the verdict.

**v0-run2/07 r1: judge and AI review disagree.** The judge said correct. The
AI review said partial: the root cause never names the selector value
`app=api-v2`, only that the selector "does not currently align with the running
pods", even though Aurora had read `selector: app: api-v2`. This is one of the
three runs flagged for a person to check.

**v0-run1/09 (superseded run, kept for the record).** Aurora found the PVC
with the missing StorageClass by step 10. Then it made 25 `write_artifact`
calls, many with no `content` argument, and some with test strings like
"placeholder" and "test2". That took about 11 of 15 minutes and $2 of $2.61. The
report's root cause never named the PVC ("a dependency the cluster could not
satisfy"), listed the PVC only under "Not checked", and said redis was "running,
not ready" (it was Pending). The investigation notes and the suggested next
steps did name the PVC and the missing `fast-ssd` StorageClass, so graded on
everything Aurora showed, this run is correct. Causes, from the fix branch work:
Aurora sets no `max_tokens` for Bedrock, 19 agent responses during the loop
stopped at exactly 4096 output tokens (the Converse default, by inference), and
the tool arguments were
cut off mid-`content`; pydantic rejected the call before the tool ran, so the model
only saw "Field required"; and artifact writes counted as evidence, which
pushed the real evidence out of the summary's last-30-citations window. This
did not happen again in v0-run2 (0 `write_artifact` calls in 20 runs).

## Patterns across the 38 graded runs

- `kubectl rollout history` was blocked by the default `observability_only`
  policy in 26 of 38 runs (16 of 20 in v0-run2, 10 of 18 in v1-run1; 27 calls). The deny rule matches the
  whole `rollout` verb. In v0-run1/05 Aurora repeated the policy text and
  called `rollout history` "a mutating operation" in its report.
- Fixes: 34 of 38 runs suggested an acceptable fix, 2 partial (12), 2 none (see Results).
- The AI review found claims the evidence does not support in 15 of the
  18 v1 reports, none of them part of the root cause: "both cluster nodes" (there are
  three), "pulled successfully" for an image that had no Pulled event, a quota
  timeline with the rollout before the quota, a worker that "processed
  Redis-backed batches" when the worker never connects to redis, and "34Mi and
  climbing" from one sample.
- Aurora's "Not checked" sections were honest. When it could not verify
  something it said so instead of guessing.

## Fix branch (v0-fix-run1)

Branch `lab/rollout-readonly` on 87d22baf, three commits, one patch each in
`patches/` (details and tests in `patches/NOTES.md`):

1. `0001`: the policy denies only `rollout (restart|undo|pause|resume)` and
   allows `rollout (history|status)`.
2. `0002`: a `write_artifact` call with cut-off arguments now reaches the tool
   and returns an error that says it was cut off, instead of a pydantic
   "Field required" that the model kept retrying.
3. `0003`: artifact and memory write receipts no longer count as evidence for
   the summary's last-30-citations window.

Images `aurora_lab_server:fix`, `aurora_lab_celery-worker:fix` and
`aurora_lab_chatbot:fix` (one image, b3f656ac73a8). New orgs get the new policy,
because the default policy is seeded per org from code. The 01 rerun was cut for
time, as the brief's order allows.

| Scenario | v0-run2 (87d22baf) | v0-fix-run1 (fix) | Median time, before / after | Median cost, before / after |
|---|---|---|---|---|
| 05-nginx-config-error | 2/2 | 2/2 | 136s / 145s | $0.32 / $0.36 |
| 09-redis-pvc-pending | 2/2 | 2/2 | 280s / 182s | $0.39 / $0.44 |
| 10-wrong-redis-host | 2/2 | 2/2 | 244s / 100s | $0.28 / $0.32 |

What the run shows: `rollout history` ran 14 times across the 6 runs and was
never denied. In v0-run2 it was denied in 16 of 20 runs, including 5 of the 6
runs of 05, 09 and 10. Aurora used it, for example
`kubectl rollout history deployment/redis -n shop --revision=...` in 09.

What it does not show:
- Patches 0002 and 0003 were not exercised. The artifact loop never came back
  in v0-run2 or here (0 `write_artifact` calls in 26 runs), so only their unit
  tests support them.
- The time differences mostly come from the prediscovery contention the
  v0-run2 runs had and this run did not, not from the fix.
- 05 r2 was partial on the report alone. It named the `proxy_conect_timeout`
  typo in the frontend-nginx ConfigMap but described only "fail parsing its
  configuration at startup and exit with code 1", without the
  `[emerg] unknown directive` line, which was in the logs it fetched. Graded on
  Aurora's full output it is correct, which is the verdict we kept. It has
  nothing to do with the patches.

## Method

**Cluster.** kind, 3 nodes (1 control plane, 2 workers), on OrbStack
on a Mac. Three identical clusters (aurora-lab, aurora-lab-2, aurora-lab-3)
ran scenarios in parallel, one per lane. metrics-server v0.9.0 is installed
(with `--kubelet-insecure-tls`). It was absent in v0-run1, so `kubectl top`
now works.

**App.** `shop`: nginx frontend, a Python api, a worker, redis and a load
generator. It is synthetic and has no customer code or data. Names and labels
do not hint at the fault.

**Run.** For each (scenario, repeat): reset the app, inject the fault with
`break.sh`, wait until `check.sh` proves the fault is visible, capture the
cluster state, then create a new Aurora user and org, upload a kubeconfig, and
send the alert text. Runs were repeat-major across 3 lanes with a 20 s stagger.

**Isolation.** Every run had its own user and org, so there was no shared
memory, artifacts or incident history. Each lane's kubeconfig context is
renamed to `kind-aurora-lab` so all lanes look the same to Aurora. The runner
checks the output of Aurora's first `list_incidents` call for any other run's
incident ID. All 38 runs were clean. v0-run1 used one shared org, and in
scenario 10 Aurora cited "prior incidents", which is why it is superseded.

**Aurora.** Commit `87d22baf`. Images `aurora_lab_server:87d22baf`
(bd2669168458), `aurora_lab_celery-worker:87d22baf` (67ea14b53c9a),
`aurora_lab_chatbot:87d22baf` (2d7e0dc6483c), built for the lab. The server
code is bind-mounted from that commit. RCA model
`bedrock/us.anthropic.claude-sonnet-5`. Default `observability_only` command
policy, read-only cluster access.

**Grading.** A judge (`us.anthropic.claude-opus-5-5`) marks each required
finding as met, partial or missed, quotes Aurora, and checks the scenario's
listed wrong answers. A second Claude instance then read every RCA, the tool
outputs and the judge's grade, and wrote its own verdict under `reviewer_ai`
in each `grade.json`.

**What is not controlled.**
- Prediscovery. Each kubeconfig upload schedules an Aurora "prediscovery"
  agent run 10 minutes later. These runs share the 4 celery worker slots with
  the RCAs. The lab tried to revoke them from 12:53 ET, but the revoker called
  a Celery option that does not exist and ignored the error, so it did nothing
  until it was fixed at 16:59 ET. So all of v0-run2 and most of v1-run1 ran
  next to prediscovery jobs. They are in separate orgs, so this should not
  affect correctness, but durations are inflated. Runs that waited for a slot
  include v0-run2/08 r1 (362 s), v0-run2/09 r1 (406 s), and v1-run1 11 r2, 12 r2
  and 13 r2 (444 to 548 s). v0-fix-run1 ran with working revocation.
  Prediscovery writes what it finds to org memory, but it starts 600 s after
  the upload and runs for 6 to 9 minutes. Each RCA was triggered right after
  its upload and took at most 548 s, so no RCA could have read that output.
  Aurora has a per-user `prediscovery_enabled` preference, but only the
  periodic scheduler checks it (`chat/background/prediscovery_task.py:37`); the
  run scheduled on kubeconfig upload does not.
- Bedrock latency and model sampling. n=2 per scenario does not measure
  variance.

## Costs

From Aurora's `llm_usage_tracking` table, 12:00 to 17:35 ET. The RCA rows match the sum of `cost_usd` in the runs' `result.json` files ($16.77 against $16.81).

| What | Cost |
|---|---|
| RCA sessions, v0-run2 (20 runs) | $6.44 |
| RCA sessions, v1-run1 (18 runs) | $8.09 |
| RCA sessions, v0-fix-run1 (6 runs) | $2.24 |
| Sessions not tied to an incident: prediscovery (30 completed runs $29.70, plus a few killed part-way) | $30.38 |
| Judge calls, 44 at about $0.15 each (estimate, not metered) | about $6.60 |
| **Total** | **about $54** |

Median RCA cost was $0.32 (v0) and $0.40 (v1). Prediscovery cost about $1 per
cluster connection, almost twice what all 44 RCAs together cost
($30.38 against $16.77).

## Limits

- kind is not production. Three nodes, one namespace, one small app, no
  real traffic and no history. Production clusters are noisier, which is
  harder for Aurora, and also have dashboards and runbooks, which Aurora
  can use.
- The vendor wrote the scenarios and graded its own product. The v1 scenarios
  were written to be harder (noise, two faults, slow leaks, partial restarts),
  but we still chose them.
- n=2 per scenario. 20/20 on v0 does not mean the error rate is 0.
- One person reviewed the verdicts, and that person works at Arvo. The judge
  is an LLM. The second review is also an LLM, from the same model family.
- One scenario ground truth was corrected before any Aurora run (19: the
  failing pod returns 502 or 504, not only 502). No scenario was changed after
  seeing Aurora's answer.
