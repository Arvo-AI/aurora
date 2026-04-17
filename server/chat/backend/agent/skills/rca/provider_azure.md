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

- Check cluster status: `cloud_exec('azure', 'aks show --name CLUSTER_NAME --resource-group RG_NAME')`
- **IMPORTANT**: Get cluster credentials first: `cloud_exec('azure', 'aks get-credentials --name CLUSTER_NAME --resource-group RG_NAME')`
- Get pod details: `cloud_exec('azure', 'kubectl get pods -n NAMESPACE -o wide')`
- Describe problematic pods: `cloud_exec('azure', 'kubectl describe pod POD_NAME -n NAMESPACE')`
- Check pod logs: `cloud_exec('azure', 'kubectl logs POD_NAME -n NAMESPACE --since=1h')`
- Check pod metrics: `cloud_exec('azure', 'kubectl top pod POD_NAME -n NAMESPACE')`
- Check events: `cloud_exec('azure', 'kubectl get events -n NAMESPACE --sort-by=.lastTimestamp')`
- Check node health: `cloud_exec('azure', 'kubectl describe node NODE_NAME')`
- Query Azure Monitor: `cloud_exec('azure', 'monitor log-analytics query -w WORKSPACE_ID --analytics-query "QUERY"')`
- Check VMs: `cloud_exec('azure', 'vm list --output table')`
- Check resource groups: `cloud_exec('azure', 'group list')`
- Check NSGs: `cloud_exec('azure', 'network nsg list')`
