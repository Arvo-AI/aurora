# Kubernetes exceeded quota: ResourceQuota blocks a rollout, fix

*A Deployment rollout that never progresses, with FailedCreate and exceeded quota on the new ReplicaSet. Confirm the quota, give it headroom, verify.*

Runbook for lab scenario `14-resourcequota-rollout`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-resourcequota-exceeded-rollout-stuck-kubernetes. Written from the lab runs in this repository; updated 2026-10-16.

## Reproduce it

```
./lab.sh up
./lab.sh break 14-resourcequota-rollout
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Kubernetes Deployments with a RollingUpdate strategy in a namespace that has a ResourceQuota on requests or limits |
|---|---|
| Verified on | Kubernetes server v1.37.0, kind v0.33.0, kubectl v1.35.3, 2 October 2026 |
| Lab folder | [scenarios/14-resourcequota-rollout](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/14-resourcequota-rollout), runs in [runs/v1-run1/14-resourcequota-rollout](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v1-run1/14-resourcequota-rollout) |

**A rollout stuck on "exceeded quota" means the namespace ResourceQuota has no room for the extra pod a rolling update creates, so the new ReplicaSet cannot create any pod at all.** No pod object exists, so there are no pod events and no logs; the evidence is on the ReplicaSet. The old pods keep serving, so this is a stuck rollout, not an outage. Confirm with `kubectl describe rs <new-replicaset> -n <namespace>` and read the `FailedCreate` event; the fix is to give the quota headroom for one surge pod, or to roll out without a surge.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| New ReplicaSet `0 current / 1 desired`, `FailedCreate`, message contains `exceeded quota` | A ResourceQuota is fully used; the surge pod is rejected at admission | `kubectl describe resourcequota -n <namespace>` shows Used equal to Hard | Raise the quota or set `maxSurge: 0`, `maxUnavailable: 1` |
| New pod exists and sits `Pending` with `FailedScheduling` | No node fits the pod; the quota is not involved | `kubectl describe pod <pod>` Events | Fix requests, taints or node capacity |
| New pod exists in `ImagePullBackOff` | The image reference is wrong or private | `kubectl describe pod <pod>` Events | See the [ImagePullBackOff runbook](https://www.aurorasre.ai/runbooks/fix-imagepullbackoff-kubernetes) |
| `FailedCreate` with a validation error, no `exceeded quota` | The pod template is invalid | `kubectl describe rs <new-replicaset>` Events | Fix the template field the error names |

## How do you confirm the quota is what blocks the rollout?

Start with the Deployment. In the lab, the alert was `KubeDeploymentRolloutStuck` for `shop/api`. Aurora's first command (lab run r1, step 4) showed the shape of a stuck rollout: ready, available, and nothing updated.

```shell
kubectl get deployment api -n shop -o wide
```

```
NAME   READY   UP-TO-DATE   AVAILABLE   AGE   CONTAINERS   IMAGES               SELECTOR
api    2/2     0            2           64s   api          python:3.12-alpine   app=api
```

`UP-TO-DATE 0` with `AVAILABLE 2` means the old pods serve and the new revision has not produced a single pod. Look at the ReplicaSets (r1, step 6):

```shell
kubectl get rs -n shop
```

```
NAME                  DESIRED   CURRENT   READY   AGE
api-57f65dc5db        1         0         0       64s
api-f64486c4c         2         2         2       66s
```

The new ReplicaSet wants 1 pod and has 0. A pod that was never created is the signature of an admission rejection, and the reason is on the ReplicaSet, not on any pod. Describe it (r1, step 7, trimmed to the condition and the first events):

```shell
kubectl describe rs api-57f65dc5db -n shop
```

```
Replicas:       0 current / 1 desired
Pods Status:    0 Running / 0 Waiting / 0 Succeeded / 0 Failed
...
Conditions:
  Type             Status  Reason
  ----             ------  ------
  ReplicaFailure   True    FailedCreate
Events:
  Type     Reason        Age                From                   Message
  ----     ------        ----               ----                   -------
  Warning  FailedCreate  75s                replicaset-controller  Error creating: pods "api-57f65dc5db-k2g2k" is forbidden: exceeded quota: compute-resources, requested: requests.memory=32Mi, used: requests.memory=200Mi, limited: requests.memory=200Mi
  Warning  FailedCreate  75s                replicaset-controller  Error creating: pods "api-57f65dc5db-zqwlr" is forbidden: exceeded quota: compute-resources, requested: requests.memory=32Mi, used: requests.memory=200Mi, limited: requests.memory=200Mi
  Warning  FailedCreate  33s (x5 over 72s)  replicaset-controller  (combined from similar events): Error creating: pods "api-57f65dc5db-fvv46" is forbidden: exceeded quota: compute-resources, requested: requests.memory=32Mi, used: requests.memory=200Mi, limited: requests.memory=200Mi
```

That one message is the whole diagnosis: the quota is named (`compute-resources`), the dimension (`requests.memory`), what the pod asked for (32Mi), what is used (200Mi) and the limit (200Mi). The [ResourceQuota documentation](https://kubernetes.io/docs/concepts/policy/resource-quotas/) describes the mechanism: "If creating or updating a resource violates a quota constraint, the control plane rejects that request with HTTP status code 403 Forbidden. The error includes a message explaining the constraint that would have been violated." The ReplicaSet controller receives that 403 and records it as `FailedCreate`.

## How much is affected, and since when?

The rollout is stuck; the service is not down. Check the Deployment conditions and the two ReplicaSets together (r1, step 9, trimmed):

```shell
kubectl describe deployment api -n shop
```

```
Replicas:               2 desired | 0 updated | 2 total | 2 available | 1 unavailable
StrategyType:           RollingUpdate
RollingUpdateStrategy:  25% max unavailable, 25% max surge
Pod Template:
  Annotations:  kubectl.kubernetes.io/restartedAt: 2026-10-02T16:28:16-04:00
...
Conditions:
  Type             Status  Reason
  ----             ------  ------
  Available        True    MinimumReplicasAvailable
  Progressing      True    NewReplicaSetCreated
  ReplicaFailure   True    FailedCreate
OldReplicaSets:    api-f64486c4c (2/2 replicas created)
NewReplicaSet:     api-57f65dc5db (0/1 replicas created)
```

`Available True` with `ReplicaFailure True` is the stuck-but-serving state. The `restartedAt` annotation tells you what started the rollout and when: here a `kubectl rollout restart`. The quota's own creation time (r1, step 13, `creationTimestamp: "2026-10-02T20:28:16Z"`) tells you whether the quota is new. In the lab both happened within seconds, which is what the scenario does on purpose; in production a quota that has been there for months and a rollout that fails today means usage grew until it reached the ceiling.

Every pod in the namespace was still Running (r1, step 5), so nothing else was affected:

```
NAME                        READY   STATUS    RESTARTS   AGE
api-f64486c4c-lr8pl         1/1     Running   0          61s
api-f64486c4c-rjvtf         1/1     Running   0          61s
frontend-76d98597b9-bb5qs   1/1     Running   0          61s
frontend-76d98597b9-x6jst   1/1     Running   0          61s
loadgen-7c6676864-gz6mq     1/1     Running   0          61s
redis-59458f55d8-7nc8l      1/1     Running   0          61s
worker-6cf88d4b58-5sczh     1/1     Running   0          61s
```

## Which quota, and why is there no room?

1. **Read the quota.** Used against Hard, per dimension (r1, step 10):

   ```shell
   kubectl describe resourcequota compute-resources -n shop
   ```

   ```
   Name:            compute-resources
   Namespace:       shop
   Resource         Used   Hard
   --------         ----   ----
   requests.memory  200Mi  200Mi
   ```

   Used equal to Hard, on exactly the dimension the `FailedCreate` message names. Any new pod that requests memory is rejected until something frees requests or the limit moves.

2. **Add up who holds the requests.** One request per pod, from the pod specs (r1, step 11):

   ```shell
   kubectl get pods -n shop -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.containers[0].resources.requests.memory}{"\n"}{end}'
   ```

   ```
   api-f64486c4c-lr8pl 32Mi
   api-f64486c4c-rjvtf 32Mi
   frontend-76d98597b9-bb5qs 16Mi
   frontend-76d98597b9-x6jst 16Mi
   loadgen-7c6676864-gz6mq 8Mi
   redis-59458f55d8-7nc8l 32Mi
   worker-6cf88d4b58-5sczh 64Mi
   ```

   32 + 32 + 16 + 16 + 8 + 32 + 64 = 200Mi. The namespace sits exactly at its limit, so there is no 32Mi for a third api pod. Quote the shell string as shown; run r2 (step 11) sent the jsonpath unquoted and got `Error from server (NotFound)` back, then used `-o custom-columns` instead (step 13).

3. **Work out how many surge pods the rollout needs.** From the strategy (r1, step 14):

   ```shell
   kubectl get deployment api -n shop -o jsonpath='{.spec.strategy}{"\n"}'
   ```

   ```
   {"rollingUpdate":{"maxSurge":"25%","maxUnavailable":"25%"},"type":"RollingUpdate"}
   ```

   Per the [Deployment documentation](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/), for `maxSurge` "The absolute number is calculated from the percentage by rounding up." and for `maxUnavailable` "The absolute number is calculated from percentage by rounding down." With 2 replicas, 25% surge rounds up to 1 extra pod and 25% unavailable rounds down to 0, so the controller must create a third pod before it may remove an old one. That third pod is the 32Mi the quota refuses.

4. **Rule out the look-alikes.** A scheduling problem leaves a pod object in `Pending` with a `FailedScheduling` event; here no pod exists. An image problem leaves a pod in `ImagePullBackOff`; the image was unchanged (`python:3.12-alpine` on both ReplicaSets, r1 steps 4 and 6). An invalid template also produces `FailedCreate`, but the message is a validation error, not `exceeded quota`.

## How do you fix it?

Pick one of three, in this order of preference.

**Raise the quota so one surge pod fits.** The lab's quota needs at least 232Mi (200Mi used plus the 32Mi surge pod); round to something with margin.

```shell
kubectl -n shop patch resourcequota compute-resources --type merge -p '{"spec":{"hard":{"requests.memory":"256Mi"}}}'
```

Risk: low. The quota is a ceiling on requests, not a change to any pod. Rollback: patch it back to the previous value. The rollout resumes on its own; the ReplicaSet controller keeps retrying.

**Free requests in the namespace.** Scale down or delete something that holds requests you do not need. Risk: whatever that workload does stops. Rollback: scale it back up.

**Roll out without a surge.** If the quota is policy and cannot move, change the strategy so the controller removes an old pod before adding a new one:

```shell
kubectl -n shop patch deployment api --type merge -p '{"spec":{"strategy":{"rollingUpdate":{"maxSurge":0,"maxUnavailable":1}}}}'
```

Tradeoff: with 2 replicas you serve from 1 pod while each new pod starts, so capacity drops by half during the rollout. Risk: a bad new revision takes a serving pod with it before you see it fail. Rollback: patch the strategy back to 25% and 25%. The documentation notes the pair cannot both be 0.

In the lab, both of Aurora's runs proposed the first option in their suggestions list: raise `requests.memory` on `compute-resources` (to 256Mi in r1, 232Mi in r2), with `maxSurge 0 / maxUnavailable 1` as the stopgap. Neither fix was applied in a graded run.

## How do you know it is fixed?

The new ReplicaSet reaches its desired count and the Deployment stops reporting `ReplicaFailure`:

```shell
kubectl -n shop rollout status deploy/api --timeout=120s
kubectl -n shop get rs -l app=api
kubectl -n shop describe resourcequota compute-resources
```

Pass condition: `rollout status` prints `successfully rolled out`, the new ReplicaSet shows `2 2 2`, the old one `0 0 0`, and Used is below Hard again once the old pods are gone. Watch for one full rollout. The lab's own check for the broken state is one line, and the pass is its absence for new events:

```shell
kubectl -n shop get events --field-selector reason=FailedCreate -o jsonpath='{.items[*].message}' | grep -q 'exceeded quota'
```

Old `FailedCreate` events stay in the list until they age out, so judge by the ReplicaSet counts, not by that grep alone.

## How do you stop it happening again?

**Alert before the quota is full.** With kube-state-metrics, `kube_resourcequota` carries both `type="used"` and `type="hard"` series per resource; alert when used reaches a percentage of hard that leaves less than one surge pod of headroom (suggested query, not captured in the lab):

```
max by (namespace, resourcequota, resource) (kube_resourcequota{type="used"}) / max by (namespace, resourcequota, resource) (kube_resourcequota{type="hard"}) > 0.9
```

**Size quotas for rollouts, not for steady state.** A namespace quota must cover steady-state requests plus one surge pod for the largest Deployment that rolls out (or `maxSurge` worth of pods, if higher). Review the sum whenever a request value or a replica count changes.

**Alert on the condition, not only on progress.** The Deployment sets `ReplicaFailure True` on the first rejected create (r1, step 9), long before a 15-minute rollout-stuck alert. With kube-state-metrics, `kube_deployment_status_condition{condition="ReplicaFailure",status="true"}` is the series to alert on (suggested, not captured in the lab).

## Common mistakes

- **Looking for a pod that does not exist.** `kubectl get pods` shows only the old, healthy pods, and `kubectl logs` has nothing new. The evidence is on the ReplicaSet and in the namespace events.
- **Treating it as a scheduling problem.** `FailedCreate` happens at admission, before the scheduler sees anything. Adding nodes changes nothing.
- **Deleting the old pods to make room.** That frees quota, but you serve from zero pods until the new one starts, and if the new revision is broken you have an outage instead of a stuck rollout.
- **Raising the quota by exactly the surge pod.** The next Deployment to roll out hits the same wall. Leave headroom for the largest surge in the namespace.
- **Forgetting that limits can be quotaed too.** This lab quotas `requests.memory`; a quota on `limits.memory` or `requests.cpu` fails the same way with a different dimension in the message.

## What we saw in the lab

This scenario is from run v1-run1 (2 October 2026), the harder set: the quota was created with the namespace already at exactly its limit, then `kubectl rollout restart deploy/api` was issued. Aurora received only the alert text and read-only kubectl under its default `observability_only` policy.

| Run | Verdict | Time | Tool calls | Cost | Fix proposed |
|---|---|---|---|---|---|
| r1 | correct (human reviewed) | 108 s | 14 (1 error) | $0.24 | raise the quota to 256Mi, or maxSurge 0 / maxUnavailable 1 |
| r2 | correct (human reviewed) | 144 s | 19 (2 errors) | $0.39 | raise the quota to 232Mi, or maxSurge 0 / maxUnavailable 1 |

Both runs met both required findings: the new ReplicaSet cannot create pods because the `compute-resources` `requests.memory` quota is exhausted, and the old pods still serve. Neither asserted the listed wrong answers (node capacity, image pull, application crash). r1's report put it this way: "Because the quota had zero headroom, every attempt by the new ReplicaSet to create its surge pod was rejected by quota admission control, producing repeated FailedCreate events on `api-57f65dc5db` [7, 8] and permanently stalling the rollout."

r1's path, in order: Deployment, pods, ReplicaSets, describe the new ReplicaSet (the `FailedCreate` events above), namespace events, describe the Deployment, describe the quota, per-pod memory requests, `rollout history` (refused by policy, see below), the quota YAML, the strategy. Eleven kubectl calls to the answer; the other 3 of the 14 were the incident list, a memory search and the cluster lookup. r1 wrote no memory.

r2 reached the same evidence in a different order: Deployment, pods, describe the Deployment, ReplicaSets, events, describe the quota, a per-pod request query that failed because the jsonpath was unquoted, `rollout history` (refused), the same query again with `custom-columns`, describe the new ReplicaSet, the quota YAML, nodes. Twelve kubectl calls, two of them errors; the other 7 of the 19 were the incident list, two memory searches, the cluster lookup, the incident fetch and two memory writes.

The reports had two slips the judge flagged: both called the alert high-severity when it was a warning, and r1's timeline said the rollout began at 20:28:14Z, two seconds before the quota's 20:28:16Z. That earlier time is the Deployment's creation time, not the rollout's: the `restartedAt` annotation in the same output (r1, step 9) is 16:28:16-04:00, the same second as the quota, and the scenario creates the quota first. The judge also flagged r2's "1 unavailable" as a slip; kubectl prints exactly that (r1, step 9), counting the surge pod that was never created. Neither report stated a remediation; the fixes were in Aurora's separate suggestions list and were graded acceptable in both runs.

`kubectl rollout history` is read-only, but Aurora's default policy denied it in both runs (r1 step 12, r2 step 12) because the deny rule matches the whole `rollout` verb. Aurora used the `restartedAt` annotation and the two ReplicaSets instead.

## How Aurora handles this

Aurora receives the alert, reads the Deployment and its ReplicaSets, finds the `FailedCreate` event on the one that has no pods, reads the quota it names and adds up the requests that fill it, then writes the root cause with each claim cited to a command output, as in the two runs above. It states that the old pods still serve and separates the quota from scheduling, image and application causes. The fix goes in a suggestions list the on-call can apply or reject. See an investigation end to end on the [root cause analysis](https://www.aurorasre.ai/rca) page.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)

## Sources and verification

- Lab scenario [14-resourcequota-rollout](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/14-resourcequota-rollout): `scenario.yaml`, `break.sh`, `check.sh`. Runs [v1-run1/14-resourcequota-rollout](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v1-run1/14-resourcequota-rollout), r1 and r2, 2 October 2026: every output block above is pasted from those transcripts and captured cluster state, trimmed but not altered.
- Versions: Kubernetes server v1.37.0, kubectl v1.35.3, kind v0.33.0, containerd 2.3.4 (lab `versions.yaml` and r2 step 16).
- [Resource Quotas](https://kubernetes.io/docs/concepts/policy/resource-quotas/), [Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/), Kubernetes documentation, quoted 16 October 2026.
- Related: [ImagePullBackOff](https://www.aurorasre.ai/runbooks/fix-imagepullbackoff-kubernetes), [Pod Pending with FailedScheduling](https://www.aurorasre.ai/runbooks/fix-pod-pending-failedscheduling-kubernetes), [root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres).

## Questions people ask

**What does exceeded quota mean in a Kubernetes FailedCreate event?**

A ResourceQuota in the namespace has no room for the pod the ReplicaSet tried to create. The control plane rejects the create request with 403 Forbidden and the message names the quota, the resource, what was requested, what is used and the limit.

**Why is my Deployment rollout stuck but the app still works?**

A RollingUpdate creates a surge pod before removing an old one. If the quota rejects the surge pod, the new ReplicaSet stays at 0 pods while the old ReplicaSet keeps serving. The Deployment shows Available True and ReplicaFailure True at the same time.

**Why are there no pod events or logs for the failing pod?**

The pod was never created. Quota is enforced at admission, so the request fails before a pod object exists. The only evidence is the FailedCreate event on the ReplicaSet and in the namespace events.

**How is this different from FailedScheduling?**

FailedScheduling means a pod exists and sits Pending because no node fits it. FailedCreate with exceeded quota means no pod exists at all. Adding nodes fixes the first and does nothing for the second.

**How much headroom does a quota need for a rolling update?**

At least the requests of one surge pod for the largest Deployment in the namespace. With the default 25 percent maxSurge, that is one extra pod for 2 to 4 replicas, since the percentage is rounded up. In the lab, a 200Mi quota needed 232Mi for a 32Mi api pod.

**Can I avoid the surge pod instead of raising the quota?**

Yes. Set maxSurge to 0 and maxUnavailable to 1 so the controller removes an old pod before creating a new one. Capacity drops during the rollout, and a bad new revision takes a serving pod with it, so prefer raising the quota when you can.

**How do I see which pods are using the quota?**

List each pod's requests, for example kubectl get pods -n <namespace> -o custom-columns=NAME:.metadata.name,MEM:.spec.containers[0].resources.requests.memory, and add them up. In the lab seven pods summed to exactly the 200Mi limit.

**How did Aurora find this in the lab?**

Both runs read the Deployment, the ReplicaSets and the FailedCreate event, then the quota (200Mi used of 200Mi) and the per-pod requests that filled it, and named the quota as the cause with the old pods still serving. 108 and 144 seconds, 24 and 39 cents, both graded correct by a judge model and a person.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-resourcequota-exceeded-rollout-stuck-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
