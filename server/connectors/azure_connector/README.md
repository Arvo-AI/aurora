# Azure Connector

Service Principal authentication for Microsoft Azure, with multi-subscription support.

## Setup

Run `setup-aurora-access.sh` in [Azure Cloud Shell](https://shell.azure.com), which
already provides `az`, `jq` and an authenticated session. Aurora serves the script at
`GET /azure/setup-script`, and the Azure connect page offers it as a download.

```bash
bash setup-aurora-access.sh                       # all enabled subscriptions
bash setup-aurora-access.sh <management-group-id> # single grant at MG scope
```

The script creates two service principals and prints a JSON blob to paste into Aurora:

| Identity   | Roles | Used by |
| ---------- | ----- | ------- |
| `agent`    | Contributor, AKS RBAC Writer, Cost Management Reader | Agent mode |
| `readonly` | Log Analytics Reader, Cost Management Reader, AKS Cluster User, AKS RBAC Reader | Ask mode |

Notes on the role choices:

- **Contributor excludes `Microsoft.Authorization/*/write`**, so Aurora can manage
  resources but can never escalate its own permissions.
- **Built-in roles only, no custom role.** `kubectl logs` needs
  `Microsoft.ContainerService/managedClusters/pods/read`, which `AKS RBAC Reader`
  already grants with an empty `notDataActions`. Azure does not model Kubernetes
  subresources separately -- there is no `pods/log/read` action, and attempting to
  define one fails with `InvalidDataActionOrNotDataAction`. Verified against a live
  tenant.
- **`Log Analytics Reader`** subsumes the `*/read` of Reader and Monitoring Reader.
- **`AKS RBAC Reader` does not grant `secrets/read`.** If an investigation needs to
  read a Secret to explain a failing pod's mounted config, Ask mode cannot do it.
  This is deliberate: Secret contents are exactly what a read-only identity should
  not expose.

Because only built-in roles are used, the script needs `User Access Administrator`
or `Owner` to create role assignments, but never permission to create role
definitions. Re-running is safe: assignments are upserted.

## Multi-subscription behaviour

Every enabled subscription is stored as a `user_connections` row (provider `azure`).
The `subscription_id` on the token row remains the display default.

- The agent's first `cloud_exec('azure', ...)` call fans out across all subscriptions
  in parallel and returns `results_by_subscription`; subsequent calls should pass
  `account_id='<SUBSCRIPTION_ID>'` to target one.
- `kubectl`/`helm` and `az aks get-credentials` never fan out: they depend on shared
  on-disk state (kubeconfig) that parallel invocations would race.
- Each invocation gets its own `AZURE_CONFIG_DIR` because `az login` writes auth
  state to disk.

## Ask mode fails closed

Azure RBAC has no equivalent of an AWS session policy, so the separate read-only
service principal *is* the enforcement boundary. If Ask mode is requested and no
distinct read-only identity is configured, credential resolution raises rather than
silently falling back to the write-capable identity. Re-run the setup script to
provision one.

## Private AKS clusters

A private cluster's API server is unreachable from both Cloud Shell and Aurora, so
RBAC alone is not sufficient. The setup script detects these and prints their names;
connect each one with the Aurora Kubernetes agent (Integrations > Kubernetes), which
dials out from inside the cluster. The RCA skill instructs the agent to prefer
`on_prem_kubectl` for those clusters.

## Troubleshooting

**"No enabled subscription found"** — The service principal has no role assignment on
any subscription. Re-run the setup script.

**Role assignment reported as "already present or denied"** — Assigning roles requires
User Access Administrator or Owner. Ask an admin to run the script.

**Cannot create app registrations** — Tenant policy may restrict this to admins
(`Users can register applications` = No). Ask an admin to run the script.

**`kubectl logs` fails with a forbidden error** — The cluster is likely not
Entra-integrated with Azure RBAC enabled (`aadProfile.enableAzureRBAC`). Azure RBAC
for Kubernetes authorization only applies to such clusters; on a local-RBAC cluster
the AKS RBAC roles are accepted but inert, and access falls back to the cluster's
own Kubernetes RBAC.

**Resource discovery looks incomplete** — Resource Graph caps responses at 1000 records
and times out at 30s. Discovery batches subscriptions and follows skip tokens; check
worker logs for throttling.
