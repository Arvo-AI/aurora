# Kubernetes Service selects the wrong pods: label overlap fix

*A Service selects pods by label, not by Deployment. Its endpoints list one pod more than the Deployment runs and that pod returns 500. Confirm and fix it.*

Runbook for lab scenario `15-stray-pods-share-label`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-service-selects-wrong-pods-label-overlap-kubernetes. Written from the lab runs in this repository; updated 2026-10-19.

## Reproduce it

```
./lab.sh up
./lab.sh break 15-stray-pods-share-label
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Any Service whose label selector matches pods owned by a workload other than the one it was written for; shown on the `api` Service of the lab's `shop` app after a second Deployment, `shop-api`, was applied with the pod label `app=api` |
|---|---|
| Verified on | Kubernetes server v1.37.0, kind v0.33.0, kubectl v1.35.3, nginx 1.27-alpine (nginx/1.27.5), python:3.12-alpine, 2026-10-02 |
| Lab folder | [scenarios/15-stray-pods-share-label](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/15-stray-pods-share-label), runs in [runs/v1-run1/15-stray-pods-share-label](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v1-run1/15-stray-pods-share-label) |

**A Service that fails a steady fraction of requests while every pod behind it is Ready is usually sending that fraction to a pod that is not yours: the Service selects by label, not by Deployment, and another workload's pod template carries the same label.** Confirm with `kubectl get endpoints <service> -n <namespace>` against the Deployment's ready replicas: in the lab the `api` endpoints read `10.244.1.37:8080,10.244.1.40:8080,10.244.2.48:8080` while `deploy/api` reported `2 available`. Map the extra IP to its pod with `kubectl get pods -n <namespace> -o wide`, then scale that pod's Deployment to zero and relabel it.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| A steady fraction of requests gets 5xx, every pod Ready, more endpoints than the Deployment has replicas | Another workload's pods carry the Service's selector label | `kubectl get endpoints <svc>` against `kubectl describe deploy <name>`; `kubectl get pods -o wide` to name the extra IP | Scale the stray workload to zero, then relabel it |
| Every request fails, `endpoints/<svc>` reads `<none>` | The selector matches no pods | `kubectl get svc <svc> -o yaml` against `kubectl get pods --show-labels` | See [Service has no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes) |
| Every request refused, endpoints populated | `targetPort` is not the port the container listens on | `kubectl describe svc <svc>` against the listen line in `kubectl logs <pod>` | See [targetPort mismatch](https://www.aurorasre.ai/runbooks/fix-service-targetport-mismatch-kubernetes) |
| A fraction fails, endpoint count equals the replica count | A real replica is failing | Per-pod `kubectl logs` | Fix that pod; the Service is fine |

## How do you confirm the Service is selecting a stray pod?

The [Service docs](https://kubernetes.io/docs/concepts/services-networking/service/): "The set of Pods targeted by a Service is usually determined by a selector that you define." No Deployment in that sentence. The [labels page](https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/) supplies the part that bites: "Unlike names and UIDs, labels do not provide uniqueness." Any pod in the namespace with `app=api`, whoever created it, is a backend of a Service selecting `app=api`.

Start at the client. The frontend nginx proxies `/api/` to the `api` Service, and its access log alternates (run r1, step 8, four of 24 `/api/items` lines):

```shell
kubectl logs frontend-76d98597b9-bb4z9 -n shop --tail=100
```

```
10.244.1.39 - - [02/Oct/2026:20:29:01 +0000] "GET /api/items HTTP/1.1" 200 38 "-" "Wget" "-"
10.244.1.39 - - [02/Oct/2026:20:29:05 +0000] "GET /api/items HTTP/1.1" 500 34 "-" "Wget" "-"
10.244.1.39 - - [02/Oct/2026:20:29:09 +0000] "GET /api/items HTTP/1.1" 500 34 "-" "Wget" "-"
10.244.1.39 - - [02/Oct/2026:20:29:13 +0000] "GET /api/items HTTP/1.1" 200 38 "-" "Wget" "-"
```

No `[error]` line accompanies the 500s: a program answered, and the two body sizes (38 bytes for a 200, 34 for a 500) say two different programs are answering. Count the backends (r1, step 13) against what the Deployment runs (r2, step 11, two lines of 50):

```shell
kubectl get endpoints api -n shop -o wide
kubectl describe deploy api -n shop
```

```
NAME   ENDPOINTS                                            AGE
api    10.244.1.37:8080,10.244.1.40:8080,10.244.2.48:8080   111s
```

```
Replicas:               2 desired | 2 updated | 2 total | 2 available | 0 unavailable
NewReplicaSet:   api-f64486c4c (2/2 replicas created)
```

Three endpoints, two replicas; the two objects count different things. The pod list prints IPs with `-o wide` (r1, step 4, three of eight rows):

```shell
kubectl get pods -n shop -o wide
```

```
NAME                        READY   STATUS    RESTARTS   AGE   IP            NODE                   NOMINATED NODE   READINESS GATES
api-f64486c4c-r95bj         1/1     Running   0          71s   10.244.1.37   aurora-lab-2-worker    <none>           <none>
api-f64486c4c-vftsb         1/1     Running   0          71s   10.244.2.48   aurora-lab-2-worker2   <none>           <none>
shop-api-69f6fc5964-jhzz8   1/1     Running   0          62s   10.244.1.40   aurora-lab-2-worker    <none>           <none>
```

`10.244.1.40` is `shop-api-69f6fc5964-jhzz8`, from a Deployment called `shop-api`. With hundreds of pods, ask for the one IP: `kubectl get pods -A -o wide --field-selector status.podIP=10.244.1.40` (`status.podIP` is a [supported Pod field selector](https://kubernetes.io/docs/concepts/overview/working-with-objects/field-selectors/); not run in the lab). Confirm the label (r1, step 14, two of eight rows):

```shell
kubectl get pods -n shop --show-labels
```

```
api-f64486c4c-r95bj         1/1     Running   0          114s   app=api,pod-template-hash=f64486c4c
shop-api-69f6fc5964-jhzz8   1/1     Running   0          105s   app.kubernetes.io/name=shop-api,app=api,pod-template-hash=69f6fc5964
```

The stray pod carries `app=api` next to its own `app.kubernetes.io/name=shop-api`. Its log says why it fails (r1, step 15, first nine of 100 lines):

```shell
kubectl logs shop-api-69f6fc5964-jhzz8 -n shop --tail=100
```

```
10.244.1.38 "GET /items HTTP/1.0" 500 -
Traceback (most recent call last):
File "/code/app.py", line 12, in do_GET
  settings()
File "/code/app.py", line 5, in settings
  return {"db": os.environ["DATABASE_URL"], "redis": os.environ.get("REDIS_HOST", "redis")}
                ~~~~~~~~~~^^^^^^^^^^^^^^^^
File "<frozen os>", line 714, in __getitem__
KeyError: 'DATABASE_URL'
```

Every `/items` request raises on a missing environment variable; every `/healthz` answers 200, so the readiness probe passes and the pod stays in the endpoints. The two `api` pods logged only 200s for `/items` (r1, steps 10 and 11).

Look-alikes. Endpoints `<none>` with every request refused is a selector matching nothing ([scenario 07](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes)); endpoints populated with every request refused is a wrong `targetPort` ([scenario 11](https://www.aurorasre.ai/runbooks/fix-service-targetport-mismatch-kubernetes)); both log nginx `connect() failed (111: Connection refused)`, absent here after startup. A real replica failing on its dependency answers differently: the `api`'s code returns 503 `backend unavailable` when redis is unreachable (r1, step 12), not 500 with a traceback.

## How much is affected, and since when?

Every client of the Service, for the share of requests that land on the stray pod. Of the load generator's 27 requests after the stack came up, 12 returned 500, 44 percent (`evidence-before` loadgen log, r1); the alert said 31 percent over ten minutes. Both are right: one of three ready endpoints fails, and kube-proxy's default iptables mode installs rules which "by default, select a backend Pod at random" ([Virtual IPs](https://kubernetes.io/docs/reference/networking/virtual-ips/)), so a one-minute sample wanders around one third.

The start time is the stray Deployment's creation: `creationTimestamp: "2026-10-02T20:29:03Z"` (r1, step 16), nine seconds after `deploy/api` at `20:28:54Z` (step 18); pod ages agree, 62 s against 71 s. The load generator's first 500 is at `20:29:05Z`, after 200s at `20:29:01Z` and `20:29:03Z`. The Service did not change: its `last-applied-configuration` annotation and live spec both read `selector: app: api` (r1, step 17). What changed is the set of pods matching an unchanged selector, so a change log scoped to the Service shows nothing.

## Which pods does the Service actually select?

Cheapest and likeliest first.

**1. Count endpoints against ready replicas.** Three against two (steps 13 and 18, `readyReplicas: 2`). If they match, this runbook does not apply.

**2. Name the owner of each endpoint IP.** r2 printed labels and owner in one command (step 13, three of eight lines):

```shell
kubectl get pods -n shop -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.labels}{" "}{.metadata.ownerReferences[0].name}{"\n"}{end}'
```

```
api-f64486c4c-ffkt4 {"app":"api","pod-template-hash":"f64486c4c"} api-f64486c4c
api-f64486c4c-w8vvz {"app":"api","pod-template-hash":"f64486c4c"} api-f64486c4c
shop-api-69f6fc5964-twjf9 {"app":"api","app.kubernetes.io/name":"shop-api","pod-template-hash":"69f6fc5964"} shop-api-69f6fc5964
```

Quote the template. r1 ran it unquoted (step 21) and got `Error from server (NotFound): pods ".items[*]}{.metadata.name}..." not found`: the shell split it at the first space.

**3. Read the stray pod's spec, not only its log.** The `shop-api` Deployment's env holds only `REDIS_HOST` (r1, step 16) and its readiness probe is `http-get http://:8080/healthz` (r2, step 16), a path that never touches the missing variable. A passing probe and a failing handler is what keeps a broken pod in rotation. `kubectl get secrets -n shop` was empty (r1, step 19).

**4. Check whether the overlap was deliberate.** The Deployment list's selector column shows it fastest (cluster state captured before the investigation): `app=api` for `deployment.apps/api`, `app=api,app.kubernetes.io/name=shop-api` for `deployment.apps/shop-api`. The stray selector is narrower, so it manages only its own pod while the Service takes both sets. The [Deployment docs](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/) warn that you "should not create other Pods whose labels match this selector, either directly, by creating another Deployment, or by creating another controller such as a ReplicaSet or a ReplicationController"; the controllers did not fight here only because the `api` ReplicaSet also selects on `pod-template-hash`, which the Service does not.

**5. Rule out the dependency.** Both `api` pods logged `connected to redis at redis:6379` and served 200s (r1, steps 10 and 11). Redis is on the scenario's list of wrong answers.

## What is the fix?

Take the stray pod out of the Service, then make sure it cannot come back.

**Mitigation: scale the stray Deployment to zero.** r1's working notes named this command, and the graders accepted it:

```shell
kubectl scale deployment shop-api -n shop --replicas=0
```

Risk: low for the Service, whose remaining endpoints are the two healthy pods; `shop-api` stops serving whatever it legitimately served, which in the lab was nothing. Rollback: `--replicas=1`. As the pod terminates, the endpoints controller drops its IP and the 500s stop. Do not restart or scale `deploy/api`: a wrong answer in the scenario, and it changes nothing about which pods the Service matches.

**Fix: give the workload labels the Service does not select.** The label is in the pod template and the Deployment's selector, and the [Deployment docs](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/) say "A Deployment's label selector is immutable after creation; it cannot be updated via kubectl patch, kubectl edit, kubectl apply, or tools like helm upgrade." So this is a delete and a recreate. Save the object first:

```shell
kubectl -n shop get deployment shop-api -o yaml > shop-api.yaml
kubectl -n shop delete deployment shop-api
```

Re-apply with `app: api` removed from both `spec.selector.matchLabels` and `spec.template.metadata.labels`, keeping `app.kubernetes.io/name: shop-api`. Risk: `shop-api` is down between the delete and the re-apply. Rollback: `kubectl apply -f shop-api.yaml`, which also restores the collision. If the stray pods should in fact serve this Service, fix their config instead and let them keep the label. From the graders' note on Aurora's own recreate suggestion: a search-and-replace on `app: api` also matches the selector line and any longer value starting with `api`, so read the diff before applying.

## How do you verify it worked?

Pass condition: the endpoints are exactly the IPs of the Deployment's ready pods, and a full minute of client requests returns 200.

```shell
kubectl -n shop get endpoints api
kubectl -n shop get endpointslices -l kubernetes.io/service-name=api
kubectl -n shop logs deploy/loadgen --tail=30
```

The first must read `10.244.1.37:8080,10.244.2.48:8080` (r1's api pod IPs) with no third address; the EndpointSlice is the same list under the label the [EndpointSlice docs](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/) say "enables simple lookups of all EndpointSlices belonging to a Service". The load generator writes one line every two seconds, so 30 lines is a minute; every line should end `-> 200`. The lab's own check for the broken state passes only when that log holds both a `-> 500` and a `-> 200`; its failure is your pass.

## How do you prevent it?

**Select on two labels, not one.** The [recommended labels](https://kubernetes.io/docs/concepts/overview/working-with-objects/common-labels/) separate `app.kubernetes.io/name`, "The name of the application", from `app.kubernetes.io/instance`, "A unique name identifying the instance of an application", and require that "Every instance of an application must have a unique name." A selector on both cannot be joined by a template copied from another app (different name) or by a second install of the same app (different instance). The lab's stray template had the recommended name label and the colliding `app` label side by side.

**Refuse pod templates without an instance label at admission.** A [ValidatingAdmissionPolicy](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/) (stable since v1.30) matching `apps/v1` Deployments can evaluate `'app.kubernetes.io/instance' in object.spec.template.metadata.labels` where the docs' example evaluates `object.spec.replicas <= 5`. (Composed from the documented example; not run in the lab.)

**Assert endpoint count equals ready replicas.** After each deploy, compare every Service's EndpointSlice address count with the `readyReplicas` of the Deployment it fronts and fail on a mismatch either way. On a schedule it is an alert; in the lab it would have read three against two a minute before the error rate fired.

**Keep per-pod access logs.** The frontend's log shows the status but not the pod: nginx dials the ClusterIP and kube-proxy picks the backend after that. The split was visible only in each backend's own log, which is how both lab runs found it.

## Common mistakes

- **Restarting or scaling `deploy/api`.** New pods, same labels, same three endpoints. A wrong answer in the scenario.
- **Removing the label from the running stray pod.** `kubectl label pod shop-api-... app-` takes it out of the Service and out of its own ReplicaSet's selector, so the ReplicaSet creates a replacement with the label and the orphan keeps running. Fix the Deployment.
- **Adding `DATABASE_URL` to the stray Deployment.** Its code then returns 200 with `{"items": []}` (the `shop-api` ConfigMap, r1, step 20): the error rate drops to zero while a third of responses carry the wrong data from the wrong program. A truncated note in r2's timeline, written as its report completed, begins "Add DATABASE_URL to the shop-api Deployment's"; the graded suggestion that followed was scaling to zero.
- **Blaming redis.** The 500 came with a traceback on an environment variable; the api's redis failure path returns 503, and the healthy pods logged `connected to redis`.

## What we saw in the lab

Scenario 15 in the [Aurora lab](https://github.com/Arvo-AI/aurora-lab): the five-component `shop` app on a three-node kind cluster, then one `kubectl apply` of a ConfigMap and a one-replica Deployment named `shop-api` whose pod template carries `app: api` and `app.kubernetes.io/name: shop-api`, whose code reads `os.environ["DATABASE_URL"]` on every request except `/healthz`, and whose env sets only `REDIS_HOST`. Aurora got the alert text ("HighErrorRate [warning] namespace=shop service=frontend: 31% of requests to /api/ returned 5xx over the last 10 minutes (threshold 5%).") and read-only kubectl under the default `observability_only` policy. Two findings were required: the failing requests come from the `shop-api` pod, selected because it shares the label; and it fails on the missing `DATABASE_URL` while the real api pods are healthy. Two runs, separate organizations.

| Run | Verdict (human reviewed) | Time | Tool calls | Aurora cost |
|---|---|---|---|---|
| r1 | correct | 175 s | 24 | $0.59 |
| r2 | correct | 118 s | 17 | $0.35 |

r1 read pods, events, Services, the frontend log, both api logs and the api's source in its first eight kubectl calls, ran `get endpoints api -o wide` at step 13 (its ninth), put three IPs next to two replicas, and went straight to `--show-labels`, the stray pod's log, both Deployments and the Service. Its report: "causing its single pod (`shop-api-69f6fc5964-jhzz8`, IP 10.244.1.40) to be inadvertently pulled into the `api` Service's endpoint pool alongside the two legitimate `api` Deployment pods", and "so every request routed to this pod throws `KeyError: 'DATABASE_URL'` and returns HTTP 500, confirmed directly in the pod's logs". Both findings met. Two things to hold against it. It called the frontend log "consistent with the alert's reported 31% 5xx error rate" when the sample it cited had 11 of 24 `/api/items` lines at 500, 46 percent; the AI reviewer behind the judge raised the same objection with a looser count ("about 16 of 33"). And one tool call failed, the unquoted jsonpath at step 21, recovered from with the `-o wide` output it already had.

r2 found the pod from the other direction: frontend logs, both api logs, the api Deployment, then the jsonpath above at step 13, where its note reads "Root cause candidate found: a third pod shop-api-69f6fc5964-twjf9 carries label app=api (same as the legitimate api Deployment) and is crashing on every /items request with KeyError: 'DATABASE_URL' because its container is missing a DATABASE_URL env var." It confirmed with the endpoints and `describe deploy shop-api` (steps 15 and 16) and reported "The root cause was a label collision between two unrelated Deployments sharing the `app=api` label." Three over-claims. "Since kube-proxy round-robins across all three registered endpoints": the iptables mode picks at random, which the AI reviewer called loose wording that does not change the conclusion. Its Not Checked list says the stray pod's probe configuration was "not retrieved", but step 16's output contains `Readiness: http-get http://:8080/healthz delay=0s timeout=1s period=5s`; it had the answer and did not read it. And its notes said the alert "fired right as shop-api's readiness probe passed", reading event ages of 2m7s and 2m15s as cluster time; the Deployment was created 57 s before the alert.

Neither report contained a fix. Both judges said so (r1: "It gives no remediation step, such as deleting or relabeling shop-api"; r2: "it recommends no remediation") and graded on the root cause, correct in both. Aurora's separate fix suggestions, generated after each report (20:33:55 and 21:05:22, outside the durations above), were graded acceptable with "Scale `shop-api` Deployment to zero replicas" as the best in both runs; r1's notes also carried the recreate step, "selector is immutable, needs delete+reapply". Neither run named redis, a restart or the network, the scenario's three wrong answers.

## How Aurora handles this

Aurora receives the alert, connects to the cluster read-only through an uploaded kubeconfig, and runs the kubectl commands shown above, choosing them itself. On this failure it counted endpoints against replicas, named the owner of the extra IP, and read the stray pod's log and Deployment spec in both runs. Mutating commands are blocked by policy, so the scale-to-zero arrives as a suggestion for a person to apply. Both reports stopped at the cause; the fix came only in the separate suggestions.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)

Related: [a Service with no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes), [a Service whose targetPort does not match the container](https://www.aurorasre.ai/runbooks/fix-service-targetport-mismatch-kubernetes), [a ConfigMap change that reached only some pods](https://www.aurorasre.ai/runbooks/fix-configmap-change-not-applied-to-all-pods-kubernetes), the [lab report covering all 19 failures](https://www.aurorasre.ai/blog/aurora-lab-report-kubernetes-19-failures), [root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres), and what an [Aurora root cause analysis](https://www.aurorasre.ai/rca) looks like.

## Sources and verification

- Every command and output block above comes from the lab run `v1-run1`, scenario `15-stray-pods-share-label`, on 2026-10-02: Aurora's tool-call transcripts for r1 and r2 and the cluster state captured before the investigation (`evidence-before/`). The fault was injected by the scenario's `break.sh`, one `kubectl apply` of the `shop-api` ConfigMap and Deployment.
- Versions, from `evidence-before/versions.yaml` and the pod logs: Kubernetes server v1.37.0 on kind v0.33.0 (three nodes, containerd 2.3.4), kubectl client v1.35.3; nginx 1.27-alpine reporting nginx/1.27.5; python:3.12-alpine for both api programs.
- Aurora commit `87d22baf`, single agent, RCA model `bedrock/us.anthropic.claude-sonnet-5`. Verdicts: judge model `us.anthropic.claude-opus-5-5`, a second AI reviewer, and a person at Arvo (`human_verdict` in each `grade.json`); fix suggestions graded separately (`fix_from_suggestions`). Durations (incident creation to report), tool calls and costs from each run's `result.json`; suggestion timestamps from each `timeline.json`.
- The 44 percent is 12 of the 27 requests after the three startup 502s in r1's captured load generator log; the 46 percent is 11 of 24 `/api/items` lines in r1's step 8 output.
- Documentation quoted, read 2026-10-19: Kubernetes [Service](https://kubernetes.io/docs/concepts/services-networking/service/), [Labels and Selectors](https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/), [Field Selectors](https://kubernetes.io/docs/concepts/overview/working-with-objects/field-selectors/), [Virtual IPs and Service Proxies](https://kubernetes.io/docs/reference/networking/virtual-ips/), [Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/), [EndpointSlices](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/), [Recommended Labels](https://kubernetes.io/docs/concepts/overview/working-with-objects/common-labels/) and [Validating Admission Policy](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/).
- The scale command is Aurora's own from r1's notes. The delete, save and verify commands, the field-selector lookup, the CEL expression and the endpoint-count assertion were not run inside a graded lab run, since Aurora's policy blocks mutations; Verify states the pass condition and the lab's own check instead of a captured result.

## Questions people ask

**Why does my Kubernetes Service return intermittent 500s when every pod is Ready?**

Because the Service is sending a share of requests to a pod that is not part of the Deployment you are looking at. A Service selects pods by label, so any pod in the namespace carrying the selector's labels becomes a backend. In the lab a second Deployment's pod carried app=api, passed its /healthz readiness probe and answered every other request with 500.

**How do I check whether a Service is selecting pods from another Deployment?**

Run kubectl get endpoints <service> -n <namespace> and compare the number of addresses with the Replicas line of kubectl describe deploy <name>. More endpoints than ready replicas means another workload matches the selector. In the lab the endpoints listed three IPs while the Deployment reported 2 available.

**How do I find which pod an endpoint IP belongs to?**

kubectl get pods -n <namespace> -o wide prints each pod's IP. For one IP across the cluster, kubectl get pods -A -o wide --field-selector status.podIP=<ip> works because status.podIP is a supported Pod field selector. A jsonpath over .metadata.labels and .metadata.ownerReferences[0].name prints the owning ReplicaSet as well; quote the whole template or the shell splits it.

**Why does the stray pod pass its readiness probe if it fails every request?**

The probe hits /healthz, which returns 200 without touching the missing configuration, while /items reads os.environ["DATABASE_URL"] and raises KeyError. A passing probe only proves the probed path works, and a Ready pod stays in the endpoints.

**Why was the error rate 31 percent in the alert but 44 percent in the log sample?**

One of three ready endpoints fails, and kube-proxy in iptables mode selects a backend at random, so short samples scatter around one third. The 31 percent was a ten-minute window; 12 of 27 requests in a one-minute sample were 500. The fraction is the stray pod's share of the endpoints, not a stable constant.

**What is the fix for a Service selecting the wrong pods?**

Scale the stray Deployment to zero to take its pod out of the endpoints, then delete and recreate that Deployment without the colliding label, because a Deployment's selector is immutable in apps/v1. Do not restart the real Deployment; its pods were correct throughout.

**Can I just remove the label from the stray pod?**

Removing it from the running pod also takes the pod out of its own ReplicaSet's selector, so the ReplicaSet creates a replacement with the label and the orphan keeps running. Change the Deployment's template and selector, which means a delete and a recreate.

**How do I prevent label selector overlap between Deployments?**

Have Services select on app.kubernetes.io/name and app.kubernetes.io/instance together rather than a bare app label, enforce the instance label on pod templates with a ValidatingAdmissionPolicy, and assert in CI or on a schedule that each Service's endpoint count equals the ready replica count of the Deployment it fronts.

**Did Aurora find this fault in the lab?**

In both runs it named the stray shop-api pod, the shared app=api label and the missing DATABASE_URL, in 175 s and 118 s, and both were graded correct. One report overstated how well the log sample matched 31 percent, the other said kube-proxy round-robins and claimed it had not retrieved a probe spec that was in its own output. Neither report stated a fix; the separate suggestions did, and were graded acceptable.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-service-selects-wrong-pods-label-overlap-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
