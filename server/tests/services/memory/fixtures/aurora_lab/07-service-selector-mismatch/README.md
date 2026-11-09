# Kubernetes Service has no endpoints: meaning and how to fix it

*A Service with no endpoints has a selector that matches no pods, so its ClusterIP refuses connections and the proxy returns 502. Confirm it and fix it.*

Runbook for lab scenario `07-service-selector-mismatch`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes. Written from the lab runs in this repository; updated 2026-10-08.

## Reproduce it

```
./lab.sh up
./lab.sh break 07-service-selector-mismatch
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Kubernetes Services of type ClusterIP whose selector matches no pods; any upstream that proxies to the Service, here nginx |
|---|---|
| Verified on | Kubernetes server v1.37.0, kind v0.33.0, nginx 1.27-alpine, 2026-10-02 |
| Lab folder | [scenarios/07-service-selector-mismatch](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/07-service-selector-mismatch), runs in [runs/v0-run2/07-service-selector-mismatch](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v0-run2/07-service-selector-mismatch) |

**A Service with no endpoints means its selector matches none of the pods, so every connection to its ClusterIP is refused and whatever proxies to it answers 502.** The pods behind it are usually healthy, which is why restarts change nothing. Confirm with `kubectl get endpoints <service> -n <namespace>`: an `ENDPOINTS` column reading `<none>` is the whole diagnosis. The fix is to make the selector match the pod labels again, nothing else. Other nginx 502 reasons, and which error.log line means which, are in [502 Bad Gateway in nginx](https://www.aurorasre.ai/runbooks/fix-502-bad-gateway-nginx-kubernetes).

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| `endpoints/<svc>` shows `<none>`, pods are Running and Ready | Service selector does not match the pod labels | `kubectl get svc <svc> -o yaml` against `kubectl get pods --show-labels` | Patch the selector back to the pod label |
| Endpoints list the pod IPs, upstream still refuses | `targetPort` is not the port the container listens on | `kubectl get svc <svc> -o yaml` against the container port in the pod spec | Set `targetPort` to the container port |
| Endpoints empty, pods `0/1` or `Running` but not Ready | Readiness probe failing, so the pods are left out of the endpoints | `kubectl describe pod <pod>` for `Readiness probe failed` | Fix the probe or the app it probes |
| Endpoints populated, connections time out instead of being refused | A NetworkPolicy blocks the client | `kubectl get networkpolicy -n <namespace>` | Allow the traffic in the policy |

## How do you confirm the Service has no endpoints?

Start from the error the client sees, then read the endpoints. In the lab the frontend nginx proxied `/api/` to the `api` Service and logged this (frontend pod log, captured before the investigation, run r1):

```
2026/10/02 16:35:46 [error] 25#25: *30 connect() failed (111: Connection refused) while connecting to upstream, client: 10.244.1.14, server: , request: "GET /api/items HTTP/1.1", upstream: "http://10.96.89.40:8080/items", host: "frontend"
10.244.1.14 - - [02/Oct/2026:16:35:46 +0000] "GET /api/items HTTP/1.1" 502 157 "-" "Wget" "-"
```

`Connection refused` against a ClusterIP, with no SYN timing out, points at the Service layer rather than the network. A timeout there is a different failure: [504 Gateway Timeout](https://www.aurorasre.ai/runbooks/fix-504-gateway-timeout-nginx-kubernetes). Read the endpoints next:

```shell
kubectl get endpoints api -n shop
```

Aurora's run r1, step 15:

```
NAME   ENDPOINTS   AGE
api    <none>      2m43s
```

The newer object says the same thing (r1, step 17):

```shell
kubectl get endpointslices -n shop -l kubernetes.io/service-name=api -o yaml
```

```yaml
- addressType: IPv4
  apiVersion: discovery.k8s.io/v1
  endpoints: null
  kind: EndpointSlice
  metadata:
    labels:
      endpointslice.kubernetes.io/managed-by: endpointslice-controller.k8s.io
      kubernetes.io/service-name: api
    name: api-qb4rq
    namespace: shop
  ports: null
```

The [debug services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/) guide covers this check: an empty ENDPOINTS column means the Service selector matched no pods.

Look-alikes: a Service whose endpoints are populated but whose upstream still refuses has a `targetPort` problem, not a selector problem. A Service with `0/1` pods behind it has a readiness problem. Both show endpoints that are not `<none>`, or pods that are not Ready, and this runbook is not the one for them.

## How much is affected, and since when?

Every client of the Service is affected, because the ClusterIP has nothing to forward to. Check what else is healthy so the fix stays narrow. Aurora's first command in both runs (r1, step 4):

```shell
kubectl get pods -n shop -o wide
```

```
NAME                        READY   STATUS    RESTARTS   AGE   IP            NODE                   NOMINATED NODE   READINESS GATES
api-f64486c4c-4rwgd         1/1     Running   0          79s   10.244.1.12   aurora-lab-2-worker    <none>           <none>
api-f64486c4c-56fw6         1/1     Running   0          79s   10.244.2.18   aurora-lab-2-worker2   <none>           <none>
frontend-76d98597b9-ml7r6   1/1     Running   0          79s   10.244.2.20   aurora-lab-2-worker2   <none>           <none>
frontend-76d98597b9-rpcht   1/1     Running   0          79s   10.244.1.13   aurora-lab-2-worker    <none>           <none>
loadgen-7c6676864-xdk98     1/1     Running   0          79s   10.244.1.14   aurora-lab-2-worker    <none>           <none>
redis-59458f55d8-h9p8h      1/1     Running   0          79s   10.244.2.17   aurora-lab-2-worker2   <none>           <none>
worker-6cf88d4b58-xgqwj     1/1     Running   0          79s   10.244.2.19   aurora-lab-2-worker2   <none>           <none>
```

Every pod is `1/1 Running` with zero restarts. That rules out the application and leaves the routing between the frontend and `api`. For "since when", the client log gives the first failure (loadgen pod log, captured before the investigation, r1): requests to `/api/items` returned 502 from `16:35:06Z` onward, every two seconds. Compare that with the Service's `resourceVersion` and any recent `kubectl apply` or `patch` in your change log; the Service's own events will usually be empty, as they were here (r1, step 20 returned only the Deployment's `ScalingReplicaSet` event).

## Which of the four causes is it?

Cheapest and likeliest first. Each step ends when its output answers the question.

**1. Compare the selector with the pod labels.** Read the Service (r1, step 16):

```shell
kubectl get svc api -n shop -o yaml
```

```yaml
metadata:
  annotations:
    kubectl.kubernetes.io/last-applied-configuration: |
      {"apiVersion":"v1","kind":"Service","metadata":{"annotations":{},"name":"api","namespace":"shop"},"spec":{"ports":[{"port":8080,"targetPort":8080}],"selector":{"app":"api"}}}
spec:
  clusterIP: 10.96.89.40
  ports:
  - port: 8080
    protocol: TCP
    targetPort: 8080
  selector:
    app: api-v2
  type: ClusterIP
```

Then the pods (run r2, step 8):

```shell
kubectl get pods -n shop -l app=api --show-labels
```

```
NAME                  READY   STATUS    RESTARTS   AGE   LABELS
api-f64486c4c-7k66j   1/1     Running   0          85s   app=api,pod-template-hash=f64486c4c
api-f64486c4c-bhgfg   1/1     Running   0          85s   app=api,pod-template-hash=f64486c4c
```

The live selector is `app: api-v2`; the pods carry `app=api`. The `last-applied-configuration` annotation still holds the intended selector, `app: api`, which tells you the change was made outside the manifest. Stop here if the labels differ: this is the cause. If you suspect the selector is deliberate, check whether anything carries the new label (r1, step 19 and r2, step 14 both returned nothing):

```shell
kubectl get pods -A -l app=api-v2
```

**2. Check `targetPort` against the container port.** Only if the selector matches. In the Service above, `targetPort: 8080`; in `kubectl describe pod api-f64486c4c-4rwgd -n shop` (r1, step 14) the container exposes `8080/TCP`. A mismatch here produces populated endpoints and a refused connection on the wrong port.

**3. Check readiness.** Only if the selector matches and endpoints are still empty. `kubectl describe pod <pod>` shows `Ready: True` for both api pods in the captured state. A pod that is not Ready is excluded from the endpoints even when its labels match.

**4. Check NetworkPolicy.** Only if endpoints are populated and the symptom is a timeout rather than a refusal. `kubectl get networkpolicy -n shop` lists any policy in the namespace.

## What is the fix?

Put the selector back to the label the pods actually carry. In the lab the fault was injected with one command, so the fix is its inverse:

```shell
kubectl -n shop patch svc api -p '{"spec":{"selector":{"app":"api"}}}'
```

Risk: low. Changing a selector only changes which pods receive traffic; it does not restart anything. The risk is choosing a label that matches more pods than you intend, so read `--show-labels` first. Rollback: patch the previous value back, or `kubectl apply` the manifest from source control, which is what the `last-applied-configuration` annotation preserved here.

Mitigation is not needed for this failure. The pods are already up; the moment the selector matches, the endpoints controller adds them. Do not restart pods, roll the Deployment, or scale it, none of which touches the Service.

If the selector change was intended (a cutover to `api-v2`), the fix is on the other side: label the new pods, or deploy the workload that carries the label, and leave the Service alone.

## How do you verify it worked?

Pass condition: the endpoints list the pod IPs, and the upstream stops returning 502.

```shell
kubectl get endpoints api -n shop
kubectl get endpointslices -n shop -l kubernetes.io/service-name=api -o jsonpath='{.items[*].endpoints[*].addresses[*]}'
```

Both must show the api pod IPs (the api pod IPs seen in run r1 were `10.244.1.12` and `10.244.2.18`; the fix was not run in a graded lab run). The EndpointSlice also lists pods that are not ready, so an IP there does not prove traffic reaches it; `get endpoints` lists only ready ones. In our [sidecar OOMKilled](https://www.aurorasre.ai/runbooks/fix-sidecar-container-oomkilled-kubernetes) run the slice still held both pod IPs while the Service had no endpoints. Then watch the client for a minute:

```shell
kubectl logs -n shop -l app=frontend --tail=20 -f
```

`GET /api/items` lines should return `200`, with no new `connect() failed` entries. The lab's own check for the broken state is one line, and its negation is the pass condition:

```shell
[ -z "$(kubectl -n shop get endpointslices -l kubernetes.io/service-name=api -o jsonpath='{.items[*].endpoints[*].addresses[*]}')" ]
```

It exits 0 while the Service has no endpoints and non-zero once it does.

## How do you prevent it?

**Alert on Services with no endpoints.** With kube-state-metrics, a Service that has a selector but no ready endpoint addresses can be alerted on by listing every endpoints object and removing the ones that still have a ready address:

```
kube_endpoint_info unless on (namespace, endpoint) kube_endpoint_address{ready="true"}
```

A sum of `kube_endpoint_address` compared with zero looks simpler but never fires: kube-state-metrics emits one series per address, so a Service with no addresses has no series to sum.

Scope it to Services that are supposed to have backends, and fire it before the error-rate alert does, because the empty endpoint list precedes the first 502.

**Guardrail.** Keep Services in source control and apply them from there. The lab's fault was an out-of-band `kubectl patch`; the `last-applied-configuration` annotation showing a different selector than the live spec is the signature of that. A policy engine (Gatekeeper or Kyverno) can require that a Service selector matches at least one existing pod template label in the namespace at admission.

**Test.** In CI, after deploying to a test namespace, assert that every Service with a selector has at least one endpoint address before running smoke tests.

## Common mistakes

- **Restarting the pods.** Both api pods were healthy. A rollout restart recreates them with the same labels, and the Service still matches none of them.
- **Fixing the app or its dependency.** Aurora's r1 investigation noticed `redis connection failed (attempt 1/5)` in the api logs; it was a startup race followed by `connected to redis at redis:6379`, and unrelated. The 502 is produced before a request reaches application code.
- **Reading `kubectl get svc` and stopping.** The default columns do not show the selector or the endpoints. `-o wide` adds the selector; `get endpoints` or `get endpointslices` shows what it matched.
- **Trusting the manifest in git.** The annotation and the live spec can disagree, as they did here. The live `spec.selector` is what the controller uses.
- **Changing the pod labels to match the Service.** That changes the Deployment's own selector match as well, which can orphan pods. Fix the Service.

## What we saw in the lab

Scenario 07 in the [Aurora lab](https://github.com/Arvo-AI/aurora-lab): a five-component `shop` app on a three-node kind cluster, the `api` Service patched to `selector: app=api-v2`, then Aurora given only the alert text ("HighErrorRate: frontend /api/ requests in namespace shop are failing with 5xx; all pods are Running") and read-only kubectl under its default `observability_only` policy. Two runs, each in a fresh organization so nothing carried over.

| Run | Verdict (human reviewed) | Time | Tool calls | Aurora cost |
|---|---|---|---|---|
| r1 | correct | 175 s | 22 | $0.52 |
| r2 | correct | 138 s | 15 | $0.40 |

Both runs reached the root cause and proposed the acceptable fix (patch the selector to `app=api`). The paths differed. r2 was direct: pods, events, then `kubectl get svc,ep -n shop` at step 6, which showed `endpoints/api <none>` next to populated endpoints for `frontend` and `redis`, then the Service YAML with `app: api-v2`, then `--show-labels` on the pods. Eleven kubectl calls and it had everything, including a search for `app=api-v2` across all namespaces to rule out a deliberate cutover.

r1 took the longer road: it read the frontend, api and loadgen logs first, found the 502s and the `Connection refused` lines, and only at step 10 ran `get endpoints api -o yaml`. The Endpoints object had no `subsets` at all, which it then confirmed with `get endpoints` and the EndpointSlice at steps 15 and 17. It also ran `kubectl top pods` and `get nodes` to rule out resource and node problems, which the captured state shows were never in question.

The miss worth knowing about is in r1's written report. Aurora had read `selector: app: api-v2` at step 16, and its final chat message and suggested fix both named it. The root-cause paragraph of the report did not: it said the selector "does not currently align with the running pods" and left `api-v2` to a "Ruled Out" line about an intentional cutover. The judge marked the run correct on the full output; a second model reading only the report called it partial for that reason, and a person at Arvo reviewed both and recorded `correct`. The investigation was right. The report under-stated what it knew.

Neither run was tempted by the wrong answers the scenario lists (restarting pods, an api application failure, NetworkPolicy or DNS). Neither could run `kubectl rollout history`, which the default policy blocked in 16 of 20 runs in this series as a false positive on the `rollout` verb; it did not matter here because the Service, not a Deployment, had changed.

## How Aurora handles this

Aurora receives the alert, connects to the cluster read-only through an uploaded kubeconfig, and runs the same kubectl commands shown above, choosing them itself. It records every tool call and output, cites them by number in the report, and lists what it could not check. Mutating commands are blocked by the organization's command policy by default, so it proposes the fix rather than applying it. On this failure it found the empty endpoint list and the selector mismatch in both runs, in about two to three minutes, for under a dollar each. The full traces for both runs are in the lab repository.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)

Related: [how to fix Kubernetes OOMKilled](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes), [how to fix ImagePullBackOff](https://www.aurorasre.ai/runbooks/fix-imagepullbackoff-kubernetes), [root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres), [a Service whose targetPort does not match the container](https://www.aurorasre.ai/runbooks/fix-service-targetport-mismatch-kubernetes), [a ConfigMap change that reached only some pods](https://www.aurorasre.ai/runbooks/fix-configmap-change-not-applied-to-all-pods-kubernetes), [a Service selecting pods from the wrong Deployment](https://www.aurorasre.ai/runbooks/fix-service-selects-wrong-pods-label-overlap-kubernetes), [a second fault the alert did not cover](https://www.aurorasre.ai/runbooks/fix-second-fault-alert-covers-only-one-kubernetes), and what an [Aurora root cause analysis](https://www.aurorasre.ai/rca) looks like.

## Sources and verification

- Every command and output block above comes from the lab run `v0-run2`, scenario `07-service-selector-mismatch`, on 2026-10-02: Aurora's tool-call transcripts (`steps-full.json`) for r1 and r2, and the cluster state captured before the investigation (`evidence-before/`). The fault was injected with `scenarios/07-service-selector-mismatch/break.sh`.
- Versions, from `evidence-before/versions.yaml`: Kubernetes server v1.37.0 on kind v0.33.0 (three nodes, containerd 2.3.4), kubectl client v1.35.3; nginx 1.27-alpine as the frontend.
- Aurora commit `87d22baf`, single agent, RCA model `bedrock/us.anthropic.claude-sonnet-5`. Verdicts: judge model `us.anthropic.claude-opus-5-5`, reviewed by a person at Arvo (`human_verdict` in each `grade.json`).
- Kubernetes documentation quoted: [Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/) and [Service](https://kubernetes.io/docs/concepts/services-networking/service/), read 2026-10-08.
- The fix command was not run inside a graded lab run; Aurora's policy blocks mutations. It is the inverse of the injected fault, and the Verify section states the pass condition instead of a captured result.

## Questions people ask

**What does it mean when a Kubernetes Service has no endpoints?**

The Service's selector matches none of the pods, so the endpoints controller has nothing to add. Connections to the ClusterIP are refused, and any proxy in front of it, such as nginx, returns 502. The pods behind it are often perfectly healthy.

**How do I check whether a Service has endpoints?**

Run kubectl get endpoints <service> -n <namespace>. An ENDPOINTS column reading <none> means no pod matched. kubectl get endpointslices -n <namespace> -l kubernetes.io/service-name=<service> -o yaml shows the same as endpoints: null.

**Why does nginx return 502 when the upstream Service has no endpoints?**

nginx tries to connect to the Service's ClusterIP, the connection is refused because kube-proxy has no backend to forward to, and nginx logs connect() failed (111: Connection refused) while connecting to upstream, then answers the client with 502.

**Should I restart the pods when a Service has no endpoints?**

No. Restarting recreates pods with the same labels, and the Service still matches none of them. Compare the Service selector with the pod labels and fix the selector.

**How do I compare the Service selector with the pod labels?**

Read spec.selector from kubectl get svc <service> -o yaml and compare it with kubectl get pods --show-labels. In the lab the Service had selector app: api-v2 while the pods carried app=api.

**Why does the last-applied-configuration annotation show a different selector?**

The annotation records the manifest from the last kubectl apply. When it disagrees with the live spec.selector, the Service was changed outside the manifest, for example with kubectl patch or kubectl edit.

**What is the difference between an empty endpoints list and a wrong targetPort?**

An empty endpoints list means no pod matched the selector. A wrong targetPort leaves the endpoints populated with pod IPs, but connections to the pod port are refused because nothing listens there.

**Can a failing readiness probe empty the endpoints?**

Yes. Pods that are not Ready are left out of the endpoints even when their labels match the selector. kubectl describe pod shows Readiness probe failed in that case.

**How can I alert before users see 502s?**

Alert on Services whose ready endpoint count is zero, for example with kube-state-metrics, kube_endpoint_info unless on (namespace, endpoint) kube_endpoint_address{ready="true"}, scoped to Services that should have backends.

**Did Aurora find this fault in the lab?**

Yes, in both runs, in 175 and 138 seconds, for $0.52 and $0.40. One report under-stated the selector value it had read; the investigation and the suggested fix were right in both runs, and a person reviewed the verdicts.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
