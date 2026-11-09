# Kubernetes alert fixed, errors continue: find the second fault

*The alerted pod is explained, yet users still get errors: a second, unalerted fault. Scope past the alert with real lab output from two faults at once.*

Runbook for lab scenario `12-two-faults-oom-and-selector`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-second-fault-alert-covers-only-one-kubernetes. Written from the lab runs in this repository; updated 2026-10-19.

## Reproduce it

```
./lab.sh up
./lab.sh break 12-two-faults-oom-and-selector
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Any incident where the alert names one workload and more than one thing changed; shown on a worker OOMKilled after a `BATCH_SIZE` change while, separately, the frontend Service selects no pods |
|---|---|
| Verified on | Kubernetes server v1.37.0, kind v0.33.0, kubectl client v1.35.3, nginx 1.27-alpine (nginx/1.27.5), python:3.12-alpine, redis:7.4-alpine, busybox:1.37, 2026-10-02 |
| Lab folder | [scenarios/12-two-faults-oom-and-selector](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/12-two-faults-oom-and-selector), runs in [runs/v1-run1/12-two-faults-oom-and-selector](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v1-run1/12-two-faults-oom-and-selector) |

**When the alerted problem is explained or fixed and users still see errors, treat it as a second, independent fault until a request through the user path proves otherwise: an alert covers the object its rule watches, not the incident.** In the lab, `KubePodCrashLooping` fired for a worker OOMKilled after `BATCH_SIZE` went from 500 to 2500, while the frontend Service had been patched to select `app=frontend,tier=web`, a label no pod carries, so every user request failed and every other pod read `1/1 Running`. Confirm the second fault with `kubectl -n shop describe svc` (an empty `Endpoints:` line) and the client's own log. Fix each fault separately: remove the extra selector key, revert `BATCH_SIZE`, and verify both.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| Alerted pod explained or fixed, client log still failing | A second fault on a path the alert does not watch | The client's log, then `kubectl -n <ns> describe svc` | Find and fix it on its own |
| `Endpoints:` empty in `describe svc`, EndpointSlice ENDPOINTS `<unset>`, pods `1/1 Running` | Service selector matches no pods | SELECTOR column of `get svc -o wide` against pod labels | Remove the extra selector key |
| Frontend pods log only `/healthz` probes, no user requests | Traffic stops before the pods: Service, Ingress or DNS | `describe svc`, `get ingress` | Fix the routing object |
| `Reason: OOMKilled`, `BATCH_SIZE: 2500`, limit `192Mi` | The alerted fault: working set raised above the limit | `describe pod`, ReplicaSet env diff | Revert `BATCH_SIZE`, or raise the limit with the reason stated |

## How do you know there is a second fault?

Start from what a user sees, not from the alert. The lab's `shop` app has one user path: a load generator calls `http://frontend/api/items`, the `frontend` Service sends it to nginx, and nginx proxies `/api/` to the `api` Service. The worker is not on that path. The load generator's log, captured before any investigation (run r1):

```shell
kubectl -n shop logs loadgen-7c6676864-7htc9 -c loadgen --tail=200
```

```
2026-10-02T20:23:30Z GET /api/items -> 502
2026-10-02T20:23:32Z GET /api/items -> 200
2026-10-02T20:23:35Z GET /api/items -> timeout
2026-10-02T20:23:38Z GET /api/items -> timeout
2026-10-02T20:23:41Z GET /api/items -> timeout
```

Every one of the 17 lines after the 200, through `20:24:23Z`, reads `timeout`. Read that word carefully: the load generator prints `${code:-timeout}`, so it means "no HTTP status came back", whether the connection was refused or timed out. The failures arrive 3 s apart, against 2 s while HTTP statuses were coming back, so each took about a second, well under the `wget -T 3` limit.

Next, the frontend pods' own logs. Neither sees a user request after 20:23:32; from then on there are only kubelet probes (frontend `785dp`, r1):

```
10.244.1.36 - - [02/Oct/2026:20:23:32 +0000] "GET /api/items HTTP/1.1" 200 38 "-" "Wget" "-"
10.244.1.1 - - [02/Oct/2026:20:23:36 +0000] "GET /healthz HTTP/1.1" 200 3 "-" "kube-probe/1.37" "-"
10.244.1.1 - - [02/Oct/2026:20:23:41 +0000] "GET /healthz HTTP/1.1" 200 3 "-" "kube-probe/1.37" "-"
```

A frontend that logs nothing while its client fails is not receiving the traffic: the break is in front of the pods.

Look-alikes:

- **Startup 502s.** The 502s at 20:23:26 to 20:23:30 came before the api was Ready (its log opens with `redis connection failed (attempt 1/5): [Errno 111] Connection refused`). The lab's reset waits for a `-> 200` before breaking anything, which fits the lone 200 at 20:23:32. Errors that end before the first success are baseline.
- **Fallout from the alerted fault.** The scenario lists "Treating the frontend failure as a consequence of the worker crash" as a wrong answer. Isolate, step 1, rules it out.

## How do you scope past the alert?

Three read-only commands, the ones the lab used to capture its evidence, cover every Service, its endpoints and the client. First, selectors and EndpointSlices:

```shell
kubectl -n shop get all,pvc,endpointslices,cm,secrets,networkpolicy,resourcequota -o wide
```

Trimmed to the Services and EndpointSlices (r1):

```
NAME               TYPE        CLUSTER-IP     EXTERNAL-IP   PORT(S)    AGE   SELECTOR
service/api        ClusterIP   10.96.234.91   <none>        8080/TCP   60s   app=api
service/frontend   ClusterIP   10.96.10.4     <none>        80/TCP     60s   app=frontend,tier=web
service/redis      ClusterIP   10.96.143.63   <none>        6379/TCP   60s   app=redis

NAME                                            ADDRESSTYPE   PORTS     ENDPOINTS                 AGE
endpointslice.discovery.k8s.io/api-tpbr5        IPv4          8080      10.244.1.34,10.244.2.43   60s
endpointslice.discovery.k8s.io/frontend-npfll   IPv4          <unset>   <unset>                   60s
endpointslice.discovery.k8s.io/redis-t42cj      IPv4          6379      10.244.2.42               60s
```

The Kubernetes [Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/) guide says that when the ENDPOINTS column is `<none>`, you "should check that the spec.selector field of your Service actually selects for metadata.labels values on your Pods". With kubectl v1.35.3 against server v1.37.0 the empty slice printed `<unset>` instead, so a script grepping for `<none>` would have passed this namespace.

Second, the Services in full:

```shell
kubectl -n shop describe svc
```

```
Name:                     frontend
Namespace:                shop
Selector:                 app=frontend,tier=web
Type:                     ClusterIP
IP:                       10.96.10.4
Port:                     <unset>  80/TCP
TargetPort:               80/TCP
Endpoints:
Events:                   <none>
```

(Trimmed; the `api` and `redis` blocks list their pod IPs.) The selector patch left no event, so `kubectl get events` cannot show this fault; both graded investigations below ran it and saw nothing about the frontend. Third, the client log, as above, for every user path in the namespace. The lab app has no Ingress (`app/shop.yaml` defines none); on your cluster add `kubectl get ingress -n <namespace>` and check each backend Service the same way (not run in the lab).

**Since when.** The client's last 200 is at 20:23:32Z and its first failure at 20:23:35Z. The new worker pod started at 20:23:33 (run r1, step 6, `Start Time`). The lab's `break.sh` runs the two changes back to back, so both landed within seconds. The alert created the incident at 20:24:25: users had been failing for about 50 s, and the page was about something else. r2 matches: last 200 at 20:47:09Z, worker start 20:47:09, alert 20:48:01.

**What not to infer.** Pod status at the same moment (r1, step 5, trimmed):

```
NAME                        READY   STATUS      RESTARTS      AGE
api-f64486c4c-cjj7k         1/1     Running     0             72s
api-f64486c4c-z7r8q         1/1     Running     0             72s
frontend-76d98597b9-785dp   1/1     Running     0             72s
frontend-76d98597b9-ccq7s   1/1     Running     0             72s
loadgen-7c6676864-7htc9     1/1     Running     0             72s
redis-59458f55d8-sc7tj      1/1     Running     0             72s
worker-86f68b9bf6-jr2kk     0/1     OOMKilled   3 (47s ago)   63s
```

Both frontend pods were `1/1 Running`, Ready, with zero restarts, while every request to the frontend failed. Readiness probes go from the kubelet straight to the pod IP (`kube-probe/1.37` in the log above), never through the Service. Pod status describes the pod, not whether anything routes to it.

## Isolate: one incident or two?

Cheapest check first. Each step ends when its output answers the question.

**1. Is the alerted component on the failing path?** Read the proxy config. The `frontend-nginx` ConfigMap (r1) has one upstream, `server api:8080;`, and `location /api/` does `proxy_pass http://api/;`. nginx calls the api and nothing else, and no Service selects `app=worker` (the three Services above). The worker cannot break `/api/items`. Two independent faults, two fixes. If the alerted component is on the path, fix it first and re-read the client before looking further.

**2. The unalerted fault: selector against labels.** The frontend pods carry `app=frontend` and `pod-template-hash=76d98597b9` (`kubectl -n shop describe pods`, r1). The Service wants `tier=web` as well. The tracked version is in the Service's own annotation (`kubectl -n shop get deploy,svc,cm,pvc -o yaml`, r1):

```
kubectl.kubernetes.io/last-applied-configuration: |
{"apiVersion":"v1","kind":"Service","metadata":{"annotations":{},"name":"frontend","namespace":"shop"},"spec":{"ports":[{"port":80,"targetPort":80}],"selector":{"app":"frontend"}}}
...
selector:
app: frontend
tier: web
```

Applied with `app: frontend`, live with `tier: web` added: an out-of-band edit, here `kubectl patch`.

**3. The alerted fault: what grew the working set.** `describe pod` shows `Reason: OOMKilled`, `Exit Code: 137`, a `192Mi` limit and `BATCH_SIZE: 2500` (r1, step 6). The two ReplicaSets side by side (r1, step 9):

```shell
kubectl get replicaset -n shop -l app=worker -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{.spec.template.spec.containers[0].resources}{"\n"}{.spec.template.spec.containers[0].env}{"\n\n"}{end}'
```

```
worker-6cf88d4b58
{"limits":{"memory":"192Mi"},"requests":{"cpu":"50m","memory":"64Mi"}}
[{"name":"BATCH_SIZE","value":"500"}]

worker-86f68b9bf6
{"limits":{"memory":"192Mi"},"requests":{"cpu":"50m","memory":"64Mi"}}
[{"name":"BATCH_SIZE","value":"2500"}]
```

The worker allocates `buf = [bytearray(100 * 1024) for _ in range(batch)]` (the `api-code` ConfigMap, r1 step 11): 2500 items of 100 KiB is about 244 MiB against 192Mi; 500 is about 49 MiB. The [OOMKilled runbook](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes) covers this fault in depth.

## What is the fix for each fault?

Fix the user-facing fault first, then the alerted one, as two separate changes.

**Frontend Service.** Save the current object, then remove the key that matches nothing:

```shell
kubectl -n shop get svc frontend -o yaml > frontend-svc.yaml
kubectl -n shop patch svc frontend --type=json -p='[{"op":"remove","path":"/spec/selector/tier"}]'
```

Use the JSON patch, not the obvious one. `kubectl patch svc frontend -p '{"spec":{"selector":{"app":"frontend"}}}'` is a strategic merge patch, and maps merge: `tier: web` stays. Re-applying the manifest from git keeps it too: for a map key in neither the file nor `last-applied-configuration`, the [declarative config docs](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/declarative-config/) give the action "Do nothing. Keep live value." `tier` is that key.

Risk: low. A selector change restarts nothing; the EndpointSlice controller adds the matching pods. Rollback: the inverse patch, `kubectl -n shop patch svc frontend -p '{"spec":{"selector":{"tier":"web"}}}'`, which puts back exactly the injected fault, with `frontend-svc.yaml` as the record of what was there. If `tier=web` was deliberate, add the label to the frontend Deployment's pod template in the manifest (a rollout) and leave the Service alone. Labelling running pods with `kubectl label pods` does not survive the next rollout.

**Worker.** Revert the variable with the verb that changed it:

```shell
kubectl -n shop set env deploy/worker BATCH_SIZE=500
```

Risk: a rollout of a single-replica worker that is already down. Rollback: set 2500 again. Re-applying the manifest also works here, since the file's `500` wins. If 2500 is a real requirement, raise `resources.limits.memory` above 244 MiB in the manifest instead, with the reason next to it.

There is no separate mitigation step: each fix is already the smallest change that restores service, and neither needs a manual restart.

## How do you verify it worked?

Both faults have to pass. The lab's own check for the broken state is two conditions joined by `&&`:

```shell
kubectl -n shop get pods -l app=worker -o jsonpath='{.items[*].status.containerStatuses[0].lastState.terminated.reason}' | grep -q OOMKilled &&
[ -z "$(kubectl -n shop get endpointslices -l kubernetes.io/service-name=frontend -o jsonpath='{.items[*].endpoints[*].addresses[*]}')" ]
```

Run the two halves separately; each must now fail. The EndpointSlice query must print the frontend pod IPs (`10.244.1.35` and `10.244.2.45` in r1), and the worker query must print nothing. Then the client, which is the condition users care about:

```shell
kubectl -n shop logs deploy/loadgen --tail=20
```

Every line should end `-> 200` (the lab's reset uses the same command with `--tail=1` as its healthy gate); twenty lines is about 40 s. Then watch the worker through two batch cycles (5 s sleep each) at `1/1 Running` with no new restarts. These are pass conditions, not captured results.

## How do you prevent it?

**Alert on the user path.** The client logged the outage from 20:23:35Z and nothing alerted on it. A synthetic request through the Service, such as a blackbox probe of `http://frontend.shop.svc/api/items`, catches what pod-level rules cannot. (Suggested; not part of the lab.) `KubePodCrashLooping` itself only sees the waiting state; see [how often a crash-looping pod actually shows CrashLoopBackOff](https://www.aurorasre.ai/blog/fix-crashloopbackoff-kubernetes).

**Alert on Services with no ready endpoints.** The query below is explained in the [Service with no endpoints runbook](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes), section "How do you prevent it?":

```
kube_endpoint_info unless on (namespace, endpoint) kube_endpoint_address{ready="true"}
```

The metric reads the v1 Endpoints API, which the API server flagged to K8sGPT during our head-to-head runs: "v1 Endpoints is deprecated in v1.33+; use discovery.k8s.io/v1 EndpointSlice". (Suggested; not run in the lab.)

**Guardrail.** Change Services only through their pipeline, and in CI compare the SELECTOR column of `kubectl get svc -o wide` with the manifests, since a re-apply will not remove a stray key.

**Triage rule.** No component goes into an incident report as "unaffected" without one request-path check: its Service's endpoints and its client's log. Write the Impact field of the RCA from the user path too; see the [RCA template](https://www.aurorasre.ai/blog/root-cause-analysis-template).

## Common mistakes

- **Closing the incident when the alert clears.** The alert was never about the users' outage.
- **Calling a component healthy from `get pods`.** `1/1 Running` with zero restarts was true of the frontend for the whole outage.
- **Re-applying manifests to undo drift.** It reverts the worker and leaves `tier: web` on the Service.
- **Linking the two faults because they arrived together.** Show the path first; here the worker was not on it.

## What we saw in the lab

Scenario 12 in the [Aurora lab](https://github.com/Arvo-AI/aurora-lab): the `shop` app on a three-node kind cluster, then `kubectl -n shop set env deploy/worker BATCH_SIZE=2500` and `kubectl -n shop patch svc frontend -p '{"spec":{"selector":{"app":"frontend","tier":"web"}}}'`. Aurora got the alert text, "KubePodCrashLooping [warning] namespace=shop deployment=worker: Pod shop/worker (container worker) is in waiting state (reason: CrashLoopBackOff).", plus read-only kubectl under its default `observability_only` policy. Two findings were required: the OOM from `BATCH_SIZE`, and, separately, the frontend Service with no endpoints. Two runs, each in a fresh organization.

| Run | Verdict (human reviewed) | Time | Tool calls | Aurora cost |
|---|---|---|---|---|
| r1 | partial | 140 s | 18 | $0.36 |
| r2 | partial | 548 s | 14 | $0.32 |

Both runs got the alerted fault right and missed the other one. Neither ran `get svc`, `get endpoints` or the load generator's log. r1's 13 kubectl calls covered the pod list, the worker's logs, events, ReplicaSets, Deployment and ConfigMap, a blocked `rollout history`, and nodes; r2's 11 were the same kind. Both reports state the OOM well; r2: "at `BATCH_SIZE=2500` it consumes ~244MiB, exceeding the limit and triggering an OOM kill almost immediately after startup".

Then both overreached. r1: "Other workloads in the `shop` namespace (api, frontend) remained healthy and unaffected [5, 14]." Citations 5 and 14 are its two `get pods` calls. r2: "Other workloads in the namespace (api, frontend pods) were running normally and unaffected [4]", where 4 is also `get pods`. Both scoped the impact to the worker: r1 "Impact is confined to the `worker` deployment in the `shop` namespace", r2 "Impact is scoped to the single-replica `worker` deployment in `shop`". The frontend was down to every client during both investigations. Neither "Not Checked" list mentions Services; r1's names rollout history, previous logs and past incidents. Missing a fault is one failure. Writing that the broken component is healthy is a worse one, because a reader acts on it.

The judge's r1 note: "It never checked Services or endpoints, so it missed the second fault." An AI reviewer, a second instance of the judge's model, called the claim "false and based only on pod status" and caught smaller errors, such as "both cluster nodes" when there were three. A person at Arvo reviewed both and recorded `partial`. The separate fix suggestions were graded partial too: both proposed reverting `BATCH_SIZE` and neither touched the selector.

On time: r1's first tool call came 5 s after the incident was created, r2's 7 min 30 s after, and r2 then concluded in about 98 s. The lab's [REPORT.md](https://github.com/Arvo-AI/aurora-lab/blob/main/REPORT.md) lists 12 r2 among runs that waited for a worker slot shared with prediscovery jobs, so its 548 s is mostly queue.

We ran the same scenario through two other tools ([r1](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/h2h-v1/r1/12-two-faults-oom-and-selector) 2026-10-15, [r2](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/h2h-v1/r2/12-two-faults-oom-and-selector) 2026-10-16; judge and two AI reviewers, no person yet). HolmesGPT 0.43.0, on the same model as Aurora, also missed the Service in both runs (26 s and 34 s); r1: "Root cause confirmed: shop/worker is being OOMKilled." It made no claim about the frontend either way. K8sGPT 0.4.40 (on claude-sonnet-4-5) flagged it in both runs, "Service has no endpoints, expected label tier=web". It is never given the alert: `k8sgpt analyze --explain --namespace shop` checks every object in the namespace. It never explained the OOM, r1: "Container ran out of memory (OOM - Out Of Memory) and was killed by Kubernetes.", no `BATCH_SIZE`, and suggested a limit of `"2Gi"` in r1 and `"512Mi"` in r2, plus `kubectl label pods <pod-name> app=frontend tier=web`. All four were graded partial.

The two LLM investigators reasoned from the alert and stayed on it; K8sGPT's analyzers had no alert to follow and swept everything. A [5 whys](https://www.aurorasre.ai/blog/5-whys-root-cause-analysis) started from the alert ends the same way, at the OOM, and never reaches the Service. That is the case for making the sweep mechanical: three commands, run before anyone writes "unaffected", whoever or whatever is doing the triage.

## How Aurora handles this

Aurora receives the alert, connects to the cluster read-only through an uploaded kubeconfig, chooses its own kubectl commands, records every call and output, and cites them by number in its report. Mutating commands are blocked by policy, so fixes arrive as suggestions. On this scenario it diagnosed the alerted OOM in both runs and missed the Service in both, and its reports called the frontend unaffected on pod status alone. A claim of health needs a request-path check behind it, and today that is not enforced.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)

Related: [how to fix Kubernetes OOMKilled](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes), [a Service with no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes), [a Service whose targetPort does not match the container](https://www.aurorasre.ai/runbooks/fix-service-targetport-mismatch-kubernetes), [HolmesGPT vs K8sGPT](https://www.aurorasre.ai/blog/holmesgpt-vs-k8sgpt), [root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres), and the [benchmark](https://www.aurorasre.ai/benchmark) with every graded run, and [alert fatigue](https://www.aurorasre.ai/blog/alert-fatigue): why every user-facing path needs a symptom alert.

## Sources and verification

- Every command and output block comes from lab run `v1-run1`, scenario `12-two-faults-oom-and-selector`, 2026-10-02: Aurora's tool-call transcripts and reports (`aurora-rca.md`) for r1 and r2, and the state `lab.sh evidence` captured before each investigation (`evidence-before/`). Trimmed where noted, otherwise unchanged. Faults injected by the scenario's `break.sh`.
- Versions from `evidence-before/versions.yaml` and the logs: Kubernetes server v1.37.0 on kind v0.33.0 (three nodes, containerd 2.3.4), kubectl client v1.35.3, nginx/1.27.5.
- Aurora commit `87d22baf`, single agent, model `bedrock/us.anthropic.claude-sonnet-5`. Verdicts: judge `us.anthropic.claude-opus-5-5`, a second Claude instance as AI reviewer (`reviewer_ai`), and a person at Arvo (`human_verdict` in each `grade.json`). Durations, calls and costs from `result.json`; first-call gaps from `timeline.json`.
- HolmesGPT and K8sGPT: `holmes.md`, `k8sgpt.txt`, their `grade.json` and `meta.json` files in `runs/h2h-v1`.
- Documentation quoted, read 2026-10-19: Kubernetes [Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/) and [Declarative Management of Kubernetes Objects Using Configuration Files](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/declarative-config/); kube-state-metrics [endpoint metrics](https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/service/endpoint-metrics.md).
- Not run in a graded lab run: the fix and verify commands, the Ingress check, the probe and the endpoints alert. The merge behaviour of `patch` and `apply` on the selector is from the documentation, not a lab capture.

## Questions people ask

**The alerted problem is fixed but users still see errors. What should I check first?**

The user path, not the alerted workload. Read the client's own log, then run kubectl -n <namespace> describe svc and look for an empty Endpoints line. In the lab the alert covered an OOMKilled worker while the frontend Service selected no pods, and every user request failed.

**Can I tell a Service is healthy from kubectl get pods?**

No. Pod status describes the pod. In the lab both frontend pods were 1/1 Running, Ready, with zero restarts, while the frontend Service had no endpoints and every request failed. Readiness probes go from the kubelet straight to the pod IP and bypass the Service.

**How do I check every Service in a namespace at once?**

Run kubectl -n <namespace> get all,endpointslices -o wide. Compare each Service's SELECTOR column with the pod labels and look for an EndpointSlice with no addresses. In the lab (server v1.37.0, kubectl v1.35.3) an empty slice printed <unset>, not <none>, so do not grep for <none> alone.

**Why does kubectl patch with app=frontend not fix a selector that has an extra tier=web key?**

A strategic merge patch merges maps, so it rewrites app and keeps tier. Remove the key with a JSON patch: kubectl -n <namespace> patch svc <service> --type=json -p='[{"op":"remove","path":"/spec/selector/tier"}]'.

**Will kubectl apply of my manifest remove a selector key someone added by hand?**

No. The Kubernetes docs say apply retains writes made to live objects, and for a map key present in neither the file nor last-applied-configuration the action is: Do nothing. Keep live value. Remove the key explicitly.

**How do I know whether the second failure is caused by the alerted one?**

Trace the request path. In the lab nginx proxied only to the api Service and no Service selected the worker, so the worker could not break the frontend. If the alerted component is on the path, fix it first and re-read the client before looking further.

**Why does kubectl get events not show the broken Service?**

Editing a Service selector creates no event. In the lab describe svc showed Events: <none> for the frontend Service, and both investigations ran get events without seeing anything about it.

**What alert catches a Service with no endpoints?**

With kube-state-metrics, kube_endpoint_info unless on (namespace, endpoint) kube_endpoint_address{ready="true"}. A sum of kube_endpoint_address compared with zero never fires, because an empty Service has no address series to sum.

**Did Aurora find both faults in the lab?**

No. In both runs (140 s and 548 s) it found the OOMKilled worker and the BATCH_SIZE change, never checked Services or endpoints, and wrote that the frontend was unaffected based on pod status. Both runs were graded partial and a person reviewed the verdicts. HolmesGPT also missed the Service; K8sGPT flagged it but never explained the OOM.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-second-fault-alert-covers-only-one-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
