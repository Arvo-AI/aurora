---
sidebar_position: 2
---

# Aurora Watch

Aurora Watch lets Aurora open the incident itself. Every minute it reads your
connected Kubernetes clusters with the same read-only access its investigations
already use, learns what normal looks like, and opens an incident when something
regresses that no alert rule caught. Each incident names the cluster change that
most likely caused it and starts a normal Aurora investigation.

Nothing is configured per alert: there are no thresholds to write. Aurora never
writes to the cluster.

## What it notices

After learning a cluster for five minutes, Aurora reports a **regression from that
cluster's own baseline**, not a fixed condition. A workload that was already failing
when Watch started is part of the baseline while it is no worse than it was then:
fewer ready replicas or more failing pods than at start is a regression. Workloads
and Services created later get the same five-minute learning window of their own.

| Signal | Example | Severity |
|---|---|---|
| A Service loses all its ready endpoints | a selector edited to match no pod | critical |
| A workload loses ready replicas it had | 0 of 2 replicas ready | critical / high |
| Pods cannot start | `ImagePullBackOff`, `CreateContainerConfigError`, `CrashLoopBackOff`, unschedulable for 2 min, stuck in an init container for 2 min | high |
| Pods cannot be created | `FailedCreate` (e.g. exceeded quota), `FailedMount`, `ProvisioningFailed` | high |
| A rollout stops progressing | no pod updated or ready for the Deployment's `progressDeadlineSeconds` (10 min by default) | high |
| Containers restart | 2+ restarts in 10 min (1 restart is a notice) | high / medium |
| Requests start failing in a workload's logs | new 5xx lines, client timeouts, proxy `connect() failed`, or failing requests and no successful ones where there used to be | high |
| Error lines never seen before for that workload, or a known one surging | `ERROR metrics push failed` | low / medium |
| A Warning event that was not firing before | `Unhealthy`, `BackOff` | medium |

Normal operations are not regressions: a rolling update that progresses and takes no
more pods down than its `maxUnavailable` allows, a paused, `OnDelete` or partitioned
rollout (a pod it loses and cannot replace is still a regression), a scale-up waiting
on new pods, a scale to zero, a pod Pending while the cluster autoscaler adds a node,
a single bad poll.

A finding must be seen in two polls. **High** and **critical** findings open an
incident; low and medium ones stay notices on the Kubernetes page and are reported
again if they become incident-worthy. A finding that gets worse while its incident is
open updates that incident (severity, findings); if a newer change made it worse, it
opens a new incident that blames that change.

## Which change caused it

Aurora diffs every poll against the previous one and keeps an hour of changes:
pod template edits (image, env, resources, probes, init containers, volumes, DNS),
Service selectors and ports, ConfigMap keys, NetworkPolicies, ResourceQuotas, PVCs,
replaced pods. Each finding ranks them by how the changed object relates to the
failing one (the same object, a ConfigMap it mounts, the Service in front of it, a
dependency named in its environment, a NetworkPolicy selecting its pods) and how
close in time the change was. A rollout restart or a replaced pod changes nothing by
itself, so a ConfigMap edit that it put into effect ranks above it. A change made
while polling had stopped (the cluster unreachable, Aurora's workers busy) is still a
suspect; the incident says it was made during that gap instead of guessing when. The
incident's investigation starts by confirming or refuting the top suspect.

Findings that blame the same change, or two closely related objects, become one
incident: a Service with no endpoints and the clients timing out on it are one
incident, and so is a workload failing because a Service it depends on is down. Two
unrelated changes stay two incidents, even in the same minute.

## Turn it on

1. Connect a cluster: **Connectors → Kubernetes** (in-cluster agent or kubeconfig).
2. On **Connectors → Kubernetes → Manage**, switch on **Aurora Watch**. Optionally
   list the namespaces to watch; by default it watches every namespace except
   `kube-system`, `kube-public` and `kube-node-lease`.

Turning it on needs the `connectors:write` permission (admins and editors). Aurora
Watch acts as the user who turned it on: incidents are opened in their name and
their RCA budget applies. Changing the namespaces later keeps that user; turning
it off and on again makes it act as you. Operators can make it unavailable on an
installation with `NEXT_PUBLIC_ENABLE_AURORA_WATCH=false`.

## What it reads, and what it keeps

* Four `kubectl get` commands per minute (one per namespace when you list
  namespaces) for pods, workloads, ReplicaSets, Services, Endpoints, events,
  ConfigMaps, NetworkPolicies, ResourceQuotas, LimitRanges, PVCs, HPAs and PDBs, and
  `kubectl logs --since --limit-bytes` for up to 25 workloads per minute. Every
  command passes Aurora's read-only check and your organization's command policy
  (Settings → Security); a denied command or an unreadable kind becomes a blind spot
  shown on the Kubernetes page. A poll reads at most 32 MB, and 8 MB in one
  response (a big namespace is read a few kinds at a time). If pods, workloads,
  Services or Endpoints cannot be read, or the limits are exceeded, that poll is
  skipped and its error shown; the cluster is then retried after 1, 2, 4 and up to
  15 minutes, or on the next minute once the settings change. A cluster that has not
  been polled for three minutes (Aurora's workers busy with investigations) is shown
  as **Not polling**.
* **Secrets are never read.** ConfigMap values are reduced to a hash per key, so
  Aurora can say "key `default.conf` changed" without storing its content. Env
  values whose name looks like a credential, URL passwords, the value after a
  credential flag (`--requirepass`), credential query parameters and probe header
  values are stored as a keyed digest (`<redacted #1a2b3c4d>`): a rotated password
  still shows as a change, and neither value is kept. Other env values, container
  arguments, probe commands and paths, and the messages the cluster writes (events,
  waiting reasons, conditions) go through Aurora's secret redaction.
* Tokens and credential values in log lines are masked before Aurora learns them,
  quoted lines are redacted, and evidence goes through the prompt-injection rail
  before it reaches the investigation.
* State per cluster (the learned baseline, open findings, the last hour of
  changes) is stored in the `aurora_watch_state` table, scoped to your
  organization.
