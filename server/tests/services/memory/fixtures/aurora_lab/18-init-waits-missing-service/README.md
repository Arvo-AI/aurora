# Kubernetes pod stuck in Init:0/1: why and how to fix it

*A pod stuck in Init:0/1 has an init container that never exits, often a wait loop on a missing Service. Read its logs, check the Service, fix the spec.*

Runbook for lab scenario `18-init-waits-missing-service`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-pod-stuck-init-container-kubernetes. Written from the lab runs in this repository; updated 2026-10-19.

## Reproduce it

```
./lab.sh up
./lab.sh break 18-init-waits-missing-service
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Any pod whose init container polls a dependency in a loop and never exits; shown here on a Deployment rollout where the loop waits for a Service name that does not exist |
|---|---|
| Verified on | Kubernetes server v1.37.0, kind v0.33.0, 2026-10-02 |
| Lab folder | [scenarios/18-init-waits-missing-service](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/18-init-waits-missing-service), runs in [runs/v1-run1/18-init-waits-missing-service](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v1-run1/18-init-waits-missing-service) |

**A pod stuck in Init:0/1 has one init container that has not finished, and because the app containers only start after every init container exits successfully, the pod stays Pending for as long as that container keeps running.** The most common cause is a wait loop (`nc`, `curl`, `pg_isready`) polling a Service that does not exist, or is not where the loop looks for it. Confirm it with `kubectl logs <pod> -c <init-container>`: in the lab the loop printed `nc: bad address 'postgres'` every two seconds. The fix is to remove the init container, point it at a dependency that exists, or roll the Deployment back. Creating a database to satisfy a check the application never uses is not a fix.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| `Init:0/1`, init container `State: Running` with `Restart Count: 0`, only Normal events, its logs repeat `bad address` or `waiting for <host>` | A wait loop on a hostname with no Service behind it, in this namespace or at all | `kubectl logs <pod> -c <init-container>`, then `kubectl get svc -n <namespace>` and `kubectl get svc -A` | Remove the init container, fix the hostname, or roll back |
| `Init:0/1`, the loop is running, the Service exists | The Service has no ready endpoints or the port is wrong | `kubectl get endpoints <service> -n <namespace>` | See [Service has no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes) |
| `Init:Error` or `Init:CrashLoopBackOff` | The init container exits non-zero (script error, missing file, a bounded wait that gave up) | `kubectl logs <pod> -c <init-container> --previous` | Fix the script; the kubelet restarts it until it succeeds |
| `Init:0/1` and the init container is `Waiting` with `ImagePullBackOff` or `CreateContainerConfigError` | The init container's image or config, not its command | `kubectl describe pod <pod>`, the init container's State | See [ImagePullBackOff](https://www.aurorasre.ai/runbooks/fix-imagepullbackoff-kubernetes) or [CreateContainerConfigError](https://www.aurorasre.ai/runbooks/fix-createcontainerconfigerror-kubernetes) |
| Pod STATUS `PodInitializing`, condition `Initialized: True` | Init is finished; the app containers are being created | Wait a few seconds, then read the app container's logs | Not an init container problem |

## How do you confirm the pod is stuck on an init container?

Start with the pod list. The STATUS column says `Init:0/1` and the pod has a node and an IP, so it was scheduled and then stopped (lab run r1, step 5):

```shell
kubectl get pods -n shop -o wide
```

```
NAME                        READY   STATUS     RESTARTS   AGE   IP            NODE                   NOMINATED NODE   READINESS GATES
api-77b4b6956f-pkcqj        0/1     Init:0/1   0          69s   10.244.2.55   aurora-lab-2-worker2   <none>           <none>
api-f64486c4c-fb7xz         1/1     Running    0          78s   10.244.1.41   aurora-lab-2-worker    <none>           <none>
api-f64486c4c-t5vss         1/1     Running    0          78s   10.244.2.52   aurora-lab-2-worker2   <none>           <none>
frontend-76d98597b9-n5447   1/1     Running    0          78s   10.244.1.42   aurora-lab-2-worker    <none>           <none>
frontend-76d98597b9-z58gc   1/1     Running    0          78s   10.244.2.54   aurora-lab-2-worker2   <none>           <none>
loadgen-7c6676864-nm28w     1/1     Running    0          78s   10.244.1.43   aurora-lab-2-worker    <none>           <none>
redis-59458f55d8-66gxg      1/1     Running    0          78s   10.244.2.51   aurora-lab-2-worker2   <none>           <none>
worker-6cf88d4b58-gg2b8     1/1     Running    0          78s   10.244.2.53   aurora-lab-2-worker2   <none>           <none>
```

The [Kubernetes init container debugging page](https://kubernetes.io/docs/tasks/debug/debug-application/debug-init-containers/) defines the status: `Init:N/M` means "The Pod has `M` Init Containers, and `N` have completed so far." One init container, none finished.

Describe the pod. Read the init container's state, the app container's state and the events together (r1, step 7, trimmed):

```shell
kubectl describe pod api-77b4b6956f-pkcqj -n shop
```

```
Status:           Pending
IP:               10.244.2.55
Controlled By:  ReplicaSet/api-77b4b6956f
Init Containers:
  wait-for-db:
    Image:         busybox:1.37
    Command:
      sh
      -c
      until nc -z -w 2 postgres 5432; do echo waiting for postgres:5432; sleep 2; done
    State:          Running
      Started:      Fri, 02 Oct 2026 20:34:56 +0000
    Ready:          False
    Restart Count:  0
Containers:
  api:
    Container ID:
    Image:         python:3.12-alpine
    State:          Waiting
      Reason:       PodInitializing
    Ready:          False
    Restart Count:  0
Conditions:
  Type                        Status
  PodReadyToStartContainers   True
  Initialized                 False
  Ready                       False
  ContainersReady             False
  PodScheduled                True
Events:
  Type    Reason     Age   From               Message
  ----    ------     ----  ----               -------
  Normal  Scheduled  88s   default-scheduler  Successfully assigned shop/api-77b4b6956f-pkcqj to aurora-lab-2-worker2
  Normal  Pulled     88s   kubelet            spec.initContainers{wait-for-db}: Container image "busybox:1.37" already present on machine and can be accessed by the pod
  Normal  Created    88s   kubelet            spec.initContainers{wait-for-db}: Container created
  Normal  Started    88s   kubelet            spec.initContainers{wait-for-db}: Container started
```

Four things decide it. `PodScheduled: True` with a node and an IP rules out scheduling, whatever Pending suggests. The init container is `Running` with `Restart Count: 0`: it has not crashed, it has not exited. The app container has an empty `Container ID` and `Reason: PodInitializing`: never created, so no logs. And every event is Normal. The [init containers documentation](https://kubernetes.io/docs/concepts/workloads/pods/init-containers/) says "If a Pod's init container fails, the kubelet repeatedly restarts that init container until it succeeds." A loop that never exits never fails, so none of that runs: no restart, no `Init:Error`, no Warning event. The failure is visible only in the init container's own output.

Read it with `-c` and the init container's name (r1, step 12, the last six of the fifty lines returned):

```shell
kubectl logs api-77b4b6956f-pkcqj -n shop -c wait-for-db --tail=50
```

```
nc: bad address 'postgres'
waiting for postgres:5432
nc: bad address 'postgres'
waiting for postgres:5432
nc: bad address 'postgres'
waiting for postgres:5432
```

`bad address` is the name lookup failing, not a refused connection: cluster DNS has nothing called `postgres` to answer with. If the loop printed only its own `waiting for` line, the name resolved and the connect failed, which points at the Service's endpoints instead (second row of the table).

Without `-c` the request goes to the app container and fails, the first wrong turn most people take (captured by the lab before Aurora ran):

```
Error from server (BadRequest): container "api" in pod "api-77b4b6956f-pkcqj" is waiting to start: PodInitializing
```

## How much is affected, and since when?

Check whether the stuck pod is a new revision or the only copy. In the lab it was a rollout: the new ReplicaSet had one pod that never became Ready while the old one kept both (r1, step 6, trimmed to the api rows):

```shell
kubectl get rs -n shop -o wide
```

```
NAME                  DESIRED   CURRENT   READY   AGE   CONTAINERS   IMAGES               SELECTOR
api-77b4b6956f        1         1         0       72s   api          python:3.12-alpine   app=api,pod-template-hash=77b4b6956f
api-f64486c4c         2         2         2       81s   api          python:3.12-alpine   app=api,pod-template-hash=f64486c4c
```

The Deployment shows the same shape and names the change (r1, step 9, trimmed):

```shell
kubectl describe deployment api -n shop
```

```
Annotations:            deployment.kubernetes.io/revision: 2
Replicas:               2 desired | 1 updated | 3 total | 2 available | 1 unavailable
StrategyType:           RollingUpdate
RollingUpdateStrategy:  25% max unavailable, 25% max surge
Pod Template:
  Init Containers:
   wait-for-db:
    Image:      busybox:1.37
    Command:
      sh
      -c
      until nc -z -w 2 postgres 5432; do echo waiting for postgres:5432; sleep 2; done
Conditions:
  Type           Status  Reason
  ----           ------  ------
  Available      True    MinimumReplicasAvailable
  Progressing    True    ReplicaSetUpdated
OldReplicaSets:  api-f64486c4c (2/2 replicas created)
NewReplicaSet:   api-77b4b6956f (1/1 replicas created)
```

`Available: True` with the old ReplicaSet at 2/2 means users are served and you have time: the stuck pod is the one surge pod the strategy allows, and no old pod is removed until it is Ready. The ReplicaSet ages date the change; here the new one is 9 seconds younger than the old because the lab injected the fault right after the deploy. `progressDeadlineSeconds` was `600` (r2, step 10); after ten minutes the Progressing condition flips to `ProgressDeadlineExceeded` and nothing else changes.

If the Deployment has no old ReplicaSet, or the pod is a bare pod, a Job or a StatefulSet's first replica, every copy is down and the fix is urgent.

## Which dependency is the loop waiting for, and does it exist?

Work from the command outward, cheapest check first.

1. **Read the loop.** The command is in `describe pod`, or straight from the template (r1, step 14):

```shell
kubectl get deployment api -n shop -o jsonpath={.spec.template.spec.initContainers}
```

```
[{"command":["sh","-c","until nc -z -w 2 postgres 5432; do echo waiting for postgres:5432; sleep 2; done"],"image":"busybox:1.37","imagePullPolicy":"IfNotPresent","name":"wait-for-db","resources":{"limits":{"memory":"16Mi"},"requests":{"cpu":"10m","memory":"8Mi"}},"terminationMessagePath":"/dev/termination-log","terminationMessagePolicy":"File"}]
```

Three facts from one line: the host is `postgres`, the port is `5432`, and the `until` loop has no bound. `-w 2` only times out a single connection attempt; the loop itself runs until the pod is deleted.

2. **Does a Service by that name exist in the pod's namespace?** The [Kubernetes DNS documentation](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/) says "DNS queries that don't specify a namespace are limited to the Pod's namespace." (r1, step 10):

```shell
kubectl get svc -n shop
```

```
NAME       TYPE        CLUSTER-IP     EXTERNAL-IP   PORT(S)    AGE
api        ClusterIP   10.96.63.231   <none>        8080/TCP   109s
frontend   ClusterIP   10.96.40.77    <none>        80/TCP     109s
redis      ClusterIP   10.96.152.67   <none>        6379/TCP   109s
```

No `postgres`. If it had existed with no endpoints, the loop would still spin, with a different error; that case is the [Service has no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes) runbook.

3. **Does it exist anywhere?** A Service in another namespace is reachable as `<service>.<namespace>.svc`, so a loop written for one namespace and deployed to another fails exactly like this (r1, step 13):

```shell
kubectl get svc -A
```

```
NAMESPACE     NAME             TYPE        CLUSTER-IP     EXTERNAL-IP   PORT(S)                  AGE
default       kubernetes       ClusterIP   10.96.0.1      <none>        443/TCP                  4h18m
kube-system   kube-dns         ClusterIP   10.96.0.10     <none>        53/UDP,53/TCP,9153/TCP   4h18m
kube-system   metrics-server   ClusterIP   10.96.215.19   <none>        443/TCP                  4h15m
shop          api              ClusterIP   10.96.63.231   <none>        8080/TCP                 2m
shop          frontend         ClusterIP   10.96.40.77    <none>        80/TCP                   2m
shop          redis            ClusterIP   10.96.152.67   <none>        6379/TCP                 2m
```

Nowhere in the cluster.

4. **Does the application need the dependency at all?** This decides between fixing the check and provisioning what it checks for. The api's code is in a ConfigMap (r1, step 19, the two lines that matter):

```shell
kubectl get configmap api-code -n shop -o yaml
```

```
    REDIS_HOST = os.environ["REDIS_HOST"]
        with socket.create_connection((REDIS_HOST, 6379), timeout=2) as s:
```

The api talks to Redis on 6379 and to nothing else. A Postgres check on a service that never opens a Postgres connection is a mistake in the manifest, not a missing database.

5. **What changed?** Compare the templates of the two ReplicaSets. The old one has no init containers at all (r1, step 16):

```shell
kubectl get rs api-f64486c4c -n shop -o jsonpath={.spec.template.spec.initContainers}
```

```
(no output)
```

Both revisions carry the same single env var, `[{"name":"REDIS_HOST","value":"redis"}]` (r1, steps 17 and 18), so the init container is the whole diff. The Deployment's `kubectl.kubernetes.io/last-applied-configuration` annotation still described a template with no `initContainers` while the live template had one (r2, step 10): the change came from a patch or an edit, not `kubectl apply`, which is where to look for who did it. `kubectl rollout history deployment/api -n shop` lists the revisions; Aurora's default policy blocked it as a mutating operation (r1 step 15, r2 step 11), but you can run it.

## What is the fix?

Three fixes, chosen by whether the dependency is real.

**Roll back.** The right call when the init container is the only change in the revision, as in the lab, and the previous ReplicaSet is still healthy. Both Aurora runs proposed this.

```shell
kubectl rollout undo deployment/api -n shop
```

Risk: every other change in revision 2 is undone with it, and a new rollout starts; the old pods keep serving throughout. Rollback of the rollback: `kubectl rollout undo deployment/api -n shop --to-revision=2`.

**Remove the init container and keep the rest of the revision.** The lab injected the fault with a JSON patch, so its inverse is the exact change:

```shell
kubectl -n shop patch deploy api --type=json \
  -p='[{"op":"remove","path":"/spec/template/spec/initContainers"}]'
```

Risk: this removes every init container in the template; in the lab there was one. A new rollout starts and the stuck pod is replaced. Rollback: `kubectl rollout undo deployment/api -n shop`. If the init container is also in your source manifests, remove it there, or the next apply brings it back.

**The dependency is real.** Then the check is right and its target is wrong: fix the hostname (`postgres.<namespace>.svc` if the Service lives elsewhere) or create the Service in the same change set as the pod that waits for it, and give the loop a bound so the next miss fails loudly. Take this path only when the application opens a connection to that dependency. In the lab it did not; a Postgres Service would have satisfied the check while adding a database nobody reads.

Mitigation is not needed here because the old pods kept serving. If the stuck pod is the only copy, the rollback is also the mitigation. Do not delete the stuck pod: the ReplicaSet recreates it from the same template and the new pod loops again.

## How do you verify it worked?

```shell
kubectl rollout status deployment/api -n shop
kubectl get pods -n shop -l app=api -o wide
kubectl -n shop get pods -l app=api -o jsonpath='{range .items[*]}{.metadata.name} {.status.initContainerStatuses[0].state.running.startedAt}{"\n"}{end}'
```

Pass condition: `rollout status` returns "successfully rolled out", two api pods `1/1 Running` with no `Init:` in STATUS, and the third command prints each pod name followed by nothing. That command is the first half of the lab's own check for the broken state, `scenarios/18-init-waits-missing-service/check.sh`, which looks for a pod with a running init container and greps its `wait-for-db` logs for `waiting for postgres`. Watch for two or three minutes: a pod that passes init and then sits at `0/1 Running` has a readiness problem, which is a different page.

## How do you prevent it?

**Bound every wait loop.** An unbounded `until` makes a missing dependency look like a slow one, forever. A loop that gives up after a deadline exits non-zero, the kubelet restarts it, and the pod shows `Init:Error` and then `Init:CrashLoopBackOff` with Warning events your alerting already watches. A suggested shape, not run in the lab:

```shell
timeout 120 sh -c 'until nc -z -w 2 postgres 5432; do echo waiting for postgres:5432; sleep 2; done'
```

**Alert on init containers that run too long.** kube-state-metrics exposes [`kube_pod_init_container_status_running`](https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/workload/pod-metrics.md) per container. A suggested expression, not captured in the lab: `sum by (namespace, pod, container) (kube_pod_init_container_status_running) > 0` held for ten minutes. Alerts on restarts or readiness probe failures never fire here, because the init container never restarts and the app container's probe never runs; the lab's alert, `KubeDeploymentRolloutStuck`, is the other signal that works.

**Check init dependencies in CI.** Every hostname an init container waits for should match a Service in the same change set and namespace. The lab's fault is a one-line patch that adds a check for a dependency nothing in the code opens; a manifest reference check catches it before the rollout does.

## Common mistakes

- **Reading the app container's logs.** `kubectl logs <pod>` without `-c` returns `is waiting to start: PodInitializing`. The evidence is in the init container's logs.
- **Treating Pending as a scheduling problem.** `PodScheduled: True`, a node and an IP are all present. [FailedScheduling](https://www.aurorasre.ai/runbooks/fix-pod-pending-failedscheduling-kubernetes) is a different page.
- **Creating the dependency to make the check pass.** If the application never connects to it, you have added a database to satisfy a typo. Read the code or the env vars first.
- **Trusting the absence of Warning events.** A running loop raises none. `Restart Count: 0` here means stuck, not healthy.
- **Deleting the pod.** The ReplicaSet recreates it with the same init container.

## What we saw in the lab

Scenario `18-init-waits-missing-service`: `break.sh` patches the api Deployment to add the `wait-for-db` init container shown above, with no `postgres` Service anywhere. Aurora received only the alert text, `KubeDeploymentRolloutStuck [warning] namespace=shop deployment=api: Rollout of deployment shop/api is not progressing for longer than 15 minutes.`, and read-only cluster access. The alert was fed to Aurora directly rather than fired by a rule, so both investigations began about a minute after the break (the stuck pod was 69 and 70 seconds old at the first `get pods`). Two runs, each in a fresh account, on two kind clusters (`aurora-lab-2` for r1, `aurora-lab` for r2).

| Run | Verdict | Time | Tool calls | Tool errors | Cost | Fix in suggestions |
|---|---|---|---|---|---|---|
| r1 | correct (judge, then human) | 158 s | 20 | 2 | $0.40 | "Rollback `deployment/api` to revision 1" (acceptable) |
| r2 | correct (judge, then human) | 160 s | 19 | 3 | $0.48 | "Roll back `deployment/api` to revision 1" (acceptable) |

r1 made sixteen kubectl calls, which took 83 of its 158 seconds; the two that settled it were the init container's logs and the old ReplicaSet's empty `initContainers`. Its report: "Since the init container never completes, the main `api` container is permanently blocked from starting, which prevents the new ReplicaSet from reaching Ready and stalls the RollingUpdate." It also read the code and concluded that "the application source code (pulled from `configmap/api-code`) only ever connects to `REDIS_HOST` on port 6379, with no Postgres client logic whatsoever".

r2 took a similar path with one difference that matters: it never read the init container's logs. It read the loop's command from `describe pod` and the Deployment YAML, confirmed no Service existed, and inferred the rest. It did read the `last-applied-configuration` annotation and wrote that the init container was "present in the running spec but absent from the `kubectl.kubernetes.io/last-applied-configuration` annotation, indicating an out-of-band edit rather than a normal declarative update", which a second automated review confirmed against the YAML. Its impact line: the rollout "will remain stuck indefinitely until the bad init container is removed, since no Postgres endpoint will ever become available". Fourteen kubectl calls took 53 of its 160 seconds; three failed: the denied `rollout history` and two `grep` pipes (Aurora's kubectl tool passes arguments to kubectl, not a shell, so `| grep -i postgres` returned `error: unknown shorthand flag: 'i' in -i` in both runs).

Both verdicts were correct, and both reports have errors worth knowing about. r1's Ruled Out section says the image `python:3.12-alpine` "was already present on the node and pulled successfully"; the only `Pulled` event on the stuck pod is for `busybox:1.37`, because the api container was never created. r2 wrote that the alert fired "once the progress deadline was exceeded"; the Deployment was 71 seconds old with `progressDeadlineSeconds: 600` and `Progressing: True`. r2 also stated that no Postgres Pod exists anywhere in the cluster after its `get pods -A | grep` had failed and was not retried. And neither report states the fix in its body: r1's judge note reads "The RCA gives no explicit remediation" and r2's "The fix is only implied". Both put the rollback in their Suggested Next Steps, where it was graded acceptable.

The grader read Aurora's full output (report, notes, suggestions); a person at Arvo then checked both verdicts. Transcripts and grades: [runs/v1-run1/18-init-waits-missing-service](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v1-run1/18-init-waits-missing-service).

## How Aurora handles this

Aurora receives the alert, lists the Deployment and its pods, describes the pod stuck in `Init:0/1`, reads the init container's command, checks whether the Service it waits for exists in the namespace and the cluster, reads the application's code from its ConfigMap to decide whether the dependency is real, and compares the old ReplicaSet's template with the new one to name the change. Every claim in its report cites the tool output it came from; what it could not check, here the rollout history, is listed as such. It runs read-only kubectl under a command policy by default and proposes the fix rather than applying it. See a full investigation on the [Aurora root cause analysis](https://www.aurorasre.ai/rca) page.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)

Related: [a hostname that does not resolve](https://www.aurorasre.ai/runbooks/fix-dns-name-not-resolving-kubernetes), [a Service with no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes), [CreateContainerConfigError](https://www.aurorasre.ai/runbooks/fix-createcontainerconfigerror-kubernetes), and [root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres), and [a pod whose dnsConfig search domains are wrong](https://www.aurorasre.ai/runbooks/fix-pod-dnsconfig-search-domain-not-resolving-kubernetes).

## Sources and verification

- Every command and output block above comes from the lab run `v1-run1`, scenario `18-init-waits-missing-service`, on 2026-10-02: Aurora's tool-call transcripts for r1 and r2, each block attributed to its run and step, plus `evidence-before/` captured before the investigation (the app container log error).
- Versions, from `evidence-before/versions.yaml`: Kubernetes server v1.37.0 on kind v0.33.0, kubectl client v1.35.3. Pod and node names differ between the two runs because they ran on two clusters.
- Aurora commit `87d22baf`, single agent, RCA model `bedrock/us.anthropic.claude-sonnet-5`. Verdicts: judge model `us.anthropic.claude-opus-5-5`, reviewed by a person at Arvo (`human_verdict` in each run's `grade.json`). Times, tool calls, tool errors and costs from `result.json`; the fix column from `fix_from_suggestions` in `grade.json`.
- Kubernetes documentation quoted, read 2026-10-19: [Debug Init Containers](https://kubernetes.io/docs/tasks/debug/debug-application/debug-init-containers/), [Init Containers](https://kubernetes.io/docs/concepts/workloads/pods/init-containers/), [DNS for Services and Pods](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/). The metric name is from the [kube-state-metrics pod metrics list](https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/workload/pod-metrics.md).
- The statuses in the At a glance table other than `Init:0/1` (`Init:Error`, `Init:CrashLoopBackOff`, `PodInitializing`) are defined on the debugging page above and were not captured in this run. The `--previous` and `timeout` commands and the alert expression are suggestions, not captured output.
- The fix commands were not run inside a graded lab run; Aurora's policy blocks mutations. The patch is the inverse of the injected fault (`scenarios/18-init-waits-missing-service/break.sh`), and the Verify section's jsonpath is the first half of the lab's `check.sh`.

## Questions people ask

**What does Init:0/1 mean in Kubernetes?**

The pod has one init container and none have completed. Kubernetes starts the app containers only after every init container exits successfully, so the pod stays Pending, with no app logs, until that init container finishes or the pod is replaced.

**Why does kubectl logs show nothing useful for a pod stuck in Init?**

Without -c, kubectl logs targets the app container, which has not been created; it returns an error ending in is waiting to start: PodInitializing. Add -c with the init container's name, which you get from kubectl describe pod.

**Why are there no Warning events or restarts?**

The kubelet restarts an init container only when it fails. A wait loop that never exits never fails, so it runs with Restart Count 0 and only Normal events. The failure is visible only in the init container's own output.

**What does nc: bad address mean in an init container?**

The hostname did not resolve. In the lab the loop waited for postgres and no Service named postgres existed in the namespace or the cluster. A Service that exists but has no endpoints produces a connection failure instead, not a bad address.

**Can an init container reach a Service in another namespace?**

Yes, by its qualified name, such as postgres.<namespace>.svc. A bare name is looked up only in the pod's own namespace, so a wait loop written for one namespace fails in another until the name is qualified.

**Should I create the missing Service or database to unblock the pod?**

Only if the application actually connects to it. In the lab the api code opened connections to Redis only, so a Postgres Service would have satisfied the check while adding a database nobody used. Remove the init container or roll back instead.

**Does a pod stuck in Init take the service down?**

Not during a RollingUpdate with an old ReplicaSet still Ready. The stuck pod is a surge pod, it is not a ready endpoint, and the controller does not remove old pods until it is Ready. If there is no old ReplicaSet, every replica is down.

**How do I stop this from happening again?**

Bound every wait loop so a missing dependency becomes Init:Error rather than an endless Running state, alert on kube_pod_init_container_status_running held for ten minutes, and have CI check that every hostname an init container waits for matches a Service in the same change.

**Did Aurora find this fault in the lab?**

Yes, in both runs, in 158 and 160 seconds, for $0.40 and $0.48. Both named the wait-for-db loop, the missing postgres Service and the fact that the code never uses Postgres. One run never read the init container's logs, one misreported the image pull, and neither stated the fix in its report body; both put the rollback in their Suggested Next Steps.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-pod-stuck-init-container-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
