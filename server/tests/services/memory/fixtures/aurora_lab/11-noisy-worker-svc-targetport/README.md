# Kubernetes Service connection refused: targetPort mismatch fix

*Endpoints list the pod IPs, yet nginx logs Connection refused and returns 502: the Service targetPort points where nothing listens. Confirm it and fix it.*

Runbook for lab scenario `11-noisy-worker-svc-targetport`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-service-targetport-mismatch-kubernetes. Written from the lab runs in this repository; updated 2026-10-19.

## Reproduce it

```
./lab.sh up
./lab.sh break 11-noisy-worker-svc-targetport
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Any ClusterIP Service whose `targetPort` names a port the selected containers do not listen on; shown on the `api` Service of the lab's `shop` app behind an nginx frontend |
|---|---|
| Verified on | Kubernetes server v1.37.0, kind v0.33.0, nginx 1.27-alpine (nginx/1.27.5), python:3.12-alpine, 2026-10-02 |
| Lab folder | [scenarios/11-noisy-worker-svc-targetport](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/11-noisy-worker-svc-targetport), runs in [runs/v1-run1/11-noisy-worker-svc-targetport](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v1-run1/11-noisy-worker-svc-targetport) |

**A Service that lists endpoints and still refuses every connection is sending traffic to the wrong port on the right pods: its `targetPort` names a port nothing in the container listens on, so kube-proxy forwards each connection to a pod and the pod refuses it.** Confirm with `kubectl get endpoints <service> -n <namespace>`: in the lab the `api` endpoints read `10.244.1.131:9090,10.244.2.124:9090` while the api process had logged `api listening on :8080`. The fix is one patch to the Service's `targetPort`; nothing needs restarting.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| Endpoints list the pod IPs, every request gets `connect() failed (111: Connection refused)` and a 502, pods Ready with zero restarts | `targetPort` is not the port the container listens on | `kubectl describe svc <svc>` (TargetPort, Endpoints) against the listening port in `kubectl logs <pod>` | Patch `targetPort` to the listening port |
| `endpoints/<svc>` reads `<none>`, same client error | The selector matches no pods | `kubectl get svc <svc> -o yaml` against `kubectl get pods --show-labels` | See [Service has no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes) |
| Endpoints populated, the client times out instead | The client dials a port the Service does not publish, or a NetworkPolicy blocks it | The client's upstream port against PORT(S) in `kubectl get svc`; `kubectl get networkpolicy` | Fix the client's port, or see [NetworkPolicy blocking traffic](https://www.aurorasre.ai/runbooks/fix-networkpolicy-blocking-traffic-kubernetes) |
| A neighbouring Deployment logs warnings and errors from about the same time | Noise, unless it sits on the request path | `kubectl get svc -o wide` for a Service that selects it; the proxy config for who it calls | Nothing here; its own ticket |

## How do you confirm it is the targetPort?

Three port fields are in play and the client's error names the one that is right. The [Service docs](https://kubernetes.io/docs/concepts/services-networking/service/): "A Service can map any incoming port to a targetPort. By default and for convenience, the targetPort is set to the same value as the port field." So `port` is what clients dial on the ClusterIP (8080 here), `targetPort` is what kube-proxy forwards to on each pod (9090 after the fault), and `containerPort` is a declaration that opens nothing: the [API reference](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/) says "Not specifying a port here DOES NOT prevent that port from being exposed." The only port that matters on the pod is the one the process bound.

Start with the client. The frontend nginx proxies `/api/` to the `api` Service (run r1, step 14, four of 30 lines):

```shell
kubectl logs frontend-76d98597b9-ktzwl -n shop --tail=30
```

```
2026/10/02 20:24:21 [error] 27#27: *49 connect() failed (111: Connection refused) while connecting to upstream, client: 10.244.2.126, server: , request: "GET /api/items HTTP/1.1", upstream: "http://10.96.190.97:8080/items", host: "frontend"
10.244.2.126 - - [02/Oct/2026:20:24:21 +0000] "GET /api/items HTTP/1.1" 502 157 "-" "Wget" "-"
2026/10/02 20:24:25 [error] 27#27: *51 connect() failed (111: Connection refused) while connecting to upstream, client: 10.244.2.126, server: , request: "GET /api/items HTTP/1.1", upstream: "http://10.96.190.97:8080/items", host: "frontend"
10.244.2.126 - - [02/Oct/2026:20:24:25 +0000] "GET /api/items HTTP/1.1" 502 157 "-" "Wget" "-"
```

The upstream is the ClusterIP on 8080, the port the Service publishes. Nothing in this log is wrong; 9090 never appears on the client side. It appears on the Service (r1, step 12, trimmed):

```shell
kubectl describe svc api -n shop
```

```
Selector:                 app=api
Type:                     ClusterIP
IP:                       10.96.190.97
Port:                     <unset>  8080/TCP
TargetPort:               9090/TCP
Endpoints:                10.244.1.131:9090,10.244.2.124:9090
```

Both api pods are endpoints, on port 9090. Now the pod (r1, step 7, five of 27 lines):

```shell
kubectl logs api-f64486c4c-fjnsl -n shop --tail=100
```

```
redis connection failed (attempt 1/5): [Errno 111] Connection refused
connected to redis at redis:6379
api listening on :8080
10.244.1.1 "GET /healthz HTTP/1.1" 200 -
10.244.1.133 "GET /items HTTP/1.0" 200 -
```

`listening on :8080` against `Endpoints: ...:9090` is the confirmation. kube-proxy rewrites each connection to `10.96.190.97:8080` into one to a pod IP on 9090, no process owns 9090, and the pod refuses it; nginx reports the refusal as if the Service had refused.

Look-alikes. A Service with no endpoints produces the identical client line: in [scenario 07](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes) nginx logged the same `connect() failed (111: Connection refused)` against a ClusterIP whose endpoints read `<none>`. The endpoints column, not the error text, separates the two. A client dialling a port the Service does not publish gets a timeout instead: in [scenario 19](https://www.aurorasre.ai/runbooks/fix-configmap-change-not-applied-to-all-pods-kubernetes) nginx pointed at `api:8000` on a Service publishing only 8080 and logged `upstream timed out`. Other nginx 502 causes, and which error.log line means which, are in [502 Bad Gateway in nginx](https://www.aurorasre.ai/runbooks/fix-502-bad-gateway-nginx-kubernetes).

## How much is affected, and since when?

Every client that goes through the Service, and nothing that goes around it. The load generator's last 20 lines (r1, step 18) were all `GET /api/items -> 502`, from `20:25:27Z` to `20:26:05Z`. Both api pods kept answering `/healthz` with 200 throughout, because the kubelet probes the pod IP on the probe's own port, 8080, and never touches the Service. So every pod was `1/1 Running` with zero restarts (r1, step 4) and the Deployment reported `readyReplicas: 2` (r1, step 13). Readiness says the process is up, not that the Service reaches it.

The start time is in the pod log above. The api served exactly one `GET /items`, from `10.244.1.133`, the frontend pod `dgg8q`; after that, only probes. The client agrees: the load generator's log captured before the investigation has `20:23:13Z GET /api/items -> 200` between startup 502s and the 502s that never stopped. One request got through between the api becoming Ready and the Service being patched; r2 shows the same (step 12). With real traffic, the last 200 in the api's access log is the moment the port changed.

The change itself left a fingerprint (r1, step 17, trimmed):

```shell
kubectl get svc api -n shop -o yaml
```

```yaml
metadata:
annotations:
  kubectl.kubernetes.io/last-applied-configuration: |
    {"apiVersion":"v1","kind":"Service","metadata":{"annotations":{},"name":"api","namespace":"shop"},"spec":{"ports":[{"port":8080,"targetPort":8080}],"selector":{"app":"api"}}}
creationTimestamp: "2026-10-02T20:23:05Z"
resourceVersion: "32995"
spec:
ports:
- port: 8080
  protocol: TCP
  targetPort: 9090
```

The last applied manifest says `targetPort: 8080`; the live object says 9090. Someone changed the Service outside the manifest, with `kubectl patch` or `kubectl edit`. The Service's events were empty (`Events: <none>`, step 12), so the annotation and the pod log date the change, not the event list.

## Which cause is it?

Cheapest and likeliest first.

**1. Are there endpoints?** Yes, two pod IPs (step 12 above). The selector is fine, so this is not the [no-endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes) case.

**2. Does the endpoint port match what the container listens on?** No: 9090 against `api listening on :8080`. This is the cause. The Deployment agrees, `containerPort: 8080` with both probes on 8080 (r1, step 13), and so does the source: r2 read the api's code out of its ConfigMap (r2, step 9) and found `PORT = int(os.environ.get("PORT", "8080"))` with no `PORT` set in the Deployment. Without a listen line in the log, read the code or run `ss -ltn` in the container; `containerPort` is documentation.

**3. Is the noisy neighbour on the request path?** In the lab a worker Deployment had been redeployed seconds before the Service was patched (pod age 61 s against 69 s for everything else, r1, step 4), and its log is the loudest thing in the namespace (r1, step 19, four of 30 lines):

```shell
kubectl logs worker-645795c659-fqn5f -n shop --tail=30
```

```
WARNING py.warnings: DeprecationWarning: StrictRedis is deprecated and will be removed in redis-py 6.0, use Redis
processed batch of 500 items
ERROR metrics: push to pushgateway:9091 failed: [Errno -2] Name does not resolve (will retry next batch)
WARNING py.warnings: DeprecationWarning: StrictRedis is deprecated and will be removed in redis-py 6.0, use Redis
```

Two commands rule it out. `kubectl get svc -n shop` lists Services for `api`, `frontend` and `redis` only (r1, step 9): no Service selects the worker, so no request can reach it. The frontend's nginx config proxies `/api/` to `http://api/` and nowhere else (r2, step 9). And the name that fails is `pushgateway`, which nothing on the request path uses; DNS itself works, since nginx resolved `api` to `10.96.190.97` and the api connected to `redis:6379` by name. Between two simultaneous changes, the request path decides, not the log volume.

**4. Was the port change intended?** The annotation says the Service was declared with 8080, and no Deployment change accompanied it (`revision: "1"`, r1, step 13).

## What is the fix?

Put `targetPort` back to the port the container listens on. The lab's fault was one JSON patch, so the fix is the same patch with the right value:

```shell
kubectl -n shop patch svc api --type=json \
-p='[{"op":"replace","path":"/spec/ports/0/targetPort","value":8080}]'
```

Risk: low. A Service edit restarts nothing; the endpoints controller and kube-proxy pick up the new port on their own. The risk is choosing the wrong number, so take it from the process's log line or the code, not from `containerPort`. Rollback: the same command with the previous value, or `kubectl apply` the Service from source control, which also realigns the annotation. Save `kubectl -n shop get svc api -o yaml` first.

No mitigation is needed and no pod should be restarted: the pods were right the whole time. If the move to 9090 was deliberate, ship pods that listen on 9090 (`PORT=9090` in the Deployment's env) before changing the Service.

## How do you verify it worked?

Pass condition: the endpoints carry the listening port, and a full minute of 200s from the client with no new `connect() failed` line from any frontend pod.

```shell
kubectl -n shop get endpoints api
kubectl -n shop logs deploy/loadgen --tail=30
kubectl -n shop logs -l app=frontend --tail=20 --prefix | grep 'connect() failed'
```

The first must read `10.244.1.131:8080,10.244.2.124:8080` (r1's pod IPs). The load generator logs one line every two seconds, so `--tail=30` is a minute; every line should end `-> 200`. The third should print nothing. The first line of the lab's own check for the broken state is `kubectl -n shop logs deploy/loadgen --tail=5 | grep -q -- '-> 502'`; its negation is your pass.

## How do you prevent it?

**Name the port and reference the name.** The Service docs: "Port definitions in Pods have names, and you can reference these names in the targetPort attribute of a Service." With `name: http` on the container port and `targetPort: http` in the Service, a port move is one change in one place, and a numeric patch against a named port stands out in review.

**Probe through the Service.** The alert here came from the load generator, which calls the Service as a user does; pod probes passed throughout. A blackbox check against `<service>.<namespace>.svc:<port>` from inside the cluster, alerting on `probe_success == 0`, catches a Service-layer break that no pod-level signal will. (Suggested; not part of the lab.)

**Guardrail.** Services change through the same pipeline as the Deployments they front; an annotation that disagrees with `spec.ports` is the signature of a `kubectl patch` on a live object. A CI step comparing each Service's `targetPort` with the `containerPort` list of the pods it selects turns the confirm step above into a test.

## Common mistakes

- **Restarting the api or the frontend.** A wrong answer in the scenario. The pods come back listening on 8080 and the Service still forwards to 9090.
- **Blaming the worker, or DNS, because of `Name does not resolve`.** The worker is not on the request path and the unresolved name is one it alone uses.
- **Reading `:8080` in the nginx error and concluding the ports match.** That is the Service port. The forwarded port is only visible in `get endpoints` or `describe svc`.
- **Trusting Ready.** Probes go to the pod IP on the probe port and bypass the Service.
- **Changing `containerPort` to 9090 to match.** The process still listens on 8080.
- **Changing the client to `api:9090`.** The Service does not publish 9090, so the refusal becomes a timeout (scenario 19).

## What we saw in the lab

Scenario 11 in the [Aurora lab](https://github.com/Arvo-AI/aurora-lab): a five-component `shop` app on a three-node kind cluster. The break script rewrote `worker.py` in the shared code ConfigMap so the worker prints a `DeprecationWarning` every batch and a metrics-push ERROR every third, rolled the worker Deployment and waited for it, then patched the `api` Service's `targetPort` from 8080 to 9090. Aurora got the alert text ("HighErrorRate [critical] namespace=shop service=frontend: 100% of requests to /api/ returned 5xx over the last 5 minutes (threshold 5%).") and read-only kubectl under the default `observability_only` policy. Two findings were required: the targetPort mismatch, and that the worker's noise is not the cause. Two runs, each in a fresh organization.

| Run | Verdict (human reviewed) | Time | Tool calls | Aurora cost |
|---|---|---|---|---|
| r1 | correct | 144 s | 19 | $0.44 |
| r2 | partial | 535 s | 16 | $0.57 |

r1 had the answer at step 9, its sixth kubectl call, when `get svc,endpoints` put `endpoints/api 10.244.1.131:9090,10.244.2.124:9090` under `service/api ... 8080/TCP`. It confirmed with `describe svc`, the Deployment YAML and the annotation, and read the worker last, at step 19. Its report: "This caused kube-proxy to route all Service traffic to pod port 9090, where nothing was listening, resulting in connection refusals that nginx surfaced as 502s". The worker went under Not Checked: "The worker pod's `pushgateway:9091` DNS errors were observed but determined to be a separate, pre-existing cosmetic issue unrelated to this alert's causal chain, and were not further investigated". Both findings met, with two over-claims the graders caught. "Pre-existing" is wrong: the worker was redeployed seconds before the fault, and the pod ages in its own step 4 showed it. The report also said the frontend's readiness probes were "failing during this window"; the only readiness failures in the events were at startup. Its separate fix suggestion, patch `targetPort` back to 8080, was graded acceptable.

r2 took 535 s with fewer tool calls, and the investigation itself was not slower. The kubectl time was the same in both runs, about 68 s (summed tool durations in each timeline), and from first tool call to conclusion r2 took 137 s; r1 took 138 s by the same measure. The difference is before the first call: r1 started 5 s after the incident was created, r2 after 6 min 37 s, and the timeline records no tool call and no model call in that gap, so the table's 535 s is mostly wait before the investigation began, for a reason the run data does not show. Its longest single call was step 9, where `describe configmap` returned the full source of both programs plus a CA certificate (15 s). It read the api's code for the listening port, pulled the Endpoints object showing `port: 9090`, and wrote the sentence this page is built on: "Kubelet-direct health checks (bypassing the Service) to both frontend and api pods succeeded throughout this window, meaning the user-facing path was fully broken while pod-level health signals remained green". It was graded partial because it never looked at the worker: its Not Checked list has "Health and configuration of the redis, worker, and loadgen deployments" with the reason "no symptoms or alerts pointed to these components, so they were not examined", so the second required finding was inferred, not shown. The judge also flagged "the fix was validated but not applied" as a claim with no fix in the report. The patch command did exist, in Aurora's working notes and in the memory it wrote at step 16, and the separate fix suggestion was graded acceptable, but the report did not contain it.

Both runs named the port in under twenty calls and neither was tempted by restarts, DNS or the worker. The difference is whether the report shows that the loudest log in the namespace was looked at and dismissed. An engineer who has seen the worker's ERROR lines will not take "not examined" as a reason to ignore them.

## How Aurora handles this

Aurora receives the alert, connects to the cluster read-only through an uploaded kubeconfig, and runs the kubectl commands shown above, choosing them itself. On this failure it read the Service, the Endpoints and the pod log in both runs, compared the endpoint port with the listening port, and used the `last-applied-configuration` annotation to call the change an out-of-band edit. Mutating commands are blocked by policy, so the fix arrives as a suggestion for a person to apply. One run checked the noisy neighbour and said so; the other did not.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)

Related: [a Service with no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes), [a NetworkPolicy blocking traffic](https://www.aurorasre.ai/runbooks/fix-networkpolicy-blocking-traffic-kubernetes), [a hostname that does not resolve](https://www.aurorasre.ai/runbooks/fix-dns-name-not-resolving-kubernetes), [a ConfigMap change that reached only some pods](https://www.aurorasre.ai/runbooks/fix-configmap-change-not-applied-to-all-pods-kubernetes), [root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres), [a Service selecting pods from the wrong Deployment](https://www.aurorasre.ai/runbooks/fix-service-selects-wrong-pods-label-overlap-kubernetes), and what an [Aurora root cause analysis](https://www.aurorasre.ai/rca) looks like.

## Sources and verification

- Every command and output block above comes from the lab run `v1-run1`, scenario `11-noisy-worker-svc-targetport`, on 2026-10-02: Aurora's tool-call transcripts for r1 and r2 and the cluster state captured before the investigation (`evidence-before/`). The fault was injected by the scenario's `break.sh` (a ConfigMap rewrite, `rollout restart deploy/worker`, then a JSON patch of `/spec/ports/0/targetPort` to 9090); the fix above is the same patch with 8080.
- Versions, from `evidence-before/versions.yaml` and the pod logs: Kubernetes server v1.37.0 on kind v0.33.0 (three nodes, containerd 2.3.4), kubectl client v1.35.3; nginx 1.27-alpine reporting nginx/1.27.5; python:3.12-alpine for the api and worker.
- Aurora commit `87d22baf`, single agent, RCA model `bedrock/us.anthropic.claude-sonnet-5`. Verdicts: judge model `us.anthropic.claude-opus-5-5`, reviewed by a person at Arvo (`human_verdict` in each `grade.json`); fix suggestions graded separately (`fix_from_suggestions`). Durations, tool calls and costs from each run's `result.json`; the 68 s kubectl totals are summed `duration_ms` from each `timeline.json`, and the first-call gaps and the 137 s and 138 s are from the same file's `started_at` and step and model-call timestamps.
- Documentation quoted, read 2026-10-19: Kubernetes [Service](https://kubernetes.io/docs/concepts/services-networking/service/) (the note under "Defining a Service" and the first sentence of "Port definitions"), the [Pod API reference](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/) (the `ports` field of Container), and [Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/), whose checklist asks "Is the targetPort correct for your Pods (some Pods use a different port than the Service)?".
- The fix and verify commands were not run inside a graded lab run; Aurora's policy blocks mutations, so Verify states the pass condition and the lab's own check instead of a captured result. The blackbox probe and CI comparison under Prevent are suggestions, not captured alerts.

## Questions people ask

**Why does my Kubernetes Service have endpoints but every connection is refused?**

Because the Service's targetPort names a port the container does not listen on. The selector matched the pods, so the endpoints are populated, but kube-proxy forwards each connection to the pod on the targetPort, nothing is bound there, and the pod refuses it. In the lab the endpoints read :9090 while the api logged listening on :8080.

**What is the difference between port, targetPort and containerPort?**

port is what clients dial on the Service's ClusterIP. targetPort is the port kube-proxy forwards to on each selected pod, and defaults to port. containerPort is a declaration in the pod spec; the Kubernetes API reference says leaving a port out of it does not stop that port from being exposed, so it neither opens nor closes anything. The port that matters on the pod is the one the process bound.

**Why does the nginx error say port 8080 when the Service forwards to 9090?**

nginx logs the address it dialled, the ClusterIP and the Service's port, which was correct. The forwarding to 9090 happens inside kube-proxy after that, so the wrong port never appears on the client side. It is visible in kubectl get endpoints and in the TargetPort line of kubectl describe svc.

**Why are the pods Ready if the Service cannot reach them?**

Readiness and liveness probes are run by the kubelet against the pod IP on the port written in the probe, 8080 here. They never pass through the Service, so a targetPort mismatch leaves every pod Ready and every Deployment at full availability while all Service traffic fails.

**How do I tell a wrong targetPort from a Service with no endpoints?**

Run kubectl get endpoints <service>. If the column reads <none>, the selector matched no pods. If it lists pod IPs with a port, compare that port with the port the container logs or binds. The client error, connect() failed (111: Connection refused), is identical in both cases.

**Should I restart the api or frontend pods?**

No. The pods were correct throughout; they listen on 8080 and will listen on 8080 after a restart, while the Service still forwards to 9090. The scenario lists restarts as a wrong answer. Patch the Service's targetPort.

**A worker in the same namespace logs errors and was redeployed at the same time. Is it the cause?**

Only if a request can reach it. In the lab no Service selected the worker and the frontend proxied to the api alone, so its DeprecationWarning and metrics push errors could not affect /api/ requests. Check for a Service that selects the component and the proxy config that calls it before reading its logs.

**How do I prevent a targetPort from drifting?**

Name the container port and reference the name in targetPort, so the Service follows the pod. Keep Services in source control; a last-applied-configuration annotation that disagrees with the live spec.ports is the signature of an out-of-band kubectl patch. Probe through the Service, not only the pods.

**Did Aurora find this fault in the lab?**

In both runs it named the targetPort mismatch and the out-of-band edit, in 144 s and 535 s. r1 was graded correct, with two over-claims noted. r2 was graded partial because it never examined the worker and its report stated no fix, though its separate fix suggestion was acceptable.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-service-targetport-mismatch-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
