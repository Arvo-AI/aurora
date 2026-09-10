---
id: provider_azure
name: Azure RCA Investigation
category: rca_provider
connection_check:
  method: provider_in_preference
index: "Azure/AKS investigation commands"
rca_priority: 5
metadata:
  author: aurora
  version: "1.0"
---

## Azure/AKS Investigation

Multiple subscriptions may be connected. Your first `cloud_exec('azure', ...)` call
fans out across all of them and returns `results_by_subscription`; once you know
which subscription owns the failing resource, pass `account_id='SUBSCRIPTION_ID'`
on every later call.

- Check cluster status: `cloud_exec('azure', 'aks show --name CLUSTER_NAME --resource-group RG_NAME', account_id='SUBSCRIPTION_ID')`
- **IMPORTANT**: Get cluster credentials first, always with an explicit subscription: `cloud_exec('azure', 'aks get-credentials --name CLUSTER_NAME --resource-group RG_NAME', account_id='SUBSCRIPTION_ID')`
- If the cluster is private (`aks show` reports `enablePrivateCluster: true`), its API server is unreachable from Aurora. Use the `on_prem_kubectl` tool via the Kubernetes connector instead of `cloud_exec` for all kubectl steps.
- Get pod details: `cloud_exec('azure', 'kubectl get pods -n NAMESPACE -o wide')`
- Describe problematic pods: `cloud_exec('azure', 'kubectl describe pod POD_NAME -n NAMESPACE')`
- Check pod logs: `cloud_exec('azure', 'kubectl logs POD_NAME -n NAMESPACE --since=1h')`
- Check pod metrics: `cloud_exec('azure', 'kubectl top pod POD_NAME -n NAMESPACE')`
- Check events: `cloud_exec('azure', 'kubectl get events -n NAMESPACE --sort-by=.lastTimestamp')`
- Check node health: `cloud_exec('azure', 'kubectl describe node NODE_NAME')`
- Query Azure Monitor: `cloud_exec('azure', 'monitor log-analytics query -w WORKSPACE_ID --analytics-query "QUERY"', account_id='SUBSCRIPTION_ID')`
- Check VMs: `cloud_exec('azure', 'vm list --output table')`
- Check resource groups: `cloud_exec('azure', 'group list')`
- Check NSGs: `cloud_exec('azure', 'network nsg list')`
