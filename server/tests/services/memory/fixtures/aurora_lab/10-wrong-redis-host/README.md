# Kubernetes Name does not resolve: wrong Service name, the fix

*The app exits with Name does not resolve because an env var names a Service that does not exist. Confirm it, rule out CoreDNS, fix the name. Lab-tested.*

Runbook for lab scenario `10-wrong-redis-host`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-dns-name-not-resolving-kubernetes. Written from the lab runs in this repository; updated 2026-10-15.

## Reproduce it

```
./lab.sh up
./lab.sh break 10-wrong-redis-host
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Any pod that reaches a dependency by a Kubernetes Service name set in an env var or config; here a Python api calling redis |
|---|---|
| Verified on | Kubernetes server v1.37.0 (kind v0.33.0, containerd 2.3.4), 2 October 2026 |
| Lab folder | [scenarios/10-wrong-redis-host](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/10-wrong-redis-host), runs in [runs/v0-run2/10-wrong-redis-host](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v0-run2/10-wrong-redis-host) |

**"Name does not resolve" in a crash-looping container means the hostname the app was given has no DNS record inside the cluster, almost always because an env var or config names a Service that does not exist, while the real Service has a different name.** The dependency itself is usually healthy. Confirm with `kubectl get svc -n <namespace>` next to the hostname in the pod's `Environment` block: if the name is not in the list, the fix is the name, not DNS and not the dependency.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| One Deployment's new pods log `Name does not resolve` or `Name or service not known`, old pods fine | An env var or config points at a Service name that does not exist | `kubectl describe pod <pod>` Environment block against `kubectl get svc -n <ns>` | Set the hostname back to the real Service name, or roll the Deployment back |
| Every pod that resolves a Service name fails at once | CoreDNS is down or misconfigured | `kubectl -n kube-system get pods -l k8s-app=kube-dns` and a lookup of `kubernetes.default` from any pod | Fix CoreDNS; see the Corefile and its logs |
| Name resolves, connection times out | A NetworkPolicy blocks the client, or the backend listens on another port | `kubectl get networkpolicy -n <ns>` and `kubectl get endpoints <svc> -n <ns>` | Allow the traffic or fix the port |
| Name resolves, `Connection refused` | The dependency is down or not listening yet | `kubectl get pods -l app=<dependency> -n <ns>` and its logs | Fix the dependency |

## How do you confirm the name is the problem?

Start with the pod list. The shape to recognise is one new pod crash-looping while the rest of the Deployment keeps running (run r1, step 4):

```
$ kubectl get pods -n shop -o wide
NAME                        READY   STATUS             RESTARTS      AGE     IP            NODE                   NOMINATED NODE   READINESS GATES
api-869d87b484-9n7bc        0/1     CrashLoopBackOff   5 (73s ago)   4m54s   10.244.1.18   aurora-lab-2-worker    <none>           <none>
api-f64486c4c-hb8mm         1/1     Running            0             5m2s    10.244.1.15   aurora-lab-2-worker    <none>           <none>
api-f64486c4c-n4zt7         1/1     Running            0             5m2s    10.244.2.22   aurora-lab-2-worker2   <none>           <none>
redis-59458f55d8-bj68x      1/1     Running            0             5m2s    10.244.2.21   aurora-lab-2-worker2   <none>           <none>
```

Then read the crashing container's log. The container has already exited, so ask for the previous one (run r2, step 6):

```shell
kubectl logs <pod> -n <namespace> --previous --tail=100
```

```
$ kubectl logs api-869d87b484-chjfx -n shop --previous --tail=100
redis connection failed (attempt 1/5): [Errno -2] Name does not resolve
redis connection failed (attempt 2/5): [Errno -2] Name does not resolve
redis connection failed (attempt 3/5): [Errno -2] Name does not resolve
redis connection failed (attempt 4/5): [Errno -2] Name does not resolve
redis connection failed (attempt 5/5): [Errno -2] Name does not resolve
giving up: redis unavailable
```

`[Errno -2] Name does not resolve` is the resolver saying the name has no record (glibc prints `Name or service not known` for the same condition). `Connection refused` and `Connection timed out` are different failures with different causes: see the table above. A redis with no endpoints gives the same crash loop with `[Errno 111] Connection refused` above `giving up` ([CrashLoopBackOff by exit code](https://www.aurorasre.ai/blog/fix-crashloopbackoff-kubernetes)). The exit is clean, code 1, so this is not an OOM kill or a probe kill (run r1, step 7):

```
    State:          Waiting
      Reason:       CrashLoopBackOff
    Last State:     Terminated
      Reason:       Error
      Exit Code:    1
      Started:      Fri, 02 Oct 2026 16:52:53 +0000
      Finished:     Fri, 02 Oct 2026 16:53:04 +0000
    Ready:          False
    Restart Count:  5
    Environment:
      REDIS_HOST:  redis-master
```

The pod ran for 11 seconds: five attempts in 11 seconds, then exit. The hostname it tried is right there in `Environment`.

## How far does it reach?

Three questions: which pods, since when, after which change.

**Which pods.** Only pods from the new ReplicaSet `api-869d87b484` fail. The two `api-f64486c4c` pods are `1/1 Running` with 0 restarts, and so is redis (step 4 above). Traffic is still served by the old pods, so this is a stuck rollout, not an outage. Check what the old pods were given (run r1, step 9):

```
$ kubectl get pod api-f64486c4c-hb8mm -n shop -o jsonpath='{.spec.containers[0].env}'
[{"name":"REDIS_HOST","value":"redis"}]
```

**Since when.** The Deployment's status shows the rollout frozen with one updated replica that never becomes ready (run r1, step 10, status block):

```
status:
  availableReplicas: 2
  conditions:
  - lastTransitionTime: "2026-10-02T16:49:15Z"
    lastUpdateTime: "2026-10-02T16:49:23Z"
    message: ReplicaSet "api-869d87b484" is progressing.
    reason: ReplicaSetUpdated
    status: "True"
    type: Progressing
  ...
  observedGeneration: 2
  readyReplicas: 2
  replicas: 3
  unavailableReplicas: 1
  updatedReplicas: 1
```

**After which change.** The spec says revision 2, and the `last-applied-configuration` annotation still carries the previous value, `"env":[{"name":"REDIS_HOST","value":"redis"}]`, while the live template says `redis-master` (run r1, step 10). That gap between the annotation and the live spec is the signature of an imperative change such as `kubectl set env`, which is exactly what the lab did:

```shell
kubectl -n shop set env deploy/api REDIS_HOST=redis-master
```

`kubectl rollout history deployment/api -n shop` would show the revisions too. In the lab it was blocked by Aurora's read-only command policy (runs r1 and r2, steps 11 and 12); from your own terminal it is the quickest way to see when revision 2 was created.

## How do you isolate the cause?

Cheapest and likeliest first.

**1. Does the Service exist under that name?** (run r1, step 8)

```shell
kubectl get svc -n <namespace>
```

```
$ kubectl get svc -n shop
NAME       TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)    AGE
api        ClusterIP   10.96.180.245   <none>        8080/TCP   5m23s
frontend   ClusterIP   10.96.88.61     <none>        80/TCP     5m23s
redis      ClusterIP   10.96.239.186   <none>        6379/TCP   5m23s
```

There is `redis`, there is no `redis-master`. A normal Service gets a DNS record of the form `my-svc.my-namespace.svc.cluster-domain.example` that "resolves to the cluster IP of the Service", and queries "that don't specify a namespace are limited to the Pod's namespace", per the [DNS for Services and Pods](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/) page. So `redis-master` from a pod in `shop` expands to `redis-master.shop.svc.cluster.local`, which does not exist. Diagnosis done; the next two steps are for when the name is present.

**2. Is cluster DNS working at all?** If the Service exists and the name still fails, test resolution from a pod in the same namespace and check CoreDNS:

```shell
kubectl -n <namespace> run dns-check --rm -it --restart=Never --image=busybox:1.37 -- nslookup <service>
kubectl -n kube-system get pods -l k8s-app=kube-dns
kubectl -n kube-system logs -l k8s-app=kube-dns --tail=50
```

A broken CoreDNS fails every name, including `kubernetes.default`, from every pod. Here redis, the frontend and the worker were all running and the frontend was serving requests through the `api` Service name (frontend and loadgen logs in the captured evidence), so cluster DNS was fine.

**3. Is the dependency itself healthy?** Only relevant once the name resolves. The lab's redis had a live endpoint (run r1, step 12):

```
$ kubectl get endpoints redis -n shop
NAME    ENDPOINTS          AGE
redis   10.244.2.21:6379   5m41s
```

If the client logged `Connection refused` against a resolving name, this is where the problem would be. If it logged a timeout against a resolving name with live endpoints, look at NetworkPolicies next.

## How do you fix it?

**Cause: the hostname names a Service that does not exist.** Set it back to the real name. In the lab the Service is `redis`:

```shell
kubectl -n shop set env deploy/api REDIS_HOST=redis
```

This creates revision 3 with the old value. Risk: low; the old pods are already running with this value. Rollback: `kubectl -n shop rollout undo deploy/api` returns to the previous revision. If the value lives in a manifest in git, change it there and apply, otherwise the next `kubectl apply` reintroduces the bad name.

**Alternative: roll back the deploy.**

```shell
kubectl -n shop rollout undo deploy/api
```

Same effect here because the only change in revision 2 was the env var. Risk: it also undoes anything else that shipped in that revision.

**Cause: the Service really should have that name.** Create it, or rename the existing Service, and then fix every client that uses the old name. Do not leave two Services pointing at the same pods as a workaround; the next person reads it as two dependencies.

Mitigation is not needed: the old pods keep serving while the rollout is stuck, which is the point of `maxUnavailable: 25%`.

## How do you verify the fix?

The pass condition: the new ReplicaSet's pods reach `1/1 Running` with 0 restarts, and their log shows a connection, not a retry loop.

```shell
kubectl -n shop rollout status deploy/api
kubectl -n shop get pods -l app=api
kubectl -n shop logs -l app=api --tail=5
```

A healthy pod logs what the old ones logged (frontend-facing api pod in the captured evidence, `api-f64486c4c-hb8mm`):

```
connected to redis at redis:6379
api listening on :8080
```

The lab's own check for the broken state is one command; its inverse is your pass condition:

```shell
kubectl -n shop get pods -l app=api -o jsonpath='{.items[*].status.containerStatuses[0].lastState.terminated.exitCode}' | grep -q 1
```

Watch for two minutes: the rollout should complete and the old ReplicaSet scale to 0.

## How do you prevent it?

**Alert on the rollout, not only the pod.** A crash-looping pod fires `KubePodCrashLooping`; a stuck rollout is the condition that matters to users of the next version. With kube-state-metrics (a suggested expression, not captured in the lab):

```
kube_deployment_status_replicas_updated != kube_deployment_spec_replicas
```

held for longer than your rollout normally takes.

**Guardrail: fail at build time, not at runtime.** Put Service names in the same manifest as the Service that defines them (Kustomize or Helm values), so a rename touches both at once. For env vars that must hold a hostname, a `kubectl set env` on a live Deployment drifts from the manifest in git; the `last-applied-configuration` gap in Scope is how you catch that after the fact. A pre-deploy check that every hostname in a pod spec matches an existing Service in the namespace is a short script against `kubectl get svc -o name`.

**Make the app say the name.** The lab app prints the hostname only on success (`connected to redis at redis:6379`), not on failure. Log the host and port in the failure line too, and the diagnosis is one `kubectl logs` away.

## Common mistakes

- **Restarting the pod.** It will fail again in 11 seconds. Nothing about a restart changes a name.
- **Blaming CoreDNS.** One Deployment failing while seven other pods resolve names fine is not a DNS outage. Check whether the name exists before touching kube-system.
- **Fixing the dependency.** redis was `1/1 Running` with a live endpoint the whole time. `Name does not resolve` is a client-side message about the name, not a server-side one.
- **Patching the running pod's env.** Pods are immutable; the fix is on the Deployment, and it rolls out a new ReplicaSet.
- **Trusting `kubectl logs` without `--previous`.** The current container may be mid-restart with an empty log; the finished one has the evidence.

## What we saw in the lab

Scenario `10-wrong-redis-host`: `kubectl -n shop set env deploy/api REDIS_HOST=redis-master` on a healthy app, then the alert "KubePodCrashLooping: an api pod in namespace shop is restarting after the latest deploy." Aurora got the alert text and read-only kubectl, nothing else. Two independent runs on 2 October 2026 (Aurora commit 87d22baf, model bedrock/us.anthropic.claude-sonnet-5), each in a fresh org.

| Run | Verdict | Time | Tool calls | Cost | Fix proposed |
|---|---|---|---|---|---|
| r1 | correct (judge and human) | 292 s | 12 | $0.23 | revert `REDIS_HOST` to `redis` on `deployment/api` |
| r2 | correct (judge and human) | 197 s | 16 | $0.34 | set `REDIS_HOST` back to `redis`, or `rollout undo` |

Both runs met both required findings: the api exits because it cannot resolve `redis-master`, and `REDIS_HOST` is `redis-master` while the Service is `redis`. Neither asserted the two wrong answers the scenario lists, redis being down or cluster DNS being broken.

r1's path, in order: list open incidents, search memory (empty), list clusters, `get pods -o wide`, `get events --sort-by=.lastTimestamp`, `logs --tail=100` (the five failed attempts above), `describe pod`, `get svc` (no `redis-master`), the old pod's env (`redis`), the Deployment YAML (revision 2, `redis-master` in the live template), `rollout history` (blocked), `get endpoints redis` (live). Its report: "No Kubernetes Service named `redis-master` exists in the `shop` namespace" and names the real one, `redis` at 10.96.239.186, with a live endpoint.

r2 started the same way, `get pods` then `get events`, and went to `logs --previous` at step 6, then `describe pod`, the Deployment YAML, `get svc`, `get rs -o wide`, the old pod's env, `rollout history` (blocked again), `get nodes`, the api-code ConfigMap (the jsonpath returned nothing, so it read no code), and `top pods`. Its report: "pods from the new ReplicaSet fail DNS resolution for the Redis host" and "the old ReplicaSet's 2 pods, still configured with the correct REDIS_HOST=redis, remain Running and continue serving traffic".

What the runs got wrong or left open:

- **`rollout history` was denied in both runs.** Aurora's default policy denies the whole `rollout` verb, so the read-only `history` and `status` went with the mutating `restart` and `undo`. Both runs reconstructed the change from the ReplicaSets and the annotation instead. The lab's fix branch narrows the deny rule; on that branch scenario 10 ran 2/2 again and `rollout history` was allowed (see the [lab report](https://github.com/Arvo-AI/aurora-lab/blob/main/REPORT.md)).
- **Neither run directly checked the redis pod or CoreDNS.** r2 inferred redis was healthy from the old api pods serving traffic; r1 checked the endpoint. The judge accepted both, but a reader who wants the ruling-out spelled out will not find a `get endpoints redis` in r2.
- **Small inconsistencies in r1's report:** the Deployment timestamp appears as 16:49:15Z in one place and 16:49:23Z in another (creation vs the new ReplicaSet), and "3 desired replicas" reflects the surge pod, not `spec.replicas: 2`.

## How Aurora handles this

Aurora receives the alert, lists the pods and events, reads the previous container's log, compares the new pod template with the running pods and the Service list, and writes the root cause with each claim cited to a tool output, as in the two runs above. It runs under a read-only command policy by default, so it reports the fix and leaves the `set env` or `rollout undo` to you. Where the policy blocks a read-only command, it says so under "Not checked" rather than guessing.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)
- **See it on an incident:** [Aurora root cause analysis](https://www.aurorasre.ai/rca)

Related runbooks: [a Service with no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes) (the name resolves, nothing answers), [OOMKilled](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes) (a crash loop with exit code 137 instead of 1), and the [root cause analysis guide](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres), and [a name that fails only because dnsPolicy None set the wrong search domains](https://www.aurorasre.ai/runbooks/fix-pod-dnsconfig-search-domain-not-resolving-kubernetes).

## Sources and verification

- Lab scenario, fault and check: [scenarios/10-wrong-redis-host](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/10-wrong-redis-host) (`scenario.yaml`, `break.sh`, `check.sh`).
- Captured outputs: [runs/v0-run2/10-wrong-redis-host](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v0-run2/10-wrong-redis-host), r1 and r2: `steps-full.json` (every tool call), `evidence-before/` (cluster state and pod logs before the investigation), `aurora-rca.md`, `grade.json` with the human verdict.
- Versions: Kubernetes server v1.37.0, kubectl v1.35.3, kind v0.33.0, containerd 2.3.4 (`evidence-before/versions.yaml` and `get nodes -o wide` in r2 step 13). Verified 2 October 2026.
- Kubernetes documentation: [DNS for Services and Pods](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/) for the Service record form and namespace scoping, quoted above.
- The kube-state-metrics expression under Prevent is a suggested query, not a captured one.

## Questions people ask

**What does Name does not resolve mean in a Kubernetes pod?**

The resolver found no DNS record for the hostname the app asked for. Inside a cluster that almost always means the name is not a Service in the pod's namespace, for example an env var pointing at redis-master when the Service is called redis.

**How do I tell a wrong Service name from a CoreDNS outage?**

Run kubectl get svc in the namespace. If the name is missing, it is the name. A CoreDNS outage fails every lookup from every pod, including kubernetes.default, and shows up in kube-system as unhealthy coredns pods.

**Why do the old pods keep working while the new ones crash?**

The old ReplicaSet's pods still carry the old env var, redis in the lab, and a rolling update keeps them running until the new pods become ready. The rollout is stuck, but traffic is still served.

**Why does kubectl logs show nothing for the crashing pod?**

The container that failed has exited and a new one may not have written anything yet. Add --previous to read the finished container's log, which holds the Name does not resolve lines.

**Should I fix the Deployment or the pod?**

The Deployment. Pods are immutable; kubectl set env on the Deployment, or kubectl rollout undo, creates a new ReplicaSet with the right value. If the value lives in a manifest in git, fix it there too or the next apply brings the bad name back.

**What exit code does this failure produce?**

Exit code 1, reason Error, after the app gives up retrying. It is not 137, so it is not OOMKilled and not a liveness probe kill. The pod's Last State block shows the code.

**How does Kubernetes turn a short name like redis into an address?**

A normal Service gets a record of the form my-svc.my-namespace.svc.cluster-domain.example that resolves to its cluster IP, and queries without a namespace are limited to the pod's namespace, so redis from a pod in shop expands to redis.shop.svc.cluster.local.

**How did Aurora investigate this in the lab?**

Two runs, both correct and human reviewed, in 292 and 197 seconds for $0.23 and $0.34: pods, events, the previous container log, describe pod, the Service list and the old pod's env, then the Deployment YAML showing revision 2 with REDIS_HOST=redis-master. Both proposed setting it back to redis.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-dns-name-not-resolving-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
