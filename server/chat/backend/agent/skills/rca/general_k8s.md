---
id: general_k8s
name: General Kubernetes RCA Investigation
category: rca_provider
connection_check:
  method: always
index: "General Kubernetes investigation commands for any provider"
rca_priority: 10
metadata:
  author: aurora
  version: "1.0"
---

## General Kubernetes Investigation (for any provider)

- Check all pods across namespaces: `kubectl get pods -A`
- Check resource usage: `kubectl top pods -n NAMESPACE`
- Check persistent volumes: `kubectl get pv,pvc -A`
- Check config maps: `kubectl get configmaps -n NAMESPACE`
- Check secrets (names only): `kubectl get secrets -n NAMESPACE` — denied in Ask mode on Azure AKS, where the read-only role excludes Secrets entirely. Treat a Forbidden here as expected, not as an incident finding, and infer mounted-config problems from the pod spec and events instead.
- Check ingress: `kubectl get ingress -A`
- Check network policies: `kubectl get networkpolicies -A`
