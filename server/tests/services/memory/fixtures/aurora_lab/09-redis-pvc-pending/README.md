# Kubernetes PVC Pending: StorageClass not found, how to fix it

*A PVC stays Pending when its storageClassName names a class the cluster lacks. The pod never schedules and whatever depends on it fails. Lab-tested.*

Runbook for lab scenario `09-redis-pvc-pending`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-pvc-pending-storageclass-not-found-kubernetes. Written from the lab runs in this repository; updated 2026-10-16.

## Reproduce it

```
./lab.sh up
./lab.sh break 09-redis-pvc-pending
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Pods that mount a PersistentVolumeClaim, and the services that depend on them |
|---|---|
| Verified on | Kubernetes server v1.37.0 (kind v0.33.0, provisioner rancher.io/local-path), 2 October 2026 |
| Lab folder | [scenarios/09-redis-pvc-pending](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/09-redis-pvc-pending), runs in [runs/v0-run2/09-redis-pvc-pending](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v0-run2/09-redis-pvc-pending) |

**A PersistentVolumeClaim stays Pending when its `storageClassName` names a StorageClass that does not exist in the cluster, so no volume is ever provisioned and the pod that mounts it is never scheduled.** The alert usually fires somewhere else: the service that depends on that pod starts failing, and that is what pages you. Confirm with `kubectl describe pvc <claim> -n <namespace>`: a `ProvisioningFailed` event reading `storageclass.storage.k8s.io "<name>" not found` is the whole diagnosis. The fix is to point the claim at a class that exists, or to undo the change that introduced it.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| PVC `Pending`, event `storageclass.storage.k8s.io "<name>" not found` | The claim names a StorageClass the cluster does not have | `kubectl describe pvc <claim>` against `kubectl get storageclass` | Recreate the claim with an existing class, or revert the change |
| PVC `Pending`, no events, class exists | `volumeBindingMode: WaitForFirstConsumer` and no pod has claimed it yet | `kubectl get storageclass <name> -o yaml` | Nothing: binding happens when the pod is scheduled |
| PVC `Pending`, event from the provisioner about quota, zone or size | The class exists but the provisioner cannot satisfy the request | `kubectl describe pvc <claim>`, the provisioner's logs | Fix the request or the provisioner |
| Pod `Pending` with `pod has unbound immediate PersistentVolumeClaims` | Any of the above, seen from the pod | `kubectl describe pod <pod>` then the PVC | Fix the claim; the pod schedules on its own |

## How do you confirm the claim is the problem?

Start from the alert and work back to the volume. In the lab the alert was a crash-looping api pod and 5xx on the frontend. The pod list already shows two different problems in the same namespace (lab run r1, step 4):

```
NAME                        READY   STATUS             RESTARTS       AGE     IP            NODE
api-5cf6469845-4khjs        0/1     CrashLoopBackOff   5 (119s ago)   5m59s   10.244.1.17   aurora-lab-3-worker
api-f64486c4c-67cfn         1/1     Running            0              6m9s    10.244.1.14   aurora-lab-3-worker
api-f64486c4c-dn9wx         1/1     Running            0              6m9s    10.244.2.16   aurora-lab-3-worker2
redis-5fff65475c-t2qf5      0/1     Pending            0              5m58s   <none>        <none>
```

A pod that is `Pending` with no node and no IP for six minutes has not been scheduled. Read the claim it mounts:

```shell
kubectl describe pvc redis-data -n shop
```

Run r2, step 13:

```
Name:          redis-data
Namespace:     shop
StorageClass:  fast-ssd
Status:        Pending
Volume:
Used By:       redis-5fff65475c-v7qqm
Events:
  Type     Reason              Age              From                         Message
  ----     ------              ----             ----                         -------
  Warning  ProvisioningFailed  6s (x9 over 2m)  persistentvolume-controller  storageclass.storage.k8s.io "fast-ssd" not found
```

`Volume` is empty and the persistentvolume-controller says why, nine times in two minutes. Compare with what the cluster actually has (r1, step 12):

```
NAME                 PROVISIONER             RECLAIMPOLICY   VOLUMEBINDINGMODE      ALLOWVOLUMEEXPANSION   AGE
standard (default)   rancher.io/local-path   Delete          WaitForFirstConsumer   false                  25m
```

One class, named `standard`. The claim asks for `fast-ssd`. That is the fault.

## How far does it reach?

The claim blocks one pod, but the pod is a dependency, so count what depends on it. The redis Deployment is at zero of one, and the api Deployment is mid-rollout (captured before the investigation, r1 `evidence-before/get-all.txt`):

```
NAME                       READY   UP-TO-DATE   AVAILABLE   AGE
deployment.apps/api        2/2     1            2           71s
deployment.apps/redis      0/1     1            0           71s

NAME                                            ADDRESSTYPE   PORTS     ENDPOINTS
endpointslice.discovery.k8s.io/redis-69z5b      IPv4          <unset>   <unset>
```

The redis Service has no endpoints. Every api pod talks to redis, so every api pod is affected, in two different ways. The new api pod cannot start (r1, step 5, `kubectl logs api-5cf6469845-4khjs -n shop --tail=100 --previous`):

```
redis connection failed (attempt 1/5): [Errno 111] Connection refused
redis connection failed (attempt 2/5): [Errno 111] Connection refused
redis connection failed (attempt 3/5): [Errno 111] Connection refused
redis connection failed (attempt 4/5): [Errno 111] Connection refused
redis connection failed (attempt 5/5): [Errno 111] Connection refused
giving up: redis unavailable
```

A name that does not resolve gives the same crash loop; the errno on the line above `giving up` tells them apart ([CrashLoopBackOff by exit code](https://www.aurorasre.ai/blog/fix-crashloopbackoff-kubernetes)).

The two old api pods stay `Running` and `Ready`, because their health check does not touch redis, and fail every real request (r1, step 10, `kubectl logs api-f64486c4c-67cfn -n shop --tail=50`, trimmed):

```
10.244.1.1 "GET /healthz HTTP/1.1" 200 -
request failed: redis error: [Errno 111] Connection refused
10.244.1.15 "GET /items HTTP/1.0" 503 -
10.244.1.1 "GET /healthz HTTP/1.1" 200 -
```

So the blast radius is the whole `/api/` path, while the pod list shows two healthy api pods. Since when: the redis Deployment is at revision 2 and the claim was created at the same minute the api got its `restartedAt` annotation (r1, step 7 for the api annotation, steps 13 and 14 for the redis revision and the claim's `creationTimestamp: "2026-10-02T16:37:19Z"`). The change and the outage share a timestamp.

## Which of the four causes is it?

Cheapest check first. Each one takes a single command and the output decides it.

1. **Does the class exist?** `kubectl get storageclass`. If the name on the claim is not in the list, you are done: this is the cause. The event text is explicit, `storageclass.storage.k8s.io "fast-ssd" not found`.
2. **Is the class waiting for a consumer?** If the class exists with `VOLUMEBINDINGMODE WaitForFirstConsumer`, a claim with no pod stays `Pending` by design and shows no `ProvisioningFailed` event. Schedule the pod; the claim binds then.
3. **Is the provisioner refusing?** If the class exists and the event comes from the provisioner rather than the persistentvolume-controller, read it: out of quota, wrong zone, size not supported. The claim is fine; the backend is not.
4. **Is the pod blocked by something else?** `kubectl describe pod <pod>` on the Pending pod. In the lab the scheduler said exactly one thing (r1, step 8):

```
Warning  FailedScheduling  75s (x2 over 6m25s)  default-scheduler  0/3 nodes are available: pod has unbound immediate PersistentVolumeClaims. not found
```

No node is excluded for CPU, memory, taints or affinity. The only reason is the unbound claim. If the message lists nodes with `Insufficient cpu` or a taint instead, the volume is not your problem.

Read the claim's own manifest to see whether the class name is a typo or a class that exists elsewhere (r1, step 14, trimmed):

```yaml
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
  storageClassName: fast-ssd
  volumeMode: Filesystem
status:
  phase: Pending
```

## How do you fix it?

Two acceptable fixes. Pick by how much you trust the change that introduced the claim.

**Recreate the claim with a class that exists.** `storageClassName` is immutable on a PVC, so you cannot patch it. Delete the Pending claim and create it again. The lab's claim came from a manifest; this is that manifest with the class corrected:

```shell
kubectl -n shop delete pvc redis-data
kubectl -n shop apply -f - <<'YAML'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: redis-data
  namespace: shop
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: standard
  resources:
    requests: {storage: 1Gi}
YAML
```

Risk: deleting a Pending claim deletes nothing on disk, because no volume was ever bound. Check `Volume` is empty in `describe pvc` before you delete; a bound claim with `reclaimPolicy: Delete` would take its data with it. Rollback: delete the new claim.

**Or revert the Deployment to the revision without the volume.** `kubectl rollout undo deployment/redis -n shop` brings back the pod template that did not mount the claim. Risk: whatever that change was meant to add (here, persistence) is gone again. Rollback: `kubectl rollout undo` once more.

Do not restart the api pods and do not raise their resources. The crash loop is a symptom; once redis has endpoints the new api pod comes up on its own.

## How do you verify it?

The claim binds, the pod schedules, the dependents recover. The lab's own check for the broken state is two conditions; both must turn false:

```shell
kubectl -n shop get pvc redis-data -o jsonpath='{.status.phase}'
kubectl -n shop get pods -l app=api -o jsonpath='{.items[*].status.containerStatuses[0].restartCount}'
```

Pass when the first prints `Bound` and the restart counts stop climbing. Then watch the service the alert came from for two minutes: `kubectl -n shop logs deploy/loadgen --tail=20` should show `GET /api/items -> 200` instead of the `503` lines the lab recorded every two seconds. Expect the api pod to need one more restart after redis is up: it only connects at startup.

## How do you prevent it?

**Alert on claims that stay Pending.** With kube-state-metrics, a suggested expression (not captured in the lab):

```
kube_persistentvolumeclaim_status_phase{phase="Pending"} == 1
```

Hold it for five minutes to skip claims that are legitimately waiting for a consumer. The lab's alert fired on the api crash loop, two hops from the cause; this one fires on the cause.

**Guardrail: validate class names before apply.** A one-line check in CI catches the typo before the cluster does:

```shell
kubectl get storageclass "$(yq '.spec.storageClassName' pvc.yaml)" >/dev/null
```

**Do not use `Recreate` on a stateful singleton without a working claim.** The redis Deployment's `strategy: type: Recreate` (r1, step 13) terminated the old pod before the new one could schedule, which is why the outage was total rather than partial. With `RollingUpdate` the old redis pod would have kept serving while the new one sat Pending.

## Common mistakes

- **Treating the alerted pod as the cause.** The api crash loop is real but secondary. Restarting api, raising its memory or rolling it back changes nothing while redis has no endpoints.
- **Trusting `Running` and `Ready`.** Two api pods were both, and both returned 503 on every request. A health check that does not exercise the dependency hides the dependency's outage.
- **Patching `storageClassName` in place.** The field is immutable after creation; the apply fails. Delete and recreate.
- **Deleting a claim that is bound.** The Pending claim has no volume, so deleting it is free. A bound claim with a `Delete` reclaim policy is not. Read `Volume:` first.
- **Expecting the class to be created for you.** The persistentvolume-controller retries forever (`x9 over 2m`) and never gives up; the claim will not fail loudly, it will sit there.

## What we saw in the lab

Two runs on 2 October 2026, fresh user and org each time, Aurora commit 87d22baf on bedrock/us.anthropic.claude-sonnet-5, read-only kubectl under the default `observability_only` policy. Both correct.

| Run | Verdict | Time | Tool calls | Cost |
|---|---|---|---|---|
| r1 | correct (human reviewed) | 406 s | 14 | $0.30 |
| r2 | correct (human reviewed) | 155 s | 19 | $0.47 |

r1 went from the alerted pod straight to the dependency: `get pods`, the crashing pod's previous log, `describe pod` on it, the api Deployment, then `describe pod` on the Pending redis pod, events, a healthy api pod's log, `get pvc`, `get storageclass`, the redis Deployment YAML and finally the claim's YAML. Its report named the chain end to end: "The root cause was a PersistentVolumeClaim (`redis-data`) created with `storageClassName: fast-ssd` [14], a storage class that does not exist in this cluster", and it ruled out OOM and an api code defect with the exit code and the clean log. The judge's note: the report never recommends a remediation. The fix was in Aurora's separate Suggested Next Steps list, where it proposed recreating the claim with `standard`; graded on everything Aurora shows the user, the fix is acceptable. r1 took 406 seconds, the slowest run in v0-run2. The lab report's Limits section attributes the delay to Aurora's own prediscovery jobs competing for its four worker slots during that batch, not to the investigation itself.

r2 was faster and wider: it read the api source from the ConfigMap to prove that the api pings redis on every request, read both Deployments, `get storageclass`, `describe pvc`, a healthy api pod's log and the load generator's log, and tried `rollout history` on both Deployments, which the policy refused (steps 16 and 17: "Command blocked by organization policy: Mutating kubectl operations"). Its report added the mechanism the alert hides: "The older, already-Ready `api` pods continued passing health checks but failed every real request due to per-request Redis calls". It also ruled out a NetworkPolicy by the error type: "connection failures returned `Connection refused` (no listener), not a timeout". r2 gave no fix in Suggested Next Steps; the judge marked the fix as implied only in the report. Graded on everything Aurora showed the user, the two fixes it did offer, switch the claim to `standard` or remove it, were both acceptable.

The same scenario is also the lab's best-known miss. In the superseded first batch (v0-run1), Aurora had found the claim and the missing class by step 10, then made 25 calls to its artifact tool, many with no content argument, and spent about 11 of 15 minutes and $2 of $2.61 retrying them. The report that came out called the cause "a dependency the cluster could not satisfy" and never named the claim. The investigation notes did, so on full output it counts as correct, but the report was wrong. The lab report traces it to two Aurora bugs, a missing `max_tokens` on Bedrock calls and artifact writes counting as evidence, both patched on the fix branch; the rerun of scenario 09 on that branch was 2 of 2 correct with a median of 182 seconds against 280 before. None of it recurred in v0-run2.

## How Aurora handles this

Aurora receives the alert for the crashing api pod, lists the pods, reads the crashing container's previous log and the Pending pod's events, then follows the claim to the StorageClass list and reads the Deployment and PVC manifests to date the change. Each sentence of the root cause cites the command output it came from, and the two runs above show the same chain found two different ways. The investigation runs under the default read-only command policy; the fix is proposed, not applied.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)

Related: [a hostname that does not resolve](https://www.aurorasre.ai/runbooks/fix-dns-name-not-resolving-kubernetes), [a Service with no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes), [root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres), and what an [Aurora root cause analysis](https://www.aurorasre.ai/rca) looks like.

## Sources and verification

Every command output on this page is from the lab's captured cluster state or Aurora's tool transcripts for scenario 09, run v0-run2 (2 October 2026), on Kubernetes v1.37.0 under kind v0.33.0 with the rancher.io/local-path provisioner: [scenario folder](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/09-redis-pvc-pending), [run folder](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v0-run2/09-redis-pvc-pending), [lab report](https://github.com/Arvo-AI/aurora-lab/blob/main/REPORT.md). The fix is the scenario's `break.sh` with the class name corrected; the verify step is its `check.sh`. StorageClass and claim semantics: the Kubernetes [persistent volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/) and [storage classes](https://kubernetes.io/docs/concepts/storage/storage-classes/) pages. The alert expression is a suggestion, not a captured query. Verified 16 October 2026.

## Questions people ask

**Why is my PersistentVolumeClaim stuck in Pending?**

Most often its storageClassName names a StorageClass that does not exist in the cluster. kubectl describe pvc shows a ProvisioningFailed event reading storageclass.storage.k8s.io "<name>" not found, and Volume stays empty. Compare the name with kubectl get storageclass.

**Why does the pod say pod has unbound immediate PersistentVolumeClaims?**

The scheduler will not place a pod whose claim has no bound volume. Fix the claim and the pod schedules on its own; the pod is not the problem.

**Can I change storageClassName on an existing PVC?**

No. The field is immutable after creation. Delete the Pending claim and create it again with a class that exists. A Pending claim has no volume, so deleting it loses no data.

**Why did the alert fire on a different service?**

In the lab the alert was an api pod crash-looping. The api only connects to redis at startup, redis had no endpoints because its pod could not schedule, and the pod could not schedule because the claim was Pending. Follow the dependency chain back to the volume.

**Why are two pods Running and Ready while every request fails?**

Their health check does not touch the dependency. The lab's api pods answered /healthz with 200 and /items with 503 on every request, logging redis error: [Errno 111] Connection refused each time. A probe that skips the dependency hides its outage.

**Is a Pending PVC always a problem?**

No. A class with volumeBindingMode WaitForFirstConsumer leaves the claim Pending, with no ProvisioningFailed event, until a pod that uses it is scheduled. The event text tells the two apart.

**How do I alert on this before users see errors?**

With kube-state-metrics, alert when kube_persistentvolumeclaim_status_phase{phase="Pending"} stays at 1 for five minutes. That fires on the cause instead of on the crash loop two hops away.

**How did Aurora do on this failure in the lab?**

Two of two runs correct in the published batch, 155 and 406 seconds, with the full chain from the crashing api pod to the missing fast-ssd class. An earlier superseded run found the same cause but lost it in the report while retrying a broken tool call, a bug that was patched and did not recur.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-pvc-pending-storageclass-not-found-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
