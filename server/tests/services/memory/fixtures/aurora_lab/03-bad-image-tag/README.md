# Kubernetes ImagePullBackOff: what it means and how to fix it

*ImagePullBackOff: the kubelet could not pull the image and retries up to 5 minutes apart. The exact event for 10 causes, captured on Kubernetes 1.37.*

Runbook for lab scenario `03-bad-image-tag`. Canonical version, with its byline, kept current: https://www.aurorasre.ai/runbooks/fix-imagepullbackoff-kubernetes. Written from the lab runs in this repository; updated 2026-10-29.

## Reproduce it

```
./lab.sh up
./lab.sh break 03-bad-image-tag
```
Then investigate with any tool you like; `scenario.yaml` holds the ground truth and the grading criteria.

| Applies to | Pods and Deployments on any Kubernetes cluster whose runtime is containerd; the registry error text inside each event comes from containerd |
|---|---|
| Verified on | Kubernetes server v1.37.0, kubectl client v1.35.3, kind v0.33.0, containerd 2.3.4, arm64 nodes, registry:2 for the private registry, 2026-10-29 |
| Lab folder | [scenarios/03-bad-image-tag](https://github.com/Arvo-AI/aurora-lab/tree/main/scenarios/03-bad-image-tag), Aurora runs in [runs/v0-run2/03-bad-image-tag](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v0-run2/03-bad-image-tag), the ten causes in [runs/search-2026-10-29/imagepullbackoff-causes](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/search-2026-10-29/imagepullbackoff-causes) |

**ImagePullBackOff means the kubelet tried to pull a container image, failed, and is now waiting longer between attempts, up to five minutes; the container has never started, so there are no logs and the restart count stays at 0.** The cause is in the pod's events: run `kubectl describe pod <pod>` and read the `Failed to pull image` line, which carries the registry's own answer. `not found` means the tag (or the platform) does not exist, `pull access denied` means missing credentials or a repository that does not exist, `401 Unauthorized` means the credentials were rejected, and `no such host` or `http: server gave HTTP response to HTTPS client` means the node cannot reach the registry the way it tried. Fix the image reference, the pull secret or the registry access; until the cause is fixed, waiting and deleting the pod change nothing.

## At a glance

We reproduced the rows below on one kind cluster on 29 October 2026, with two exceptions: the Docker Hub rate limit comes from Docker's docs, and the full disk was not planned (our lab VM ran out of space during the run, see the last section before Fix). The first column is the end of the `Failed to pull image` event, as kubelet printed it. Ordered by how often we expect each in practice, most common first.

| What the event ends with | Cause | Confirm with | Fix |
|---|---|---|---|
| `code = NotFound` ... `<image>: not found` | Tag does not exist (typo, never pushed) | `docker manifest inspect <image>` from a laptop | Correct the tag, or `kubectl rollout undo` |
| `pull access denied, repository does not exist or may require authorization: server message: insufficient_scope: authorization failed` | Docker Hub: repository name wrong, or private and no credentials | Check the repository path in the registry UI | Fix the name, or add an `imagePullSecret` |
| `pull access denied, repository does not exist or may require authorization: authorization failed: no basic auth credentials` | Private registry, the pod sent no credentials | `kubectl get pod <pod> -o jsonpath='{.spec.imagePullSecrets}'` | Create the secret in the pod's namespace and reference it |
| `401 Unauthorized` | Credentials sent and rejected (wrong or expired password, token) | Log in with the same credentials from a laptop | Recreate the secret with a valid credential |
| Extra event `FailedToRetrieveImagePullSecret`: `Unable to retrieve some image pull secrets (<name>)` | The referenced secret is not in the pod's namespace | `kubectl get secret <name> -n <pod namespace>` | Create the secret in that namespace |
| `dial tcp: lookup <host> ... no such host` | Registry hostname wrong or not resolvable from the node | Resolve the name from a node | Fix the hostname or node DNS |
| `http: server gave HTTP response to HTTPS client` | Registry serves plain HTTP; containerd tries HTTPS by default | `curl -I https://<registry>/v2/` | Put TLS on the registry |
| `code = NotFound` ... `no match for platform in manifest: not found` | Image index has no manifest for the node's architecture | `docker buildx imagetools inspect <image>` lists the platforms | Build for the node's platform, or pin the pod to nodes of the right architecture |
| Contains `You have reached your pull rate limit` (Docker's documented 429 body; we did not hit it) | Docker Hub anonymous or Personal pull limit | Rate-limit headers, see below | Authenticate pulls, or use a mirror |
| `pull access denied` ... `no basic auth credentials` although `crictl images` shows the image on the node | Kubernetes 1.35+ re-checks credentials for private images the kubelet pulled with a secret | Does the failing pod reference the secret? | Give every pod that uses the image the `imagePullSecret` |
| `Error: failed to record image pull intent` ... `no space left on device` (seen by accident, see below) | The node's disk is full | `kubectl describe node <node>`, disk usage on the node | Free disk on the node |

Three statuses look like an image-pull failure but are not `ImagePullBackOff`: `InvalidImageName` (the reference cannot be parsed), `ErrImageNeverPull` (`imagePullPolicy: Never` and the image is not on the node), and `Init:ImagePullBackOff`, which is the same failure on an init container. They are covered two sections down.

## What does ImagePullBackOff mean?

The [container images](https://kubernetes.io/docs/concepts/containers/images/#imagepullbackoff) documentation defines it: "The status `ImagePullBackOff` means that a container could not start because Kubernetes could not pull a container image (for reasons such as invalid image name, or pulling from a private registry without `imagePullSecret`). The `BackOff` part indicates that Kubernetes will keep trying to pull the image, with an increasing back-off delay. Kubernetes raises the delay between each attempt until it reaches a compiled-in limit, which is 300 seconds (5 minutes)."

The docs give the cap but not the steps, so we measured them. These are the start times of every pull containerd received for one pod whose repository does not exist, read from the node's containerd log:

```shell
docker exec rpull-worker2 journalctl -u containerd --no-pager | grep 'level=info msg="PullImage \\"docker.io/rpullnoorg'
```

The table is the eight matching log lines, with the waits computed from their timestamps (the raw lines are in the lab folder). containerd was restarted once, between attempts 2 and 3, when we changed its registry config; the kubelet, which owns the backoff, was not.

| Attempt | Time (UTC) | Wait since the previous attempt |
|---|---|---|
| 1 | 15:53:57 | |
| 2 | 15:54:13 | 15.7 s |
| 3 | 15:54:38 | 25.0 s |
| 4 | 15:55:28 | 50.0 s |
| 5 | 15:57:01 | 93.0 s |
| 6 | 15:59:43 | 161.4 s |
| 7 | 16:04:50 | 307.6 s |
| 8 | 16:10:01 | 311.0 s |

The wait roughly doubles and hits the five-minute cap on the seventh attempt, about 11 minutes after the pod was created. From then on a fix to the image is picked up on the next attempt, which can be up to five minutes away; the pod does not notice a fix sooner on its own. If you have fixed the cause (pushed the missing tag, created the secret) and do not want to wait, delete the pod so its controller makes a fresh one.

Do not count attempts from the events. At 16:11:55, after 8 pulls, `describe pod` showed `Pulling 14m (x5 over 17m)`: the event stream had stopped recording pulls while `BackOff` stood at `x65`. The node's containerd log is the reliable count.

## ErrImagePull vs ImagePullBackOff, and the look-alikes

They are the same failure at two moments. Every failed attempt emits `Error: ErrImagePull` and the status column shows `ErrImagePull` for a moment; between attempts the kubelet emits `Back-off pulling image` with `Error: ImagePullBackOff`, and that is the status you see most of the time. The real reason is never in either word; it is in the `Failed to pull image` line between them.

The pods in our test namespace, 19 seconds after creation:

```shell
kubectl -n pulltest get pods -o wide
```

```
NAME           READY   STATUS                  RESTARTS   AGE   IP            NODE            NOMINATED NODE   READINESS GATES
bad-host       0/1     ImagePullBackOff        0          19s   10.244.3.7    rpull-worker2   <none>           <none>
init-bad-tag   0/1     Init:ImagePullBackOff   0          19s   10.244.3.9    rpull-worker2   <none>           <none>
invalid-name   0/1     InvalidImageName        0          19s   10.244.3.10   rpull-worker2   <none>           <none>
never-pull     0/1     ErrImageNeverPull       0          19s   10.244.3.8    rpull-worker2   <none>           <none>
private-http   0/1     ImagePullBackOff        0          19s   10.244.3.6    rpull-worker2   <none>           <none>
repo-missing   0/1     ImagePullBackOff        0          19s   10.244.3.5    rpull-worker2   <none>           <none>
```

| Status | Meaning | Event that proves it |
|---|---|---|
| `ErrImagePull` | One pull attempt just failed | `Failed to pull image "<image>": <registry answer>` |
| `ImagePullBackOff` | Between attempts, waiting | `Back-off pulling image "<image>"` |
| `Init:ImagePullBackOff` | The same, on an init container; the app container waits with `PodInitializing` | `spec.initContainers{wait}: Back-off pulling image "busybox:1.37-typo"` |
| `InvalidImageName` | The reference does not parse; nothing is sent to a registry | `Failed to apply default image tag "Nginx:1.27-alpine": couldn't parse image name "Nginx:1.27-alpine": invalid reference format: repository name (library/Nginx) must be lowercase` |
| `ErrImageNeverPull` | `imagePullPolicy: Never` and the image is not on that node | `Container image "busybox:1.36.1" is not present with pull policy of Never` |

## CrashLoopBackOff vs ImagePullBackOff

Both are backoffs with the same five-minute cap by default, and that is where the likeness ends.

| | ImagePullBackOff | CrashLoopBackOff |
|---|---|---|
| Has the container run? | Never | Yes, and it exited |
| `RESTARTS` | 0 | Climbing |
| Where the cause is | Pod events (`Failed to pull image`) | `kubectl logs <pod> --previous` and `Last State` |
| Backoff | Increasing, capped at 300 s (measured above) | "10s, 20s, 40s, …", capped at 300 s, reset after 10 minutes of running ([pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#restart-policy)); since 1.35 the cap can be lowered per node with `crashLoopBackOff.maxContainerRestartPeriod` in the kubelet config |
| Fixed by | The image reference, credentials or registry access | The app, its config, its probes or its limits |

If the restart count is above 0, read [how to fix CrashLoopBackOff](https://www.aurorasre.ai/blog/fix-crashloopbackoff-kubernetes) instead. If the status is `CreateContainerConfigError`, the image pulled fine and a Secret or ConfigMap the container needs is missing: see [CreateContainerConfigError](https://www.aurorasre.ai/runbooks/fix-createcontainerconfigerror-kubernetes).

## Confirm: is it a stuck rollout or an outage?

The most common shape is a Deployment update with a bad image. Our lab's scenario 03 sets the frontend to `nginx:1.27-alpne` (a typo for `1.27-alpine`). Captured on 29 October:

```shell
kubectl get deploy frontend -n shop -o wide
```

```
NAME       READY   UP-TO-DATE   AVAILABLE   AGE     CONTAINERS   IMAGES             SELECTOR
frontend   2/2     1            2           4m36s   nginx        nginx:1.27-alpne   app=frontend
```

```shell
kubectl get pods -n shop -l app=frontend -o wide
```

```
NAME                        READY   STATUS             RESTARTS   AGE     IP           NODE            NOMINATED NODE   READINESS GATES
frontend-76d98597b9-fzgzx   1/1     Running            0          4m24s   10.244.1.2   rpull-worker    <none>           <none>
frontend-76d98597b9-vvb9h   1/1     Running            0          4m24s   10.244.3.3   rpull-worker2   <none>           <none>
frontend-7b6c998456-mc594   0/1     ImagePullBackOff   0          4m2s    10.244.1.7   rpull-worker    <none>           <none>
```

Two old pods serve; one new pod has never started (`RESTARTS 0`). `kubectl describe deploy frontend -n shop` says the same in one line:

```
Replicas:               2 desired | 1 updated | 3 total | 2 available | 1 unavailable
```

`2 available` of `2 desired` is a blocked deploy, not an outage: the RollingUpdate strategy keeps the old ReplicaSet until a new pod is Ready. If `available` is below `desired` (a new Deployment, a scale-up, or a node replaced under pods whose image no longer exists), users are affected and the rollback in Fix comes first.

## Isolate: read the Failed line

```shell
kubectl describe pod frontend-7b6c998456-mc594 -n shop
```

```
Events:
Type     Reason     Age                   From               Message
----     ------     ----                  ----               -------
Normal   Scheduled  4m2s                  default-scheduler  Successfully assigned shop/frontend-7b6c998456-mc594 to rpull-worker
[one Failed line removed here: a no space left on device error from our lab VM, not part of the scenario; shown in its own section below]
Normal   Pulling    38s (x4 over 4m2s)    kubelet            spec.containers{nginx}: Pulling image "nginx:1.27-alpne"
Warning  Failed     34s (x4 over 4m1s)    kubelet            spec.containers{nginx}: Failed to pull image "nginx:1.27-alpne": rpc error: code = NotFound desc = failed to pull and unpack image "docker.io/library/nginx:1.27-alpne": failed to resolve reference "docker.io/library/nginx:1.27-alpne": docker.io/library/nginx:1.27-alpne: not found
Warning  Failed     34s (x4 over 4m1s)    kubelet            spec.containers{nginx}: Error: ErrImagePull
Normal   BackOff    7s (x6 over 4m1s)     kubelet            spec.containers{nginx}: Back-off pulling image "nginx:1.27-alpne"
Warning  Failed     7s (x6 over 4m1s)     kubelet            spec.containers{nginx}: Error: ImagePullBackOff
```

The `not found` line is the typo, and it is the scenario's only fault; in the graded 2 October runs it was the only pull error in this pod's events. Our 29 October capture of the same pod also had a disk error, which came from our machine, not the scenario, so we cut it from this block and show it separately below. The lesson from it still applies: read every `Failed` line, not only the newest one.

Work through the `Failed to pull image` message left to right:

1. **The resolved reference.** `docker.io/library/nginx:1.27-alpne` is what the node asked for. The spec said `nginx:1.27-alpne`; the runtime added the registry and `library/`. The images docs: "If you don't specify a registry hostname, Kubernetes assumes that you mean the Docker public registry." A reference that resolves to the wrong registry is a cause by itself.
2. **The rpc code.** `code = NotFound` means the registry answered and the thing does not exist. The auth and network failures below carry no `NotFound`.
3. **The last clause.** It is the registry's or the runtime's own words, and it picks the row in the table.

Then find what the working pods run. Run on the same cluster:

```shell
kubectl get rs -n shop -l app=frontend -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.template.spec.containers[0].image}{" replicas="}{.spec.replicas}{" ready="}{.status.readyReplicas}{"\n"}{end}'
```

```
frontend-76d98597b9 nginx:1.27-alpine replicas=2 ready=2
frontend-7b6c998456 nginx:1.27-alpne replicas=1 ready=
```

The rest of this section is the other causes, each from a bare pod in a `pulltest` namespace on the same cluster. The private registry is a `registry:2` container with basic auth and a throwaway user, on the kind network.

### "pull access denied, repository does not exist or may require authorization"

Docker Hub, a repository that does not exist:

```
Warning  Failed   3s (x2 over 19s)  kubelet  spec.containers{app}: Failed to pull image "docker.io/rpullnoorg7f3k/app:1.0": failed to pull and unpack image "docker.io/rpullnoorg7f3k/app:1.0": failed to resolve reference "docker.io/rpullnoorg7f3k/app:1.0": pull access denied, repository does not exist or may require authorization: server message: insufficient_scope: authorization failed
```

Our private registry, an image that exists, pod with no `imagePullSecrets`:

```
Warning  Failed   5s (x2 over 18s)  kubelet  spec.containers{app}: Failed to pull image "rpull-registry:5000/private/busybox:1.37": failed to pull and unpack image "rpull-registry:5000/private/busybox:1.37": failed to resolve reference "rpull-registry:5000/private/busybox:1.37": pull access denied, repository does not exist or may require authorization: authorization failed: no basic auth credentials
```

The first half of the message is the same whether the repository is missing or private: the registry does not tell an anonymous client which. Settle it from outside the cluster: if the repository exists when you are logged in, it is credentials; if it does not exist for anyone, it is the name. Note there is no `code = NotFound` here, even for a repository that does not exist.

### "401 Unauthorized"

A secret with the wrong password:

```
Warning  Failed   5s (x2 over 18s)  kubelet  spec.containers{app}: Failed to pull image "rpull-registry:5000/private/busybox:1.37": failed to pull and unpack image "rpull-registry:5000/private/busybox:1.37": failed to resolve reference "rpull-registry:5000/private/busybox:1.37": unexpected status from HEAD request to http://rpull-registry:5000/v2/private/busybox/manifests/1.37: 401 Unauthorized
```

The secret was found and used; the registry rejected what was in it.

### FailedToRetrieveImagePullSecret: the secret is in another namespace

The pod references `othercred`, which exists only in `default`:

```
Warning  Failed                           4s (x2 over 18s)  kubelet  spec.containers{app}: Failed to pull image "rpull-registry:5000/private/busybox:1.37": failed to pull and unpack image "rpull-registry:5000/private/busybox:1.37": failed to resolve reference "rpull-registry:5000/private/busybox:1.37": pull access denied, repository does not exist or may require authorization: authorization failed: no basic auth credentials
Warning  Failed                           4s (x2 over 18s)  kubelet  spec.containers{app}: Error: ErrImagePull
Warning  FailedToRetrieveImagePullSecret  4s (x2 over 18s)  kubelet  Unable to retrieve some image pull secrets (othercred); attempting to pull the image may not succeed.
```

The pull message is identical to "no secret at all"; only the extra event tells you the reference is there and the object is not. The docs: "All `imagePullSecrets` must be Secrets that exist in the same Namespace as the Pod."

### The image is already on the node and the pull still fails

On Kubernetes 1.35 and later this one surprises people who remember that a cached image used to run anywhere on that node. After a pod with valid credentials had pulled `rpull-registry:5000/private/busybox:1.37` onto `rpull-worker2` (`crictl images` listed it), a new pod on the same node, `imagePullPolicy: IfNotPresent`, no secret:

```
Normal   Pulling  10s (x2 over 23s)  kubelet  spec.containers{app}: Pulling image "rpull-registry:5000/private/busybox:1.37"
Warning  Failed   10s (x2 over 23s)  kubelet  spec.containers{app}: Failed to pull image "rpull-registry:5000/private/busybox:1.37": failed to pull and unpack image "rpull-registry:5000/private/busybox:1.37": failed to resolve reference "rpull-registry:5000/private/busybox:1.37": pull access denied, repository does not exist or may require authorization: authorization failed: no basic auth credentials
```

That is the `KubeletEnsureSecretPulledImages` feature, "Beta since Kubernetes v1.35; enabled by default" per the [images docs](https://kubernetes.io/docs/concepts/containers/images/#ensure-image-pull-credential-verification): "Kubernetes will validate image credentials for every image that requires credentials to be pulled, even if that image is already present on the node." A public image is unaffected; ours logged `Container image "nginx:1.27-alpine" already present on machine and can be accessed by the pod`. Two limits, both from the same docs page: under the default kubelet setting, `imagePullCredentialsVerificationPolicy: NeverVerifyPreloadedImages`, images that got onto the node outside the kubelet (preloaded into the node image, for example) are not checked; and when the feature is first turned on, by an upgrade to 1.35 for instance, every image already on the node counts as preloaded. So the check bites on private images the kubelet pulls after the upgrade, which is how a private image ends up working for one pod and not for another on the same node. The fix is to give every pod that uses it the secret. The feature gate was alpha and off in 1.33 and 1.34 ([feature gates](https://kubernetes.io/docs/reference/command-line-tools-reference/feature-gates/), [KEP-2535](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/2535-ensure-secret-pulled-images)).

### "no such host"

```
Warning  Failed   4s (x2 over 19s)  kubelet  spec.containers{app}: Failed to pull image "registry.rpull-nonexistent.example/app:1.0": failed to pull and unpack image "registry.rpull-nonexistent.example/app:1.0": failed to resolve reference "registry.rpull-nonexistent.example/app:1.0": failed to do request: Head "https://registry.rpull-nonexistent.example/v2/app/manifests/1.0": dial tcp: lookup registry.rpull-nonexistent.example on 0.250.250.254:53: no such host
```

The lookup happens on the node, with the node's resolver (here `0.250.250.254`, the resolver our OrbStack-hosted kind node uses), not the cluster DNS. A registry name that resolves inside pods can still fail here.

### "http: server gave HTTP response to HTTPS client"

Our registry before we configured the nodes for it:

```
Warning  Failed   3s (x2 over 20s)  kubelet  spec.containers{app}: Failed to pull image "rpull-registry:5000/private/busybox:1.37": failed to pull and unpack image "rpull-registry:5000/private/busybox:1.37": failed to resolve reference "rpull-registry:5000/private/busybox:1.37": failed to do request: Head "https://rpull-registry:5000/v2/private/busybox/manifests/1.37": http: server gave HTTP response to HTTPS client
```

containerd tried HTTPS. For the rest of the tests we added a `hosts.toml` on both worker nodes that tells containerd to use HTTP for `rpull-registry:5000` only. That is fine for a throwaway lab registry and not a fix for a shared one: there the fix is a certificate on the registry.

### "no match for platform in manifest: not found"

Our nodes are arm64. An image index that lists only `linux/amd64`:

```
Warning  Failed   3s (x2 over 18s)  kubelet  spec.containers{app}: Failed to pull image "rpull-registry:5000/private/busybox-amd64index:1.37": rpc error: code = NotFound desc = failed to pull and unpack image "rpull-registry:5000/private/busybox-amd64index:1.37": no match for platform in manifest: not found
```

This is the trap in the table: `code = NotFound` and `not found`, the same words as a mistyped tag, and the tag is correct. Check the platforms before you hunt for a typo:

```shell
docker buildx imagetools inspect 127.0.0.1:5055/private/busybox-amd64index:1.37
```

```
Name:      127.0.0.1:5055/private/busybox-amd64index:1.37
MediaType: application/vnd.oci.image.index.v1+json
Manifests:
MediaType: application/vnd.oci.image.manifest.v1+json
Platform:  linux/amd64
```

(run from the Mac against the same registry's host port before the pods existed, and copied from our terminal rather than the capture log; trimmed to the media type and platform lines, the digest and per-manifest name lines removed). The same amd64 image pushed as a single manifest, not an index, pulled and started on the arm64 node, and `uname -m` inside it printed `x86_64`: OrbStack gives the VM amd64 emulation, so the binary ran. On a node without emulation that image would pull and then fail when the container starts, which is a crash, not a pull error. We could not show that failure on this machine.

### Docker Hub pull rate limit

We did not hit the limit, so there is no captured kubelet event. Docker's [pull usage and limits](https://docs.docker.com/docker-hub/usage/pulls/) page says that over the limit, "Docker Hub returns a 429 response code with the following body when the manifest is requested: You have reached your pull rate limit. You may increase the limit by authenticating and upgrading: https://www.docker.com/increase-rate-limits". It lists 100 pulls per 6 hours for unauthenticated users, "per IPv4 address or IPv6 /64 subnet", and 200 for authenticated Personal accounts. Because the anonymous limit is per address, every node that leaves through one NAT address draws on the same 100.

Check where you stand without spending a pull (Docker's docs say a `HEAD` request does not count):

```shell
TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:ratelimitpreview/test:pull" | jq -r .token)
curl -s --head -H "Authorization: Bearer $TOKEN" https://registry-1.docker.io/v2/ratelimitpreview/test/manifests/latest
```

```
HTTP/2 200
ratelimit-limit: 100;w=3600
ratelimit-remaining: 50;w=3600
```

(trimmed to the status line and the two rate-limit headers, the `docker-ratelimit-source` header, which is our IP, removed; we used Python instead of `jq` to read the token). The limit, 100, matches the docs. The window does not: the header says `w=3600` (one hour), while the docs page says 6 hours and shows `w=21600` in its own example. We saw `w=3600` again on a second request at 16:22 UTC the same day. We did not test which window Docker Hub enforces, so plan for the docs' 6 hours and read the header for your own address.

### "failed to record image pull intent ... no space left on device"

This is what a full node disk looked like in our run. We did not set it up: several kind clusters shared one Docker VM on the Mac, and the VM's disk filled while the scenario 03 pod above was retrying. It is the line we cut from that pod's events, same capture:

```
Warning  Failed     94s (x10 over 3m24s)  kubelet            spec.containers{nginx}: Error: failed to record image pull intent: failed to create temporary file "/var/lib/kubelet/image_manager/pulling/sha256-75357163e8e36fbb5af87c3934a9a17da12dc25ecdcdc418f914f78cd23e0986.tmp": open /var/lib/kubelet/image_manager/pulling/sha256-75357163e8e36fbb5af87c3934a9a17da12dc25ecdcdc418f914f78cd23e0986.tmp: no space left on device
```

The file it could not create is the kubelet's own pull record under `/var/lib/kubelet/image_manager/pulling` (KEP-2535, the 1.35+ credential check above, keeps "pull intent" records), so the attempt failed on the node, not at a registry. The same pod's last `not found` event came after its last disk error, so the disk error hid the real cause for a while but did not replace it. Look at the node, not the image: `kubectl describe node <node>` for a `DiskPressure` condition, and the disk usage of the kubelet and containerd directories on the node.

## Fix

Pick by cause. A change to the pod template (image, `imagePullSecrets`) makes new pods at once. A fix outside the spec (a pushed tag, a created secret, the node) is picked up on the next pull attempt, up to five minutes later; delete the pod to retry now.

**Tag or repository name wrong.** If you know the correct tag, check it exists, then set it:

```shell
docker manifest inspect nginx:1.27-alpine > /dev/null && echo exists
docker manifest inspect nginx:1.27-alpne > /dev/null; echo "exit=$?"
```

```
exists
no such manifest: docker.io/library/nginx:1.27-alpne
exit=1
```

The second exits 1, so the same line works as a CI check. Then:

```shell
kubectl -n shop set image deploy/frontend nginx=nginx:1.27-alpine
kubectl -n shop rollout status deploy/frontend --timeout=120s
```

```
deployment.apps/frontend image updated
Waiting for deployment spec update to be observed...
Waiting for deployment spec update to be observed...
Waiting for deployment "frontend" rollout to finish: 1 old replicas are pending termination...
deployment "frontend" successfully rolled out
```

Setting the image back to exactly what the old ReplicaSet runs does not create a new ReplicaSet. The Deployment adopts the old one as revision 3 and scales the broken one to 0, so the serving pods are never replaced:

```shell
kubectl -n shop get rs -l app=frontend -o jsonpath='{range .items[*]}{.metadata.name}{" rev="}{.metadata.annotations.deployment\.kubernetes\.io/revision}{" "}{.spec.template.spec.containers[0].image}{" replicas="}{.spec.replicas}{"\n"}{end}'
```

```
frontend-76d98597b9 rev=3 nginx:1.27-alpine replicas=2
frontend-7b6c998456 rev=2 nginx:1.27-alpne replicas=0
```

Risk: low; if the new tag is also wrong you get the same stuck rollout one revision later and the old pods keep serving. Rollback: `kubectl -n shop rollout undo deploy/frontend`. If you do not know the intended tag, or users are affected, run `kubectl rollout undo` first; it drops every other change bundled in the bad revision (env, resources), so note them.

**No or wrong credentials.** Create the secret in the pod's namespace and reference it:

```shell
kubectl -n <ns> create secret docker-registry regcred --docker-server=<registry> --docker-username=<user> --docker-password=<password>
```

Then add `imagePullSecrets: [{name: regcred}]` to the pod template, or attach the secret to the namespace's ServiceAccount ([docs](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/#add-imagepullsecrets-to-a-service-account)). Our `private-ok` pod with a valid secret logged `Successfully pulled image "rpull-registry:5000/private/busybox:1.37" in 106ms (211ms including waiting)`. Risk: low; a pod template change rolls the Deployment. Rollback: remove the reference and delete the secret. The password lands in your shell history with this form, so run it from a machine where that is acceptable.

**Registry unreachable.** Fix the hostname or node DNS for `no such host`; put TLS on the registry for the HTTP-to-HTTPS error. Do not switch a shared registry to plain HTTP on the nodes to make the error go away.

**Wrong platform.** Build and push an index that includes the node's platform (`docker buildx build --platform linux/amd64,linux/arm64 --push ...`), or schedule the workload only on nodes that match with a `nodeSelector` on `kubernetes.io/arch`. Risk of the selector: pods go `Pending` if no node of that architecture has room.

**Rate limit.** Authenticate pulls from Docker Hub with an `imagePullSecret` or a node-level credential, or pull through a mirror. Waiting for the window to reset is a mitigation, not a fix.

**Disk full.** Free space on the node, or drain and replace it. Deleting pods does not help while the node cannot write.

Things that do not work: deleting the pod before fixing the cause (the controller recreates it with the same spec), `kubectl rollout restart` (a new ReplicaSet with the same bad image), and raising `maxUnavailable` to force the rollout through.

## Verify

Pass condition for a Deployment: `rollout status` prints `successfully rolled out`, `kubectl get deploy` shows `READY`, `UP-TO-DATE` and `AVAILABLE` equal to desired, the ReplicaSet with the bad reference is at 0, and no pod in the namespace has `ErrImagePull` or `ImagePullBackOff` as its waiting reason. After the fix above:

```
NAME       READY   UP-TO-DATE   AVAILABLE   AGE
frontend   2/2     2            2           10m
```

For a new pod, the proof is a `Pulled` event followed by `Created` and `Started`. Watch for five minutes, one full backoff cycle, so a pod still waiting out an old backoff cannot fool you.

## Prevent

**Alert.** With kube-state-metrics (not tested in our lab, where the alert was text sent to the agent):

```
kube_pod_container_status_waiting_reason{reason=~"ImagePullBackOff|ErrImagePull"} > 0
kube_deployment_status_replicas_updated < kube_deployment_spec_replicas
```

The second fires on a stuck rollout even when every pod still serving is healthy. Give the first a `for:` of a few minutes so one failed attempt that recovers on the next try does not page.

**Guardrail in CI.** Check that every image reference exists, and exists for every platform you run, before `kubectl apply`. `docker manifest inspect <image>` exits non-zero for a missing tag; `docker buildx imagetools inspect <image>` lists the platforms. Pinning by digest (`image@sha256:...`) removes tag typos and tag moves entirely.

**Registry hygiene.** Authenticate every pull from Docker Hub and never rely on an image being cached on a node for a private registry on 1.35+. The images docs also advise against `:latest` "as it is harder to track which version of the image is running and more difficult to roll back properly."

## Common mistakes

- **Reading `not found` as "typo" every time.** `no match for platform in manifest: not found` uses the same words. Check platforms before you stare at the tag.
- **Reading `pull access denied` as "credentials" every time.** On Docker Hub a repository that does not exist returns the same message as a private one. Check the name first; it is cheaper.
- **Adding a secret to the wrong namespace.** The pull error does not change; only `FailedToRetrieveImagePullSecret` tells you.
- **Waiting it out.** The backoff caps at five minutes and repeats forever. Nothing changes until the cause changes.
- **Counting attempts from events.** Event counts stopped at `x5` while containerd made 8 pulls.
- **Calling a stuck rollout an outage.** Compare `available` with `desired` first.

## What we saw in the lab

**Scenario 03 with Aurora.** We injected `kubectl -n shop set image deploy/frontend nginx=nginx:1.27-alpne` into a 3-node kind cluster running a small app (`shop`: nginx frontend, a Python api, a worker, redis, a load generator). Aurora got only the alert text, "KubeDeploymentRolloutStuck: deployment frontend in namespace shop has not progressed.", read-only kubectl under its default `observability_only` policy, model `bedrock/us.anthropic.claude-sonnet-5`, Aurora commit `87d22baf`. Two runs on 2 October 2026, each checked by a person:

| Run | Verdict | Time | Tool calls | Cost |
|---|---|---|---|---|
| r1 | correct | 101 s | 13 (1 blocked) | $0.24 |
| r2 | correct | 77 s | 11 (1 blocked) | $0.19 |

Both followed roughly the path above: the image column, the pods, the `not found` event (r1 from `describe pod`, r2 from `get events`), and the ReplicaSets for the good tag. Both said the old pods were still serving. Neither blamed registry credentials or the network, the two wrong answers the scenario lists; neither report mentions them at all, so they were not ruled out either. Both tried `kubectl rollout history` and got `Command blocked by organization policy: Mutating kubectl operations`: the default policy denies the whole `rollout` verb, read-only subcommands included ([FINDINGS.md](https://github.com/Arvo-AI/aurora-lab/blob/main/FINDINGS.md)). r1 also made errors that did not touch the cause: its report spoke of "reduced redundancy" with 2 of 2 desired pods available, and its working notes said the rollout was stuck "because Kubernetes cannot schedule a healthy replacement pod" although the pod was scheduled (its final report gets this right). r1 put no fix in the report itself; the `set image` fix was in its suggested next steps, and r2 named "image tag revert or rollout rollback" without a command. The judge flagged r2's 16:57:25 creation time as doubtful; the Deployment's `CreationTimestamp` in the run's evidence is 16:57:25, so we found no error in r2.

On the same fault, HolmesGPT 0.43.0 was graded correct in both runs and K8sGPT 0.4.40 partial in both, by the judge model with two AI review passes agreeing and no person checking them (runs of 15 and 16 October 2026, [benchmark](https://www.aurorasre.ai/benchmark)). K8sGPT named the typo but also reported the Service's missing endpoint as a readiness problem and never said the old pods were serving.

**The other causes, 29 October 2026.** Only scenario 03 is a lab scenario with an Aurora run. Eight other causes on this page were reproduced on bare pods for this rewrite; the full disk was an accident of our own machine, and the rate limit was not reproduced. Aurora has not been run on any of them. Three things surprised us. `no match for platform` reads like a typo. A private image the kubelet pulled with a secret no longer runs for a pod without credentials on 1.37. And our own VM's disk filled up mid-test, which is how we have a real event for a full node disk.

## How Aurora handles this

Aurora receives the alert, lists the Deployment and its pods, reads the waiting pod's events and compares the ReplicaSets, then writes the root cause with each claim tied to a tool output, as in the two runs above. It runs read-only by default: kubectl goes through an allowlist, and fixes are written as suggested next steps for a person to apply. During an investigation it saves notes to the org's memory automatically; they are marked agent-written and can be edited or deleted. Paste an event into [/diagnose](https://www.aurorasre.ai/diagnose) to match it to a runbook; the method behind the investigations is in the [root cause analysis guide](https://www.aurorasre.ai/blog/root-cause-analysis-complete-guide-sres).

Aurora is open source at [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora); hosted at [aurora-ai.net](https://aurora-ai.net/); [book a demo](https://cal.com/arvo-ai/demo).

## Sources and verification

- Scenario 03 output and every `pulltest` event above: captured 29 October 2026 on kind cluster `rpull` (kind v0.33.0, Kubernetes v1.37.0, containerd 2.3.4, kubectl v1.35.3, arm64 nodes), trimmed to the relevant lines and otherwise unchanged; the `imagetools inspect` output was copied from our terminal. The disk error is from the same scenario 03 capture and is not part of the scenario or of the 2 October runs. Transcripts, manifests and the exact setup commands: [runs/search-2026-10-29/imagepullbackoff-causes](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/search-2026-10-29/imagepullbackoff-causes).
- Aurora runs: [runs/v0-run2/03-bad-image-tag](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/v0-run2/03-bad-image-tag) (`steps-full.json`, `grade.json` with `human_verdict`). HolmesGPT and K8sGPT: [runs/h2h-v1](https://github.com/Arvo-AI/aurora-lab/tree/main/runs/h2h-v1) and [benchmark/results.json](https://github.com/Arvo-AI/aurora-lab/blob/main/benchmark/results.json).
- Kubernetes [container images](https://kubernetes.io/docs/concepts/containers/images/) (ImagePullBackOff definition and 300-second cap, default registry, `imagePullSecrets` namespace rule, image indexes, credential verification on 1.35+, `:latest` advice), [pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#restart-policy) (CrashLoopBackOff schedule, configurable cap), [feature gates](https://kubernetes.io/docs/reference/command-line-tools-reference/feature-gates/) and [KEP-2535](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/2535-ensure-secret-pulled-images) (`KubeletEnsureSecretPulledImages`: alpha 1.33, beta and on by default 1.35) and [service accounts](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/#add-imagepullsecrets-to-a-service-account): read 29 October 2026.
- Docker [pull usage and limits](https://docs.docker.com/docker-hub/usage/pulls/): read 29 October 2026; the rate-limit headers are our own request that day.

Related: [how to fix CrashLoopBackOff](https://www.aurorasre.ai/blog/fix-crashloopbackoff-kubernetes), [CreateContainerConfigError](https://www.aurorasre.ai/runbooks/fix-createcontainerconfigerror-kubernetes), [a pod stuck on its init container](https://www.aurorasre.ai/runbooks/fix-pod-stuck-init-container-kubernetes), [a rollout stuck on a ResourceQuota](https://www.aurorasre.ai/runbooks/fix-resourcequota-exceeded-rollout-stuck-kubernetes), [Pod Pending with FailedScheduling](https://www.aurorasre.ai/runbooks/fix-pod-pending-failedscheduling-kubernetes), and the [benchmark](https://www.aurorasre.ai/benchmark) with every graded run.

## Questions people ask

**What does ImagePullBackOff mean in Kubernetes?**

The kubelet could not pull the container image and is waiting longer between attempts, capped at 300 seconds. The container has never run, so there are no logs and RESTARTS is 0. The reason is in the Failed to pull image event from kubectl describe pod.

**What is the difference between ErrImagePull and ImagePullBackOff?**

They are the same failure at two moments. ErrImagePull is the status right after an attempt fails; ImagePullBackOff is the status while the kubelet waits before the next attempt. Neither word is the cause: read the Failed to pull image event between them.

**What is the difference between CrashLoopBackOff and ImagePullBackOff?**

ImagePullBackOff means the image never arrived, so the container never ran and the restart count is 0. CrashLoopBackOff means the container ran and exited repeatedly; the restart count climbs and the cause is in kubectl logs --previous. Both back off to a five-minute cap.

**How long does Kubernetes wait between image pull retries?**

The docs give a compiled-in cap of 300 seconds. In our measurement on Kubernetes 1.37 the waits were 16, 25, 50, 93 and 161 seconds, then about 308 and 311: the cap was reached on the seventh attempt, roughly 11 minutes after the pod was created.

**Why does my image pull say not found when the tag is correct?**

Check the platform. An image index with no manifest for the node's architecture fails with no match for platform in manifest: not found, under the same NotFound code as a missing tag. docker buildx imagetools inspect lists the platforms the image has.

**Why does a private image fail on a node where it is already cached?**

Since Kubernetes 1.35 the kubelet verifies pull credentials for images it pulled with credentials, even when the image is on the node (KubeletEnsureSecretPulledImages, beta and on by default; alpha and off in 1.33 and 1.34). Images preloaded outside the kubelet are not checked by default. A pod without the secret gets no basic auth credentials. Give every pod that uses the image the imagePullSecret.

**How do I fix ImagePullBackOff?**

Read the Failed to pull image event, then fix that cause: correct the tag or roll back for not found, add or correct the imagePullSecret in the pod's namespace for pull access denied or 401, fix DNS or TLS for registry errors. Delete the pod afterwards if you do not want to wait for the next retry.


---
This file mirrors https://www.aurorasre.ai/runbooks/fix-imagepullbackoff-kubernetes; edits land there first. Benchmark of three agents on this and 18 other faults: https://www.aurorasre.ai/benchmark
