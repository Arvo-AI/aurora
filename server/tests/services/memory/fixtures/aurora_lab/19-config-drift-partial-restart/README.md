# Kubernetes ConfigMap change not applied to all pods: the fix

*After a ConfigMap edit, only pods started since the edit run the new config; others keep what they loaded at startup. Compare per-pod logs, fix, restart.*

Runbook for lab scenario `19-config-drift-partial-restart`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-configmap-change-not-applied-to-all-pods-kubernetes. Written from the lab runs in this repository; updated 2026-10-19.

## Reproduce it

```
./lab.sh up
./lab.sh break 19-config-drift-partial-restart
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Any pod that reads a mounted ConfigMap once at startup (nginx here) and keeps running after the ConfigMap is edited; shown on the two-replica frontend of the lab's `shop` app |
|---|---|
| Verified on | Kubernetes server v1.37.0, kind v0.33.0, nginx 1.27-alpine (nginx/1.27.5), 2026-10-02 |
| Lab folder | [scenarios/19-config-drift-partial-restart](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/19-config-drift-partial-restart), runs in [runs/v1-run1/19-config-drift-partial-restart](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v1-run1/19-config-drift-partial-restart) |

**When a ConfigMap change applies to only some pods, the replicas are running different configurations: after an edit to a mounted ConfigMap and a partial restart (one pod deleted, one node drained, one crash), pods started after the edit loaded the new file while pods started before it still run what they read at startup, even though the file in their volume has since been updated underneath them.** Confirm by comparing the same log line across replicas: in the lab one nginx pod proxied to `10.96.10.144:8000` and returned 504 while its sibling, with the same ConfigMap mounted, proxied to `10.96.10.144:8080` and returned 200. Make the ConfigMap right first, then restart every pod that mounts it, in that order.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| About 1/N of requests fail for N replicas, every pod Ready, zero restarts, Deployment revision unchanged | The ConfigMap was edited after the pods started; only recreated pods loaded it | `kubectl get pods -o wide` for one pod younger than its siblings, then `kubectl logs` per pod and compare the error lines | Correct the ConfigMap, then `kubectl rollout restart` the Deployment |
| Every request to the route fails from every pod | The new config is wrong and every pod has loaded it | `kubectl get configmap <name> -o yaml` against the Service it points to | Correct the ConfigMap, restart again |
| New pods crash-loop while old ones serve | The new config does not parse | `kubectl logs <pod> --previous` shows `[emerg]` | See [nginx [emerg] unknown directive](https://www.aurorasre.ai/runbooks/fix-nginx-emerg-unknown-directive-kubernetes) |
| Every pod logs `connect() failed (111: Connection refused)` on the right port | The upstream Service has no endpoints | `kubectl get endpoints <service>` | See [Service has no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes) |

## How do you confirm pods are running different configs?

Start from the client, then split by pod. The lab's load generator calls `/api/items` every two seconds through the `frontend` Service (run r1, step 9, trimmed):

```shell
kubectl logs loadgen-7c6676864-9vwtl -n shop --tail=100
```

```
2026-10-02T20:35:52Z GET /api/items -> 504
2026-10-02T20:35:54Z GET /api/items -> 200
2026-10-02T20:35:56Z GET /api/items -> 200
2026-10-02T20:36:01Z GET /api/items -> 504
2026-10-02T20:36:03Z GET /api/items -> 200
2026-10-02T20:36:07Z GET /api/items -> 504
2026-10-02T20:36:11Z GET /api/items -> 504
2026-10-02T20:36:15Z GET /api/items -> 504
```

Successes and failures alternate with no trend: a Service balancing across one broken and one healthy backend. The pods look fine (r1, step 4, frontend rows of seven):

```shell
kubectl get pods -n shop -o wide
```

```
NAME                        READY   STATUS    RESTARTS   AGE     IP            NODE                   NOMINATED NODE   READINESS GATES
frontend-76d98597b9-g5zrg   1/1     Running   0          3m9s    10.244.1.44   aurora-lab-3-worker    <none>           <none>
frontend-76d98597b9-kt2jr   1/1     Running   0          3m18s   10.244.2.53   aurora-lab-3-worker2   <none>           <none>
```

Every pod in the namespace was `1/1 Running` with zero restarts; the frontend pods share a ReplicaSet and differ only in age, and those nine seconds are one pod being deleted and recreated. Now read each pod's log on its own. The younger pod (r1, step 7, one of the pairs the log repeats every few seconds):

```shell
kubectl logs frontend-76d98597b9-g5zrg -n shop --tail=100
```

```
10.244.1.43 - - [02/Oct/2026:20:35:48 +0000] "GET /api/items HTTP/1.1" 504 167 "-" "Wget" "-"
2026/10/02 20:35:48 [error] 29#29: *8 upstream timed out (110: Operation timed out) while connecting to upstream, client: 10.244.1.43, server: , request: "GET /api/items HTTP/1.1", upstream: "http://10.96.10.144:8000/items", host: "frontend"
```

The older pod (r1, step 8, trimmed):

```shell
kubectl logs frontend-76d98597b9-kt2jr -n shop --tail=100
```

```
2026/10/02 20:35:25 [notice] 1#1: nginx/1.27.5
2026/10/02 20:35:28 [error] 27#27: *2 connect() failed (111: Connection refused) while connecting to upstream, client: 10.244.1.43, server: , request: "GET /api/items HTTP/1.1", upstream: "http://10.96.10.144:8080/items", host: "frontend"
10.244.1.43 - - [02/Oct/2026:20:35:28 +0000] "GET /api/items HTTP/1.1" 502 157 "-" "Wget" "-"
10.244.1.43 - - [02/Oct/2026:20:35:34 +0000] "GET /api/items HTTP/1.1" 200 38 "-" "Wget" "-"
```

Same request, same client, same image, two upstream addresses: `:8000` in the pod that fails, `:8080` in the pod that works. nginx logs the upstream it resolved from the configuration it loaded, so these lines are as close as you can get to reading each process's configuration in memory. That is the confirmation. The older pod's single `502` at 20:35:28 is a different failure, `Connection refused` on the correct port three seconds after startup, before the api pods were ready; it never recurred.

Look-alikes: if every pod logs the wrong upstream, every pod has loaded the bad config and there is no drift to find; a crash-looping new pod is a [config that does not parse](https://www.aurorasre.ai/runbooks/fix-nginx-emerg-unknown-directive-kubernetes); `Connection refused` from every pod on the right port is a Service with [no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes).

## How much is affected, and since when?

The alert said 48% of requests to `/api/` over ten minutes. The load generator's last 78 lines in r1 (step 9) agree: 40 returned `200`, 35 returned `504`, and 3 were startup `502` or `timeout`. One bad pod of two costs about half the traffic on the route, and only that route: `/healthz` returned 200 on both pods throughout (r1, steps 7 and 8), which is why the readiness probe never removed the bad pod from the Service.

Since when: the Deployment did not change. `kubectl get deploy frontend -n shop -o yaml` shows `deployment.kubernetes.io/revision: "1"`, `generation: 1` and `updatedReplicas: 2` (r1, step 12); a ConfigMap edit does not touch the pod template, so `kubectl rollout history` has nothing to tell you. The events hold the restart instead (r1, step 5, three of 52 lines):

```shell
kubectl get events -n shop --sort-by=.lastTimestamp
```

```
3m11s       Normal    SuccessfulCreate    replicaset/frontend-76d98597b9   Created pod: frontend-76d98597b9-g5zrg
3m11s       Normal    Scheduled           pod/frontend-76d98597b9-g5zrg    Successfully assigned shop/frontend-76d98597b9-g5zrg to aurora-lab-3-worker
3m11s       Normal    Killing             pod/frontend-76d98597b9-2vqr4    Stopping container nginx
```

One pod killed and one created in the same second: the ReplicaSet replacing a deleted pod. `kubectl describe pod` gives the exact times, `Start Time: Fri, 02 Oct 2026 20:35:34 +0000` for `g5zrg` against `20:35:25 +0000` for `kt2jr` (r1, steps 14 and 15). The ConfigMap holds the other half of the timeline (r1, step 13, trimmed):

```shell
kubectl get configmap frontend-nginx -n shop -o yaml
```

```yaml
apiVersion: v1
data:
default.conf: |
  upstream api {
      server api:8000;
  }
  server {
      listen 80;
      location /healthz { return 200 "ok\n"; }
      location /api/ {
          proxy_pass http://api/;
          proxy_connect_timeout 2s;
      }
      location / { return 200 "shop frontend\n"; }
  }
kind: ConfigMap
metadata:
annotations:
  kubectl.kubernetes.io/last-applied-configuration: |
    {"apiVersion":"v1","data":{"default.conf":"upstream api {\n    server api:8000;\n} ... "},"kind":"ConfigMap","metadata":{"annotations":{},"creationTimestamp":"2026-10-02T20:35:25Z","name":"frontend-nginx","namespace":"shop","resourceVersion":"14417","uid":"d0a60a78-27aa-4974-b47d-55ec7ba0351c"}}
creationTimestamp: "2026-10-02T20:35:25Z"
name: frontend-nginx
namespace: shop
resourceVersion: "14598"
```

Two things in that output. The upstream is `api:8000`, while `kubectl get svc -n shop` shows the `api` Service publishing `8080/TCP` only (r1, step 6). And the `last-applied-configuration` annotation holds a `resourceVersion` of `14417` while the live object is at `14598`: someone fetched the ConfigMap, changed it and applied it back, the fingerprint of an in-place edit (`kubectl edit`, or `get | sed | apply`, which is what the lab did). The creation time, `20:35:25Z`, is the second the old pod started; the edit happened between then and `20:35:34Z`, when the new pod started and read the new file.

## Which cause is it?

Cheapest and likeliest first. Stop when the output answers the question.

**1. Is the backend up?** Half the requests succeed, so something answers on the right port. Check anyway; it is one command, and it removes the wrong answer both of Aurora's runs leaned toward at some point (r2, step 21):

```shell
kubectl describe svc api -n shop
```

```
Port:                     <unset>  8080/TCP
TargetPort:               8080/TCP
Endpoints:                10.244.1.52:8080,10.244.2.62:8080
```

Both api pods are endpoints, and their logs show `api listening on :8080` followed by nothing but `200` responses (r2, steps 8 and 10). The backend is not the problem. The failing pod's error agrees: `upstream timed out`, not `Connection refused`, because no Service publishes port 8000 on that ClusterIP, so nothing answers and nginx waits out the 2 s `proxy_connect_timeout` before returning 504. Which nginx error means what is in [502 Bad Gateway in nginx](https://www.aurorasre.ai/runbooks/fix-502-bad-gateway-nginx-kubernetes) and [504 Gateway Timeout in nginx](https://www.aurorasre.ai/runbooks/fix-504-gateway-timeout-nginx-kubernetes).

**2. Does the upstream in each pod's log match the ConfigMap?** From the confirm step: `:8000` in the young pod, `:8080` in the old one. The ConfigMap says `api:8000`. So the young pod runs the ConfigMap as it is now, and the old pod runs it as it was. This is the cause; the rest is proof and what not to trust.

**3. Is the young pod young because of the edit?** The events and start times above say one pod was deleted and replaced nine seconds after the Deployment came up; whether someone restarted it to pick up the edit or it restarted on its own, the mechanism is the same.

**4. Can you read the loaded config directly?** Not from the file. Kubernetes updates the mounted file in running pods too: "When a ConfigMap currently consumed in a volume is updated, projected keys are eventually updated as well." ([Kubernetes docs, ConfigMaps](https://kubernetes.io/docs/concepts/configuration/configmap/)). So `cat /etc/nginx/conf.d/default.conf` inside the old pod shows `api:8000` once the kubelet has synced, and proves nothing about what nginx is running, since nginx applies a changed file only on reload or restart ([nginx Beginner's Guide](https://nginx.org/en/docs/beginners_guide.html)). The process's log lines and start time are your evidence. In the lab, Aurora's `exec` was blocked by policy in both runs (r1 step 10, r2 step 20):

```
Command blocked by organization policy: Mutating kubectl operations
```

Both runs found the port mismatch without it; only r2 went on to name the cause.

## What is the fix?

Put the ConfigMap back, then restart every pod that mounts it. The order matters: a restart on its own recreates every pod with the bad ConfigMap, and half the requests failing becomes all of them. The lab's fault was a `sed` on the ConfigMap followed by deleting one pod, so the fix is the inverse:

```shell
kubectl -n shop get cm frontend-nginx -o json \
| sed 's/server api:8000;/server api:8080;/' \
| kubectl apply -f -
kubectl -n shop rollout restart deploy/frontend
```

Risk: low for the ConfigMap edit, one token that only pods created from now on read. The restart rolls under `RollingUpdate` with `maxSurge: 25%` and `maxUnavailable: 25%` (r1, step 12): with two replicas, one extra pod at a time and no old pod removed before its replacement is Ready. One caveat: `/healthz` is served by nginx itself (`return 200 "ok\n"` in the ConfigMap) without touching the upstream, so a wrong upstream passes readiness; `g5zrg` was `Ready: True` (r1, step 14) while failing every `/api/` request. The rollout will not catch a bad fix; the client logs will. Rollback: `kubectl apply` the ConfigMap from source control and restart again. Save `kubectl -n shop get cm frontend-nginx -o yaml` before you edit so you hold the exact previous object.

Mitigation, separate from the fix: if you need the error rate down before you can roll the whole Deployment, revert the ConfigMap first and then delete only the failing pod, `kubectl -n shop delete pod frontend-76d98597b9-g5zrg`; its replacement reads the reverted ConfigMap at creation. Risk: the Service drops to one endpoint until the replacement is Ready, and a delete before the revert produces a replacement with the same bad config; do the full `rollout restart` afterwards anyway.

If the edit was intended and the api really is moving to port 8000, change the Service and api pods first, then restart the frontend so every replica agrees.

## How do you verify it worked?

Pass condition: a full minute of `200` on `/api/items` from the client, no frontend pod logging an upstream on `:8000`, and both frontend pods started after the fix.

```shell
kubectl -n shop rollout status deploy/frontend
kubectl -n shop get pods -l app=frontend -o wide
kubectl -n shop logs deploy/loadgen --tail=30
kubectl -n shop logs -l app=frontend --tail=50 --prefix
```

[`--prefix`](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_logs/) labels each line with its pod, so a surviving `:8000/items` stands out. The lab client logs one line every two seconds, so `--tail=30` is about a minute. For these runs the lab's own check grepped the last 30 client lines for `-> 502` next to `-> 200`; it passed only because it ran right after the break, while the startup `502`s were still in the tail, since the steady state is `504`. The check was fixed on 2026-10-19 to accept either code. Do the same:

```shell
kubectl -n shop logs deploy/loadgen --tail=30 | grep -E -- '-> 50[24]' && echo still broken
```

Watch two minutes after the rollout finishes: one bad pod produces a `504` every few seconds, so two clean minutes means none is behind the Service.

## How do you prevent it?

**Make a ConfigMap change a rollout.** Put a hash of the ConfigMap in the pod template, for example an annotation `checksum/config: <sha256 of the rendered file>`, the pattern Helm documents as [Automatically Roll Deployments](https://helm.sh/docs/howto/charts_tips_and_tricks/). Every ConfigMap change then changes the template and rolls all replicas together, instead of waiting for whichever pod restarts first. The alternative is to mark the ConfigMap `immutable: true` and give each version a new name referenced from the Deployment, which the Kubernetes docs say "protects you from accidental (or unwanted) updates that could cause applications outages" ([Kubernetes docs, ConfigMaps](https://kubernetes.io/docs/concepts/configuration/configmap/)).

**Alert on per-replica divergence, not on the aggregate.** A 48% error rate on the Service is one number; split by pod it is 100% on one and 0% on the other, which names the cause. If your proxy exports per-pod metrics, alert on a 5xx ratio per pod. With kube-state-metrics alone, a start-time spread between pods of one ReplicaSet is a drift signal:

```
max by (namespace, owner_name) (kube_pod_start_time * on (namespace, pod) group_left (owner_name) kube_pod_owner{owner_kind="ReplicaSet"})
- min by (namespace, owner_name) (kube_pod_start_time * on (namespace, pod) group_left (owner_name) kube_pod_owner{owner_kind="ReplicaSet"}) > 300
```

It also fires on any single-pod restart, so treat it as a prompt to compare configs, not a page. (Suggested query, not captured in the lab.)

**Guardrail.** Edit ConfigMaps from source control through the same pipeline as the Deployment, never with `kubectl edit` on a ConfigMap that pods mount; the `resourceVersion` inside `last-applied-configuration` is how you spot an in-place edit after the fact.

## Common mistakes

- **Restarting the Deployment without fixing the ConfigMap.** Every replica loads `api:8000` and half the requests failing becomes all of them; the lab scenario lists this as a wrong answer.
- **Blaming the api.** `upstream timed out` reads like a slow backend, but the backend logged `200` for every request it received; the failing requests never reached it. Both Aurora runs wrote an intermediate note saying the backend was down before the per-pod comparison corrected it.
- **Trusting `kubectl get configmap`.** It shows the desired state; the old pod here ran `api:8080` while the ConfigMap said `api:8000`.
- **Trusting `cat` inside the pod.** After the kubelet sync the file shows the new content in the pod that is still running the old one.
- **Deleting the bad pod as the fix.** Its replacement reads the same ConfigMap and fails the same way. Revert first.

## What we saw in the lab

Scenario 19 in the [Aurora lab](https://github.com/Arvo-AI/aurora-lab): a five-component `shop` app on a three-node kind cluster, the `frontend-nginx` ConfigMap re-applied with `server api:8000;` in place of `server api:8080;`, one of the two frontend pods deleted, then Aurora given only the alert text ("HighErrorRate [warning] namespace=shop service=frontend: 48% of requests to /api/ returned 5xx over the last 10 minutes (threshold 5%).") and read-only kubectl under the default `observability_only` policy. Two runs, each in a fresh organization.

| Run | Verdict (human reviewed) | Time | Tool calls | Aurora cost |
|---|---|---|---|---|
| r1 | partial | 280 s | 15 | $0.39 |
| r2 | correct | 253 s | 28 | $0.84 |

r2 found it. Its report: "Because nginx only reads this config at container startup and does not hot-reload, the two frontend replicas diverged: `rr7xl`, which started before the bad edit, continued running a working configuration and successfully proxied to the backend, while `r4thz`, which came up after the ConfigMap was changed, mounted the broken config and returned repeated `504 Gateway Timeout` errors on every `/api/items` request due to upstream connection timeouts to `10.96.137.47:8000`". It listed what it could not check: `rollout history` and `exec` were blocked by policy, so "the 'good' replica's config could only be inferred from its logs, not confirmed by file content". The path was not direct: 28 tool calls, four of them errors, the failing pod's log read only at step 24, and a summary of that log calling the backend "unreachable or down" before the final analysis put the two pods side by side. The judge noted the report gave no remediation step; the fix it suggested separately, patch the ConfigMap to port 8080 and roll the frontend pods, was graded acceptable.

r1 is the miss, and the instructive one. It had every fact by step 13: both pods' logs, the Service on 8080, the ConfigMap on 8000, the pod ages, the events with the kill-and-create pair. Its report said: "The most likely cause is a port mismatch between the frontend's configured upstream and the actual backend service endpoint." True, and not the root cause. It never said the ConfigMap had been edited, never said only the recreated pod had loaded it, and never said the healthy pod was running a configuration that no longer exists in the API. Instead: "This discrepancy was not fully reconciled", because the `exec` was blocked, and "intermittent backend unavailability as a contributing factor cannot be ruled out", although it had not read the api logs and said so under Not Checked. The judge's verdict was partial: "It never says the ConfigMap was edited, or that the healthy pod still runs the old api:8080 config from memory because nginx only reads its config at start. It recommends no fix". A second reviewer model agreed and pointed out that the evidence was in hand, the pod age gap and the ConfigMap `resourceVersion` that differed from the last-applied one. A person at Arvo reviewed the grade and recorded partial. r1's suggested fix, graded on its own, was still right: patch the ConfigMap to `api:8080` and roll the frontend.

The difference between the runs is what counts as a cause. "The ConfigMap says 8000 and the Service says 8080" describes the state. "The ConfigMap was changed and one pod restarted" explains why half the fleet differs, and it is the sentence that tells the on-call engineer not to restart the other pod. r2 wrote that sentence. r1 wrote the first one and stopped. The full RCA for this incident, field by field, is in our [root cause analysis template](https://www.aurorasre.ai/blog/root-cause-analysis-template).

## How Aurora handles this

Aurora receives the alert, connects to the cluster read-only through an uploaded kubeconfig, and runs the kubectl commands shown above, choosing them itself. On this failure it read each frontend pod's log separately, compared the upstream in each with the Service's port and the ConfigMap, and wrote a report that cites each tool output by number and lists what it could not check. Mutating commands are blocked by policy, so the fix arrives as a suggestion for a person to apply. One of the two runs named the full cause; the other stopped at the port mismatch, and this page says so.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)

Related: [nginx [emerg] unknown directive](https://www.aurorasre.ai/runbooks/fix-nginx-emerg-unknown-directive-kubernetes), [a Service with no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes), [root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres), and what an [Aurora root cause analysis](https://www.aurorasre.ai/rca) looks like.

## Sources and verification

- Every command and output block above comes from the lab run `v1-run1`, scenario `19-config-drift-partial-restart`, on 2026-10-02: Aurora's tool-call transcripts for r1 and r2 and the cluster state captured before the investigation (`evidence-before/`). The fault was injected by the scenario's `break.sh` (a `sed` on the ConfigMap piped through `kubectl apply`, then `kubectl delete` of one frontend pod); the fix above is its inverse. The 78-line count and 40/35/3 split are from the r1 step 9 output.
- Versions, from `evidence-before/versions.yaml` and the pod logs: Kubernetes server v1.37.0 on kind v0.33.0 (three nodes), kubectl client v1.35.3; nginx 1.27-alpine reporting nginx/1.27.5.
- Aurora commit `87d22baf`, single agent, RCA model `bedrock/us.anthropic.claude-sonnet-5`. Verdicts: judge model `us.anthropic.claude-opus-5-5`, reviewed by a person at Arvo (`human_verdict` in each `grade.json`); fix suggestions graded separately (`fix_from_suggestions`).
- Documentation quoted or linked, all read 2026-10-19: Kubernetes [ConfigMaps](https://kubernetes.io/docs/concepts/configuration/configmap/) (sections "Mounted ConfigMaps are updated automatically" and "Immutable ConfigMaps") and the [kubectl logs reference](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_logs/) for `--prefix`; Helm, [Automatically Roll Deployments](https://helm.sh/docs/howto/charts_tips_and_tricks/); nginx, [Beginner's Guide](https://nginx.org/en/docs/beginners_guide.html).
- The fix and mitigation commands were not run inside a graded lab run; Aurora's policy blocks mutations, so Verify states the pass condition and the lab's own check instead of a captured result. That `check.sh` keyed on `502` when these runs were recorded and was changed on 2026-10-19 to accept `502` or `504`. The kube-state-metrics query is a suggestion, not a captured alert.

## Questions people ask

**Why did my ConfigMap change apply to only some pods?**

Because only the pods started after the edit read the new file. A process like nginx reads its configuration once at startup, so pods that were already running keep the old configuration in memory even though Kubernetes updates the mounted file underneath them. Any pod recreated after the edit, by a delete, a crash or a node drain, loads the new content.

**Does Kubernetes update a mounted ConfigMap inside running pods?**

Yes, eventually. The Kubernetes docs say projected keys are updated on the kubelet's periodic sync, with a delay of the sync period plus cache propagation. That updates the file, not the process that read it. ConfigMaps consumed as environment variables are not updated at all without a pod restart.

**How do I tell which configuration a running nginx pod is using?**

Not from the file: cat inside the pod shows the updated ConfigMap content in every pod. Read each pod's log and compare the upstream address or setting in its request or error lines, and compare the pods' start times with the time of the ConfigMap edit. In the lab the failing pod logged upstream :8000 and the healthy pod logged :8080.

**Why does one pod return 504 and the other 200 for the same request?**

The two pods are running different configurations. In the lab the newer pod proxied to api:8000, a port no Service publishes, so each connection timed out after the 2 s proxy_connect_timeout and nginx answered 504. The older pod still proxied to api:8080 and answered 200.

**Should I restart the Deployment to pick up the ConfigMap?**

Only after the ConfigMap is correct. Restarting with the bad ConfigMap in place makes every pod load it, and a half-failing service becomes a fully failing one. Revert or fix the ConfigMap first, then kubectl rollout restart so every replica runs the same file.

**Why does kubectl rollout history show nothing?**

A ConfigMap edit does not change the Deployment's pod template, so there is no new revision and no new ReplicaSet. In the lab the Deployment stayed at revision 1 and generation 1. The timeline is in the namespace events (a Killing and a SuccessfulCreate for the replaced pod) and in the pods' start times.

**What is the difference between upstream timed out and connection refused here?**

In the lab, connections to the ClusterIP on port 8000 timed out because no Service publishes that port, so nothing answered. The single connection refused on port 8080 happened three seconds after startup, before the api pods were ready endpoints, and never recurred. A refusal on the right port points at endpoints; a timeout on an unpublished port points at the config.

**How do I make a ConfigMap change roll out to every pod?**

Put a checksum of the ConfigMap in the pod template as an annotation, so every ConfigMap change changes the template and triggers a rolling update. Or mark the ConfigMap immutable and give each version a new name referenced from the Deployment, which the Kubernetes docs recommend as protection against accidental updates.

**Did Aurora find this fault in the lab?**

In one of two runs: r2 named the edited ConfigMap and the one-pod restart as the cause, while r1 stopped at the port mismatch between the ConfigMap and the Service and was graded partial. Both runs' suggested fix, revert the ConfigMap and restart the frontend, was graded acceptable.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-configmap-change-not-applied-to-all-pods-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
