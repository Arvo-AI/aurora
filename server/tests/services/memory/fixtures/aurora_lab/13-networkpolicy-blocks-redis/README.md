# Kubernetes NetworkPolicy blocking traffic: timeouts and the fix

*Requests time out while every pod is Running: a NetworkPolicy lets only other pods reach the dependency. Read the error, find the policy, fix it.*

Runbook for lab scenario `13-networkpolicy-blocks-redis`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-networkpolicy-blocking-traffic-kubernetes. Written from the lab runs in this repository; updated 2026-10-16.

## Reproduce it

```
./lab.sh up
./lab.sh break 13-networkpolicy-blocks-redis
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Pods that call another pod through a Service and get a connect timeout, in a cluster whose network plugin enforces NetworkPolicy; here api to redis under kindnet |
|---|---|
| Verified on | Kubernetes server v1.37.0 (kind v0.33.0, kubectl v1.35.3, containerd 2.3.4), redis 7.4.11, nginx 1.27.5, 2 October 2026 |
| Lab folder | [scenarios/13-networkpolicy-blocks-redis](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/13-networkpolicy-blocks-redis), runs in [runs/v1-run1/13-networkpolicy-blocks-redis](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v1-run1/13-networkpolicy-blocks-redis) |

**A connection that times out, while the same dependency is Running, Ready and has endpoints, is being dropped, not refused.** Inside a cluster the usual reason is a NetworkPolicy that selects the dependency's pods and lists the wrong clients in its `from` rule. Confirm with `kubectl get networkpolicies -n <namespace>` and `kubectl describe networkpolicy <name>`: if the client pod's labels do not match any `from` selector, every packet it sends is dropped and the client waits out its own timeout. The fix is to add the client's label to the policy, or remove the policy.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| Client logs say `timed out`; dependency is Running and has endpoints | A NetworkPolicy selects the dependency and does not allow this client | `kubectl describe networkpolicy -n <namespace>` against `kubectl get pod <client> --show-labels` | Add the client's label to the `from` list, or delete the policy |
| Client logs say `timed out`; the client's own namespace has an egress policy | An egress policy on the client blocks the destination | `kubectl get networkpolicies -n <client-namespace> -o yaml`, look for `Egress` in `policyTypes` | Allow the destination in the egress rule |
| Client logs say `Connection refused` | Nothing is listening: the dependency is starting, crashed, or its Service has no endpoints | `kubectl get endpoints <service>` | See [Service has no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes) |
| Client logs say `Name does not resolve` | Wrong hostname or broken cluster DNS | `kubectl get svc -n <namespace>` | See [Name does not resolve](https://www.aurorasre.ai/runbooks/fix-dns-name-not-resolving-kubernetes) |
| Client logs say `timed out` for every destination | Cluster-wide network fault or a default-deny policy | `kubectl get networkpolicies -A`, node and CNI health | Fix the plugin or the default-deny rule |

## How do you confirm the connection is timing out?

Start from the error the client prints. In the lab the api pods answered `/items` with 503 and logged the reason each time (Aurora's run r1, step 8, `kubectl logs -n shop -l app=api --tail=100`, trimmed):

```
redis connection failed (attempt 1/5): [Errno 111] Connection refused
connected to redis at redis:6379
api listening on :8080
10.244.1.1 "GET /healthz HTTP/1.1" 200 -
request failed: redis error: timed out
10.244.2.45 "GET /items HTTP/1.0" 503 -
request failed: redis error: timed out
10.244.2.45 "GET /items HTTP/1.0" 503 -
```

Two different errors sit in that log. The first line, `Connection refused`, is the startup race before redis was listening: the kernel answered at once because nothing was on the port. The later lines say `timed out`: the connection attempt got no answer at all for 2 seconds and the client gave up. Refused means a listener is missing. Timed out means packets are being dropped somewhere between the two pods.

The proxy in front shows the same failure as a status code only (frontend nginx, run r1, step 7, two lines):

```
10.244.1.36 - - [02/Oct/2026:20:23:56 +0000] "GET /api/items HTTP/1.1" 503 32 "-" "Wget" "-"
10.244.1.36 - - [02/Oct/2026:20:24:44 +0000] "GET /api/items HTTP/1.1" 503 32 "-" "Wget" "-"
```

Health checks keep passing, because `/healthz` does not touch the dependency. Nothing restarts, nothing is Pending, and every pod reports 1/1 Running (run r1, step 4):

```
NAME                        READY   STATUS    RESTARTS   AGE   IP            NODE                   NOMINATED NODE   READINESS GATES
api-f64486c4c-2nqsk         1/1     Running   0          73s   10.244.1.34   aurora-lab-3-worker    <none>           <none>
api-f64486c4c-mvpnl         1/1     Running   0          73s   10.244.2.43   aurora-lab-3-worker2   <none>           <none>
frontend-76d98597b9-h4vn7   1/1     Running   0          72s   10.244.2.45   aurora-lab-3-worker2   <none>           <none>
frontend-76d98597b9-vgf28   1/1     Running   0          72s   10.244.1.35   aurora-lab-3-worker    <none>           <none>
loadgen-7c6676864-6wnwp     1/1     Running   0          72s   10.244.1.36   aurora-lab-3-worker    <none>           <none>
redis-59458f55d8-n6glm      1/1     Running   0          73s   10.244.2.42   aurora-lab-3-worker2   <none>           <none>
worker-6cf88d4b58-vk9gh     1/1     Running   0          72s   10.244.2.44   aurora-lab-3-worker2   <none>           <none>
```

A 100 percent error rate with a green pod list is the shape of this failure.

## How much is affected, and since when?

Read the client that drives real traffic. The lab's load generator calls `/api/items` every 4 seconds, and every call failed (run r1, captured cluster state, `logs/loadgen-7c6676864-6wnwp.loadgen.log`, last five lines):

```
2026-10-02T20:24:28Z GET /api/items -> 503
2026-10-02T20:24:32Z GET /api/items -> 503
2026-10-02T20:24:36Z GET /api/items -> 503
2026-10-02T20:24:40Z GET /api/items -> 503
2026-10-02T20:24:44Z GET /api/items -> 503
```

Then find what changed. Pods, Services and Deployments do not have to change for traffic to stop: a NetworkPolicy is its own object with its own creation time. Both api replicas were affected, on both worker nodes, which rules out a single bad node. The policy's `Created on` timestamp, 20:23:54 (below), falls 9 seconds after the api pods started at 20:23:45 and 52 seconds before the alert fired at 20:24:46. If the policy is older than the outage, something else changed: a label on the client pods, or a second policy.

## Which of the possible causes is it?

Work from the cheapest check to the most specific.

**1. Is the dependency itself healthy?** Logs, state and endpoints, in that order (run r1, step 10, `kubectl logs -n shop -l app=redis --tail=100`, last three lines):

```
1:M 02 Oct 2026 20:23:45.678 * Running mode=standalone, port=6379.
1:M 02 Oct 2026 20:23:45.678 * Server initialized
1:M 02 Oct 2026 20:23:45.678 * Ready to accept connections tcp
```

`describe pod` showed `State: Running`, `Ready: True`, `Restart Count: 0` (run r1, step 11), and the Service has a backend (run r2, step 13):

```
NAME    ENDPOINTS          AGE
redis   10.244.2.55:6379   7m22s
```

A healthy dependency with a populated endpoint list and a client that still times out means the path between them is blocked. If the endpoint list is empty, you have a different problem: [Service has no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes).

**2. Does the name resolve to the right Service?** The client's config and the Service list have to agree (run r2, step 12, then run r1, step 15):

```
[{"name":"REDIS_HOST","value":"redis"}]
```

```
NAME       TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)    AGE
api        ClusterIP   10.96.37.132    <none>        8080/TCP   2m2s
frontend   ClusterIP   10.96.44.77     <none>        80/TCP     2m1s
redis      ClusterIP   10.96.107.182   <none>        6379/TCP   2m2s
```

A resolution failure prints a different error (`Name does not resolve`, `Name or service not known`), so if the log says `timed out`, DNS is already ruled out.

**3. Is there a NetworkPolicy selecting the dependency?** This is the check most people run last. Run it third (run r1, step 20):

```
NAME    POD-SELECTOR   AGE
redis   app=redis      2m49s
```

Then read who it lets in (run r1, step 22):

```
Name:         redis
Namespace:    shop
Created on:   2026-10-02 20:23:54 +0000 UTC
Labels:       <none>
Annotations:  <none>
Spec:
  PodSelector:     app=redis
  Allowing ingress traffic:
    To Port: 6379/TCP
    From:
      PodSelector: app=worker
  Not affecting egress traffic
  Policy Types: Ingress
```

**4. Do the client's labels match the `from` selector?** They have to match exactly (run r1, step 23):

```
NAME                  READY   STATUS    RESTARTS   AGE    LABELS
api-f64486c4c-2nqsk   1/1     Running   0          3m8s   app=api,pod-template-hash=f64486c4c
api-f64486c4c-mvpnl   1/1     Running   0          3m8s   app=api,pod-template-hash=f64486c4c
```

`app=api` is not `app=worker`. The [NetworkPolicy documentation](https://kubernetes.io/docs/concepts/services-networking/network-policies/) describes the mechanism: "By default, a pod is non-isolated for ingress; all inbound connections are allowed. A pod is isolated for ingress if there is any NetworkPolicy that both selects the pod and has "Ingress" in its policyTypes". Once the redis pods are isolated, only the listed sources get through, and the api is not listed. Nothing in the api pod's own namespace, labels or events records the drop; the only evidence on the client side is the timeout.

If the policy allows the client and the timeout persists, look for a second policy with `Egress` in `policyTypes` that selects the client, then for a `namespaceSelector` that excludes the client's namespace, then at the network plugin itself. The same documentation page notes that "Creating a NetworkPolicy resource without a controller that implements it will have no effect", so a cluster whose plugin does not enforce policies will show the opposite symptom: a policy that looks restrictive but blocks nothing. The lab's `check.sh` relies on kindnet enforcing the policy: without enforcement the api keeps answering 200.

## How do you fix it?

Two acceptable fixes. Pick the one that matches the policy's intent.

**Add the client to the policy.** Keeps the restriction for everyone else:

```shell
kubectl -n shop patch networkpolicy redis --type=json -p '[{"op":"add","path":"/spec/ingress/0/from/-","value":{"podSelector":{"matchLabels":{"app":"api"}}}}]'
```

Risk: low. The patch widens one rule; no pod restarts, no connection to any other workload changes. Rollback: `kubectl -n shop patch networkpolicy redis --type=json -p '[{"op":"remove","path":"/spec/ingress/0/from/1"}]'`, which removes the entry you added.

**Remove the policy.** The lab's `break.sh` applies the policy; its inverse undoes the fault:

```shell
kubectl -n shop delete networkpolicy redis
```

Risk: the dependency is open to every pod in the cluster again, which is the state before the policy existed. Rollback: re-apply the policy manifest from version control.

Do not restart the api or redis. Restarting changes nothing about which pods the policy admits, and it is one of the wrong answers the lab scenario lists.

## How do you verify the fix?

The client must stop logging the timeout and the caller must get 200s. The lab's `check.sh` proves the broken state with one line; its two conditions, negated, are the pass condition:

```shell
kubectl -n shop logs deploy/loadgen --tail=5 | grep -q -- '-> 503' &&
kubectl -n shop logs -l app=api --tail=20 | grep -q 'redis error'
```

Pass when neither `grep` matches: the last five load generator lines end in `-> 200` and the api's last 20 lines contain no `redis error`. Watch for two minutes; the api opens a new connection per request, so recovery is immediate once the policy admits it.

## How do you prevent it?

**Alert.** The lab alert was the right one: a 5xx ratio on the service in front, not a pod status. Every pod here was Running, so a restart or readiness alert would have stayed quiet. With nginx or ingress controller metrics, alert when 5xx responses exceed 5 percent of requests for 5 minutes (suggested expression, not captured in the lab):

```
sum(rate(nginx_ingress_controller_requests{status=~"5.."}[5m])) / sum(rate(nginx_ingress_controller_requests[5m])) > 0.05
```

**Guardrail.** Treat a NetworkPolicy change like a firewall change: the review lists every client of the selected pods and checks each one against the `from` selectors. In the lab the policy's author allowed the worker, which never talks to redis, and forgot the api, which does on every request.

## Common mistakes

- **Reading "Running" as "reachable".** Pod status says the container is up. It says nothing about who is allowed to talk to it.
- **Restarting the client.** A new pod gets the same labels, so the policy drops it the same way.
- **Blaming the dependency.** redis was healthy on every check: logs, state, endpoints, 3m CPU and 4Mi memory (run r1, step 12). The failure lives in the policy between them.
- **Treating refused and timed out as the same error.** Refused points at the listener or the endpoints; timed out points at the path. Behind nginx, a timed-out connection shows up as a [504 while connecting to upstream](https://www.aurorasre.ai/runbooks/fix-504-gateway-timeout-nginx-kubernetes).
- **Looking only at the client's namespace.** The policy lives on the dependency's pods. If the dependency is in another namespace, check there too.

## What we saw in the lab

Scenario 13 was part of run v1-run1 (2 October 2026), the harder set where noise sits next to the fault. The alert said `HighErrorRate [critical] namespace=shop service=frontend: 100% of requests to /api/ returned 5xx over the last 5 minutes`. Aurora got the alert text and read-only kubectl under its default `observability_only` policy, two runs in fresh orgs.

| Run | Verdict | Time | Tool calls | Cost | Fix proposed |
|---|---|---|---|---|---|
| r1 | correct (human reviewed) | 202 s | 25 | $0.69 | Patch the `redis` NetworkPolicy to allow `app=api` ingress |
| r2 | correct (human reviewed) | 444 s | 19 | $0.54 | Patch the `redis` NetworkPolicy to allow ingress from `app=api` pods |

Both runs met both required findings: the api cannot reach redis and redis itself is healthy, and the cause is the policy that allows only `app=worker`. Neither blamed redis or DNS, and neither proposed a restart.

r1 took the long way. Its 19 kubectl calls went pods, events, frontend logs, api logs, ConfigMaps, redis logs, redis describe, `top pods`, worker logs, the `api-code` ConfigMap (where it read the 2 second socket timeout in the api's `redis_ping`), Services, `top nodes`, worker describe, nodes, the loadgen pod, and only then `get networkpolicies` as its 16th kubectl call, followed by the api Deployment, `describe networkpolicy` and the api pod labels. Its report put it plainly: "The root cause was a NetworkPolicy named `redis` in the shop namespace, created at 2026-10-02T20:23:54Z, which restricts ingress to redis:6379 to pods labeled `app=worker` only".

r2 was more direct with 14 kubectl calls: pods, events, frontend log, api log, loadgen log, redis log, redis describe, Services, the api's `REDIS_HOST`, the redis endpoints, the policy YAML, then the redis and api labels, then the worker logs. It also wrote something the evidence does not support: that the worker "successfully processed Redis-backed batches during the same window". The worker's code in the `api-code` ConfigMap only allocates and frees buffers and never connects to redis, a point r1 noticed and flagged under "Not Checked". The AI review recorded in the grade flagged the sentence as unsupported; it did not change the verdict because it was not part of the root cause.

Across the nine v1 scenarios this one tied for the most expensive for Aurora at a median of $0.61, and was the third slowest at a median of 323 s. The reason is visible in r1's path: the policy object is not attached to any pod, Deployment or event, so an investigator that starts from the pods has to run out of other explanations before listing policies. Running `get networkpolicies` third, as this runbook does, is the shortcut the lab taught us.

## How Aurora handles this

Aurora receives the alert, reads the pods, events and logs for the service in front and the one behind, confirms the dependency is healthy, and then lists the namespace's NetworkPolicies and compares their selectors to the client's labels, citing each finding to a command output. In both lab runs it named the policy, the exact label mismatch and the fix, and it refused to blame redis or propose a restart. Aurora runs read-only by default, so the patch above stays a suggestion until a person applies it.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)
- **See it on an incident:** [Aurora root cause analysis](https://www.aurorasre.ai/rca)

## Sources and verification

- Lab scenario and fault: [scenarios/13-networkpolicy-blocks-redis](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/13-networkpolicy-blocks-redis) (`scenario.yaml`, `break.sh`, `check.sh`).
- Every output block above is from [runs/v1-run1/13-networkpolicy-blocks-redis](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v1-run1/13-networkpolicy-blocks-redis): the run's tool transcripts (`steps-full.json`), the captured cluster state (`evidence-before/`), Aurora's reports (`aurora-rca.md`) and the grades (`grade.json`), with the run and step named next to each block. Outputs are trimmed, never edited.
- Versions from `evidence-before/versions.yaml` and `kubectl get nodes -o wide` (run r1, step 18): Kubernetes server v1.37.0, kubectl v1.35.3, kind v0.33.0, containerd 2.3.4. redis 7.4.11 from its startup log, nginx 1.27.5 from the frontend log.
- The two quoted sentences on isolation and on controllers are from the [Kubernetes NetworkPolicy documentation](https://kubernetes.io/docs/concepts/services-networking/network-policies/), read on 16 October 2026.
- The alert expression under Prevent is a suggestion, not something the lab captured.
- Method and timings: [Root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres) and the [lab report](https://www.aurorasre.ai/blog/aurora-lab-report-kubernetes-19-failures).

## Questions people ask

**Why does my pod time out connecting to another pod when both are Running?**

A NetworkPolicy that selects the destination pods and does not list your pod in its from rule drops every packet you send, so the connection waits out its timeout. Pod status never shows this; only the client's error text and the policy's selectors do.

**What is the difference between connection refused and connection timed out?**

Refused means the packet arrived and nothing was listening: the dependency is starting, crashed, or its Service has no endpoints. Timed out means no answer came back at all, which inside a cluster usually means a NetworkPolicy or another network rule is dropping the traffic.

**How do I find the NetworkPolicy that blocks my traffic?**

Run kubectl get networkpolicies in the namespace of the pod you are trying to reach, then kubectl describe networkpolicy on each one. Compare the podSelector under From with the labels of your client pod from kubectl get pod --show-labels. If they do not match, your pod is blocked.

**Does a NetworkPolicy block traffic from pods it does not mention?**

Yes. Once any policy with Ingress in its policyTypes selects a pod, that pod is isolated for ingress and only the sources the policies list can reach it. Everything else is dropped silently.

**How do I fix a NetworkPolicy that blocks a legitimate client?**

Add the client's labels to the policy's from list with kubectl patch, or delete the policy if it should not exist. Neither needs a restart. Do not restart the client: a new pod carries the same labels and is dropped the same way.

**Why do my health checks pass while requests fail?**

Health endpoints usually do not call the dependency. In the lab, /healthz returned 200 on every probe while /items returned 503 on every request, so readiness, liveness and restart counts all stayed green.

**Can a NetworkPolicy exist without blocking anything?**

Yes. Policies are enforced by the network plugin. The Kubernetes documentation says creating a NetworkPolicy without a controller that implements it has no effect, so on a cluster without enforcement a restrictive policy blocks nothing.

**How did Aurora find the NetworkPolicy in the lab?**

In both runs it confirmed redis was healthy, checked the Service and endpoints, then listed the namespace's NetworkPolicies and compared the from selector app=worker with the api pods' label app=api. Both runs named the policy and the fix; one took 202 seconds and 25 tool calls, the other 444 seconds and 19.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-networkpolicy-blocking-traffic-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
