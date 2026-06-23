# Pre-incident Detection Artifact — Independent Audit

**Artifact version:** 14 (run 14, 2026-06-19)
**Audit date:** 2026-06-19
**Method:** Direct kubectl/gcloud queries against production cluster, independent of artifact claims.

---

## Audit Results

| # | Finding | Artifact Severity | Verdict | Notes |
|---|---------|:-:|---|---|
| 26 | Both server pods co-located on same node | Critical | **Partially valid** | Anti-affinity gap is real (confirmed empty). Pods were co-located at scan time but are on separate nodes now. Structural risk confirmed; has caused incidents (Jun 18-19). |
| 22 | AWS IAM AccessDeniedException — GitHub App JWT minting | Critical | **Valid (confirmed by user)** | Not reproducible in current logs (pods restarted since), but user confirmed this was a real issue they fixed. |
| 1 | Server pod memory leak — `--preload` | Critical | **Overstated root cause** | `--preload` is present, memory request is too low (512Mi), no `--max-requests`. But `--preload` is not the leak cause. Real issue: unbounded module-level caches (`_mcp_tools_cache`, `_langchain_tools_cache`) + no worker recycling. Memory WILL grow but blaming `--preload` is inaccurate. |
| 17 | Autoscaler churn — nodes cycling every 2-4 min | High | **Confirmed, root cause wrong** | Churn is real and active (observed live). But blaming server memory request as "the primary lever" is inaccurate — it's Autopilot's optimize-utilization behavior. |
| 29 | Celery worker `gkz88` memory spike to 1112Mi | High | **Wrong diagnosis** | WorkerLostError is real but memory is NOT leaking. `max_tasks_per_child=50` already configured. 1240Mi is normal for 5 Python processes (concurrency=4). |
| 23 | Cluster-wide CNI/network instability | High | **Confirmed but overstated severity** | CNI errors only hit DaemonSet pods on churning nodes. Zero impact on aurora namespace app pods. |
| 4 | Celery worker memory — diverging between workers | High | **Wrong diagnosis** | Memory is normal structural cost (~285MB x 5 processes). Not a leak. `max_tasks_per_child=50` is already recycling subprocesses. |
| 19 | Server pods — no anti-affinity rule | High | **Duplicate of #26** | Same finding, different number. Valid. |
| 18 | external-secrets webhook: 2Gi request, 27Mi actual | High | **Confirmed** | Actual pod is the main `external-secrets` pod (not webhook as claimed), but 2Gi request with 41Mi actual usage is verified. |
| 11 | kubectl-agent readiness probe — recurring 503s | High | **Not yet audited** | |
| 2 | Server readiness probe timeouts | High | **Not yet audited** | |
| 3 | SQL syntax error in Slack events handler | High | **Not yet audited** | |
| 5 | Redis persistence disabled | High | **Not yet audited** | |
| 6 | GKE balloon pods causing node memory overcommit | High | **Not yet audited** | |
| 28 | One undeployed commit on main | Medium | **Stale** | Image is now `sha-00d9699` (that commit deployed). Artifact was out of date at time of audit. |
| 20 | Webhook handlers generating `No user_id or org_id` warnings | Medium | **Not yet audited** | |
| 21 | `[Prefs:get] Missing org_id` ERROR in celery worker | Medium | **Not yet audited** | |
| 7 | Frontend probes misconfigured — redirect warnings | Medium | **Not yet audited** | |
| 8 | PVCs zone-pinned to us-west1-a | Medium | **Not yet audited** | |
| 9 | Celery race condition on incident status | Medium | **Not yet audited** | |
| 10 | Gunicorn fork/thread deprecation | Medium | **Not yet audited** | |
| 12 | Memgraph `--memory-limit` below container limit | Low | **Not yet audited** | |
| 13 | Weaviate memory limit low relative to PVC | Low | **Not yet audited** | |
| 14 | Cloud SQL — no read replica | Low | **Not yet audited** | |
| 15 | Repeated unauthorized probes | Low | **Not yet audited** | |
| 24 | Image streaming errors for ghcr.io | Low | **Not yet audited** | |

---

## Detailed Audit Notes

### #26 — Server Pod Co-location (Anti-Affinity Gap)

**Claimed:** Both server pods on same node `pxv7`, no anti-affinity configured.

**Verified:**
- `affinity` field on deployment is empty (confirmed)
- `topologySpreadConstraints` is also empty (confirmed)
- At audit time, pods are on DIFFERENT nodes (`wbb7` and `tprr`) — the co-location was transient
- Frontend deployment HAS anti-affinity configured in `values.generated.yaml`, server does not
- Jun 18-19 incident pattern (14 alerts, emergency deploys) is consistent with co-location causing repeated outages

**Verdict:** The structural gap is real and has caused production incidents. The "both on same node right now" claim was true at scan time but is transient. Linear ticket DEV-1295 created.

---

### #22 — AWS IAM AccessDeniedException

**Claimed:** `aurora-eso-reader` lacks permission for `aurora/system/*` path, breaking GitHub App discovery.

**Verified:**
- No AccessDeniedException in current server logs (pods are ~2.5h old, nobody triggered the flow since)
- GitHub App config loads successfully (`enabled`, client_id present)
- User confirmed this was a real issue they fixed

**Verdict:** Valid at the time. Now fixed.

---

### #1 — Server Memory Leak / --preload

**Claimed:** `--preload` breaks copy-on-write, causes workers to accumulate memory independently toward 4Gi.

**Verified:**
- `--preload` IS present in the gunicorn command
- Memory request: 512Mi, limit: 4Gi (confirmed)
- Current usage: 493Mi and 425Mi at ~2.5h pod age
- No `--max-requests` configured (workers never recycle)
- Connection pool already handles fork correctly (detects PID change)
- Real leak vectors found: `_mcp_tools_cache`, `_langchain_tools_cache`, `_user_credentials_cache` — all unbounded dicts with TTL but no maxsize
- `RealMCPServerManager` holds subprocess handles that may not be cleaned up

**Verdict:** Memory growth is real but root cause attribution is wrong. `--preload` itself doesn't "break copy-on-write" in a meaningful way here — the issue is unbounded caches and no worker recycling. The fix recommendations (remove `--preload`) are partially off-target. Better fixes: add `--max-requests`, bound cache sizes, raise memory request.

---

### #17 — Autoscaler Churn

**Claimed:** Nodes cycling every 2-4 minutes, worst observed rate across 14 runs. Root cause: server memory request too low driving balloon displacement.

**Verified:**
- 5 nodes at time of second check (down from 8 earlier). Only 1 (`7j3x`) older than today (from Jun 13).
- Node `7z5n` was scale-down removed during audit (utilization 0.5%). Node `6nrj` appeared 10 seconds before check.
- ScaleDown event observed live: "removing node, utilization: 0.005"
- CNI NetworkNotReady event firing 1 second before check (new node not yet initialized)
- Every node at 99% memory REQUESTS, but only 6.5 GiB of 27.6 GiB is actually used per node
- Balloon pods fill 53-94% of each node's memory requests (14.7-25.8 GiB)
- Nodes `x4bj` and `9tkn` have 94%/93% balloon with almost no app pods — they're "spare capacity" nodes the autoscaler keeps provisioning then removing

**Root cause analysis:**
The artifact claims server memory request (512Mi vs actual ~450Mi) is "the primary lever." This is WRONG as the primary driver. The actual numbers:
- Total app pod requests: 9.1 GiB across all pods
- Total actual usage: 6.5 GiB
- The mismatch that matters is NOT server specifically — it's the GKE Autopilot `optimize-utilization` profile combined with balloon pods. The autoscaler provisions nodes, balloon fills them to 99%, then determines the real workload is minimal and removes them.
- The real churn driver is GKE Autopilot's aggressive consolidation behavior + the fact that some nodes end up with only system pods after app pods get moved during consolidation.

The server memory request IS slightly low (512Mi for 450-500Mi actual), but raising it alone won't stop the churn. The churn is an autoscaler behavior pattern, not a memory request problem.

**Verdict:** Churn is confirmed and active. Root cause attribution in the artifact is partially wrong — it's an Autopilot scheduling behavior, not primarily caused by server memory request mismatch.

---

### #29 — Celery Worker Memory Spike / WorkerLostError

**Claimed:** Worker `gkz88` jumped from 739Mi to 1112Mi after a WorkerLostError during a heavy RCA session ($8.02 cost, 322 stream events). Fix: add `--max-tasks-per-child 10`.

**Verified:**
- `worker_max_tasks_per_child=50` is ALREADY configured in `celery_config.py`
- Current workers have completed 131 and 112 tasks respectively — subprocess recycling IS happening
- Process tree shows 5 Python processes (1 parent + 4 children at concurrency=4)
- Each process loads ~285MB of application code at baseline
- 5 x 285MB = ~1425MB minimum structural cost just to exist
- Active child running heavy LLM task grows to 543MB temporarily, then gets recycled after 50 tasks
- WorkerLostError (exit code 0) is billiard killing a child — likely timeout-related during LLM API calls, NOT memory pressure

**Verdict:** Wrong diagnosis. Memory is NOT leaking. 1240Mi is the normal operating cost of 5 Python processes running the full Aurora application. The suggested fix (`--max-tasks-per-child`) is already in place. No action needed.

---

### #23 — Cluster-Wide CNI/Network Instability

**Claimed:** NodeNotReady and CNI initialization failures firing continuously. NetworkPluginNotReady events. "High" severity.

**Verified:**
- NodeNotReady warnings ARE firing (7m50s before check) across DaemonSet pods on churning nodes
- NetworkNotReady / NetworkPluginNotReady events confirmed firing (4 minutes before check)
- However: ALL these events are on **kube-system DaemonSet pods on nodes being added/removed**
- Zero CNI or NetworkNotReady events in the `aurora` namespace
- All aurora app pods are healthy, running, 0 restarts, stable networking
- The CNI errors are the transient initialization noise of newly-joining nodes (takes ~1-2 minutes for CNI to become ready on a fresh node)

**Root cause:** This is a SYMPTOM of the autoscaler churn (#17), not an independent finding. When a new node joins, there's a brief window where CNI isn't ready — that's normal Kubernetes node initialization.

**Verdict:** The events are real, but calling this "cluster-wide CNI/network instability" is an over-exaggeration. It's normal transient noise from node initialization during autoscaler churn. It has ZERO impact on running application pods. Should be Low/Informational at most, not High.

---

### #4 — Celery Worker Memory Divergence

**Claimed:** `gkz88` at 1112Mi, `7kr67` at 766Mi — diverging memory between workers. Fix: add `--max-tasks-per-child 10`.

**Verified:**
- `worker_max_tasks_per_child=50` already configured and working
- Current workers: `8gjzt` at 1240Mi, `tqj6x` at 1265Mi (basically identical, not diverging)
- Process tree: 5 Python processes x ~285MB baseline = ~1425MB structural floor
- Active child temporarily grows during heavy tasks, then gets recycled
- Previous "divergence" was just one worker having an active child running a heavy task vs the other being idle

**Verdict:** Wrong diagnosis. Not a leak, not a risk. This is normal memory for the configured concurrency. No action needed.

---

### #18 — external-secrets Over-Requesting Memory

**Claimed:** external-secrets webhook declares 2Gi request, uses 27Mi.

**Verified:**
- Main `external-secrets` pod (not the webhook) has 2Gi memory request
- Actual usage: 41Mi (main), 25Mi (webhook), 54Mi (cert-controller)
- The specific pod identification was slightly wrong (main pod, not webhook)

**Verdict:** Substance confirmed — massive over-declaration. Pod name was misidentified.

---

### #28 — Undeployed Commit

**Claimed:** `00d96994` (Slack notifications) undeployed, prod still on `sha-3269b7a`.

**Verified:**
- Current server image is `ghcr.io/arvo-ai/aurora-server:sha-00d9699`
- This IS the Slack notifications commit — it has been deployed
- `values.generated.yaml` still says `sha-3269b7a` (file not updated to match actual deploy)

**Verdict:** Stale at time of audit. The commit was deployed between the scan and this audit.
