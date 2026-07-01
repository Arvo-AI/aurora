# Independent Validation of Aurora RCAs — "User-Facing 5xx Errors"

**Author:** Blind re-investigation using only live GCP (Cloud Logging, k8s events, Cloud SQL logs, kubectl).
**Method:** Aurora's incident records were NOT consulted until after each independent root cause was derived from raw evidence. Each Aurora RCA is then scored against that evidence.
**Date of review:** 2026-06-26
**Scope:** The recurring `Aurora Prod - User-Facing 5xx Errors` alert (`aurora_user_facing_5xx`, "> 5 in 5 min"). ~21 firings 06-19 → 06-26; 5 representative incidents deep-dived below.

---

## TL;DR

- The recurring alert does **not** have one root cause. There are **three genuinely distinct causes** across different firings.
- Aurora **correctly identified the right cause per-incident in 4 of the 5** reviewed. No outright hallucination of a fabricated cause was found.
- The one **partial** case (INC-646) is a scoping error, not a fabrication: Aurora reported a real, correctly-evidenced cause for that window but over-generalized it as *the* explanation for the whole recurring pattern.
- The single most expensive failure mode is **redundant rediscovery**: each firing re-runs an expensive blind log hunt (one cost **$14.60 / 4.6M tokens**), because the alert payload carried no specifics. This is what the `metrics.tf` label-enrichment PR (#66) addresses.

---

## Per-incident comparison

| # | Incident / Time (UTC) | Aurora's stated root cause | My independent finding (live GCP) | Verdict | Evidence agreement |
|---|---|---|---|---|---|
| 1 | **INC-615** — 06-25 08:27 | Next.js frontend emits duplicate `Transfer-Encoding: chunked` on `/ingest/*` PostHog proxy → nginx 502. Fix = PR #551 (`proxy_hide_header`). | nginx logs in window: 502s on `/ingest/*`, upstream = frontend:3000, `upstream sent duplicate header line: Transfer-Encoding`. | **Correct** | Full. Error string, endpoints, upstream all match. |
| 2 | **INC-624** — 06-25 18:46 | GKE autoscaler (`OPTIMIZE_UTILIZATION` + NAP) evicting pods → upstream unreachable → 502 w/ 0 upstream bytes. | k8s events show `ScaleDown` evicting `aurora-oss-server-...-lxl87` @18:10 (+ chatbot, searxng). 502s with empty/0-byte upstream. | **Correct** | Full. ScaleDown events confirmed in window. |
| 3 | **5e167341** — 06-25 20:10 | DB connection pool exhaustion in `aurora-oss-server` (`psycopg2.pool.PoolError`), probe failures, exit 137; attributed to commit `faebae42` ("perf: reduce chat latency"). | server container logs: continuous `psycopg2.pool.PoolError: connection pool exhausted` from ~21:55; startup/readiness probes fail; crash-loop. (Same signature I traced for the 22:00–22:33 503 storm.) | **Correct** | Full on mechanism. Commit attribution not independently re-verified (code-level), but error signature exact. |
| 4 | **f34fd3d5** — 06-25 22:34 (slow-requests sibling) | New server pod 7 consecutive startup-probe failures (connection refused :5080); 1.6 GB image → 1m45s pull; single remaining pod overloaded; MCP `httpx.ConnectError`. | k8s events: `Startup probe failed: connection refused` repeatedly 22:00–22:44; image pull ~1m45s; `ScaleDown` @22:34/22:42. Root driver upstream = pool exhaustion (last `pool exhausted` @22:33:58). | **Correct (proximate)** | Full on proximate cause. Aurora described the symptom layer accurately; the DB-pool driver (incident 3) is the deeper cause. |
| 5 | **INC-646** — 06-26 14:17 | Duplicate `Transfer-Encoding` on `/ingest/*` is THE root cause of the recurring 5xx; "same defect drove four firings on 06-25 incl. one with 162 errors." | This window: 11 5xx, **all 502 on `/ingest/*`** via frontend — Aurora's cause is exactly right *for this window*. BUT the dominant 06-25 storm (393× `/github/webhook` 503) was DB-pool/crash-loop, NOT Transfer-Encoding. | **Partial** | Correct for the window; **over-generalized** to the whole pattern. |

---

## Where Aurora was right vs. where it slipped

**Right (no hallucination):**
- Every per-incident proximate cause matched raw evidence. Aurora ran real `gcloud logging` / `kubectl` and cited real error strings (duplicate Transfer-Encoding, `psycopg2.pool.PoolError`, `ScaleDown`, startup-probe `connection refused`). None were invented.
- It correctly distinguished *three separate causes* across firings rather than forcing one narrative — except in case 5.

**Slipped (partial, INC-646):**
- Claimed the Transfer-Encoding `/ingest` defect was *the* recurring root cause and that it "drove four firings on 06-25." Independent evidence shows the **largest** 06-25 firing (the 22:00–22:33 burst) was **393 `/github/webhook` 503s from server crash-loop / DB-pool exhaustion**, a different cause on a different upstream. So the generalization is wrong even though the specific 14:17 window was correctly diagnosed.
- Root of the slip: with no error detail in the alert payload, each RCA starts blind and tends to anchor on whatever 5xx happens to be most visible in its narrow lookback (here, the always-present `/ingest` noise), then assumes continuity with past firings.

---

## Independent ground-truth: 7-day 5xx population (sampled 500)

| Status | Count | | Top path | Count | Upstream |
|---|---|---|---|---|---|
| 503 | 378 | | `/github/webhook` | 393 | aurora-oss-server (393) |
| 500 | 63 | | `/api/grafana/status` | 36 | aurora-oss-frontend (106) |
| 502 | 59 | | `/ingest/*` (all) | ~58 | aurora-oss-chatbot (1) |

- **76% of all 5xx** = `/github/webhook` 503 (GitHub-Hookshot retries amplify a server outage). Concentrated in the **06-25 22:19–22:33** crash-loop window.
- `/ingest/*` 502 (the Transfer-Encoding class) is a **persistent minority (~12%)** — real, but not the driver of the big bursts.

---

## Net assessment

| Metric | Result |
|---|---|
| Incidents reviewed | 5 (of ~21) |
| Outright hallucinations (fabricated cause) | **0** |
| Fully correct | **4** (INC-615, INC-624, 5e167341, f34fd3d5) |
| Partial / over-generalized | **1** (INC-646) |
| Wrong | 0 |

**Conclusion:** Aurora is **not hallucinating root causes** on these incidents — its per-incident diagnoses are evidence-backed and accurate. Its real weakness is **cross-incident generalization** (assuming one recurring cause) and **cost from blind rediscovery** (no specifics in the alert payload). Both are mitigated by enriching the alert with the offending method/path/status/upstream (PR #66): it would have immediately shown `/github/webhook 503 server` vs `/ingest 502 frontend`, preventing the INC-646 mis-generalization and cutting the per-incident token spend.

---

### Sources (all independently pulled)
- nginx ingress 5xx access logs (`ingress-nginx/controller`, 7d + per-incident windows)
- k8s events (`Startup probe failed`, `Readiness probe failed`, `ScaleDown`, `Killing`) for `aurora-oss-server`, 06-25 22:00–22:45
- `aurora-server` container logs 06-25 21:55–22:08 (`psycopg2.pool.PoolError: connection pool exhausted`)
- Cloud SQL `aurora_db` logs 06-25 21:45–22:45 (`FATAL: connection to client lost`, `deadlock detected`)
- Aurora MCP `get_incident` records: c44c70d6 (INC-646), 5e167341, f34fd3d5, b8d39ac6 (INC-624), 72038512 (INC-615)
