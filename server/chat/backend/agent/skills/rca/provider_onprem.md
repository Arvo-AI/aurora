---
id: provider_onprem
name: On-Premise Kubernetes RCA Investigation
category: rca_provider
connection_check:
  method: provider_in_preference
index: "On-Premise Kubernetes investigation commands"
rca_priority: 5
metadata:
  author: aurora
  version: "1.0"
---

## On-Premise Kubernetes Investigation

- Available clusters are listed in the "ON-PREM KUBERNETES CLUSTERS" section above
- Get pod details: `on_prem_kubectl('get pods -n NAMESPACE -o wide', 'CLUSTER_ID')`
- Describe pods: `on_prem_kubectl('describe pod POD_NAME -n NAMESPACE', 'CLUSTER_ID')`
- Check pod logs: `on_prem_kubectl('logs POD_NAME -n NAMESPACE --since=1h --tail=200', 'CLUSTER_ID')`
- Check events: `on_prem_kubectl('get events -n NAMESPACE --sort-by=.lastTimestamp', 'CLUSTER_ID')`
- Check node health: `on_prem_kubectl('describe node NODE_NAME', 'CLUSTER_ID')`
- Check deployments: `on_prem_kubectl('get deployments -n NAMESPACE', 'CLUSTER_ID')`
- Check all pods: `on_prem_kubectl('get pods -A', 'CLUSTER_ID')`
- **CRITICAL**: Use `on_prem_kubectl` tool with `cluster_id` from list above, NOT `terminal_exec` or `cloud_exec`
