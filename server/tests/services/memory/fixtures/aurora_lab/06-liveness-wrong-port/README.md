# Kubernetes liveness probe failed, connection refused: the fix

*Liveness probe failed with connection refused while the app serves traffic: the probe points at a port nothing listens on. Confirm, fix the port, verify.*

Runbook for lab scenario `06-liveness-wrong-port`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-liveness-probe-failed-kubernetes. Written from the lab runs in this repository; updated 2026-10-15.

## Reproduce it

```
./lab.sh up
./lab.sh break 06-liveness-wrong-port
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Kubernetes containers with an HTTP or TCP liveness probe; the lab app is a Python HTTP server behind a Deployment |
|---|---|
| Verified on | Kubernetes server v1.37.0, kind v0.33.0, python:3.12-alpine, 2026-10-02 |
| Lab folder | [scenarios/06-liveness-wrong-port](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/06-liveness-wrong-port), runs in [runs/v0-run2/06-liveness-wrong-port](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v0-run2/06-liveness-wrong-port) |

**A liveness probe that fails with connection refused while the app is serving requests means the probe is checking a port, path or scheme the app does not expose, and the kubelet is killing a healthy container on a timer.** The pod shows `Running`, the restart count climbs, the last state reads `Error` with exit code 137, and the readiness probe keeps passing between kills. Confirm with `kubectl describe pod <pod> -n <namespace>`: a `Liveness probe failed` warning next to a `Killing` event that says `failed liveness probe, will be restarted` is the whole diagnosis. The fix is to point the probe at the port the app listens on, not to remove the probe.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| `Liveness probe failed: ... connect: connection refused` on a port different from the container port, readiness passing | Probe port, path or scheme does not match what the app exposes | `kubectl get deploy <name> -o yaml`: compare `livenessProbe.httpGet.port` with `containerPort` and the app's own log line | Set the probe to the port the app listens on |
| `Liveness probe failed` with an HTTP 5xx, app logs show errors | The app really is unhealthy | `kubectl logs <pod> --previous` shows the failure | Fix the app, not the probe |
| Probe failures only in the first seconds after start, then clean | `initialDelaySeconds` or `failureThreshold` too tight for the app's startup | Compare `Started` and the first `Unhealthy` event age in `kubectl describe pod` | Raise the delay or add a startup probe |
| `Liveness probe failed: ... context deadline exceeded`, `kubectl top` shows the container at its CPU request | The app cannot answer within `timeoutSeconds` under load | `kubectl top pods`, `kubectl describe node` | Raise `timeoutSeconds` or the CPU request |

## How do you confirm the liveness probe is the thing restarting the container?

Start from the pod list. A liveness kill looks like a crash loop from a distance: `Running`, `1/1`, and a restart count that keeps moving. In the lab, run r1, step 4:

```
$ kubectl get pods -n shop -o wide
NAME                        READY   STATUS    RESTARTS      AGE     IP            NODE                   NOMINATED NODE   READINESS GATES
api-c7b987b4d-9knph         1/1     Running   1 (62s ago)   2m12s   10.244.2.14   aurora-lab-3-worker2   <none>           <none>
api-c7b987b4d-c897z         1/1     Running   1 (63s ago)   2m14s   10.244.1.13   aurora-lab-3-worker    <none>           <none>
frontend-76d98597b9-5x7wj   1/1     Running   0             2m21s   10.244.2.12   aurora-lab-3-worker2   <none>           <none>
frontend-76d98597b9-x8l47   1/1     Running   0             2m21s   10.244.1.11   aurora-lab-3-worker    <none>           <none>
```

Both api pods are `Ready` and both have restarted. That combination, ready but restarting, already says the kubelet is killing something that works. Read the events on one of them (run r1, step 10, the end of `kubectl describe pod api-c7b987b4d-c897z -n shop`):

```
Events:
  Type     Reason     Age                  From               Message
  ----     ------     ----                 ----               -------
  Normal   Scheduled  2m58s                default-scheduler  Successfully assigned shop/api-c7b987b4d-c897z to aurora-lab-3-worker
  Normal   Killing    67s (x2 over 2m17s)  kubelet            spec.containers{api}: Container api failed liveness probe, will be restarted
  Normal   Pulled     37s (x3 over 2m57s)  kubelet            spec.containers{api}: Container image "python:3.12-alpine" already present on machine and can be accessed by the pod
  Normal   Created    37s (x3 over 2m57s)  kubelet            spec.containers{api}: Container created
  Normal   Started    37s (x3 over 2m57s)  kubelet            spec.containers{api}: Container started
  Warning  Unhealthy  37s (x3 over 2m57s)  kubelet            spec.containers{api}: Readiness probe failed: Get "http://10.244.1.13:8080/healthz": dial tcp 10.244.1.13:8080: connect: connection refused
  Warning  Unhealthy  7s (x8 over 2m37s)   kubelet            spec.containers{api}: Liveness probe failed: Get "http://10.244.1.13:8081/healthz": dial tcp 10.244.1.13:8081: connect: connection refused
```

Two lines matter. `Killing ... Container api failed liveness probe, will be restarted` names the kubelet as the one stopping the container. `Liveness probe failed: Get "http://10.244.1.13:8081/healthz"` names the port it is probing: 8081. The readiness failures above it are different: they hit port 8080 once per start, in the second before the server is up, and then stop. The liveness failures keep coming on the 10 second period, eight times so far.

The same describe output shows the container state (run r1, step 10):

```
    State:          Running
      Started:      Fri, 02 Oct 2026 16:32:59 +0000
    Last State:     Terminated
      Reason:       Error
      Exit Code:    137
      Started:      Fri, 02 Oct 2026 16:31:49 +0000
      Finished:     Fri, 02 Oct 2026 16:32:59 +0000
    Ready:          True
    Restart Count:  2
    Limits:
      memory:  128Mi
    Requests:
      cpu:      50m
      memory:   32Mi
    Liveness:   http-get http://:8081/healthz delay=15s timeout=1s period=10s successThreshold=1 failureThreshold=3
    Readiness:  http-get http://:8080/healthz delay=0s timeout=1s period=5s successThreshold=1 failureThreshold=3
```

Exit code 137 is a SIGKILL. With `Reason: Error` rather than `OOMKilled`, and the `Killing` event above, the signal came from the kubelet's probe handling, not from the kernel's memory killer. The two probe lines sit next to each other: liveness on 8081, readiness on 8080. The probe decision comes about 45 seconds after start: 15 seconds of initial delay, then three failed probes 10 seconds apart. The container is gone about 70 seconds after start (Started 16:31:49, Finished 16:32:59 in the output above); the difference is the 30 second `terminationGracePeriodSeconds` between the kubelet's SIGTERM and its SIGKILL. A container that ignores SIGTERM exits 137 after the grace period, which looks like an OOM kill; [here is how to tell them apart](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes).

The Kubernetes [pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#container-probes) page states the rule: "If a container fails its liveness probe more times than the configured tolerance, the kubelet restarts that container."

## How much is affected and since when?

Check whether the restarts follow a change. The Deployment carries the answer in its annotations (run r1, step 6, `kubectl get deployment api -n shop -o yaml`, trimmed to the lines that differ):

```
metadata:
  annotations:
    deployment.kubernetes.io/revision: "2"
    kubectl.kubernetes.io/last-applied-configuration: |
      {... "livenessProbe":{"httpGet":{"path":"/healthz","port":8080},"initialDelaySeconds":15,"periodSeconds":10} ...}
spec:
  template:
    spec:
      containers:
      - image: python:3.12-alpine
        livenessProbe:
          failureThreshold: 3
          httpGet:
            path: /healthz
            port: 8081
            scheme: HTTP
          initialDelaySeconds: 15
          periodSeconds: 10
          successThreshold: 1
          timeoutSeconds: 1
        name: api
        ports:
        - containerPort: 8080
          protocol: TCP
        readinessProbe:
          failureThreshold: 3
          httpGet:
            path: /healthz
            port: 8080
            scheme: HTTP
```

Revision 2, and the last applied configuration still says port 8080 while the live spec says 8081. Something changed the probe outside the manifest that was applied. The scope is every pod of this Deployment, from the moment the new ReplicaSet rolled out. In the lab the events (run r1, evidence captured before the investigation) show the old ReplicaSet `api-f64486c4c` scaled to 0 and `api-c7b987b4d` scaled to 2 about two minutes before the first `Killing`.

`kubectl rollout history deployment/api` would show the revisions, if your policy allows it. Aurora's default read-only policy blocked it in both lab runs (run r1, step 8):

```
$ kubectl rollout history deployment/api -n shop
Command blocked by organization policy: Mutating kubectl operations
```

## Which of the four causes is it?

Cheapest and likeliest check first.

**1. Does the probe target match what the app exposes?** Compare three numbers: the probe port, `containerPort`, and the port in the app's own startup line. In the lab all three are visible without touching the app. The Deployment above says probe 8081 and `containerPort: 8080`. The previous container's log (run r1, step 7) says what the process actually bound:

```
$ kubectl logs api-c7b987b4d-c897z -n shop --previous --tail=100
connected to redis at redis:6379
api listening on :8080
10.244.1.1 "GET /healthz HTTP/1.1" 200 -
10.244.1.11 "GET /items HTTP/1.0" 200 -
10.244.2.12 "GET /items HTTP/1.0" 200 -
10.244.1.11 "GET /items HTTP/1.0" 200 -
10.244.1.1 "GET /healthz HTTP/1.1" 200 -
```

The killed container was listening on 8080, answering `/healthz` with 200 and serving `/items` right up to the kill. The probe asked 8081. That is the cause, and the next three checks are how you rule out the alternatives in a case that is less clear.

**2. Is the app actually unhealthy?** A real liveness failure comes with an HTTP 5xx or a timeout, and the logs show the app in trouble. Here the probe error is `connection refused`, which means nothing accepted the TCP connection on that port. An app that is up on 8080 cannot refuse on 8080. Run r2 read both pods at once (step 7):

```
$ kubectl logs -n shop -l app=api --all-containers --tail=100 --prefix
[pod/api-c7b987b4d-f98rz/api] connected to redis at redis:6379
[pod/api-c7b987b4d-f98rz/api] api listening on :8080
[pod/api-c7b987b4d-f98rz/api] 10.244.2.1 "GET /healthz HTTP/1.1" 200 -
[pod/api-c7b987b4d-mbfs7/api] connected to redis at redis:6379
[pod/api-c7b987b4d-mbfs7/api] api listening on :8080
[pod/api-c7b987b4d-mbfs7/api] 10.244.1.1 "GET /healthz HTTP/1.1" 200 -
```

**3. Is it a startup race?** If the only probe failures are in the first seconds after `Started` and the restart count stays at 0, the probe is firing before the app is up. Here `initialDelaySeconds: 15` is generous for a server that logs `listening` within a second, the failures continue every 10 seconds for the life of the container, and the restart count climbs. Not a startup race.

**4. Is it resource pressure?** A starved container can miss `timeoutSeconds: 1`. Memory and CPU for the api pods were tiny (run r1, step 11):

```
$ kubectl top pods -n shop
NAME                        CPU(cores)   MEMORY(bytes)
api-c7b987b4d-9knph         2m           11Mi
api-c7b987b4d-c897z         5m           14Mi
frontend-76d98597b9-5x7wj   1m           7Mi
frontend-76d98597b9-x8l47   1m           7Mi
loadgen-7c6676864-xxv7f     4m           0Mi
redis-59458f55d8-rkw9m      4m           4Mi
worker-6cf88d4b58-kdmbc     3m           3Mi
```

14Mi against a 128Mi limit and 5m of CPU. The probe failures are also `connection refused`, not `context deadline exceeded`, which is what a slow answer produces.

## How do you fix it?

**Cause 1, probe target wrong.** Point the probe at the port the app listens on. In the lab that is one patch:

```
kubectl -n shop patch deploy api --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/httpGet/port","value":8080}]'
```

Risk: the patch triggers a rolling update. With two replicas, `maxUnavailable: 25%` rounds down to 0 and `maxSurge: 25%` rounds up to 1, so a new pod is created and must be Ready before an old one is removed. The service stays up for that reason. Rollback: the same patch with the previous value, or `kubectl rollout undo deploy/api -n shop` to return to the prior revision. Mitigation if you cannot change the spec right now: nothing safe. Deleting the probe stops the restarts but leaves the Deployment without a liveness check, and the scenario's own rubric lists that as a wrong answer.

If the manifest is managed by GitOps or a Helm chart, fix it at the source too, or the next sync puts 8081 back. The `last-applied-configuration` annotation in the lab shows the applied manifest had 8080, so the live edit was the drift.

**Cause 2, app unhealthy.** Fix the app. Do not loosen the probe to hide it.

**Cause 3, startup race.** Add a `startupProbe` with a long `failureThreshold`, or raise `initialDelaySeconds`. Rollback is the previous values.

**Cause 4, resource pressure.** Raise `timeoutSeconds` to a value the app meets under load, or raise the CPU request. Rollback is the previous values.

## How do you verify the fix?

Pass condition: restart counts stop increasing and no new `Liveness probe failed` event appears for at least three probe periods plus the initial delay, here 45 seconds, and the probe line in `describe pod` shows the corrected port.

```
kubectl -n shop get pods -l app=api -w
kubectl -n shop describe pod -l app=api | grep -E 'Liveness|Restart Count'
kubectl -n shop get events --field-selector reason=Unhealthy --sort-by=.lastTimestamp | tail -5
```

The lab's own check for the broken state is two conditions, a `Liveness probe failed` event and a non-zero restart count on an api pod (`scenarios/06-liveness-wrong-port/check.sh`):

```
kubectl -n shop get events --field-selector reason=Unhealthy -o jsonpath='{.items[*].message}' | grep -q 'Liveness probe failed' &&
kubectl -n shop get pods -l app=api -o jsonpath='{.items[*].status.containerStatuses[0].restartCount}' | grep -Eq '[1-9]'
```

After the fix, new pods start at 0 restarts and stay there. Watch for five minutes; the restart interval before the fix was 70 seconds, so five minutes without a restart is well past it.

## How do you prevent it?

**Alert on restarts, not on CrashLoopBackOff.** In the lab the pods showed `Running` with a restart count of 1 and then 2 while the kills were already happening, so an alert on the status misses the first restarts. With kube-state-metrics, alert on the restart counter moving:

```
increase(kube_pod_container_status_restarts_total[15m]) > 2
```

This is a suggested expression, not one captured in the lab; check the metric name against your kube-state-metrics version.

**Guardrail: one source of truth for the port.** Name the container port and reference it by name in both probes, so a port change is one edit:

```
ports:
- name: http
  containerPort: 8080
livenessProbe:
  httpGet:
    path: /healthz
    port: http
readinessProbe:
  httpGet:
    path: /healthz
    port: http
```

**Test:** in CI, apply the manifest to a kind cluster and fail the pipeline if any pod restarts within two minutes of becoming Ready. The two conditions in the lab's `check.sh` above, negated, are that test.

## Common mistakes

- **Reading the restart as a crash.** Exit code 137 plus `Reason: Error` plus a `Killing ... failed liveness probe` event is the kubelet, not the application. Only `Reason: OOMKilled` is the memory killer. See [OOMKilled](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes) for that case and [CrashLoopBackOff](https://www.aurorasre.ai/blog/fix-crashloopbackoff-kubernetes) for a container that exits on its own.
- **Deleting the liveness probe to stop the restarts.** The restarts stop and so does the protection the probe existed for. Fix the target.
- **Trusting `kubectl logs` without `--previous`.** The current container is a fresh one that has not been killed yet. The evidence is in the one that was.
- **Treating the readiness failures as the problem.** In this lab both probes show `Unhealthy` events. The readiness ones happen once per start and stop; the liveness ones repeat. Count them.
- **Fixing the live object only.** If a manifest or chart still says the wrong port, the next apply reintroduces it. The `last-applied-configuration` annotation tells you which side drifted.

## What we saw in the lab

Two runs on 2026-10-02, Aurora commit 87d22baf, model bedrock/us.anthropic.claude-sonnet-5, single agent, read-only kubectl under the default `observability_only` policy, a fresh user and org per run. Aurora got the alert text only: "KubePodCrashLooping: api pods in namespace shop restart every minute or so."

| Run | Verdict (human reviewed) | Time | Tool calls | Cost | Fix proposed |
|---|---|---|---|---|---|
| r1 | correct | 123 s | 14 | $0.35 | patch the liveness probe port from 8081 to 8080 (acceptable) |
| r2 | correct | 87 s | 12 | $0.28 | patch the liveness probe port to 8080 (acceptable) |

Both runs named both required findings: the kubelet restarts the container because the liveness probe fails, and the probe targets 8081 while the app listens on 8080. Neither asserted the two wrong answers the scenario lists, OOM or an application crash; both ruled them out explicitly from `top pods` and the logs. Neither proposed removing the probe.

r1's path: list open incidents, search memory (empty), list clusters, `get pods -o wide`, `get events --sort-by=.lastTimestamp`, `get deployment api -o yaml` (where it saw 8081 against the annotation's 8080), `logs --previous` (the app listening on 8080 and serving 200s until the kill), `rollout history` (blocked), `get pods -l app=api` (restart count now 2), `describe pod` (the `Killing` and `Liveness probe failed` events), `top pods`, the `api-code` ConfigMap (to confirm the code binds `PORT`, default 8080), `get nodes -o wide`. Its report called the kills "kubelet-initiated SIGKILL from failed liveness probe" and listed the probe port change as the root cause.

r2 took a shorter route: pods, events, the Deployment YAML, then logs for both pods with `--prefix` (step 7, above), `rollout history` (blocked again), `describe pod -l app=api`, the ConfigMap, `top pods`, nodes. 12 calls, 87 seconds.

Two things to note. `kubectl rollout history` is read-only; Aurora's default policy denies the whole `rollout` verb, so in both runs the "what changed" question was answered from the annotation diff instead. The policy fix is described in the [lab report](https://github.com/Arvo-AI/aurora-lab/blob/main/REPORT.md). And neither report stated a remediation in the report body; both fixes were in Aurora's separate suggestions list, which is where Aurora puts them by design.

## How Aurora handles this

Aurora receives the alert, lists the namespace's pods and events, reads the Deployment and the previous container's logs, and separates a probe kill from a crash by the `Killing` event, the exit code and the serving logs, with each claim cited to a tool output. It runs under a read-only kubectl policy by default, so it investigates without changing anything. In both lab runs it reached this root cause in under two and a half minutes for less than 40 cents.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)
- **See it on an incident:** [Aurora root cause analysis](https://www.aurorasre.ai/rca)

## Sources and verification

Every output block above comes from the lab runs in [runs/v0-run2/06-liveness-wrong-port](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v0-run2/06-liveness-wrong-port) (`r1/steps-full.json`, `r2/steps-full.json`, `r1/evidence-before/`) and the scenario definition in [scenarios/06-liveness-wrong-port](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/06-liveness-wrong-port). The fault was injected with the `break.sh` patch that sets the probe port to 8081; the fix above is the same patch with 8080. Versions: Kubernetes server v1.37.0, kind v0.33.0, python:3.12-alpine, kubectl client v1.35.3. The kubelet rule is quoted from the Kubernetes pod lifecycle page. Related: [Service has no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes). Verified 2026-10-02, written 2026-10-15.

## Questions people ask

**What does 'Liveness probe failed: connect: connection refused' mean?**

Nothing accepted a TCP connection on the port the probe targets. If the app is serving traffic on another port at the same time, the probe is pointed at the wrong port, path or scheme, and the kubelet is restarting a healthy container.

**Why does the pod show Running and Ready if the liveness probe keeps failing?**

Readiness and liveness are separate probes. In the lab the readiness probe hit port 8080, where the app listens, and passed; the liveness probe hit 8081 and failed. The pod is Ready between kills, then the kubelet restarts the container after three failures.

**Is exit code 137 always OOMKilled?**

No. 137 is SIGKILL. The kubelet also sends it when a liveness probe fails. Look at the terminated state: Reason OOMKilled is the memory killer, Reason Error with a Killing event that says failed liveness probe is the probe.

**Why does kubectl get pods not show CrashLoopBackOff?**

Early on the pod shows Running with a climbing restart count: in the lab, 1 and then 2 while the kills were already happening. The status catches up later. Alert on the restart counter, not on the CrashLoopBackOff status.

**How long does the kubelet wait before killing on liveness failures?**

initialDelaySeconds, then failureThreshold consecutive failures periodSeconds apart: in the lab 15 seconds plus 3 failures 10 seconds apart, so the decision came about 45 seconds after start. The container was gone about 70 seconds after start because terminationGracePeriodSeconds added 30 seconds between SIGTERM and SIGKILL.

**Should I remove the liveness probe to stop the restarts?**

No. The restarts stop, and so does the protection the probe exists for. Fix the probe target. The lab scenario lists removing the probe as a wrong answer.

**How do I tell a wrong probe port from a real app failure?**

A wrong port fails with connection refused while the previous container's logs show the app listening and answering 200 on its real port. A real failure shows HTTP 5xx or timeouts from the probe and errors in the logs.

**How does an AI SRE handle a failing liveness probe?**

Aurora reads the pods, events, Deployment and previous logs, matches the Killing event and exit code 137 to the probe, finds the port mismatch in the spec, and rules out OOM and crashes from top pods and the logs. In the lab it did this in 87 to 123 seconds for under 40 cents.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-liveness-probe-failed-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
