---
id: provider_scaleway
name: Scaleway RCA Investigation
category: rca_provider
connection_check:
  method: provider_in_preference
index: "Scaleway investigation commands"
rca_priority: 5
metadata:
  author: aurora
  version: "1.0"
---

## Scaleway Investigation

- List instances: `cloud_exec('scaleway', 'instance server list')`
- Check instance details: `cloud_exec('scaleway', 'instance server get SERVER_ID')`
- List Kubernetes clusters: `cloud_exec('scaleway', 'k8s cluster list')`
- Get kubeconfig: `cloud_exec('scaleway', 'k8s kubeconfig get CLUSTER_ID')`
- Check cluster nodes: `cloud_exec('scaleway', 'k8s node list cluster-id=CLUSTER_ID')`
- List databases: `cloud_exec('scaleway', 'rdb instance list')`
- Check database logs: `cloud_exec('scaleway', 'rdb log list instance-id=INSTANCE_ID')`
- List load balancers: `cloud_exec('scaleway', 'lb list')`
- **ALWAYS use `cloud_exec('scaleway', ...)` NOT `terminal_exec` for Scaleway commands**
