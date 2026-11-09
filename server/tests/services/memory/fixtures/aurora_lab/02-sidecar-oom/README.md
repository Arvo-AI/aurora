# Kubernetes sidecar OOMKilled: pod 1/2 Ready, app healthy

*A sidecar OOMKilled under its own 16Mi limit left a healthy api 1/2 Ready and its Service empty. Find the container, fix its limit, verify. Lab-tested.*

Runbook for lab scenario `02-sidecar-oom`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-sidecar-container-oomkilled-kubernetes. Written from the lab runs in this repository; updated 2026-10-19.

## Reproduce it

```
./lab.sh up
./lab.sh break 02-sidecar-oom
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Multi-container pods where a sidecar, such as a log shipper or proxy, is OOMKilled repeatedly while the app container stays `Ready`; shown on a two-replica Python api with a log-shipper in `spec.containers` |
|---|---|
| Verified on | Kubernetes server v1.37.0, kind v0.33.0, python:3.12-alpine, 2026-10-02 |
| Lab folder | [scenarios/02-sidecar-oom](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/02-sidecar-oom), runs in [runs/v0-run2/02-sidecar-oom](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v0-run2/02-sidecar-oom) |

**When a pod shows `1/2` under READY and `OOMKilled` under STATUS while the app's own logs look fine, find out which container is being killed before you touch the app: Kubernetes keeps a separate state, `lastState` and `restartCount` for every container, but readiness is decided for the whole pod, so one dying sidecar takes a healthy app out of its Service.** Confirm it with `kubectl describe pod <pod>` and read `Last State` and `Restart Count` under each container name. The fix belongs to the sidecar: raise its own memory limit, shrink its buffer, or roll it back. The kill itself is covered in [Kubernetes OOMKilled (exit code 137)](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes).

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| READY `1/2`, STATUS `OOMKilled`, app container `Ready: True`, `Restart Count: 0`, sidecar dead within a second of `Started` | The sidecar allocates past its own limit at startup | Each container's `Last State` and `Limits` in `kubectl describe pod` | Raise the sidecar's limit or shrink its buffer; roll back first if the Service is empty |
| Sidecar killed after minutes, the same length each cycle | A leak in the sidecar | `Started` to `Finished` per cycle | See [OOMKilled from a slow memory leak](https://www.aurorasre.ai/runbooks/fix-memory-leak-oomkilled-kubernetes) |
| Service has no endpoints although its selector matches the pods | One container keeps every matching pod unready | `Endpoints:` in `kubectl describe svc`, pod `Ready` condition | Fix that container; selector faults are in [Service has no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes) |

## Which container in the pod was killed?

Read READY before RESTARTS. Lab run r1, step 4:

```shell
kubectl get pods -n shop -o wide
```

```
NAME                        READY   STATUS      RESTARTS      AGE   IP            NODE                   NOMINATED NODE   READINESS GATES
api-7d4b6f6f7d-5n8h7        1/2     OOMKilled   3 (57s ago)   72s   10.244.1.8    aurora-lab-2-worker    <none>           <none>
api-7d4b6f6f7d-g95qf        1/2     OOMKilled   3 (55s ago)   70s   10.244.2.11   aurora-lab-2-worker2   <none>           <none>
...
```

`1/2` says one of two containers is not ready. `OOMKilled` and `3` belong to one container, but the row does not say which. `describe` does, per container (r1, step 6, trimmed):

```shell
kubectl describe pod api-7d4b6f6f7d-5n8h7 -n shop
```

```
Status:           Running
Containers:
  api:
    State:          Running
      Started:      Fri, 02 Oct 2026 16:25:30 +0000
    Ready:          True
    Restart Count:  0
    Limits:
      memory:  128Mi
  log-shipper:
    Command:
      ...
      buf=bytearray(40*1024*1024)
      while True: time.sleep(5)
    State:          Terminated
      Reason:       OOMKilled
      Exit Code:    137
      Started:      Fri, 02 Oct 2026 16:26:10 +0000
      Finished:     Fri, 02 Oct 2026 16:26:10 +0000
    Last State:     Terminated
      Reason:       OOMKilled
      Exit Code:    137
      Started:      Fri, 02 Oct 2026 16:25:43 +0000
      Finished:     Fri, 02 Oct 2026 16:25:44 +0000
    Ready:          False
    Restart Count:  3
    Limits:
      memory:  16Mi
    Requests:
      cpu:        10m
      memory:     8Mi
Conditions:
  Type                        Status
  ...
  Ready                       False
  ContainersReady             False
```

The pod is `Running`, the api is `Running`, `Ready: True`, zero restarts. The log-shipper is the one being killed, and the pod's `Ready` and `ContainersReady` are `False` because of it. The [pod lifecycle page](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/) says `describe` "shows the state for each container within that Pod"; read every block, not the first.

Across replicas, filter `containerStatuses` by name. The lab's `check.sh`, which matched 5 seconds after the change in both runs, is this plus a grep:

```shell
kubectl -n shop get pods -l app=api -o jsonpath='{range .items[*].status.containerStatuses[?(@.name=="log-shipper")]}{.lastState.terminated.reason}{"\n"}{end}'
```

When you do not know which container to suspect, print them all (a suggested variant, not run in the lab):

```shell
kubectl get pod <pod> -n <namespace> -o jsonpath='{range .status.containerStatuses[*]}{.name}{"\t"}{.restartCount}{"\t"}{.lastState.terminated.reason}{"\n"}{end}'
```

A native sidecar is an entry in `initContainers`, so its status is under `.status.initContainerStatuses` instead.

Two traps:

- **The events do not say OOMKilled.** Neither run's event stream contained the word. They say `Back-off restarting failed container log-shipper in pod api-7d4b6f6f7d-5n8h7_shop(...)` (r1, step 7): the container, not the reason.
- **The sidecar's logs are thin.** r2 asked for the killed container's output with `kubectl logs api-7d4b6f6f7d-d68bn -n shop -c log-shipper --previous` (step 11) and got `unable to retrieve container logs for containerd://78fc1be6...`. The live one, captured before the investigation, had printed only `log-shipper: buffering 40 MB before first flush`. Pass `-c`, and expect `--previous` to fail on a container that lives one second. Why `--previous` fails there is explained in [CrashLoopBackOff by exit code](https://www.aurorasre.ai/blog/fix-crashloopbackoff-kubernetes).

## What does a crash-looping sidecar do to the Deployment and the Service?

Here the two runs diverged on the same patch. ReplicaSets, r1 step 10 and r2 step 9, api rows:

```
NAME                  DESIRED   CURRENT   READY   AGE    CONTAINERS        IMAGES                                  SELECTOR
# r1
api-7d4b6f6f7d        2         2         0       102s   api,log-shipper   python:3.12-alpine,python:3.12-alpine   app=api,pod-template-hash=7d4b6f6f7d
api-f64486c4c         0         0         0       109s   api               python:3.12-alpine                      app=api,pod-template-hash=f64486c4c
# r2
api-7d4b6f6f7d        2         2         0       94s    api,log-shipper   python:3.12-alpine,python:3.12-alpine   app=api,pod-template-hash=7d4b6f6f7d
api-f64486c4c         1         1         1       102s   api               python:3.12-alpine                      app=api,pod-template-hash=f64486c4c
```

CONTAINERS shows what changed and AGE shows when, without `rollout history`. In r1 the old ReplicaSet was at zero; in r2 one old pod survived. The Service shows the cost (harness capture before each investigation):

```shell
kubectl describe svc api -n shop
```

```
# r1
Selector:                 app=api
Endpoints:
# r2
Selector:                 app=api
Endpoints:                10.244.1.18:8080
```

r1's Service had no endpoints while both api containers answered `/healthz` with 200. Its load generator got 502 on every request from 16:26:03 to the end of the capture at 16:26:19, and nginx logged `connect() failed (111: Connection refused) while connecting to upstream` against the Service IP. r2's one endpoint was the surviving old pod, and every request from 16:54:20 on got 200. Same fault: a full outage in one run, half capacity in the other.

The chain is in the docs. A pod is `Ready` when it "is able to serve requests and should be added to the load balancing pools of all matching Services", and `ContainersReady` means "all containers in the Pod are ready" ([pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)). A sidecar in `spec.containers` is ready only while it runs, so every kill and every back-off wait pulls the pod out of the Service.

The split is rollout timing. With two replicas, the default 25% `maxUnavailable` rounds down to 0 and `maxSurge` rounds up to 1 ([Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)), so an old pod goes only after a new one counts as available. In r1 the controller scaled the old ReplicaSet from 2 to 0 within three seconds of creating the new pods (events at 81 s, 79 s and 78 s, step 7), so both new pods briefly counted as available: the `NewReplicaSetAvailable` reason below is set only when "All of the replicas associated with the Deployment are available" ([Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)), and the two new pods' api logs show three `/items` requests served. In r2 only one did, and the rollout stalled.

Now the Deployment's own claim, r1's `kubectl describe deployment api -n shop` before the investigation:

```
Replicas:               2 desired | 2 updated | 2 total | 0 available | 2 unavailable
...
Conditions:
  Type           Status  Reason
  ----           ------  ------
  Progressing    True    NewReplicaSetAvailable
  Available      False   MinimumReplicasUnavailable
```

`NewReplicaSetAvailable` marks a completed rollout, and the docs say the condition "holds even when availability of replicas changes" ([Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)). A pipeline gated on `kubectl rollout status` would most likely have exited 0 in the moment both new pods counted as available, and moved on before the 502s began. Run again at capture time it would have waited, because it also compares available with updated replicas (the same page's example prints "Waiting for rollout to finish: 2 of 3 updated replicas are available..."). The condition keeps reading complete after that.

A trap: the harness's EndpointSlice listing in r1 showed `10.244.1.8,10.244.2.11` for the api, the two unready pods, while the Service had no endpoints. The slice keeps unready addresses and marks them; for a pod, the `serving` condition "maps to the Pod's Ready condition" ([EndpointSlices](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/)). The pod lifecycle page says a failed readiness probe makes the controller remove "the Pod's IP address from the EndpointSlices"; in both runs the unready IPs stayed listed (r2's slice showed three addresses behind its one ready endpoint). IPs in a slice do not mean traffic has somewhere to go.

## Why is the sidecar over its limit?

Limits are per container: "For each container, you can specify resource limits and requests", and "memory limits are enforced by the kernel with out of memory (OOM) kills" ([Resource Management for Pods and Containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)). The pod's limits sum to 144Mi, but the api's unused room is not lent to the sidecar.

1. **Compare the allocation with the sidecar's own limit.** `bytearray(40*1024*1024)` is 40 MiB against 16Mi, 2.5 times the limit before the interpreter is counted.
2. **Read the lifetime.** The last two cycles lasted under a second and one second (16:25:43 to 16:25:44). That is a startup allocation. A sidecar that buffers under load dies after minutes; use the [leak runbook](https://www.aurorasre.ai/runbooks/fix-memory-leak-oomkilled-kubernetes)'s cycle method on it.
3. **Find where the sidecar came from.** In r2's `kubectl get deployment api -n shop -o yaml` (step 7), the `kubectl.kubernetes.io/last-applied-configuration` annotation holds one container and the live template two, the second being `log-shipper` with `memory: 16Mi`. The sidecar arrived outside the tracked manifest, in the lab as a `kubectl patch`. A sidecar injected by an admission webhook is not in the Deployment at all, only in the pod, so compare `kubectl get pod <pod> -o yaml` with the template before blaming a rollout.

Do not wait for a memory graph: a container that lives one second can fall between scrapes, and neither run sampled pod memory. The termination record is the evidence.

### Native sidecars, as far as the docs go

This log-shipper is a regular container. Since v1.33 the [sidecar containers page](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/) marks native sidecars stable: an `initContainers` entry with `restartPolicy: Always`. They "can be started, stopped, or restarted without affecting the main application container", and "If a readinessProbe is specified for this init container, its result will be used to determine the ready state of the Pod." The docs do not say what a crash-looping native sidecar without a readiness probe does to pod readiness, and the lab did not run one. Test it on your version before you rely on it to protect the Service.

## What is the fix?

The scenario accepts a larger sidecar limit, a smaller buffer, or removing the sidecar. None of these commands were run in the lab.

**Mitigate first if the Service is empty**, as in r1:

```shell
kubectl -n shop rollout undo deployment/api
```

Risk: log shipping stops until the fix ships; under GitOps, change the repository instead or the next sync reverts it. Rollback: run the same command again. With one old pod still serving, as in r2, fix forward.

**Raise the sidecar's limit, not the app's.** 64Mi, the value in both runs' suggested fixes, leaves 24 MiB over the buffer; measure the shipper's real peak before settling on it:

```shell
kubectl -n shop set resources deployment/api -c log-shipper --limits=memory=64Mi
```

Risk: a rollout, and the request stays at 8Mi, so the scheduler reserves 8Mi for a container that needs over 40 MiB; set `--requests=memory` to the measured working set in the same command. Rollback: `kubectl -n shop rollout undo deployment/api`. Then put the values in the tracked manifest, or the next `apply` restores 16Mi.

**Shrink the buffer** below the limit when the shipper lets you configure it; when one shipper runs beside many apps, this beats raising a limit in each Deployment. Risk: more frequent flushes, or drops under a burst. Change the buffer or the limit, not both in one rollout.

## How do you verify it?

Run the lab's check without its grep, the jsonpath above. While broken it prints `OOMKilled` once per pod; on new pods it prints empty lines, because a container that never terminated has no `lastState.terminated`. Then: `kubectl get pods -n shop -l app=api` shows `2/2` and `0` restarts per replica, and `kubectl describe svc api -n shop` lists one address per replica under `Endpoints:`. Pass condition: both hold for 10 minutes, when the kubelet "resets the restart backoff timer" ([pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)), and frontend errors are back to baseline. A shipper that buffers under load also needs a traffic peak.

## How do you prevent the next one?

**Alert per container, and on the Service.** `kube_pod_container_status_restarts_total` is STABLE and counts "container restarts per container"; put its `container` label in the alert title so the page says `log-shipper`, not just `api`. Two suggested rules, not captured in the lab, using the kube-state-metrics [pod](https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/workload/pod-metrics.md) and [endpoint](https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/service/endpoint-metrics.md) metrics:

```
increase(kube_pod_container_status_restarts_total[10m]) > 2
  and on (namespace, pod, container)
kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} == 1
```

```
count by (namespace, endpoint) (kube_endpoint_address)
  unless
count by (namespace, endpoint) (kube_endpoint_address{ready="true"})
```

The first names the killed container (`last_terminated_reason` is EXPERIMENTAL; native sidecars need the `kube_pod_init_container_*` series). The second fires when a Service has addresses and none ready: r1's outage.

**Set `minReadySeconds` on Deployments with sidecars.** It is "the minimum number of seconds for which a newly created Pod should be ready without any of its containers crashing, for it to be considered available" ([Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/)). r1 ran the default, 0. A value longer than the sidecar survives should have kept the old pods serving; that follows from the docs and was not tested in the lab.

**Review rule:** a sidecar's limit sits next to its buffer size, where a 40 MB buffer over a 16Mi limit is obvious.

## Common mistakes

- **Raising the app's limit, or debugging the app, because the alert names its Deployment.** The api had zero restarts.
- **Trusting a finished rollout.** r1's Deployment read `Progressing True NewReplicaSetAvailable` and `Available False` at once; a `rollout status` that exited 0 during the rollout says nothing about the minute after.
- **Reading EndpointSlice IPs as healthy endpoints.** In r1 the slice listed both pods; the Service routed to nothing.

## What we saw in the lab

Scenario `02-sidecar-oom`: `break.sh` JSON-patches the api Deployment to append a `log-shipper` container that allocates 40 MiB at startup under a 16Mi limit. Aurora got only the alert, `KubePodCrashLooping: pods of deployment api in namespace shop are restarting.`, a kubeconfig and read-only kubectl under its default `observability_only` policy, in a fresh user and org per run, on kind clusters `aurora-lab-2` and `aurora-lab-3`. Required findings: the sidecar, not the api, is being killed, and because 16Mi is below its usage. Wrong answers: raising the api's limit, saying the api is crashing.

| Run | Verdict | Time | Tool calls | Tool errors | Cost | Fix in suggestions |
|---|---|---|---|---|---|---|
| r1 | correct (judge, AI review, human) | 82 s | 10 | 1 | $0.19 | "Patch `log-shipper` sidecar memory limit/request" (acceptable) |
| r2 | correct (judge, AI review, human) | 98 s | 12 | 1 | $0.30 | "Raise `log-shipper` memory limit above its actual allocation" (acceptable) |

Both met both findings and asserted neither wrong answer, and both found the container the same way: `1/2` in the pod list, then `describe pod` as the third kubectl call. r1 also used the CONTAINERS column of the Deployment and ReplicaSets to date the sidecar, and wrote: "The `api` container itself remained healthy with 0 restarts, isolating the crash loop to the sidecar [6]." r2 read the Deployment YAML and named the readiness mechanism: "because both containers share a pod, the pod as a whole never becomes Ready, preventing the deployment from satisfying its minimum replica availability [4]." Each run's one tool error was `kubectl rollout history`, read-only but blocked by the default policy as "Mutating kubectl operations"; both listed it under Not Checked.

Where the reports went past the evidence:

- **Neither followed the outage to the Service.** r1's "the service had no available replicas at the time of investigation" was true but inferred from the Deployment's `0/2`; it never says requests were failing. r2 read the surviving old ReplicaSet as "no loss of the previously stable pod", with the api at one of two replicas. Neither read the Service, an EndpointSlice or the frontend logs.
- **r1's age arithmetic.** It compared the Deployment's 75 s (step 5) with ReplicaSet ages read about 30 seconds later (step 10); the judge flagged it. Its conclusion, that the sidecar "was introduced immediately before the alert fired", holds.
- **r2 ruled out "Readiness-probe-triggered restarts".** Readiness probes do not restart containers; liveness probes do ([pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)).
- **r2 ruled out node pressure from `kubectl get nodes`**, all `Ready`, and healthy neighbours, with no memory data. r1 ran `kubectl top nodes` (0 to 3 percent).
- **r2 called the alert "high-severity"**, repeating the incident record it read at step 1; the alert text has no severity.
- **No fix in either report body**, as the judge noted. Both put it in the separate suggested next steps: the sidecar's limit from 16Mi to 64Mi, per the grader, and r2 also a rollback.

The grader read each report, note and suggestion list; an AI reviewer and a person at Arvo agreed with both verdicts on 2026-10-02.

## How Aurora handles this

Aurora receives the alert and investigates with read-only `kubectl` behind a command policy, so by default it cannot change the cluster while it looks. For a crash-looping multi-container pod it reads each container's state and limits, the Deployment and its ReplicaSets, then writes a root cause with a citation per claim, Ruled Out and Not Checked lists, and separate suggested fixes. In both lab runs it separated the sidecar from the app on its first `describe`; it did not follow the outage to the Service.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)
- **See it on an incident:** [Aurora root cause analysis](https://www.aurorasre.ai/rca)

Related: [Kubernetes OOMKilled (exit code 137)](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes), [OOMKilled from a slow memory leak](https://www.aurorasre.ai/runbooks/fix-memory-leak-oomkilled-kubernetes), [Service has no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes), [how to fix CrashLoopBackOff](https://www.aurorasre.ai/blog/fix-crashloopbackoff-kubernetes), and the [lab report on 19 Kubernetes failures](https://www.aurorasre.ai/blog/aurora-lab-report-kubernetes-19-failures).

## Sources and verification

- Outputs: lab run `v0-run2`, scenario `02-sidecar-oom`, 2026-10-02, from Aurora's recorded tool calls (by run and step, trimmed with `...`, pod UIDs as `(...)`) and from `evidence-before/`, captured seconds before each investigation (Service, EndpointSlice, Deployment conditions, frontend and load generator logs).
- Versions from `evidence-before/versions.yaml`: Kubernetes server v1.37.0, kind v0.33.0, kubectl client v1.35.3. Aurora commit `87d22baf`, single agent, RCA model `bedrock/us.anthropic.claude-sonnet-5`. Verdicts: judge `us.anthropic.claude-opus-5-5`, an AI review, then a person (`grade.json`). Time, tool calls, errors and cost from `result.json`, measured from incident creation to report; the first tool call came about 6 s (r1) and 9 s (r2) in.
- Kubernetes docs quoted, read 2026-10-19: [Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/), [Sidecar Containers](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/), [Resource Management for Pods and Containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/), [Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/), [EndpointSlices](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/); kube-state-metrics metric lists, same date.
- Not captured in the lab: the all-containers jsonpath, every fix and rollback command, both alert rules, `minReadySeconds` as a guard, native sidecar behaviour. The moment r1's new pods counted as available is inferred from the scale-down events, the `NewReplicaSetAvailable` reason and the new pods' api logs; no capture shows them Ready. The `rollout status` behaviour is from the Deployments page, not run.

## Questions people ask

**How do I tell which container in a pod was OOMKilled?**

Run kubectl describe pod and read State, Last State and Restart Count under each container name. The pod list only shows READY 1/2 and STATUS OOMKilled for the pod. In the lab the api container was Running with 0 restarts while the log-shipper sidecar showed Reason OOMKilled, Exit Code 137 and 3 restarts. A jsonpath over status.containerStatuses, filtered by name, gives the same answer for every replica.

**Why is my pod 1/2 Ready when the app container is healthy?**

Pod readiness requires every container to be ready. A sidecar in spec.containers that is being OOMKilled is not ready while it is dead or waiting in back-off, so the pod's Ready and ContainersReady conditions stay False even though the app container reports Ready: True.

**Why does a crashing sidecar take my Service down?**

A Service routes only to Ready pods. In the first lab run both new api pods were unready because of the sidecar, the old ReplicaSet had already been scaled to zero, and the Service showed no endpoints, so the frontend returned 502 on every request while both api containers answered their health checks with 200. In the second run one old pod survived the rollout and kept serving.

**Should I raise the app container's memory limit?**

No. Limits are per container and the app is not the one being killed. Raise the sidecar's own limit above what it allocates, shrink its buffer, or roll the sidecar back. In the lab the sidecar allocated 40 MiB under a 16Mi limit; the api's 128Mi limit was irrelevant.

**Why does the Deployment say the rollout completed when no replicas are available?**

A rollout is complete once all new replicas have been available at the same time, and the Progressing condition with reason NewReplicaSetAvailable holds even when availability changes later. In the lab the Deployment read Progressing True NewReplicaSetAvailable and Available False at the same time. A kubectl rollout status that exited 0 at the moment of completion stays passed; run later, it waits, because it also checks available against updated replicas. Setting minReadySeconds makes new pods prove they stay ready before they count as available.

**Why can't I get the sidecar's logs with kubectl logs --previous?**

A container that is killed within a second may be gone from the runtime by the time you ask. In the lab kubectl logs with -c log-shipper --previous returned unable to retrieve container logs. Always pass -c with the container name in a multi-container pod, and rely on the termination record in describe pod.

**Do native sidecars avoid this?**

The Kubernetes docs say native sidecars, init containers with restartPolicy Always, can be restarted without affecting the main application container, and that a readinessProbe on one is used to determine the pod's ready state. They do not say what a crash-looping native sidecar without a readiness probe does to pod readiness, and the lab did not test it, so test it on your version first.

**Which alert names the sidecar instead of the app?**

Use the container label from kube-state-metrics: kube_pod_container_status_restarts_total counts restarts per container, and kube_pod_container_status_last_terminated_reason with reason OOMKilled names the killed one. Add an alert for a Service whose endpoint addresses exist but none are ready, which catches the outage whatever the cause.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-sidecar-container-oomkilled-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
