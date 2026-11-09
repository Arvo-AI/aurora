# Kubernetes CreateContainerConfigError: meaning and how to fix it

*CreateContainerConfigError means a Secret or ConfigMap the pod references is missing. Read the event, create the object or roll back, verify. Lab-tested.*

Runbook for lab scenario `04-missing-secret`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-createcontainerconfigerror-kubernetes. Written from the lab runs in this repository; updated 2026-10-15.

## Reproduce it

```
./lab.sh up
./lab.sh break 04-missing-secret
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Pods whose containers take an env var or volume from a Secret or ConfigMap; here a Deployment rollout in Kubernetes |
|---|---|
| Verified on | Kubernetes server v1.37.0, kind v0.33.0, containerd 2.3.4, 2 October 2026 |
| Lab folder | [scenarios/04-missing-secret](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/04-missing-secret), runs in [runs/v0-run2/04-missing-secret](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v0-run2/04-missing-secret) |

**CreateContainerConfigError means the kubelet could not build the container's configuration, almost always because a Secret or ConfigMap the pod references does not exist.** The image is on the node, the pod is scheduled, and nothing has run yet, so there are no logs. Confirm it with `kubectl describe pod <pod>`: the last event reads `Error: secret "<name>" not found` (or `configmap "<name>" not found`). The fix is to create the missing object with the key the pod expects, or roll the change back. Nothing else helps.

## At a glance

| Signal | Likely cause | Confirm with | Fix |
|---|---|---|---|
| `CreateContainerConfigError`, event `Error: secret "<name>" not found` | The Secret named in `secretKeyRef` or `envFrom` does not exist in this namespace (a missing `secret` volume shows `ContainerCreating` with a `FailedMount` event instead) | `kubectl get secret <name> -n <namespace>` | Create the Secret with the expected key, or roll back |
| `CreateContainerConfigError`, event `Error: configmap "<name>" not found` | Same for a ConfigMap (`configMapKeyRef` or `envFrom`) | `kubectl get configmap <name> -n <namespace>` | Create the ConfigMap, or roll back |
| `CreateContainerConfigError`, event `couldn't find key <key> in Secret <ns>/<name>` | The object exists, the key does not | `kubectl get secret <name> -n <namespace> -o jsonpath='{.data}'` | Add the key, or fix the key name in the spec |
| `ImagePullBackOff` instead | A different failure: the image, not the config | See [ImagePullBackOff](https://www.aurorasre.ai/runbooks/fix-imagepullbackoff-kubernetes) | |
| `CrashLoopBackOff` instead | The container started and exited | `kubectl logs <pod> --previous` | See [OOMKilled](https://www.aurorasre.ai/runbooks/fix-oomkilled-kubernetes) for one common cause |

## How do you confirm it is CreateContainerConfigError?

List the pods. The failing pod shows the reason in the STATUS column and 0 restarts, because nothing has started (from the lab run r1, step 5):

```shell
kubectl get pods -n shop -o wide
```

```
NAME                        READY   STATUS                       RESTARTS   AGE   IP             NODE                 NOMINATED NODE   READINESS GATES
api-5f7c48ddc6-nc8f7        0/1     CreateContainerConfigError   0          71s   10.244.1.107   aurora-lab-worker    <none>           <none>
api-f64486c4c-c9rqz         1/1     Running                      0          81s   10.244.2.106   aurora-lab-worker2   <none>           <none>
api-f64486c4c-dr5x9         1/1     Running                      0          81s   10.244.1.103   aurora-lab-worker    <none>           <none>
```

Then describe the pod. The container state and the events name the missing object (run r1, step 6, trimmed):

```shell
kubectl describe pod api-5f7c48ddc6-nc8f7 -n shop
```

```
    State:          Waiting
      Reason:       CreateContainerConfigError
    Ready:          False
    Restart Count:  0
    Environment:
      REDIS_HOST:   redis
      DB_PASSWORD:  <set to the key 'db-password' in secret 'api-secrets'>  Optional: false
Events:
  Type     Reason     Age               From               Message
  ----     ------     ----              ----               -------
  Normal   Scheduled  82s               default-scheduler  Successfully assigned shop/api-5f7c48ddc6-nc8f7 to aurora-lab-worker
  Normal   Pulled     3s (x8 over 82s)  kubelet            spec.containers{api}: Container image "python:3.12-alpine" already present on machine and can be accessed by the pod
  Warning  Failed     3s (x8 over 82s)  kubelet            spec.containers{api}: Error: secret "api-secrets" not found
```

Three things in that output decide the diagnosis. `Pulled` says the image is fine. `Restart Count: 0` and an empty `Container ID` say the container never ran, so `kubectl logs` has nothing to show. `Optional: false` says the pod is not allowed to start without the value. The [Secrets documentation](https://kubernetes.io/docs/concepts/configuration/secret/), written about Secret volumes, describes the same retry that applies here: "a Secret needs to be created before any Pods that depend on it. If the Secret cannot be fetched (perhaps because it does not exist, or due to a temporary lack of connection to the API server) the kubelet periodically retries running that Pod. The kubelet also reports an Event for that Pod, including details of the problem fetching the Secret."

## How much is affected, and since when?

Check whether the failing pod is a new revision or the only copy. In the lab it was a rollout: the old ReplicaSet kept two Ready pods while the new one could not start (run r1, step 7):

```shell
kubectl get rs -n shop -o wide
```

```
NAME                  DESIRED   CURRENT   READY   AGE   CONTAINERS   IMAGES               SELECTOR
api-5f7c48ddc6        1         1         0       85s   api          python:3.12-alpine   app=api,pod-template-hash=5f7c48ddc6
api-f64486c4c         2         2         2       95s   api          python:3.12-alpine   app=api,pod-template-hash=f64486c4c
```

The Deployment confirms the shape: a stuck rollout, not an outage (run r1, step 15, trimmed):

```
Annotations:            deployment.kubernetes.io/revision: 2
Replicas:               2 desired | 1 updated | 3 total | 2 available | 1 unavailable
Conditions:
  Type           Status  Reason
  ----           ------  ------
  Available      True    MinimumReplicasAvailable
  Progressing    True    ReplicaSetUpdated
OldReplicaSets:  api-f64486c4c (2/2 replicas created)
NewReplicaSet:   api-5f7c48ddc6 (1/1 replicas created)
```

`Available: True` with an old ReplicaSet still at 2/2 means users are served and you have time. If the Deployment has no old ReplicaSet, or the pod is a bare pod or a Job, every replica is down and the fix is urgent. The ReplicaSet age tells you when the change landed: here the new ReplicaSet was 10 seconds younger than the old one.

## Which object is missing, and why?

Work from the pod spec outward, cheapest check first.

1. Read what the new pods want. Compare the running template to a running pod of the old revision (run r1, steps 13 and 10):

```shell
kubectl get deployment api -n shop -o jsonpath='{.spec.template.spec.containers[0].env}'
```

```
[{"name":"REDIS_HOST","value":"redis"},{"name":"DB_PASSWORD","valueFrom":{"secretKeyRef":{"key":"db-password","name":"api-secrets"}}}]
```

```shell
kubectl get pod api-f64486c4c-dr5x9 -n shop -o jsonpath='{.spec.containers[0].env}'
```

```
[{"name":"REDIS_HOST","value":"redis"}]
```

The diff is the change: one new env var, `DB_PASSWORD`, read from Secret `api-secrets`, key `db-password`.

2. Check the object exists in the pod's namespace (run r1, step 8). Secrets are namespaced; a Secret in another namespace does not count.

```shell
kubectl get secrets -n shop
```

```

```

Empty. No Secret of any name in the namespace.

3. Check it was not created somewhere else by mistake (run r1, step 12):

```shell
kubectl get secrets -A
```

```
NAMESPACE     NAME                     TYPE                            DATA   AGE
kube-system   bootstrap-token-abcdef   bootstrap.kubernetes.io/token   6      23h
```

Nowhere in the cluster. If the object does exist, the event would instead say `couldn't find key <key>`, and the check is the key list: `kubectl get secret <name> -n <namespace> -o jsonpath='{.data}'`.

4. If a secrets operator is supposed to create it (External Secrets, Sealed Secrets, Vault Agent), check that its pods exist and read its own status object. Aurora's run r2 checked for operator pods with `kubectl get pods -A` (step 13) and found none, so in the lab there was nothing to wait for.

5. Find out what changed. `kubectl rollout history deployment/api -n shop` shows the revisions. In the lab the `last-applied-configuration` annotation on the Deployment still held the old spec with only `REDIS_HOST`, while the live template had `DB_PASSWORD` (run r2, step 9): the change was made out of band, not through `kubectl apply`.

## How do you fix it?

Two acceptable fixes. Pick by whether the new revision should exist.

**Create the missing object with the key the pod expects.** The pod recovers on the kubelet's next retry, within about ten seconds in the lab; no restart is needed.

```shell
kubectl create secret generic api-secrets -n shop --from-literal=db-password='<value from your secrets manager>'
```

Risk: you are adding a credential the running code may not use. In the lab the api code read only `REDIS_HOST` (run r1, step 16), so the variable was introduced into the manifest without a code change, which is worth a question to whoever made it. Rollback: `kubectl delete secret api-secrets -n shop` after rolling the Deployment back, never while pods depend on it.

**Roll the Deployment back** when the new revision was not meant to ship yet:

```shell
kubectl rollout undo deployment/api -n shop
```

Risk: you also undo any other change in that revision. Rollback of the rollback: `kubectl rollout undo deployment/api -n shop --to-revision=<n>`.

Do not mark the reference `optional: true` as a fix. The pod would start without the value, and the application would fail later, further from the cause.

## How do you verify the fix?

Pass condition: no pod of the Deployment in `CreateContainerConfigError`, and the rollout complete.

```shell
kubectl -n shop get pods -l app=api -o jsonpath='{.items[*].status.containerStatuses[0].state.waiting.reason}'
kubectl rollout status deployment/api -n shop
```

The first command prints nothing when every container is past the waiting state. It is the negation of the lab's own check for the broken state, `scenarios/04-missing-secret/check.sh`, which greps that same field for `CreateContainerConfigError`. Watch for two minutes: a pod that leaves the waiting state and then restarts has a second problem.

## How do you prevent it?

**Alert on the waiting reason.** With kube-state-metrics (a suggested query, not one captured in the lab):

```
sum by (namespace, pod) (kube_pod_container_status_waiting_reason{reason="CreateContainerConfigError"}) > 0
```

**Validate references before the rollout.** In CI, render the manifests and check that every `secretKeyRef`, `configMapKeyRef`, `secret` volume and `configMap` volume names an object that exists in the target namespace, or is created in the same change. The lab's fault is a one-line patch that adds a reference without the object; a dry-run cannot catch it, only a reference check can.

**Create the Secret in the same change set as the pod that reads it**, and in the same namespace.

## Common mistakes

- **Reading logs.** `kubectl logs` returns nothing useful; the container never started. The evidence is in `describe pod` events.
- **Treating it as an image problem.** The `Pulled` event is Normal. `ImagePullBackOff` is a different reason with a different fix.
- **Creating the Secret in the wrong namespace.** `kubectl get secrets -A` shows where it landed.
- **Creating the Secret without the key.** The error changes from `not found` to `couldn't find key`. Check `.data`.
- **Deleting the stuck pod.** The ReplicaSet makes an identical one; the reference is in the template.
- **Setting `optional: true` to make the pod start.** The failure moves into the application, where it is harder to find.

## What we saw in the lab

Scenario `04-missing-secret`: `break.sh` patches the api Deployment to add `DB_PASSWORD` from `secretKeyRef` `api-secrets/db-password` and never creates the Secret. Aurora received only the alert text, `KubeDeploymentRolloutStuck: deployment api in namespace shop has not progressed; new pods not starting.`, and read-only cluster access. Two runs, each in a fresh account.

| Run | Verdict | Time | Tool calls | Cost | Fix in suggestions |
|---|---|---|---|---|---|
| r1 | correct (judge, then human) | 194 s | 18 | $0.48 | none |
| r2 | correct (judge, then human) | 133 s | 17 | $0.35 | create the Secret; roll back as the stopgap |

Both runs reached the same evidence: list the Deployment, list the pods, describe the stuck pod, read the ReplicaSets, `get secrets -n shop` (empty), then `get secrets -A` (nothing but the bootstrap token). Both tried `kubectl rollout history` and were refused by Aurora's default `observability_only` command policy, which denies the whole `rollout` verb including the read-only subcommands (one tool error per run). Both worked around it by comparing the live template with a running pod of the old revision, and r2 also read the `last-applied-configuration` annotation.

r1's report named the cause plainly: "The secret `api-secrets` was never created in the `shop` namespace, and no application secrets exist anywhere in the cluster", and added a point from reading the code in the `api-code` ConfigMap: the application never reads `DB_PASSWORD`, so the variable was added without a code dependency. r1 ruled out image pull, resource limits, node problems and probe failures with the evidence for each. The report says no fix was applied because mutating kubectl was policy-blocked; it never attempted one, so the sentence describes the policy, not an attempt.

r1 left its Suggested Next Steps list empty, which is why the fix column reads none. Its closing message did name both fixes, create the Secret with the `db-password` key or roll the Deployment back, and graded on everything Aurora showed, both were acceptable. r2's suggestions were to create `api-secrets` with the `db-password` key, with rollback as the stopgap, both acceptable.

The grader read Aurora's full output (report, notes, suggestions); a person at Arvo then checked both verdicts. Transcripts and grades: [runs/v0-run2/04-missing-secret](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v0-run2/04-missing-secret).

## How Aurora handles this

Aurora receives the alert, lists the Deployment and its pods, describes the pod in `CreateContainerConfigError`, confirms the referenced Secret is missing in the namespace and in the cluster, and diffs the new template against a running pod to name the change. Every claim in its report cites the tool output it came from, and what it could not check is listed as such. It runs with read-only kubectl under a command policy by default. See a full investigation on the [Aurora root cause analysis](https://www.aurorasre.ai/rca) page.

- **Start free:** [aurora-ai.net](https://aurora-ai.net/) (hosted, no infrastructure to run)
- **GitHub:** [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
- **Book a demo:** [cal.com/arvo-ai/demo](https://cal.com/arvo-ai/demo)

## Sources and verification

Every command output above is pasted from the lab runs of 2 October 2026 (Kubernetes server v1.37.0, kind v0.33.0, containerd 2.3.4, Aurora commit 87d22baf, model bedrock/us.anthropic.claude-sonnet-5) and attributed to its run and step. The Secret retry behaviour is quoted from the [Kubernetes Secrets documentation](https://kubernetes.io/docs/concepts/configuration/secret/), read 15 October 2026. The scenario, fault injection and check are in [scenarios/04-missing-secret](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/04-missing-secret). Related: [root cause analysis for SREs](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres), [Service has no endpoints](https://www.aurorasre.ai/runbooks/fix-service-no-endpoints-kubernetes), [a pod stuck in Init:0/1](https://www.aurorasre.ai/runbooks/fix-pod-stuck-init-container-kubernetes).

## Questions people ask

**What does CreateContainerConfigError mean in Kubernetes?**

The kubelet could not build the container's configuration before starting it, almost always because a Secret or ConfigMap the pod references does not exist, or exists without the key the pod names. The container never starts, so there are no logs.

**How do I find which Secret or ConfigMap is missing?**

Run kubectl describe pod <pod> and read the last Warning event. It names the object: Error: secret "api-secrets" not found, or configmap "<name>" not found, or couldn't find key <key> in Secret <namespace>/<name>.

**Why does kubectl logs show nothing for a CreateContainerConfigError pod?**

The container never ran. Restart Count is 0 and the Container ID is empty. The evidence is in the pod's events and in the Deployment's template, not in logs.

**Is CreateContainerConfigError the same as ImagePullBackOff?**

No. ImagePullBackOff means the image could not be pulled. In CreateContainerConfigError the Pulled event is Normal and the image is on the node; the failure is in the config the container needs.

**Does the pod recover on its own after I create the Secret?**

Yes. The kubelet periodically retries running the pod and reports an Event each time, per the Kubernetes Secrets documentation. In the lab the retry interval was about ten seconds. No restart is needed.

**Should I set optional: true on the reference to fix it?**

No. The pod would start without the value and the application would fail later, further from the cause. Create the object or roll back the change.

**Can a Secret in another namespace satisfy the reference?**

No. Secrets and ConfigMaps are namespaced. kubectl get secrets -A shows where a misplaced Secret landed; recreate it in the pod's namespace.

**How did Aurora handle the missing Secret in the lab?**

In both runs it found the stuck pod, confirmed the Secret was missing in the namespace and in the cluster, and diffed the new template against a running pod to name the change, in 194 and 133 seconds for $0.48 and $0.35. Both runs named the fix; one of them only in its closing message, not in the Suggested Next Steps list.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-createcontainerconfigerror-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
