---
id: tool_mapping
name: RCA Tool Name Mapping
category: rca_provider
connection_check:
  method: always
index: "Tool name mapping for RCA investigation"
rca_priority: 20
metadata:
  author: aurora
  version: "1.0"
---

## Tool Name Reference

The correct tool names for RCA investigation calls are:

- **`cloud_exec`** -- Execute cloud provider CLI commands (GCP, AWS, Azure, OVH, Scaleway)
- **`terminal_exec`** -- Execute terminal/shell commands (kubectl with kubeconfig, SSH, etc.)
- **`on_prem_kubectl`** -- Execute kubectl commands against on-premise clusters (requires cluster_id)

Always use these exact tool names when making calls. Legacy references to `cloud_tool` and `terminal_tool` map to `cloud_exec` and `terminal_exec` respectively.
