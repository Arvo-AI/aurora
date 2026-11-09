# Kubernetes memory leak OOMKilled: tell a leak from a small limit

*OOMKilled on a steady interval with clean logs and no spec change is a leak. Tell it from a small limit by cycle length and growth, then fix the code.*

Runbook for lab scenario `17-slow-memory-leak`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-memory-leak-oomkilled-kubernetes. Written from the lab runs in this repository; updated 2026-10-19.

## Reproduce it

```
./lab.sh up
./lab.sh break 17-slow-memory-leak
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Any container with a memory limit that is killed on a repeating interval while its logs show normal work and its spec, limit and environment have not changed; shown here on a single-replica Python worker whose code is mounted from a ConfigMap |
|---|---|
| Verified on | Kubernetes server v1.37.0, kind v0.33.0, python:3.12-alpine, 2026-10-02 |
| Lab folder | [scenarios/17-slow-memory-leak](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/17-slow-memory-leak), runs in [runs/v1-run1/17-slow-memory-leak](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v1-run1/17-slow-memory-leak) |

**A container that is OOMKilled on a steady interval, with clean logs and no change to its spec or limit, is leaking: every cycle it starts small, grows at a constant rate, reaches `resources.limits.memory`, is killed with `Reason: OOMKilled` and `Exit Code: 137`, restarts and begins again.** Two numbers separate it from a limit that is too small: in `kubectl describe pod`, the gap between the last container's `Started` and `Finished` is minutes, not seconds, and the same every cycle (2m20s, 2m21s and 2m20s across the three cycles captured in the lab); and `kubectl top pod --containers` rises between samples. The fix is in the code that keeps memory it should release. Raising the limit changes only how long each cycle lasts.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| `OOMKilled`, `Exit Code: 137`, the previous container ran for minutes and the same length each cycle, `--previous` logs stop mid-work, live spec equals `last-applied-configuration` | Memory retained across iterations (a leak), usually in code that arrived with the last restart | `Started` to `Finished` gap in `describe pod`; two `kubectl top pod --containers` samples a minute apart | Fix or revert the code, roll out, verify for longer than one old cycle |
| `OOMKilled` within seconds of `Started`, every time, after a config or image change | The working set exceeds the limit from the first iteration | Live spec against `last-applied-configuration` | See [Kubernetes OOMKilled (exit code 137)](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes) |
| `OOMKilled` only during traffic peaks, usage falls back afterwards | Load-proportional working set, no leak | `kubectl top` through a quiet period | Size the limit for the peak, or bound concurrency |
| Restarts with `Reason: Error` and a traceback at the end of `--previous` | An application crash, not a kill | `kubectl logs <pod> --previous` | See [how to fix CrashLoopBackOff](https://www.aurorasre.ai/blog/fix-crashloopbackoff-kubernetes) |

## How do you confirm it is a leak and not a small limit?

Confirming the kill itself is the same as for any OOMKilled container; the [OOMKilled runbook](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes) covers the signature and its look-alikes. The difference here is in the timestamps (lab run r1, step 6, trimmed):

```shell
kubectl describe pod worker-5b67b55dd5-wlxf4 -n shop
```

```
Status:           Running
    State:          Running
      Started:      Fri, 02 Oct 2026 20:38:06 +0000
    Last State:     Terminated
      Reason:       OOMKilled
      Exit Code:    137
      Started:      Fri, 02 Oct 2026 20:35:32 +0000
      Finished:     Fri, 02 Oct 2026 20:37:53 +0000
    Ready:          True
    Restart Count:  2
    Limits:
      memory:  192Mi
    Environment:
      BATCH_SIZE:  500
Events:
  Warning  BackOff    26s                 kubelet            spec.containers{worker}: Back-off restarting failed container worker in pod worker-5b67b55dd5-wlxf4_shop(...)
```

The killed container ran from 20:35:32 to 20:37:53: 2 minutes 21 seconds. The cycle before it, captured before the investigation, ran 2m20s; r2's first cycle on another cluster ran 2m20s (21:04:56 to 21:07:16). A working set that exceeds the limit at startup, as in the OOMKilled runbook's batch allocation, dies within a second of starting (same-second `Started` and `Finished` there). A leak needs time to reach the limit, and because it grows at the rate the code allocates, it needs the same time every cycle. Cadence alone is not proof: a process that ramps to a steady state above its limit, a cache warming to a fixed size for instance, also dies on a fixed interval with rising `top` readings. The discriminator is the arithmetic and the code path in the Isolate section, a fixed number of bytes retained per unit of work with no plateau, and the test of a higher limit: it lengthens a leak's cycle by room divided by rate and ends a sizing problem's kills outright. A spike also kills on a steady interval but with flat `kubectl top` samples; see [OOMKilled causes](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes).

The [resource management page](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) says "`memory` limits are enforced by the kernel with out of memory (OOM) kills. When a container uses more than its `memory` limit, the kernel may terminate it." Between kills the pod looks healthy: `Status: Running`, `Ready: True`, and in r2 no Warning event at all. `CrashLoopBackOff` comes late because the kubelet restarts containers "with an exponential backoff delay (10s, 20s, 40s, …), that is capped at 300 seconds (5 minutes)" and resets it only "Once a container has executed for 10 minutes without any problems" ([pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)). A container that lives 140 seconds never resets it: the gap between kills stretches while the running time inside each cycle stays fixed.

## How much is affected, since when, and what changed?

One workload, one replica: r1's pod list (step 4) showed the worker `1/1 Running` with `2 (19s ago)` restarts and six other pods with none. The ReplicaSets date it (r1, step 11):

```shell
kubectl get replicaset -n shop -l app=worker
```

```
NAME                DESIRED   CURRENT   READY   AGE
worker-5b67b55dd5   1         1         1       5m39s
worker-6cf88d4b58   0         0         0       5m47s
```

A new ReplicaSet eight seconds after the old one looks like a rollout. The Deployment says otherwise (r1, step 5, trimmed):

```shell
kubectl get deployment worker -n shop -o yaml
```

```
metadata:
  annotations:
    deployment.kubernetes.io/revision: "2"
    kubectl.kubernetes.io/last-applied-configuration: |
      {...,"env":[{"name":"BATCH_SIZE","value":"500"}],...,"limits":{"memory":"192Mi"},...}
spec:
  template:
    metadata:
      annotations:
        kubectl.kubernetes.io/restartedAt: "2026-10-02T16:33:10-04:00"
    spec:
      containers:
      - env:
        - name: BATCH_SIZE
          value: "500"
        image: python:3.12-alpine
        resources:
          limits:
            memory: 192Mi
      volumes:
      - configMap:
          name: api-code
        name: code
```

`BATCH_SIZE` is 500 live and 500 in `last-applied-configuration`; the limit is 192Mi in both. All revision 2 added to the template is `kubectl.kubernetes.io/restartedAt`, which `kubectl rollout restart` writes. Someone restarted the worker at 16:33:10 local time without changing its spec. A restart with no spec change points at what the container loads when it starts: here the code in the `api-code` ConfigMap, elsewhere an image tag that moved. ConfigMaps have no revision history; the annotation's timestamp is the lead you take to source control.

## Where does the memory go?

1. **Read the killed container's log to the end** (r1, step 7, 29 lines, trimmed):

```shell
kubectl logs worker-5b67b55dd5-wlxf4 -n shop --previous --tail=100
```

```
worker started, batch size 500
processed batch of 500 items
processed batch of 500 items
...
processed batch of 500 items
```

Exactly 28 `processed batch` lines, then nothing: no traceback, no partial line. The same count in r2 (step 7). The log clears the application of crashing and hands you a unit: 5 seconds per batch, 28 batches is 140 seconds, and 140 seconds is the `Started` to `Finished` gap. Whatever grows, grows per batch.

2. **Sample usage more than once.** Each lab run took one sample. r1, step 10, about 40 seconds after the container started at 20:38:06:

```shell
kubectl top pod worker-5b67b55dd5-wlxf4 -n shop --containers
```

```
POD                       NAME     CPU(cores)   MEMORY(bytes)
worker-5b67b55dd5-wlxf4   worker   3m           34Mi
```

r2's sample (`kubectl top pods -n shop`, step 12), about 90 seconds after its container started at 21:07:17, read `79Mi` for the worker and 12Mi or less for every other pod. Together: about 1 MiB per second of uptime. On your own incident sample the same container three times a minute apart (not captured in the lab). A leak gives three rising numbers; a working set that is too large gives one number near the limit. The [`kubectl top pod` reference](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_top/kubectl_top_pod/) warns of "the metrics pipeline delay"; a reading just after a restart lags the process.

3. **Read the code that runs per iteration.** The worker's source is a ConfigMap key (r1, step 8):

```shell
kubectl get configmap api-code -n shop -o jsonpath='{.data.worker\.py}'
```

```python
import os, time
batch = int(os.environ.get("BATCH_SIZE", "500"))
print(f"worker started, batch size {batch}", flush=True)
recent = []  # tail of recent batches, so a retried batch can skip items already done
while True:
    # Each queued item holds about 100 KiB while the batch is processed.
    buf = [bytearray(100 * 1024) for _ in range(batch)]
    recent.extend(buf[-50:])
    print(f"processed batch of {len(buf)} items", flush=True)
    del buf
    time.sleep(5)
```

`buf` is 500 items of 100 KiB, about 49 MiB, and `del buf` frees it. `recent` keeps the last 50 of them, about 4.9 MiB, every 5 seconds, and nothing trims it, whatever the comment promises. After 28 batches `recent` holds about 137 MiB; the 29th batch's 49 MiB on top passes 192Mi, so the kernel kills the process mid-batch and the log stops at 28. The same arithmetic prices a bigger limit: 384Mi buys 192 MiB of room, at 4.9 MiB per 5 seconds that is 39 more batches, about three more minutes per cycle, then the same kill.

## What is the fix?

The scenario's acceptable fix is to fix or revert the code so it stops retaining items across batches. Three paths, in order of preference.

**Fix the code.** Bound the list where it grows. The one-line change (a suggestion, not run in the lab):

```python
    recent.extend(buf[-50:])
    del recent[:-50]
```

Apply the ConfigMap, then restart the Deployment: the worker read `/code/worker.py` once at startup and a ConfigMap edit does not reach a running process (see [ConfigMap change not applied to all pods](https://www.aurorasre.ai/runbooks/fix-configmap-change-not-applied-to-all-pods-kubernetes)):

```shell
kubectl -n shop apply -f api-code.yaml
kubectl -n shop rollout restart deploy/worker
```

Risk: one more restart of a worker already restarting every 140 seconds. Rollback: apply the previous ConfigMap and restart again. Under a GitOps tool, change the repository.

**Revert the code.** When the previous version is in source control and the retention was never needed, re-apply the old ConfigMap and run the same `rollout restart`. Same risk and rollback. The lab's `break.sh` is this in reverse: a new `worker.py` applied to `api-code`, then `kubectl -n shop rollout restart deploy/worker`.

**Raise the limit, as a mitigation with an expiry.** Only when the fix cannot ship in time, with the arithmetic written next to the new number: 384Mi turns a 2m20s cycle into roughly 5m35s. Edit `resources.limits.memory` in the tracked manifest and apply. Risk: the request stays at 64Mi while the limit doubles, so the scheduler does not check for the room and a full node can OOM-kill a neighbour instead. Rollback: re-apply the previous manifest. Do not combine it with the code fix, or you will not know which one stopped the kills.

## How do you verify it worked?

The lab's `check.sh` greps this jsonpath for `OOMKilled`; run it without the grep:

```shell
kubectl -n shop get pods -l app=worker -o jsonpath='{.items[*].status.containerStatuses[0].lastState.terminated.reason}'
```

While broken it prints `OOMKilled`; after the rollout the new pod has no terminated last state and prints nothing. Not sufficient: a leak passes that check for one cycle. Watch for at least three old cycles, seven minutes here, sampling `kubectl top pod <pod> -n shop --containers` each minute. Pass condition: `RESTARTS` stays at 0 and the readings level off instead of climbing by a steady amount per sample.

## How do you prevent the next one?

**Alert on slope, not on restarts.** A restart alert such as `KubePodCrashLooping` fires after the kills have started; `kube_pod_container_status_restarts_total` (Counter, STABLE in the [kube-state-metrics pod metrics list](https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/workload/pod-metrics.md)) is the series behind restart counts. In the lab the climb was visible within 90 seconds of a container start (34Mi at about 40 seconds in r1, 79Mi at about 90 seconds in r2). A suggested expression, not captured in the lab, using cAdvisor's [`container_memory_working_set_bytes`](https://github.com/google/cadvisor/blob/master/docs/storage/prometheus.md) and kube-state-metrics' `kube_pod_container_resource_limits`:

```
predict_linear(container_memory_working_set_bytes{container!=""}[15m], 1800)
  > on (namespace, pod, container) kube_pod_container_resource_limits{resource="memory"}
```

It fires when the last 15 minutes of growth will cross the limit within 30 minutes, before the first kill; tune the windows to your cycle.

**Review rule: a collection defined outside a loop and appended inside it needs a bound on the same screen.** `recent = []` above `while True:` with `.extend` below it is the whole bug.

**Soak test the hot loop.** Run it for a fixed number of iterations under a small memory limit and fail if it is killed or if resident memory at iteration N exceeds iteration N/2 by more than a few MiB.

## Common mistakes

- **Raising the limit and closing the ticket.** The kills pause for exactly as long as the arithmetic says. Here each extra 100 MiB of limit is about 100 more seconds per cycle.
- **Reading one `kubectl top` sample as a trend.** One number says how much; only a second says which way.
- **Treating "no config change" as "no change".** The Deployment diff was one annotation and the cause was still a change: a ConfigMap key the Deployment mounts. Code that arrives through a ConfigMap or a moving image tag leaves nothing in `kubectl rollout history`.
- **Reading the current container's log.** It shows a healthy worker a few batches in. The killed one, via `--previous`, shows the clean stop that rules out a crash and gives the cycle length.

## What we saw in the lab

Scenario `17-slow-memory-leak`: `break.sh` replaces `worker.py` in the `api-code` ConfigMap and runs `kubectl -n shop rollout restart deploy/worker`; `BATCH_SIZE` and the 192Mi limit are untouched. Aurora received only the alert text, `KubePodCrashLooping [warning] namespace=shop deployment=worker: Pod shop/worker (container worker) has restarted repeatedly in the last 30 minutes.`, and read-only kubectl under its default policy. Two runs in fresh accounts on two kind clusters (`aurora-lab` for r1, `aurora-lab-2` for r2). Required findings: OOMKilled rather than a crash, and retention across batches in the new code rather than the batch size. Wrong answers: `BATCH_SIZE` too large, raising the limit as the only fix, node pressure.

| Run | Verdict | Time | Tool calls | Tool errors | Cost | Fix in suggestions |
|---|---|---|---|---|---|---|
| r1 | correct (judge, then human) | 201 s | 13 | 0 | $0.31 | "Patch `api-code` ConfigMap to bound the `recent` list and roll out" (acceptable) |
| r2 | correct (judge, then human) | 86 s | 12 | 1 | $0.20 | "Patch `code/worker.py` to bound the `recent` list and roll out via ConfigMap `api-code`" (acceptable) |

Both runs met both findings and asserted none of the wrong answers. r1 read the Deployment YAML second, so it had the unchanged spec and the `restartedAt` annotation early. Its root cause: "the `worker` container is repeatedly OOMKilled due to an unbounded memory leak in the application code mounted from ConfigMap `api-code` (worker.py)", the clean previous log "confirming the termination was a hard resource kill rather than a code-level fault". The note it saved for next time says of raising the limit "that only delays the crash, it does not fix the leak".

r2 reasoned from the count in the previous log, and its one tool error was `kubectl rollout history`, blocked by Aurora's default policy as a mutating operation. Its root cause names the distinction this page is about: the limit "is being exceeded by this unbounded growth rather than by a one-time spike or undersized baseline". It never read the Deployment YAML, so it never saw the annotation that dates the restart.

Both verdicts were correct and both reports overreach in the same places. Each called a single `kubectl top` reading "climbing"; one sample shows a level, not a direction; the automated review flagged it in both runs. Both ruled out node pressure because other pods were healthy, with no node metrics; neither ran `kubectl top nodes`. Both called the alert high-severity when its text says `[warning]`; the incident record both read at step 1 already carried `severity: high`, so the reports repeated the record rather than the alert text. r2 called the 28-batch crash point "deterministic" after one observed cycle. Neither report states the fix in its body: r1 says remediation "requires `kubectl edit/patch configmap` and `rollout restart`" without the code change; r2 names it only under Not Checked as "ConfigMap patch or memory limit increase". The fixes were in both runs' Suggested Next Steps: both proposed bounding `recent`, and both also listed raising the limit as a stopgap, r2 first. r1's bound kept 500 items, about 49 MiB, which still retains items across batches; the judge noted a reviewer might not accept it against the scenario's wording. Neither report says how the code changed or ties it to the restart: r1 had the `restartedAt` timestamp in its step 5 output and did not use it; r2 dated the ReplicaSet rollover but not the ConfigMap.

The grader read Aurora's full output (report, note, suggestions) and a person at Arvo checked both verdicts. Transcripts and grades: [runs/v1-run1/17-slow-memory-leak](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v1-run1/17-slow-memory-leak).

## How Aurora handles this

Aurora receives the alert and investigates with read-only `kubectl` behind a command policy, so by default it cannot change the cluster while it looks. For a repeating OOMKilled it reads the container's last termination and its timestamps, the previous container's log, the code or configuration the container loads at startup, the Deployment spec against its `last-applied-configuration` annotation, and pod metrics, then writes a root cause with a numbered citation for each claim and a separate list of suggested fixes. What it could not verify, here the rollout history, goes in a Not Checked section.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)
- **See it on an incident:** [Aurora root cause analysis](https://www.aurorasre.ai/rca)

Related: [OOMKilled after a config change](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes), [ConfigMap change not applied to all pods](https://www.aurorasre.ai/runbooks/fix-configmap-change-not-applied-to-all-pods-kubernetes), [liveness probe failures](https://www.aurorasre.ai/runbooks/fix-liveness-probe-failed-kubernetes), [how to fix CrashLoopBackOff](https://www.aurorasre.ai/blog/fix-crashloopbackoff-kubernetes), and [root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres), and [a sidecar container OOMKilled while the app is healthy](https://www.aurorasre.ai/runbooks/fix-sidecar-container-oomkilled-kubernetes).

## Sources and verification

- Every command and output block above comes from lab run `v1-run1`, scenario `17-slow-memory-leak`, on 2026-10-02: Aurora's tool-call transcripts for r1 and r2, each block attributed to its run and step and trimmed with `...` where noted, plus `evidence-before/` captured before the investigation (the first cycle's timestamps).
- Versions, from `evidence-before/versions.yaml`: Kubernetes server v1.37.0 on kind v0.33.0, kubectl client v1.35.3. Aurora commit `87d22baf`, single agent, RCA model `bedrock/us.anthropic.claude-sonnet-5`. Verdicts: judge model `us.anthropic.claude-opus-5-5`, a second automated review, then a person at Arvo (`human_verdict` in each run's `grade.json`). Times, tool calls, tool errors and costs from `result.json`; the fix column from `fix_from_suggestions` in `grade.json`. Time runs from incident creation to the report: r1's 201 s includes 1 min 43 s before its first tool or model call, with nothing recorded in the gap; r2's gap was 5 s.
- The memory arithmetic (4.9 MiB per batch, 49 MiB transient, 28 batches to the limit) is computed from the code and the limit; it matches the 28 log lines and the 140-second cycles but was not instrumented inside the process. The "about 1 MiB per second" rate rests on one `top` sample per run, at different uptimes, on two clusters.
- Kubernetes documentation quoted, read 2026-10-19: [Resource Management for Pods and Containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/), [Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/), [kubectl top pod](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_top/kubectl_top_pod/). Metric names from the [kube-state-metrics pod metrics list](https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/workload/pod-metrics.md) and the [cAdvisor Prometheus metrics list](https://github.com/google/cadvisor/blob/master/docs/storage/prometheus.md), same date.
- Not captured in the lab: the load-proportional row of the At a glance table, the repeated `top` loop, the `del recent[:-50]` patch, the fix and rollback commands (Aurora's policy blocks mutations), and the `predict_linear` expression. The Verify jsonpath is the lab's `check.sh` without its `grep`.

## Questions people ask

**How do I tell a memory leak from a memory limit that is too small?**

Read the killed container's Started and Finished in kubectl describe pod. A working set that exceeds the limit at startup kills within a second of start. A leak kills after minutes, and after the same number of minutes each cycle; the three cycles captured in the lab ran 2m20s, 2m21s and 2m20s. Then sample kubectl top pod --containers twice a minute apart: a leak rises between samples.

**Why does my pod get OOMKilled on a regular interval?**

The process allocates memory it never frees, at a roughly constant rate, so it takes the same time to reach the limit each cycle. The kubelet restarts it, the counter resets and the climb starts again. The interval between kills slowly stretches because the restart backoff grows and only resets after ten minutes of running.

**Will raising the memory limit fix a memory leak?**

No. It lengthens each cycle by the extra room divided by the leak rate and then the kill happens again. In the lab, going from 192Mi to 384Mi would add about three minutes to a 2m20s cycle. Raise it only as a dated mitigation while the code fix ships.

**Why do the logs look clean if the container keeps dying?**

An OOM kill is a SIGKILL from the kernel, not an exception in the process, so the application writes nothing about it. The previous container's log simply stops mid-work. In the lab it ended after exactly 28 normal processed batch lines with no traceback.

**Nothing changed in my Deployment, so what changed?**

Something the container loads at startup. In the lab the live spec matched last-applied-configuration and the only template diff was the restartedAt annotation from kubectl rollout restart; the new code had arrived in a ConfigMap the Deployment mounts. A moving image tag leaves the same trace: a restart with no spec change.

**Why does kubectl rollout history not show the change?**

Rollout history records Deployment template revisions. A ConfigMap edit changes none of the template, and a rollout restart adds only an annotation. ConfigMaps have no revision history in Kubernetes, so the restart timestamp is the lead you take to source control.

**Which alert catches a leak before the first OOMKilled?**

One on the slope of container_memory_working_set_bytes compared with kube_pod_container_resource_limits for memory, for example predict_linear over the last 15 minutes crossing the limit within 30 minutes. Restart-based alerts such as KubePodCrashLooping fire only after the kills have started.

**Did Aurora find this fault in the lab?**

Yes, in both runs, in 201 and 86 seconds, for $0.31 and $0.20, naming the untrimmed recent list and ruling out BATCH_SIZE, node pressure and an undersized baseline. Both called one kubectl top sample climbing, neither ran kubectl top nodes, and neither stated the fix in its report body; both put the ConfigMap patch in their Suggested Next Steps.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-memory-leak-oomkilled-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
