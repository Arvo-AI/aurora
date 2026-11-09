# Kubernetes pod Pending, FailedScheduling Insufficient cpu: fix

*A pod Pending with FailedScheduling Insufficient cpu asks for more CPU than any node has. Read the event, compare request and nodes, fix the request.*

Runbook for lab scenario `08-unschedulable-cpu`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-pod-pending-failedscheduling-kubernetes. Written from the lab runs in this repository; updated 2026-10-16.

## Reproduce it

```
./lab.sh up
./lab.sh break 08-unschedulable-cpu
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Any pod whose CPU or memory request is larger than the allocatable resources of every node; shown here on a Deployment rollout |
|---|---|
| Verified on | Kubernetes server v1.37.0, kind v0.33.0 (three nodes, 10 allocatable CPU each), 2026-10-02 |
| Lab folder | [scenarios/08-unschedulable-cpu](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/08-unschedulable-cpu), runs in [runs/v0-run2/08-unschedulable-cpu](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v0-run2/08-unschedulable-cpu) |

**A pod that stays Pending with a FailedScheduling event saying Insufficient cpu is asking for more CPU than any node has left to give, and the scheduler will keep refusing it until the request or the nodes change.** The most common cause is a request typed in whole cores where millicores were meant. Confirm it with `kubectl describe pod <pod>`: the Events section carries the exact count of nodes that failed on CPU. The fix is to set the request back to a value that fits, not to add nodes.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| `0/N nodes are available: ... Insufficient cpu` on every worker node, request far above a node's allocatable CPU | The request is wrong (cores instead of millicores, or a copied value) | `kubectl describe pod <pod>` against `kubectl get nodes -o custom-columns=NAME:.metadata.name,CPU:.status.allocatable.cpu` | Set the request back to what the container needs |
| `Insufficient cpu` only on some nodes, request is reasonable, nodes show high Allocated resources | The cluster is full | `kubectl describe nodes`, the Allocated resources block per node | Scale down something, or add a node |
| `node(s) had untolerated taint(s)` is the only reason given | A taint or missing toleration | `kubectl describe nodes` Taints line against the pod's Tolerations | Add the toleration or remove the taint |
| `node(s) didn't match Pod's node affinity/selector` | Node selector or affinity excludes every node | Pod spec `nodeSelector` and `affinity` against node labels | Fix the selector or label a node |
| `node(s) didn't have free ports for the requested pod ports` | A `hostPort` is already taken on every node | Pod spec `hostPort` against other pods on the nodes | Drop or change the hostPort |

A pod that is scheduled but Waiting with `ImagePullBackOff` is a pull failure, not a scheduling one: see [ImagePullBackOff](https://www.aurorasre.ai/runbooks/fix-imagepullbackoff-kubernetes).

## How do you confirm the pod is unschedulable because of CPU?

Start with the pod list. A Pending pod with no node and no IP next to a Running pod from the same Deployment is the shape of a stuck rollout (lab run r1, step 4):

```shell
kubectl get pods -n shop -o wide
```

```
NAME                        READY   STATUS    RESTARTS   AGE     IP             NODE                 NOMINATED NODE   READINESS GATES
api-f64486c4c-lnrzg         1/1     Running   0          5m5s    10.244.2.109   aurora-lab-worker2   <none>           <none>
api-f64486c4c-wb44h         1/1     Running   0          5m5s    10.244.1.109   aurora-lab-worker    <none>           <none>
frontend-76d98597b9-6dnxv   1/1     Running   0          5m5s    10.244.2.110   aurora-lab-worker2   <none>           <none>
frontend-76d98597b9-krf8c   1/1     Running   0          5m5s    10.244.1.110   aurora-lab-worker    <none>           <none>
loadgen-7c6676864-278sd     1/1     Running   0          5m5s    10.244.1.112   aurora-lab-worker    <none>           <none>
redis-59458f55d8-ndfwm      1/1     Running   0          5m5s    10.244.1.108   aurora-lab-worker    <none>           <none>
worker-6cf88d4b58-wcqmk     1/1     Running   0          5m5s    10.244.1.111   aurora-lab-worker    <none>           <none>
worker-7fdf4ffb67-h89dz     0/1     Pending   0          4m56s   <none>         <none>               <none>           <none>
```

Then read the pending pod. The scheduler writes one event that names every reason a node was rejected (r1, step 6, trimmed to the request and the event):

```shell
kubectl describe pod worker-7fdf4ffb67-h89dz -n shop
```

```
Status:           Pending
IP:
IPs:              <none>
Controlled By:    ReplicaSet/worker-7fdf4ffb67
Containers:
  worker:
    Image:      python:3.12-alpine
    Limits:
      memory:  192Mi
    Requests:
      cpu:     64
      memory:  64Mi
Conditions:
  Type           Status
  PodScheduled   False
Events:
  Type     Reason            Age   From               Message
  ----     ------            ----  ----               -------
  Warning  FailedScheduling  5m8s  default-scheduler  0/3 nodes are available: 1 node(s) had untolerated taint(s), 2 Insufficient cpu. preemption: 0/3 nodes are available: 3 Preemption is not helpful for scheduling.
```

Read the message as a tally: three nodes, one excluded by a taint, two by CPU. Only the two matter here, because the tainted node is the control plane and never runs these pods. "Preemption is not helpful" says evicting lower-priority pods would not free enough either: nothing on the node adds up to the request.

`cpu: 64` with no unit is 64 whole cores. The healthy pod from the previous revision asks for `50m` (r1, step 13):

```shell
kubectl get pod worker-6cf88d4b58-wcqmk -n shop -o jsonpath={.spec.containers[0].resources}
```

```
{"limits":{"memory":"192Mi"},"requests":{"cpu":"50m","memory":"64Mi"}}
```

That is a factor of 1,280. The [Kubernetes CPU resource docs](https://kubernetes.io/docs/tasks/configure-pod-container/assign-cpu-resource/) state the rule the scheduler applied: "Pod scheduling is based on requests. A Pod is scheduled to run on a Node only if the Node has enough CPU resources available to satisfy the Pod CPU request."

## How much is affected, and since when?

Check whether the old pods are still serving. In a rolling update the new pod has to schedule before an old one is removed, so an unschedulable new pod stalls the rollout without taking the service down (r1, step 11):

```shell
kubectl get rs -n shop -o wide
```

```
NAME                  DESIRED   CURRENT   READY   AGE     CONTAINERS   IMAGES               SELECTOR
worker-6cf88d4b58     1         1         1       5m43s   worker       python:3.12-alpine   app=worker,pod-template-hash=6cf88d4b58
worker-7fdf4ffb67     1         1         0       5m34s   worker       python:3.12-alpine   app=worker,pod-template-hash=7fdf4ffb67
```

The Deployment status says the same from the other side (r1, step 9, status block):

```yaml
status:
  availableReplicas: 1
  conditions:
  - lastTransitionTime: "2026-10-02T16:36:48Z"
    message: Deployment has minimum availability.
    reason: MinimumReplicasAvailable
    status: "True"
    type: Available
  - lastTransitionTime: "2026-10-02T16:36:47Z"
    lastUpdateTime: "2026-10-02T16:36:56Z"
    message: ReplicaSet "worker-7fdf4ffb67" is progressing.
    reason: ReplicaSetUpdated
    status: "True"
    type: Progressing
  observedGeneration: 2
  readyReplicas: 1
  replicas: 2
  terminatingReplicas: 0
  unavailableReplicas: 1
  updatedReplicas: 1
```

`Available: True` with `unavailableReplicas: 1` means one old pod is doing all the work and the new revision has not landed. With `progressDeadlineSeconds: 600` (same output), the Progressing condition flips to `ProgressDeadlineExceeded` after ten minutes; it does not fix anything, it only changes the message. The revision annotation reads `"2"`, so the change is one rollout old.

## Which of the five causes is it?

The event names the cause, but the numbers decide it. Compare the request with what each node can allocate (r1, step 8, `kubectl describe nodes`, trimmed to the capacity lines):

```
Name:               aurora-lab-control-plane
Taints:             node-role.kubernetes.io/control-plane:NoSchedule
Capacity:
  cpu:                10
Allocatable:
  cpu:                10
Allocated resources:
  Resource           Requests    Limits
  cpu                950m (9%)   0 (0%)
Name:               aurora-lab-worker
Taints:             <none>
Capacity:
  cpu:                10
Allocatable:
  cpu:                10
```

1. **Request larger than any node.** Allocatable CPU is 10 on every node; the pod wants 64. No amount of freeing capacity helps, and the message lists every schedulable node as `Insufficient cpu`. This is the lab's case.
2. **Cluster genuinely full.** Allocatable minus Allocated resources is smaller than the request on each node, but the request itself is sane. Here the control plane shows 950m of 10 cores used; the workers are nowhere near full.
3. **Taint.** If `untolerated taint(s)` were the only reason, every node would carry a taint the pod does not tolerate. In the lab only the control plane is tainted, and `node-role.kubernetes.io/control-plane:NoSchedule` is the default for kind; it is not the cause, and r2 said so explicitly.
4. **Node selector or affinity.** The pod spec above shows `Node-Selectors: <none>`, and the message does not mention affinity.
5. **hostPort conflict.** `Host Port: <none>` in the pod spec, and the message does not mention ports.

When the request looks plausible and you still see `Insufficient cpu`, read the Allocated resources block of every node: the scheduler sums requests, not usage, so a node with 5% CPU in use can still be full by requests.

## What is the fix?

Set the request back to a value that fits. The lab injected the fault with a JSON patch, so the inverse is the exact fix:

```shell
kubectl -n shop patch deploy worker --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/cpu","value":"50m"}]'
```

Or roll back to the previous revision, which carries the same `50m`:

```shell
kubectl rollout undo deployment/worker -n shop
```

Risk: both commands start a new rollout. The pending pod is replaced by one that schedules; the old pod is removed only after the new one is Ready, so the worker keeps running throughout. Rollback: if the new request is still wrong, `kubectl rollout undo` again returns to the last working revision. Do not delete the Pending pod by hand: its ReplicaSet recreates it with the same request within seconds.

Mitigation is not needed here because the old pod kept serving. If the failing rollout had been a scale-up with no old pods, the same patch is also the mitigation.

## How do you verify it worked?

The new ReplicaSet must reach Ready and the old one scale to zero:

```shell
kubectl rollout status deployment/worker -n shop
kubectl get rs -n shop -l app=worker -o wide
kubectl get pods -n shop -l app=worker -o wide
```

Pass condition: `rollout status` returns "successfully rolled out", exactly one worker pod, Running, with a node assigned, and no new `FailedScheduling` event for it. The lab's own check for the broken state is one line; its negation is the pass:

```shell
kubectl -n shop get events --field-selector reason=FailedScheduling -o jsonpath='{.items[*].message}' | grep -q 'Insufficient cpu'
```

Watch for ten minutes: a request that fits but leaves the node tight can still fail later when another rollout lands on the same node.

## How do you prevent it?

**Cap requests at the namespace.** A LimitRange rejects the pod at admission with a clear error instead of leaving it Pending (a suggested guard, not captured in the lab):

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: container-max
  namespace: shop
spec:
  limits:
  - type: Container
    max:
      cpu: "4"
```

**Alert on Pending, not on restarts.** This failure never restarts anything. With kube-state-metrics, a suggested expression: `sum by (namespace, pod) (kube_pod_status_phase{phase="Pending"}) > 0` held for five minutes, which is what the lab's alert text describes.

**Check the arithmetic in CI.** A request above the largest node's allocatable CPU can never schedule; compare `resources.requests.cpu` in manifests against `kubectl get nodes -o jsonpath='{.items[*].status.allocatable.cpu}'` before applying. Always write CPU with a unit when you mean millicores: `50m`, never `50`.

## Common mistakes

- **Adding nodes.** A 64-core request does not fit a 10-core node no matter how many you add. Read the request before touching capacity.
- **Blaming the taint.** `1 node(s) had untolerated taint(s)` appears in almost every FailedScheduling message on a cluster with a control plane; it is noise unless it accounts for all nodes.
- **Deleting the Pending pod.** The ReplicaSet recreates it with the same spec.
- **Raising limits.** Scheduling reads requests. Changing `limits.cpu` does nothing for a Pending pod.
- **Waiting for the deadline.** `ProgressDeadlineExceeded` after 600 seconds is a label, not a rollback.

## What we saw in the lab

Both runs found the cause and the trigger (lab run v0-run2, 2026-10-02, judged by `us.anthropic.claude-opus-5-5` and reviewed by a person):

| Run | Verdict | Time | Tool calls | Cost | Fix proposed |
|---|---|---|---|---|---|
| r1 | correct | 362 s | 14 | $0.35 | Correct the oversized `cpu` request on the worker Deployment (acceptable) |
| r2 | correct | 89 s | 10 | $0.21 | Patch the request back to `50m`, add a LimitRange as a guard (acceptable) |

r1 ran `get pods`, `get events`, `describe pod`, `get nodes`, `describe nodes`, the Deployment YAML, then `rollout history` (refused: "Command blocked by organization policy: Mutating kubectl operations", step 10), `get rs`, a postmortem lookup, and finally the old pod's resources to confirm `50m`. Its report put it this way: "Every node in the cluster has only 10 allocatable CPUs [7, 8], so no node could ever satisfy this request". The 362 seconds were mostly model time; the cluster calls together took about 40 seconds.

r2 went `get pods`, `get nodes`, `describe pod`, `get events`, `describe nodes`, the Deployment YAML, `get replicaset`, and stopped. It ruled the taint out explicitly: "it accounts for only one of three nodes being excluded, not the "Insufficient cpu" failures on the other two".

Two things a reader should know. r1's report only implied the fix ("manual intervention" to correct the request or roll back) and r2's stated none; the fixes above come from each run's Suggested Next Steps list, which is where Aurora puts them. And both reports inferred that the change was made outside `kubectl apply`, because the `last-applied-configuration` annotation still shows the old request; that is consistent with how the fault was injected (a `kubectl patch`), but the reports could not prove it, since `rollout history` was denied by the default policy.

## How Aurora handles this

Aurora receives the alert, lists the pods, reads the pending pod's scheduling event, compares the request with every node's allocatable CPU, and reports the cause with each claim cited to a command output, as in the two runs above. It runs read-only under its default command policy and proposes the fix rather than applying it. It flags what it could not verify, here the rollout history, instead of guessing. Every investigation is a page like the ones linked in the table.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)

Related: [how to fix Kubernetes OOMKilled](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes), [how to fix CreateContainerConfigError](https://www.aurorasre.ai/runbooks/fix-createcontainerconfigerror-kubernetes), [root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres), and [how Aurora investigates an incident](https://www.aurorasre.ai/rca).

## Sources and verification

- Every command and output block above comes from the lab run `v0-run2`, scenario `08-unschedulable-cpu`, on 2026-10-02: Aurora's tool-call transcripts (`steps-full.json`) for r1 and r2, each block attributed to its run and step, plus `evidence-before/` captured before the investigation.
- Versions, from `evidence-before/versions.yaml`: Kubernetes server v1.37.0 on kind v0.33.0 (three nodes, 10 allocatable CPU each, containerd 2.3.4), kubectl client v1.35.3.
- Aurora commit `87d22baf`, single agent, RCA model `bedrock/us.anthropic.claude-sonnet-5`. Verdicts: judge model `us.anthropic.claude-opus-5-5`, reviewed by a person at Arvo (`human_verdict` in each run's `grade.json`). Times and costs from `result.json`.
- Kubernetes documentation quoted: [Assign CPU Resources to Containers and Pods](https://kubernetes.io/docs/tasks/configure-pod-container/assign-cpu-resource/). The messages in the At a glance table other than the lab's own are the scheduler's standard reasons and were not captured in this run.
- The fix commands were not run inside a graded lab run; Aurora's policy blocks mutations. The patch is the inverse of the injected fault (`scenarios/08-unschedulable-cpu/break.sh`), and the Verify section's one-liner is the lab's `check.sh`, negated.

## Questions people ask

**What does FailedScheduling Insufficient cpu mean?**

The scheduler checked every node and none had enough unreserved CPU to satisfy the pod's CPU request. The number in front of Insufficient cpu is how many nodes failed for that reason; other nodes may have failed for taints, affinity or ports, each listed separately in the same message.

**Why does the pod stay Pending instead of crashing?**

Scheduling happens before any container starts. A pod that no node can take is never started, so it has no restarts, no logs and no exit code. It sits Pending until the request or the nodes change.

**How do I tell a wrong request from a full cluster?**

Compare the pod's requests with each node's Allocatable in kubectl describe nodes. If the request is larger than a whole node, it is the request. If the request is reasonable but Allocated resources are close to Allocatable on every node, the cluster is full by requests, even when actual usage is low.

**What does cpu: 64 mean in a Kubernetes manifest?**

Sixty-four whole CPU cores. Kubernetes reads a bare number as cores and a number with the m suffix as millicores, so 64 is 1,280 times more than 64m. In the lab the intended value was 50m.

**Why does the message mention an untolerated taint?**

Control-plane nodes carry a NoSchedule taint by default, so they appear in every FailedScheduling message as 1 node(s) had untolerated taint(s). It is the cause only when every node is tainted and the pod tolerates none of them.

**Will deleting the Pending pod help?**

No. The pod belongs to a ReplicaSet, which recreates it with the same spec within seconds. Fix the request in the Deployment template so the new pod schedules.

**Does a stuck rollout take the service down?**

Not when the strategy is RollingUpdate with maxUnavailable at the default 25 percent. The old pod stays until the new one is Ready, so in the lab the worker kept running the whole time; only the new revision never landed.

**How do I stop this from happening again?**

Add a LimitRange with a per-container maximum so an oversized request is rejected at admission with a clear error, alert on pods that stay Pending for more than five minutes, and write CPU with the m suffix when you mean millicores.

**Did Aurora find this fault in the lab?**

Yes, in both runs, in 362 and 89 seconds, for $0.35 and $0.21. Both named the 64-core request against 10 allocatable cores per node; r2 also ruled out the control-plane taint explicitly. Both put the fix in their Suggested Next Steps; r1's report only implied it and r2's did not state one.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-pod-pending-failedscheduling-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
