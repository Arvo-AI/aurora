# Provenance

Every value the verifier reads comes from `aurora-lab` at one commit. This table is the human-readable half of `contracts/scenario_12.yaml`: the YAML carries the machine-readable `source` on each check and fix, and this file carries the words and the line a reviewer can go read.

Pinned commit: `4edcd9dfbc48cbd9ca8ea031e429832cf72842d0` in `Arvo-AI/aurora-lab`, committed 2026-11-06T00:30:13Z, read on 2026-11-15.
Fork base: `kpnaranj/aurora` at `12c67cffd45c6b0b8c5da1d2e160a945e2936f49`, the merge base with `Arvo-AI/aurora:main`.

Any row can be checked without a local checkout:

```bash
git clone https://github.com/Arvo-AI/aurora-lab
git -C aurora-lab checkout 4edcd9dfbc48cbd9ca8ea031e429832cf72842d0
sed -n '3p' aurora-lab/scenarios/12-two-faults-oom-and-selector/scenario.yaml
```

Two rules apply to every row below. A value with no row is not used. A quote is exact, and a quote marked as trimmed ends at the end of the cell.

## The incident

| Value in the contract | Path | Anchor | Quote |
|---|---|---|---|
| `agent_view.alert` | `scenarios/12-two-faults-oom-and-selector/scenario.yaml` | `#L3` | `KubePodCrashLooping [warning] namespace=shop deployment=worker: Pod shop/worker (container worker) is in waiting state (reason: CrashLoopBackOff).` |
| first fault | `scenarios/12-two-faults-oom-and-selector/scenario.yaml` | `#L9-L10` | `Fault 1 (alerted): the worker's BATCH_SIZE went from 500 to 2500, about 250 MB per batch against a 192Mi limit, so the container is OOMKilled.` |
| second fault | `scenarios/12-two-faults-oom-and-selector/scenario.yaml` | `#L10-L13` | `Fault 2 (not alerted): the frontend Service selects app=frontend,tier=web, which no pod carries, so it has no endpoints and every request to the frontend fails` |
| what a fix must identify | `scenarios/12-two-faults-oom-and-selector/scenario.yaml` | `#L15-L16` | `Separately, the frontend Service has no endpoints because its selector (tier=web) matches no pods, so the frontend is down for users` |
| `fix: full` | `scenarios/12-two-faults-oom-and-selector/scenario.yaml` | `#L17-L18` | `Revert BATCH_SIZE to 500 (or raise the limit with the reason stated) and set the frontend Service selector back to app=frontend` |
| the wrong answer the checks reject | `scenarios/12-two-faults-oom-and-selector/scenario.yaml` | `#L19-L20` | `Treating the frontend failure as a consequence of the worker crash` |
| first fault injection | `scenarios/12-two-faults-oom-and-selector/break.sh` | `#L1` | `kubectl -n shop set env deploy/worker BATCH_SIZE=2500` |
| second fault injection | `scenarios/12-two-faults-oom-and-selector/break.sh` | `#L2` | `kubectl -n shop patch svc frontend -p '{"spec":{"selector":{"app":"frontend","tier":"web"}}}'` |
| first fault in the broken state | `scenarios/12-two-faults-oom-and-selector/check.sh` | `#L1` | `lastState.terminated.reason` and `grep -q OOMKilled` |
| second fault in the broken state | `scenarios/12-two-faults-oom-and-selector/check.sh` | `#L2` | `kubectl -n shop get endpointslices -l kubernetes.io/service-name=frontend -o jsonpath='{.items[*].endpoints[*].addresses[*]}'` |

## The workload

| Value in the contract | Path | Anchor | Quote |
|---|---|---|---|
| namespace | `app/shop.yaml` | `#L10` | `name: shop` |
| worker Deployment | `app/shop.yaml` | `#L174` | `name: worker` |
| worker container | `app/shop.yaml` | `#L185` | `- name: worker` |
| worker pod labels | `app/shop.yaml` | `#L182` | `labels: {app: worker}` |
| `BATCH_SIZE` in the manifest | `app/shop.yaml` | `#L189` | `- {name: BATCH_SIZE, value: "500"}` |
| worker memory limit | `app/shop.yaml` | `#L192` | `limits: {memory: 192Mi}` |
| memory per batch | `app/shop.yaml` | `#L72` | `buf = [bytearray(100 * 1024) for _ in range(batch)]` |
| `processing_evidence.pattern` | `app/shop.yaml` | `#L73` | `print(f"processed batch of {len(buf)} items", flush=True)` |
| seconds per batch | `app/shop.yaml` | `#L75` | `time.sleep(5)` |
| `http_expected_response` body | `app/shop.yaml` | `#L48` | `self._send(200, {"items": ["kettle", "mug", "teapot"]})` |
| nginx upstream | `app/shop.yaml` | `#L85` | `server api:8080;` |
| nginx route to the api | `app/shop.yaml` | `#L91` | `proxy_pass http://api/;` |
| frontend Service selector | `app/shop.yaml` | `#L232` | `selector: {app: frontend}` |
| frontend Service port | `app/shop.yaml` | `#L233` | `ports: [{port: 80, targetPort: 80}]` |
| frontend readiness probe | `app/shop.yaml` | `#L215-L217` | `httpGet: {path: /healthz, port: 80}` |
| the client the http check imitates | `app/shop.yaml` | `#L256-L257` | `wget -q -S -O /dev/null -T 3 http://frontend/api/items` and `GET /api/items -> ${code:-timeout}` |

## The six checks

| Check | Type | Path | Anchor | Why it fails at baseline and passes after a fix within the contract |
|---|---|---|---|---|
| `no_oomkilled_in_window` | k8s | `scenarios/12-two-faults-oom-and-selector/check.sh` | `#L1` | The lab proves the fault by finding `OOMKilled` in the last termination reason. The pass condition is the absence of that reason on a current worker pod, and no new occurrence inside the window. |
| `restart_count_not_increasing` | k8s | `scenarios/12-two-faults-oom-and-selector/README.md` | `#L202` | The runbook asks for no new restarts. The contract requires 0 at the start of the window and 0 at the end (D11), so a pod that is still crash-looping cannot pass while a backoff is long. |
| `processing_evidence` | logs | `app/shop.yaml` | `#L73` | The worker prints one line per batch. Two lines inside the window match the runbook's two cycles (`scenarios/12-two-faults-oom-and-selector/README.md#L202`), and the number in the line is compared with the `BATCH_SIZE` read from the Deployment (`app/shop.yaml#L189`), so the alternative fix that keeps 2500 is not rejected for the wrong reason. |
| `service_has_endpoints` | k8s | `scenarios/12-two-faults-oom-and-selector/check.sh` | `#L2` | The broken-state check asserts the address list is empty, so a non-empty list is the pass condition. |
| `endpoints_ready` | k8s | `scenarios/12-two-faults-oom-and-selector/check.sh` | `#L2` | The same slice. An address that never becomes ready still routes nothing, so the flag is checked as well as the count. |
| `http_expected_response` | http | `app/shop.yaml` | `#L256-L257` | A request through the Service is the condition users care about, and the body comes from `app/shop.yaml#L48`. Three requests, two seconds apart, five second timeout, one status and one body expected. |

## The fixes

| Fix | Path | Anchor | Why this is the fix |
|---|---|---|---|
| `full` | `scenarios/12-two-faults-oom-and-selector/scenario.yaml` and `scenarios/12-two-faults-oom-and-selector/README.md` | `#L17-L18`, `#L164`, `#L170`, `#L180` | The scenario allows reverting `BATCH_SIZE` and removing the selector key. The runbook fixes the user-facing fault first (`#L164`), removes the key with a JSON patch because a strategic merge keeps it (`#L170`), then reverts the variable with the verb that changed it (`#L180`). |
| `partial_worker_only` | `scenarios/12-two-faults-oom-and-selector/break.sh` | `#L1` | The inverse of the first injected change: the same verb, back to 500. |
| `partial_frontend_only` | `scenarios/12-two-faults-oom-and-selector/break.sh` | `#L2` | The inverse of the second injected change: the key the patch added is removed. |
| `noop` | none | none | Deliberate absence of remediation (D6). No path exists because nothing changed. |

## The observation window

| Value in the contract | Path | Anchor | Why 60 seconds |
|---|---|---|---|
| `oracle_view.observation.window_seconds` | `scenarios/12-two-faults-oom-and-selector/README.md` | `#L202` | The runbook watches the worker through two batch cycles of five seconds. The window is 60 seconds so a rollout, one restart and two batch lines all fit inside it. |

## Supporting sources

The observation layer reads two things that no check consumes. They are listed here because every cited path needs a row (D20).

| Used by | Path | Anchor | Quote |
|---|---|---|---|
| `logs.loadgen` | `app/shop.yaml` | `#L236-L243` | `name: loadgen` |

## Aurora's claim

This row is a README citation, not a verifier input. It is here because the evaluator asks for the claim and its source in one place.

| Claim | Path | Anchor | Public source |
|---|---|---|---|
| run r1 called the frontend healthy from pod status alone | `scenarios/12-two-faults-oom-and-selector/README.md` | `#L238` | https://www.aurorasre.ai/blog/aurora-lab-report-kubernetes-19-failures (read 2026-11-15) |

The sentence quoted in the runbook is `Other workloads in the shop namespace (api, frontend) remained healthy and unaffected [5, 14].` The verifier's answer to it is the frontend half of this contract: a remediation that leaves the frontend Service with no endpoints is rejected, whatever the pod list says.
