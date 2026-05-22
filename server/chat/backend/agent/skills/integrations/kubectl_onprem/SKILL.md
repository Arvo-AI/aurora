---
name: kubectl_onprem
id: kubectl_onprem
description: "On-prem Kubernetes cluster integration for running kubectl commands on connected clusters via Aurora agent relay"
category: infrastructure
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.kubectl_onprem_tool
  function: is_kubectl_onprem_connected
tools:
  - get_connected_clusters
  - on_prem_kubectl
index: "Infrastructure -- run kubectl on connected on-prem Kubernetes clusters"
rca_priority: 8
allowed-tools: get_connected_clusters, on_prem_kubectl
metadata:
  author: aurora
  version: "1.0"
---

# On-Prem Kubernetes (kubectl) Integration

## Overview
On-prem Kubernetes cluster integration for running kubectl commands on connected clusters. Commands are relayed through the Aurora agent installed on the cluster.

**IMPORTANT:** Always call `get_connected_clusters` first to discover available clusters and their `cluster_id` before running any kubectl commands.

**Note:** For cloud-managed clusters (GCP GKE, AWS EKS, Azure AKS), use `terminal_exec` with kubectl commands instead.

## Instructions

### Tool: get_connected_clusters

Discover available on-prem clusters. Call this before using `on_prem_kubectl`.

**Usage:**
`get_connected_clusters()`

Returns cluster names, IDs, connection status, and metadata.

### Tool: on_prem_kubectl

Run kubectl commands on connected on-prem Kubernetes clusters.

**Usage:**
`on_prem_kubectl(cluster_id='CLUSTER_ID', command='get pods -n default')`

Specify the cluster using the `cluster_id` returned by `get_connected_clusters`.

### RCA Investigation Flow

1. Discover available clusters:
   `get_connected_clusters()`
2. List pods and check status:
   `on_prem_kubectl(cluster_id='ID', command='get pods -n NAMESPACE')`
3. Check pod logs for errors:
   `on_prem_kubectl(cluster_id='ID', command='logs PODNAME -n NAMESPACE --tail=100')`
4. Describe failing pods for events:
   `on_prem_kubectl(cluster_id='ID', command='describe pod PODNAME -n NAMESPACE')`
5. Check recent events in the namespace:
   `on_prem_kubectl(cluster_id='ID', command='get events -n NAMESPACE --sort-by=.lastTimestamp')`
6. Check node status:
   `on_prem_kubectl(cluster_id='ID', command='get nodes -o wide')`
7. Check resource usage:
   `on_prem_kubectl(cluster_id='ID', command='top pods -n NAMESPACE')`

### Important Rules
- Always specify `cluster_id` to target the correct cluster.
- For cloud-managed clusters (GCP GKE, AWS EKS, Azure AKS), use `terminal_exec` with kubectl commands.
- This tool is for on-prem clusters connected via the Aurora kubectl agent only.
- Check pod status and events before diving into logs.
