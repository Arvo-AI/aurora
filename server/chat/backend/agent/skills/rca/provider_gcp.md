---
id: provider_gcp
name: GCP RCA Investigation
category: rca_provider
connection_check:
  method: provider_in_preference
index: "GCP/GKE investigation commands"
rca_priority: 5
metadata:
  author: aurora
  version: "1.0"
---

## GCP/GKE Investigation

- Check cluster status: `cloud_exec('gcp', 'container clusters list')`
- **IMPORTANT**: Get cluster credentials first: `cloud_exec('gcp', 'container clusters get-credentials CLUSTER_NAME --region=REGION')`
- Get pod details: `cloud_exec('gcp', 'kubectl get pods -n NAMESPACE -o wide')`
- Describe problematic pods: `cloud_exec('gcp', 'kubectl describe pod POD_NAME -n NAMESPACE')`
- Check pod logs: `cloud_exec('gcp', 'kubectl logs POD_NAME -n NAMESPACE --since=1h')`
- Check pod metrics: `cloud_exec('gcp', 'kubectl top pod POD_NAME -n NAMESPACE')`
- Check events: `cloud_exec('gcp', 'kubectl get events -n NAMESPACE --sort-by=.lastTimestamp')`
- Check node health: `cloud_exec('gcp', 'kubectl describe node NODE_NAME')`
- Query Stackdriver logs: `cloud_exec('gcp', 'logging read "resource.type=k8s_container" --limit=50 --freshness=1h')`
- Check deployments: `cloud_exec('gcp', 'kubectl get deployments -n NAMESPACE')`
- Check services: `cloud_exec('gcp', 'kubectl get svc -n NAMESPACE')`
- Check HPA: `cloud_exec('gcp', 'kubectl get hpa -n NAMESPACE')`
- Check PVC status: `cloud_exec('gcp', 'kubectl get pvc -n NAMESPACE')`

## Multi-service-account guidance

When an alert tag includes a project ID (`project_id:foo` in tags, `gcp_project` in labels, or `projects/foo/...` in resource names), pass `project_id='foo'` to `cloud_exec('gcp', ...)`. To enumerate across all connected projects, omit `project_id` — Aurora will fan out across every active service account.
