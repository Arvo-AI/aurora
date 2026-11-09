# Kubernetes dnsPolicy None: Name does not resolve, the fix

*New pods log Name does not resolve for a Service that exists. The template set dnsPolicy None with search domains for another cluster domain. Lab-tested.*

Runbook for lab scenario `16-dns-search-domain`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-pod-dnsconfig-search-domain-not-resolving-kubernetes. Written from the lab runs in this repository; updated 2026-10-19.

## Reproduce it

```
./lab.sh up
./lab.sh break 16-dns-search-domain
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Any pod with `dnsPolicy: None`, a custom `dnsConfig` search list or `ndots` that cannot resolve a Service by its short name; here a Python api calling `redis` after a rollout |
|---|---|
| Verified on | Kubernetes server v1.37.0 (kind v0.33.0), 2 October 2026 |
| Lab folder | [scenarios/16-dns-search-domain](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/16-dns-search-domain), runs in [runs/v1-run1/16-dns-search-domain](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v1-run1/16-dns-search-domain) |

**When a pod logs `Name does not resolve` for a Service that exists, and the hostname in its environment is spelled right, the fault is the pod's resolver configuration, not the name: `dnsPolicy: None` with a `dnsConfig` whose search domains belong to another cluster domain (here `cluster.internal` on a `cluster.local` cluster) makes every short name expand to records that do not exist, while the dependency, CoreDNS and the previous revision's `ClusterFirst` pods stay healthy.** Confirm with `kubectl get pod <pod> -n <namespace> -o jsonpath='{.spec.dnsPolicy}{"\n"}{.spec.dnsConfig}'` next to the `kubernetes` zone in the CoreDNS Corefile. The fix is to drop the `dnsConfig` and return to `ClusterFirst`, correct the search domains to the real cluster domain, or roll the Deployment back. If the hostname itself is wrong, that is the [Name does not resolve](https://www.aurorasre.ai/runbooks/fix-dns-name-not-resolving-kubernetes) runbook; this page is for when the name is right and the pod cannot look it up.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| New pods of one Deployment log `Name does not resolve`, the env hostname is an existing Service, the template has `dnsPolicy: None` | `dnsConfig.searches` carries another cluster's domain | `.spec.dnsConfig.searches` against the `kubernetes` zone in the coredns ConfigMap | Remove the `dnsConfig` and restore `ClusterFirst`, fix the suffixes, or roll back |
| Same symptom, but the env hostname is not in `kubectl get svc -n <namespace>` | The name is wrong | The Environment block against the Service list | See [a wrong Service hostname](https://www.aurorasre.ai/runbooks/fix-dns-name-not-resolving-kubernetes) |
| Short names fail, `<svc>.<namespace>.svc.cluster.local` works | `dnsPolicy: Default` (the node's resolv.conf) or `None` without the cluster suffixes | `kubectl exec <pod> -- cat /etc/resolv.conf` | Set `ClusterFirst`, or add the cluster suffixes to `searches` |
| External names resolve slowly, CoreDNS (with the `log` plugin on) shows several NXDOMAIN per name | `ndots:5` applied to names with fewer than five dots | Count the queries per name in CoreDNS logs, which needs the `log` plugin in the Corefile | A trailing dot in the app, or a lower `ndots` for that pod only |

## How do you confirm the name is right and the resolver is wrong?

The pod list looks exactly like the wrong-name case: one new api pod in `Error` with 3 restarts, the two old api pods and redis `1/1 Running` (run r1, step 4). So does the finished container's log (r1, step 6):

```
$ kubectl logs api-779966d57-f8nsq -n shop --previous --tail=100
redis connection failed (attempt 1/5): [Errno -2] Name does not resolve
redis connection failed (attempt 2/5): [Errno -2] Name does not resolve
redis connection failed (attempt 3/5): [Errno -2] Name does not resolve
redis connection failed (attempt 4/5): [Errno -2] Name does not resolve
redis connection failed (attempt 5/5): [Errno -2] Name does not resolve
giving up: redis unavailable
```

Two checks separate this page from that one. First, the hostname the app was given (r1, step 7, trimmed):

```
    Last State:     Terminated
      Reason:       Error
      Exit Code:    1
      Started:      Fri, 02 Oct 2026 20:31:18 +0000
      Finished:     Fri, 02 Oct 2026 20:31:28 +0000
    Restart Count:  3
    Environment:
      REDIS_HOST:  redis
```

Second, whether a Service by that name exists (r2, step 10):

```
$ kubectl get svc -n shop -o wide
NAME       TYPE        CLUSTER-IP      EXTERNAL-IP   PORT(S)    AGE    SELECTOR
api        ClusterIP   10.96.167.158   <none>        8080/TCP   2m8s   app=api
frontend   ClusterIP   10.96.246.174   <none>        80/TCP     2m8s   app=frontend
redis      ClusterIP   10.96.198.186   <none>        6379/TCP   2m8s   app=redis
```

`REDIS_HOST` is `redis` and `redis` is a Service in the same namespace. The name is right; the question is how this pod turns it into a query.

The timestamps add one fact: ten seconds for five attempts with a two-second sleep between them (`time.sleep(2)` in `api.py`, r2 step 22), so each lookup failed at once. An unreachable nameserver fails slowly, on the resolver timeout; a fast `Name does not resolve` means a server answered and said no.

## How far does it reach?

**Which pods.** Only the new ReplicaSet: `kubectl get rs -n shop -o wide` shows `api-779966d57` with 1 desired and 0 ready, 89 seconds old, next to `api-f64486c4c` at 2/2, 95 seconds old (r2, step 9). Users are still served: the `api` Service's endpoints hold only the two old pods, `10.244.1.48:8080,10.244.2.58:8080` (r2, step 21).

**Since when.** The Deployment status shows the stuck rollout: `replicas: 3`, `updatedReplicas: 1`, `unavailableReplicas: 1`, `Progressing: True` (r1, step 5). The new ReplicaSet is six seconds younger than the old one; the lab broke the Deployment right after creating it.

**After which change.** The live template has a `dnsConfig` block and `dnsPolicy: None`; the `kubectl.kubernetes.io/last-applied-configuration` annotation on the same Deployment has neither. A field present in the live spec and absent from the annotation came from a patch, an edit or a controller, not `kubectl apply`; the lab's `break.sh` was such a patch, adding the block shown in the next section. `kubectl rollout history deployment/api -n shop` lists the revisions; the lab's read-only policy denied it in both runs, but you can run it.

## How do you isolate the cause?

**1. Read the pod's DNS settings and compare them with a healthy pod's.** From the crashing pod (r2, step 13, the two fields that matter):

```shell
kubectl get pod api-779966d57-v8d2r -n shop -o yaml
```

```
  dnsConfig:
    nameservers:
    - 10.96.0.10
    options:
    - name: ndots
      value: "5"
    searches:
    - shop.svc.cluster.internal
    - svc.cluster.internal
    - cluster.internal
  dnsPolicy: None
```

From a pod of the old ReplicaSet (r2, step 14): `dnsPolicy: ClusterFirst` and no `dnsConfig` block. Same image, same command, same `REDIS_HOST`, both on `aurora-lab-3-worker`. The DNS block is the whole diff.

**2. Read the cluster's real domain.** It is in the `kubernetes` plugin line of the Corefile (r1, step 11, trimmed):

```shell
kubectl get configmap coredns -n kube-system -o yaml
```

```
    .:53 {
        kubernetes cluster.local in-addr.arpa ip6.arpa {
           pods insecure
           fallthrough in-addr.arpa ip6.arpa
           ttl 30
        }
        forward . /etc/resolv.conf {
           max_concurrent 1000
        }
    }
```

CoreDNS answers for `cluster.local` and forwards everything else upstream. With this search list, a lookup of `redis` goes out as `redis.shop.svc.cluster.internal`, `redis.svc.cluster.internal`, `redis.cluster.internal` and finally `redis` on its own. None is a `cluster.local` name, so each is forwarded upstream and answered with no such record. The record `redis.shop.svc.cluster.local` existed the whole time; this pod never asked for it.

**3. Check the nameserver is the right one.** It was, which is why the failure was fast rather than slow: `kubectl get svc kube-dns -n kube-system` shows ClusterIP `10.96.0.10` (r2, step 16), the address in the `dnsConfig`, and both CoreDNS pods were `1/1 Running` with 0 restarts (r2, step 15). The server is right; only the suffixes are wrong.

**4. Read `/etc/resolv.conf` when you can.** The kubelet writes this file for every pod, and for `dnsPolicy: "None"` the [Kubernetes DNS page](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/) says "All DNS settings are supposed to be provided using the `dnsConfig` field in the Pod Spec": the file is the `dnsConfig` written out as `nameserver`, `search` and `options` lines.

```shell
kubectl -n shop exec <pod> -- cat /etc/resolv.conf
```

Nobody ran it in the lab. Aurora tried it on a healthy old pod and its read-only policy blocked `exec` as a mutating operation (r1, step 10); on the crashing pod your own attempt is also a race against a container that lives ten seconds. From the spec, the broken pod's file has to read (derived, not captured):

```
nameserver 10.96.0.10
search shop.svc.cluster.internal svc.cluster.internal cluster.internal
options ndots:5
```

while a `ClusterFirst` pod gets the cluster's own list, in the docs' example `search <namespace>.svc.cluster.local svc.cluster.local cluster.local` with `options ndots:5`. Neither block is captured output. `nameserver` is who gets asked, `search` is what gets appended to short names, in order, and `ndots` is the threshold, defined in the [resolv.conf manual](https://man7.org/linux/man-pages/man5/resolv.conf.5.html) as "a threshold for the number of dots which must appear in a name given to res_query(3) (see resolver(3)) before an initial absolute query will be made. The default for n is 1". At 5, even `redis.shop.svc.cluster.local` (four dots) walks the search list before it is tried as written; a trailing dot skips the walk.

The four `dnsPolicy` values, from the same page: `ClusterFirst` is the default ("If `dnsPolicy` is not explicitly specified, then "ClusterFirst" is used"); `Default` means "The Pod inherits the name resolution configuration from the node"; `ClusterFirstWithHostNet` is `ClusterFirst` for `hostNetwork` pods; `None` gives the pod only what `dnsConfig` says. Under `ClusterFirst` a `dnsConfig` is additive: its searches "will be merged into the base search domain names generated from the chosen DNS policy", so the same three `cluster.internal` suffixes would have cost a few extra failed queries per lookup and nothing else. `None` turned a sloppy copy into a dead revision.

## How do you fix it?

None of these commands was run inside a graded lab run. Each is the inverse of the injected patch or a standard rollout operation.

**Cause: the `dnsConfig` was copied from another cluster and nothing needs it.** Remove it and let the pod go back to `ClusterFirst`:

```shell
kubectl -n shop patch deploy api --type=json \
  -p='[{"op":"remove","path":"/spec/template/spec/dnsConfig"},{"op":"replace","path":"/spec/template/spec/dnsPolicy","value":"ClusterFirst"}]'
```

Risk: low, but anything else intentional in the block (a stub nameserver, an `edns0` option) goes with it, so read it first. Rollback: `kubectl -n shop rollout undo deploy/api`. Fix the manifest or Helm values in git too, or the next apply brings it back.

**Cause: the pod needs `dnsPolicy: None` for a reason, and only the suffixes are wrong.** Replace them with the real domain:

```shell
kubectl -n shop patch deploy api --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/dnsConfig/searches","value":["shop.svc.cluster.local","svc.cluster.local","cluster.local"]}]'
```

Risk: you now maintain by hand a list the kubelet would have generated, and it goes stale when the pod moves to another namespace or cluster. Rollback: `kubectl -n shop rollout undo deploy/api`.

**Alternative: roll back.**

```shell
kubectl -n shop rollout undo deploy/api
```

Same result in the lab, because the DNS block was the only change in revision 2. Risk: anything else in revision 2 is undone; `--to-revision=2` reverses it.

Mitigation is not needed while the old ReplicaSet is healthy: `Available: True` and two live endpoints mean users are served.

## How do you verify the fix?

Pass condition: the rollout completes, every api pod reports `dnsPolicy: ClusterFirst` (or your corrected `dnsConfig`), and the new pods log a connection, not a retry loop.

```shell
kubectl -n shop rollout status deploy/api
kubectl -n shop get pods -l app=api -o jsonpath='{range .items[*]}{.metadata.name} {.spec.dnsPolicy} {.spec.dnsConfig.searches}{"\n"}{end}'
kubectl -n shop logs -l app=api --tail=3
```

A healthy api pod logs `connected to redis at redis:6379` and then `api listening on :8080` (`evidence-before` log of `api-f64486c4c-zt6dk`, run r2). The lab's own check for the broken state is one line; when it stops matching, you are done:

```shell
kubectl -n shop get pods -l app=api -o jsonpath='{.items[*].status.containerStatuses[0].lastState.terminated.exitCode}' | grep -q 1
```

Watch for two minutes: the new ReplicaSet should reach 2/2 and the old one scale to 0.

## How do you prevent it?

**Do not set `dnsPolicy: None` to add a search domain.** Keep `ClusterFirst` and put the extra entries in `dnsConfig`; the kubelet merges them. Reserve `None` for pods that must not use cluster DNS at all, and treat any `None` in review as a question about where the pod expects `redis` to live.

**Never hardcode the cluster domain in an application manifest.** It belongs to the cluster; `cluster.local` is only the default. A pre-deploy check that every `dnsConfig.searches` entry on a `dnsPolicy: None` template ends in the domain from the Corefile's `kubernetes` line is a short script against two `kubectl get` calls (suggested, not run in the lab).

**Alert on the rollout, not only the pod.** `KubePodCrashLooping` fires for any crash; a Deployment that cannot finish updating is the condition that describes this one. With kube-state-metrics, `kube_deployment_status_replicas_unavailable > 0` held longer than a normal rollout is a suggested expression, not captured in the lab.

## Common mistakes

- **Changing `REDIS_HOST`.** It is correct. Setting it to `redis.shop.svc.cluster.local` would make this one lookup work from the broken pod, since the nameserver is CoreDNS, and leave every other short name broken.
- **Blaming CoreDNS.** The old api pods are serving `/items`, which needs redis, both CoreDNS pods are healthy, and the broken pod never sent CoreDNS a `cluster.local` query.
- **Testing from a fresh debug pod.** `kubectl run` gives the new pod `ClusterFirst`, so `nslookup redis` succeeds and proves nothing about the failing pod. Read that pod's spec, or `exec` into it if it lives long enough.

## What we saw in the lab

Scenario `16-dns-search-domain`: `break.sh` patched the healthy api Deployment with the `dnsPolicy: None` and `dnsConfig` block shown above, within seconds of creating it (`break.log` puts the patch 15 s into the scenario; the revision 2 ReplicaSet is dated 10 s after the Deployment in r1 and 6 s in r2). Aurora received the alert text, `KubePodCrashLooping [warning] namespace=shop deployment=api: Pod shop/api (container api) is in waiting state (reason: CrashLoopBackOff) after the latest rollout.`, and read-only kubectl under its default policy. Two runs on 2 October 2026 (Aurora commit `87d22baf`, single agent, model `bedrock/us.anthropic.claude-sonnet-5`), each in a fresh account, on cluster `aurora-lab-3`. A correct run had to name the symptom (`redis` does not resolve) and the cause (the pod's `dnsPolicy: None` and `cluster.internal` search domains), and blame neither `REDIS_HOST`, the redis Service nor CoreDNS.

| Run | Verdict | Time | Tool calls | Tool errors | Cost | Fix in suggestions |
|---|---|---|---|---|---|---|
| r1 | correct (judge, then human) | 140 s | 15 | 2 | $0.40 | "Correct `dnsConfig.searches` in `deployment/api` to use `cluster.local`" (acceptable) |
| r2 | correct (judge, then human) | 205 s | 24 | 2 | $0.56 | "Remove the invalid `dnsPolicy: None`/`dnsConfig` override from `shop/api`" (acceptable) |

Both separated "name wrong" from "resolver wrong" the same way: the crashing pod's log, then the Deployment or pod YAML, then the Corefile. r1 made ten kubectl calls (44 seconds of summed tool time, issued in four parallel batches) in its 140 seconds and had the cause by step 8, when it compared the two ReplicaSets' templates. Its report: "The revision 2 pod template introduced `dnsPolicy: None` with an explicit `dnsConfig.searches` list using the domain suffix `cluster.internal` [8], whereas the cluster's actual CoreDNS zone is `cluster.local`, as declared in the `coredns` ConfigMap's Corefile (`kubernetes cluster.local in-addr.arpa ip6.arpa`) [11]."

r2 made nineteen kubectl calls (84 seconds of summed tool time, seven batches) in its 205 seconds, adding the old pod's log, both Services' endpoints, the CoreDNS pods and the application code. Its report named the mechanism: "Because `dnsPolicy: None` disables fallback to default DNS behavior, pods under revision 2 could not resolve the `redis` hostname, producing `[Errno -2] Name does not resolve` across all 5 connection attempts in the container logs [7]." It also noted that the override was "absent from the `kubectl.kubernetes.io/last-applied-configuration` annotation [19]". One of its two tool errors was its own: a jsonpath containing `{"\n"}` reached kubectl with the backslash intact and failed with `unrecognized character in action: U+005C`, so it fell back to the full YAML.

What the reports got wrong or did not prove:

- **Neither run read `/etc/resolv.conf`.** r1 tried `exec ... cat /etc/resolv.conf`, was denied, and listed it under Not Checked; r2 never tried. Both diagnoses rest on the pod spec plus the Corefile; the file itself was never seen.
- **r1 inferred the old pods' health from their status.** Its Ruled Out says revision-1 pods "connect to it successfully with zero restarts"; r1 never read an old pod's log. The second automated review flagged this. r2 did read one.
- **r1's first guess survived into its report.** After one read of the Deployment it wrote "the rollout trigger was likely a ConfigMap change"; no ConfigMap changed, and the guess is still in its Not Checked list as "who/what triggered the ConfigMap or template change", which the judge called stray wording.
- **r2 cited the wrong pod for a same-node claim.** It wrote that "a sibling pod on the same node resolved names and served traffic", citing the log of `api-f64486c4c-7skgm`, which runs on `worker2`; the crashing pod is on `worker`. The same review flagged its "1 of 3 desired replicas unavailable": the Deployment wants 2, and 3 is the surge total during the rollout.
- **Neither report states the fix in its body.** Both judge notes read "The RCA gives no explicit remediation". The fix is in each run's closing summary (r1: "Roll back deployment/api in shop to revision 1"; r2: "kubectl rollout undo deployment/api -n shop", each with a forward-fix on the `dnsConfig` as the alternative) and in the suggestions, where it was graded acceptable.

Transcripts, grades and the pre-investigation evidence: [runs/v1-run1/16-dns-search-domain](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v1-run1/16-dns-search-domain).

## How Aurora handles this

Aurora receives the alert, lists the pods, reads the previous container's log, and compares the crashing pod's template with a running pod's from the previous ReplicaSet, which is where `dnsPolicy: None` and the `dnsConfig` block show up. It then reads the CoreDNS Corefile for the cluster's real domain and names the mismatch, each claim cited to the tool output it came from. It runs read-only kubectl under a command policy by default, so `exec` and `rollout` are denied and listed under Not Checked, and it proposes the patch or the rollback rather than applying either. See a full investigation on the [Aurora root cause analysis](https://www.aurorasre.ai/rca) page.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)

Related: [a wrong Service hostname](https://www.aurorasre.ai/runbooks/fix-dns-name-not-resolving-kubernetes), [a Service with no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes), [an init container waiting on a name that does not resolve](https://www.aurorasre.ai/runbooks/fix-pod-stuck-init-container-kubernetes), and [root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres).

## Sources and verification

- Every command and output block above comes from lab run `v1-run1`, scenario `16-dns-search-domain`, on 2026-10-02: Aurora's tool-call transcripts for r1 and r2, each block attributed to its run and step, plus `evidence-before/` captured before the investigation (the healthy pod's startup log). Versions from `evidence-before/versions.yaml`: Kubernetes server v1.37.0 on kind v0.33.0, kubectl client v1.35.3.
- Aurora commit `87d22baf`, single agent, RCA model `bedrock/us.anthropic.claude-sonnet-5`. Verdicts: judge model `us.anthropic.claude-opus-5-5` on Aurora's full output, then a person at Arvo (`human_verdict` in each run's `grade.json`). Times, tool calls, tool errors and costs from `result.json`; summed tool time from `duration_ms` per step in `timeline.json`; the fix column from `fix_from_suggestions` in `grade.json`.
- Kubernetes documentation quoted, read 2026-10-19: [DNS for Services and Pods](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/) for the `dnsPolicy` values, the `dnsConfig` merge rule and the resolv.conf example. The `ndots` definition is quoted from the [resolv.conf(5) manual page](https://man7.org/linux/man-pages/man5/resolv.conf.5.html), which the Kubernetes page links to.
- Not captured, and labelled as such above: both `/etc/resolv.conf` blocks (derived from the pod spec and the documented mapping), the search-list expansion of `redis`, the fix and verify commands, the alert expression and the CI check. The fix patches invert `scenarios/16-dns-search-domain/break.sh`; the last Verify command is the lab's `check.sh`.

## Questions people ask

**Why does my pod get Name does not resolve when the Service exists?**

Because the pod's resolver is not looking where the record is. With dnsPolicy None, the pod uses only the search domains in its dnsConfig; if those end in another cluster's domain, a short name like redis expands to names under that domain, none of which exist, and the lookup fails even though redis.shop.svc.cluster.local is there.

**What does dnsPolicy None do?**

It tells the kubelet to ignore the cluster's DNS settings and write /etc/resolv.conf from the pod's dnsConfig alone. The Kubernetes docs say all DNS settings are supposed to be provided using the dnsConfig field. Nothing from the cluster, including its search list, is merged in.

**What is the difference between dnsPolicy None and ClusterFirst with a dnsConfig?**

Under ClusterFirst the dnsConfig is additive: its searches are merged into the cluster's own search list, so short names still resolve. Under None the dnsConfig replaces everything. In the lab the same three wrong suffixes would have been harmless under ClusterFirst and were fatal under None.

**What does ndots:5 mean in a pod's resolv.conf?**

A name with fewer than five dots is tried with each search domain appended before it is tried as written. Kubernetes sets ndots to 5 so that redis, redis.shop and redis.shop.svc all work; the cost is extra lookups for external names. A trailing dot skips the search list.

**How do I find my cluster's DNS domain?**

Read the kubernetes plugin line in the CoreDNS Corefile: kubectl -n kube-system get configmap coredns -o yaml. In the lab it read kubernetes cluster.local in-addr.arpa ip6.arpa. Any search domain a pod uses for Services has to end in that domain.

**How do I read /etc/resolv.conf in a pod that keeps crashing?**

Often you cannot; the container lives seconds. Read the pod spec instead: kubectl get pod <pod> -o jsonpath for .spec.dnsPolicy and .spec.dnsConfig is the same information, and for dnsPolicy None the file is that dnsConfig written out as nameserver, search and options lines.

**Why do the old pods keep working?**

They were created from the previous ReplicaSet's template, which has dnsPolicy ClusterFirst and no dnsConfig. A rolling update keeps them until the new pods are Ready, which never happens, so the rollout is stuck and traffic still flows to the old pods.

**Did Aurora find the cause in the lab?**

Yes, in both runs, in 140 and 205 seconds, for $0.40 and $0.56. Both named dnsPolicy None with cluster.internal search domains against a cluster.local Corefile. Neither read /etc/resolv.conf: one was blocked from exec, the other did not try. One inferred the old pods' health from their status without reading a log, and neither stated the fix in its report body.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-pod-dnsconfig-search-domain-not-resolving-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
