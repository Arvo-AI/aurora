---
id: provider_ovh
name: OVH RCA Investigation
category: rca_provider
connection_check:
  method: provider_in_preference
index: "OVH investigation commands"
rca_priority: 5
metadata:
  author: aurora
  version: "1.0"
---

## OVH Investigation

- List projects: `cloud_exec('ovh', 'cloud project list --json')`
- List instances: `cloud_exec('ovh', 'cloud instance list --cloud-project PROJECT_ID --json')`
- Check instance details: `cloud_exec('ovh', 'cloud instance get INSTANCE_ID --cloud-project PROJECT_ID --json')`
- List Kubernetes clusters: `cloud_exec('ovh', 'cloud kube list --cloud-project PROJECT_ID --json')`
- Get kubeconfig: `cloud_exec('ovh', 'cloud kube kubeconfig --cloud-project PROJECT_ID --kube-id CLUSTER_ID --output /tmp/kubeconfig.yaml')`
- If the above fails, use the API: `terminal_exec('curl -s -H "X-Ovh-Application:$OVH_APPLICATION_KEY" "https://api.ovh.com/v1/cloud/project/PROJECT_ID/kube/CLUSTER_ID/kubeconfig" | jq -r .content > /tmp/kubeconfig.yaml')`
- Then use kubectl: `terminal_exec('kubectl --kubeconfig=/tmp/kubeconfig.yaml get pods -A')`
- Check cluster nodes: `terminal_exec('kubectl --kubeconfig=/tmp/kubeconfig.yaml get nodes')`
- Check pod logs: `terminal_exec('kubectl --kubeconfig=/tmp/kubeconfig.yaml logs POD_NAME -n NAMESPACE')`
- **ON ANY OVH ERROR**: Use Context7 MCP to look up correct syntax:
  * For CLI errors: `mcp_context7_get_library_docs(context7CompatibleLibraryID='/ovh/ovhcloud-cli', topic='COMMAND')`
  * For Terraform errors: `mcp_context7_get_library_docs(context7CompatibleLibraryID='/ovh/terraform-provider-ovh', topic='RESOURCE')`
