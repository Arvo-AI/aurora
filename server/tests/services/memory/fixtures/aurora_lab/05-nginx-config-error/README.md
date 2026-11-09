# nginx [emerg] unknown directive in Kubernetes: cause and fix

*nginx [emerg] unknown directive means a config line nginx does not know, usually a typo in a mounted ConfigMap. Find the line, fix it, restart, check.*

Runbook for lab scenario `05-nginx-config-error`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-nginx-emerg-unknown-directive-kubernetes. Written from the lab runs in this repository; updated 2026-10-15.

## Reproduce it

```
./lab.sh up
./lab.sh break 05-nginx-config-error
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | nginx containers on Kubernetes whose configuration comes from a mounted ConfigMap; here the frontend of the lab's `shop` app |
|---|---|
| Verified on | Kubernetes server v1.37.0, kind v0.33.0, nginx 1.27-alpine (nginx/1.27.5), 2026-10-02 |
| Lab folder | [scenarios/05-nginx-config-error](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/05-nginx-config-error), runs in [runs/v0-run2/05-nginx-config-error](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v0-run2/05-nginx-config-error) |

**`nginx: [emerg] unknown directive "..."` means nginx read a configuration line it does not recognise and refused to start, so the container exits with code 1 and the pod crash-loops.** The most likely cause is a typo in the ConfigMap that is mounted as the config file. Confirm with `kubectl logs <pod> --previous`: the `[emerg]` line names the directive, the file and the line number. The fix is to correct that line in the ConfigMap and restart the pods that mount it.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| `CrashLoopBackOff`, exit code 1, `[emerg] unknown directive` in the previous log | A directive is misspelled or unsupported in the mounted config | `kubectl logs <pod> --previous` then `kubectl get configmap <name> -o yaml` | Correct the directive, restart the Deployment |
| `[emerg]` names a directive that exists, but the module is missing | The image was built without that module | `nginx -V` in a healthy pod of the same image | Use an image with the module, or remove the directive |
| Exit code 137, no `[emerg]` line | OOMKilled, not a config problem | `kubectl describe pod` shows `Reason: OOMKilled` | See [how to fix Kubernetes OOMKilled](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes) |
| Pod Running, `Readiness probe failed`, no restarts | The app is up but the probe path or port is wrong | `kubectl describe pod` events, `kubectl logs` with no `[emerg]` | Fix the probe, not the config |

## How do you confirm it is a config error?

Read the pods, then the log of the container that already died. In the lab the alert said a frontend pod was restarting after a config change. lab run r1, step 4:

```shell
kubectl get pods -n shop -o wide
```

```
NAME                        READY   STATUS             RESTARTS      AGE   IP            NODE                   NOMINATED NODE   READINESS GATES
api-f64486c4c-nxp5w         1/1     Running            0             72s   10.244.2.12   aurora-lab-2-worker2   <none>           <none>
api-f64486c4c-q657d         1/1     Running            0             72s   10.244.1.9    aurora-lab-2-worker    <none>           <none>
frontend-7547556c6b-svhfr   0/1     CrashLoopBackOff   3 (30s ago)   65s   10.244.2.16   aurora-lab-2-worker2   <none>           <none>
frontend-76d98597b9-7fnr2   1/1     Running            0             72s   10.244.1.10   aurora-lab-2-worker    <none>           <none>
frontend-76d98597b9-crvl6   1/1     Running            0             72s   10.244.2.15   aurora-lab-2-worker2   <none>           <none>
```

One frontend pod crash-loops while two others, with a different pod-template-hash, are fine. The crashing one belongs to a newer ReplicaSet. Its current container has nothing useful to say, so read the previous one (r1, step 6):

```shell
kubectl logs frontend-7547556c6b-svhfr -n shop --previous --tail=100
```

```
/docker-entrypoint.sh: Configuration complete; ready for start up
2026/10/02 16:30:48 [emerg] 1#1: unknown directive "proxy_conect_timeout" in /etc/nginx/conf.d/default.conf:9
nginx: [emerg] unknown directive "proxy_conect_timeout" in /etc/nginx/conf.d/default.conf:9
```

That line is the whole diagnosis: the directive, the file and the line number. `describe pod` confirms the shape of the failure (r1, step 7, trimmed):

```
    State:          Terminated
      Reason:       Error
      Exit Code:    1
    Last State:     Terminated
      Reason:       Error
      Exit Code:    1
    Ready:          False
    Restart Count:  4
    Readiness:    http-get http://:80/healthz delay=0s timeout=1s period=5s successThreshold=1 failureThreshold=3
    Mounts:
      /etc/nginx/conf.d from conf (rw)
```

Exit code 1 with an `[emerg]` line is a config parse failure. The readiness probe failures in the events are a consequence: nothing is listening on port 80 because nginx never started.

## How much is affected, and since when?

The Deployment view shows a rollout stopped halfway (cluster state captured before the investigation, run r1):

```
NAME                       READY   UP-TO-DATE   AVAILABLE   AGE   CONTAINERS   IMAGES               SELECTOR
deployment.apps/frontend   2/2     1            2           57s   nginx        nginx:1.27-alpine    app=frontend

NAME                                  DESIRED   CURRENT   READY   AGE   CONTAINERS   IMAGES               SELECTOR
replicaset.apps/frontend-7547556c6b   1         1         0       50s   nginx        nginx:1.27-alpine    app=frontend,pod-template-hash=7547556c6b
replicaset.apps/frontend-76d98597b9   2         2         2       57s   nginx        nginx:1.27-alpine    app=frontend,pod-template-hash=76d98597b9
```

Two old pods are Ready and still in the Service (run r2, step 14):

```shell
kubectl get endpoints frontend -n shop
```

```
NAME       ENDPOINTS                       AGE
frontend   10.244.1.23:80,10.244.2.27:80   2m27s
```

So users are not down yet. The rolling update stopped at the first failing pod, and the old pods keep serving. They carry the same bad ConfigMap mount, so they will fail the next time they restart. Since when: the Deployment's pod template carries `kubectl.kubernetes.io/restartedAt: "2026-10-02T12:30:13-04:00"` (r1, step 9), the moment someone ran a rollout restart to pick up the edited config. In the lab that restart was the second line of the injected fault.

## Which of the four causes is it?

Cheapest check first.

1. **Is it OOM?** Exit code 1, not 137, and `Reason: Error`, not `OOMKilled` (r1, step 7). No.
2. **Is it the image?** Both ReplicaSets run `nginx:1.27-alpine`, and the events say `Container image "nginx:1.27-alpine" already present on machine`. The old pods run the same image fine. No.
3. **Is it the probe?** The probe fails with `connect: connection refused`, which is what happens when the process is not listening. The `[emerg]` line in the log comes first. The probe is a symptom.
4. **Is it the config?** Read the ConfigMap the pod mounts at `/etc/nginx/conf.d` (r1, step 8):

```shell
kubectl get configmap frontend-nginx -n shop -o yaml
```

```yaml
apiVersion: v1
data:
  default.conf: |
    upstream api {
        server api:8080;
    }
    server {
        listen 80;
        location /healthz { return 200 "ok\n"; }
        location /api/ {
            proxy_pass http://api/;
            proxy_conect_timeout 2s;
        }
        location / { return 200 "shop frontend\n"; }
    }
kind: ConfigMap
```

Line 9 of `default.conf` is `proxy_conect_timeout 2s;`. The directive nginx knows is [`proxy_connect_timeout`](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_connect_timeout). One missing letter. The `last-applied-configuration` annotation on the ConfigMap carries the same typo, so the bad value was applied, not patched by hand.

## What is the fix?

Correct the directive and restart the pods that mount the ConfigMap. The lab's acceptable fixes are to fix the typo and restart the frontend, or to restore the previous ConfigMap.

```shell
kubectl -n shop get cm frontend-nginx -o json \
  | sed 's/proxy_conect_timeout/proxy_connect_timeout/' \
  | kubectl apply -f -
kubectl -n shop rollout restart deploy/frontend
```

Risk: low. The edit changes one token in one file. The restart replaces pods one at a time under the Deployment's `RollingUpdate` strategy (`maxSurge: 25%`, `maxUnavailable: 25%`, r1 step 9), so a still-broken config stops the rollout again instead of taking the site down. Rollback: `kubectl apply` the ConfigMap from source control, then restart again.

Do not skip the restart. A mounted ConfigMap is only read when nginx starts, and the crash-looping pod rereads it on each restart, but the healthy old pods do not. Restarting the Deployment is what gets every pod onto the corrected file. Validate the file before you apply it if you can: `nginx -t` against the config catches an unknown directive in a second.

## How do you verify it worked?

Pass condition: every frontend pod is Ready, the restart count stops climbing, and the new pods' logs show nginx starting instead of `[emerg]`.

```shell
kubectl -n shop rollout status deploy/frontend
kubectl -n shop get pods -l app=frontend
kubectl -n shop logs -l app=frontend --tail=5
```

The healthy pods in the lab logged this at startup (frontend pod log, captured before the investigation, run r1):

```
2026/10/02 16:30:07 [notice] 1#1: using the "epoll" event method
2026/10/02 16:30:07 [notice] 1#1: nginx/1.27.5
```

The lab's own check for the broken state is one line, and its negation is the pass condition:

```shell
kubectl -n shop get pods -l app=frontend -o jsonpath='{.items[*].status.containerStatuses[0].state.waiting.reason}' | grep -Eq 'CrashLoopBackOff|Error'
```

It exits 0 while any frontend pod is crash-looping and non-zero once none is. Watch for two minutes: a pod that was mid-backoff needs one more restart to pick up the fix.

## How do you prevent it?

**Test the config in CI.** Run `nginx -t -c <file>` in the same image the pods use, against the rendered ConfigMap, before the manifest is applied. It fails on exactly the line that would have crashed production.

**Alert on the restart loop, not only on readiness.** With kube-state-metrics, a container waiting in CrashLoopBackOff is visible as `kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"} > 0`, which fires minutes before the old pods are gone. Scope it to the namespace and container you care about. (Suggested query, not captured in the lab.)

**Guardrail.** Keep the ConfigMap in source control and apply it from there; review a config change like code. In the lab the fault was a one-character edit followed by a restart, which is also how most real nginx outages start.

## Common mistakes

- **Reading the current container's log.** It is empty or shows only the entrypoint lines. The error is in the previous container: `kubectl logs --previous`.
- **Fixing the readiness probe.** The events say `Readiness probe failed: ... connect: connection refused`. The probe is right; nothing is listening because nginx exited.
- **Raising the memory limit.** The exit code is 1, not 137. The exit code is 1, not 137, so the 64Mi limit is not what killed it.
- **Correcting the ConfigMap and waiting.** The old, healthy pods keep the config they started with until they restart. Restart the Deployment to roll everyone onto the fixed file, and to confirm the fix works before the next unplanned restart does it for you.
- **Rolling back the image.** Both ReplicaSets run the same image. Only the mounted file changed.

## What we saw in the lab

Scenario 05 in the [Aurora lab](https://github.com/Arvo-AI/aurora-lab): a five-component `shop` app on a three-node kind cluster, the `frontend-nginx` ConfigMap edited so `proxy_connect_timeout` reads `proxy_conect_timeout`, the frontend restarted to pick it up, then Aurora given only the alert text ("KubePodCrashLooping: a frontend pod in namespace shop is restarting after a config change.") and read-only kubectl.

| Run | Verdict (human reviewed) | Time | Tool calls | Aurora cost |
|---|---|---|---|---|
| r1 | correct | 120 s | 13 | $0.27 |
| r2 | correct | 152 s | 16 | $0.36 |

Both runs found both required facts: nginx fails to start with `[emerg] unknown directive`, and the bad directive is `proxy_conect_timeout` in the `frontend-nginx` ConfigMap. Neither asserted the wrong answers the scenario lists (a probe misconfiguration, resource limits). r1 went pods, previous log, describe pod, ConfigMap, Deployment, a refused `rollout history`, events, nodes: eight kubectl calls, and the answer was in hand by step 8. r2 went pods, events, previous log, describe pod, ConfigMap, a refused `rollout history`, ReplicaSets, Deployment, then the Service endpoints, which is how it could state that two old pods were still serving traffic.

The difference worth knowing about is in the fixes. r2 suggested correcting the typo and restarting the rollout, the lab's first acceptable fix. r1 suggested patching the ConfigMap and said no restart was needed; the judge accepted the fix but noted that a restart may still be required, which this runbook agrees with.

Both runs tried `kubectl rollout history deployment/frontend` and were refused with `Command blocked by organization policy: Mutating kubectl operations`; the default policy matched the whole `rollout` verb, a false positive fixed on the lab's fix branch. Both worked around it by comparing the ReplicaSets and the `restartedAt` annotation. Both listed what they could not check instead of guessing: the rollout history and who made the edit.

## How Aurora handles this

Aurora receives the alert, connects to the cluster read-only through an uploaded kubeconfig, and runs the same kubectl commands shown above, choosing them itself. It reads the previous container log, opens the mounted ConfigMap, matches the `[emerg]` line to the directive and the line number, and writes a report that cites each tool output by number and lists what it could not check. Mutating commands are blocked by policy, so the fix arrives as a suggestion for a person to apply.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)

Related: [how to fix Kubernetes OOMKilled](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes), [how to fix CrashLoopBackOff](https://www.aurorasre.ai/blog/fix-crashloopbackoff-kubernetes), [nginx 502 Bad Gateway causes](https://www.aurorasre.ai/runbooks/fix-502-bad-gateway-nginx-kubernetes), [a ConfigMap change that reached only some pods](https://www.aurorasre.ai/runbooks/fix-configmap-change-not-applied-to-all-pods-kubernetes), [root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres), and what an [Aurora root cause analysis](https://www.aurorasre.ai/rca) looks like.

## Sources and verification

- Every command and output block above comes from the lab run `v0-run2`, scenario `05-nginx-config-error`, on 2026-10-02: Aurora's tool-call transcripts (`steps-full.json`) for r1 and r2, and the cluster state captured before the investigation (`evidence-before/`). The fault was injected by the scenario's `break.sh` (a `sed` on the ConfigMap followed by `kubectl rollout restart deploy/frontend`); the fix command above is its inverse.
- Versions, from `evidence-before/versions.yaml` and the pod logs: Kubernetes server v1.37.0 on kind v0.33.0 (three nodes, containerd 2.3.4), kubectl client v1.35.3; nginx 1.27-alpine, reporting nginx/1.27.5.
- Aurora commit `87d22baf`, single agent, RCA model `bedrock/us.anthropic.claude-sonnet-5`. Verdicts: judge model `us.anthropic.claude-opus-5-5`, reviewed by a person at Arvo (`human_verdict` in each `grade.json`).
- nginx documentation linked: [ngx_http_proxy_module, proxy_connect_timeout](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_connect_timeout), read 2026-10-15.
- The fix command was not run inside a graded lab run; Aurora's policy blocks mutations. The Verify section states the pass condition and the lab's own check instead of a captured result.

## Questions people ask

**What does nginx [emerg] unknown directive mean?**

nginx read a line in its configuration that is not a directive it knows, so it refused to start. The message names the directive, the file and the line number. In a Kubernetes pod the container then exits with code 1 and the pod goes into CrashLoopBackOff.

**Where do I find the [emerg] line in Kubernetes?**

In the log of the container that already died: kubectl logs <pod> -n <namespace> --previous. The current container usually shows only the entrypoint output, because nginx exits before it logs anything else.

**Why does the readiness probe fail at the same time?**

Nothing is listening on the port, because nginx exited during startup. The events show Readiness probe failed with connection refused. The probe is a symptom of the config error, not the cause.

**Why are some frontend pods still Running?**

They started before the ConfigMap was edited and keep the configuration they loaded. The rolling update stopped at the first new pod that crashed, so the old pods keep serving. They will fail the next time they restart unless the ConfigMap is fixed.

**Do I need to restart the pods after fixing the ConfigMap?**

Yes. nginx reads its configuration at startup. Restart the Deployment with kubectl rollout restart so every pod picks up the corrected file, and so you confirm the fix works before an unplanned restart does.

**How is this different from OOMKilled?**

An OOMKilled container shows exit code 137 and Reason OOMKilled in kubectl describe pod. A config error shows exit code 1, Reason Error, and an [emerg] line in the previous log. Raising memory does nothing for a config error.

**How can I catch an nginx config typo before deploying?**

Run nginx -t against the rendered configuration file in CI, using the same image the pods use. It fails on the exact line that would crash the pod.

**Did Aurora find this fault in the lab?**

Yes, in both runs, in 120 and 152 seconds, for $0.27 and $0.36. Both named the [emerg] error and the misspelled directive in the frontend-nginx ConfigMap; one run's suggested fix skipped the restart, which the judge flagged.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-nginx-emerg-unknown-directive-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
