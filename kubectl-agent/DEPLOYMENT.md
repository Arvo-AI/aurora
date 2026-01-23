# kubectl Agent Deployment Guide

This guide covers building, configuring, and deploying the Aurora kubectl agent to customer clusters.

## Overview

The kubectl agent is a lightweight WebSocket client that runs in customer Kubernetes clusters, providing Aurora with secure, read-only kubectl access without requiring inbound network access or direct credential sharing.

## Building the Agent

### 1. Build the Docker Image

```bash
cd kubectl-agent/src/

# Build for your target architecture
docker build -t your-registry/aurora-kubectl-agent:1.0.3 .

# For multi-arch builds (amd64 + arm64)
docker buildx build --platform linux/amd64,linux/arm64 \
  -t your-registry/aurora-kubectl-agent:1.0.3 \
  --push .
```

### 2. Push to Container Registry

Choose your registry:

**Docker Hub:**
```bash
docker tag your-registry/aurora-kubectl-agent:1.0.3 your-dockerhub-user/aurora-kubectl-agent:1.0.3
docker push your-dockerhub-user/aurora-kubectl-agent:1.0.3
```

**GitHub Container Registry:**
```bash
docker tag your-registry/aurora-kubectl-agent:1.0.3 ghcr.io/your-org/aurora-kubectl-agent:1.0.3
docker push ghcr.io/your-org/aurora-kubectl-agent:1.0.3
```

**Google Container Registry:**
```bash
docker tag your-registry/aurora-kubectl-agent:1.0.3 gcr.io/your-project/aurora-kubectl-agent:1.0.3
docker push gcr.io/your-project/aurora-kubectl-agent:1.0.3
```

## Configuration

### Required Configuration

Create a `values.yaml` file with your deployment-specific settings:

```yaml
aurora:
  # Your Aurora backend URL
  backendUrl: "https://your-aurora-instance.com"
  
  # WebSocket endpoint for agent communication
  wsEndpoint: "wss://your-aurora-instance.com/kubectl-agent"
  
  # Agent authentication token (generate in Aurora UI)
  agentToken: "your-generated-token-here"

agent:
  image:
    # Your container registry path
    repository: your-registry/aurora-kubectl-agent
    tag: "1.0.3"
    pullPolicy: IfNotPresent
```

### Optional Configuration

**High Availability:**
```yaml
agent:
  replicaCount: 2  # Multiple replicas for HA
  
podDisruptionBudget:
  enabled: true
  minAvailable: 1
```

**Resource Limits:**
```yaml
agent:
  resources:
    requests:
      memory: "128Mi"
      cpu: "100m"
    limits:
      memory: "256Mi"
      cpu: "200m"
```

**Corporate Proxy:**
```yaml
agent:
  env:
    - name: HTTP_PROXY
      value: "http://proxy.corp.com:8080"
    - name: HTTPS_PROXY
      value: "http://proxy.corp.com:8080"
    - name: NO_PROXY
      value: "localhost,127.0.0.1,.cluster.local,.svc"
```

**Custom RBAC Permissions:**
```yaml
rbac:
  create: true
  rules:
    # Only allow specific resources
    - apiGroups: [""]
      resources: ["pods", "pods/log", "services"]
      verbs: ["get", "list", "watch"]
```

## Deployment

### 1. Install via Helm

```bash
helm install aurora-kubectl-agent ./kubectl-agent/chart \
  --namespace aurora --create-namespace \
  -f values.yaml
```

### 2. Verify Installation

```bash
# Check pod status
kubectl get pods -n aurora -l app=aurora-kubectl-agent

# Check logs
kubectl logs -n aurora -l app=aurora-kubectl-agent --tail=50 -f

# Check agent connection
kubectl exec -n aurora deployment/aurora-kubectl-agent -- \
  curl http://localhost:8080/ready
```

### 3. Upgrade

```bash
helm upgrade aurora-kubectl-agent ./kubectl-agent/chart \
  --namespace aurora \
  --reuse-values \
  -f values.yaml
```

### 4. Uninstall

```bash
helm uninstall aurora-kubectl-agent --namespace aurora
```

## Publishing to OCI Registry

To distribute the Helm chart via OCI registry:

### Package the Chart

```bash
cd kubectl-agent/
helm package chart/
```

### Push to Registry

**GitHub Container Registry:**
```bash
helm registry login ghcr.io -u your-username
helm push aurora-kubectl-agent-1.0.3.tgz oci://ghcr.io/your-org
```

**Google Artifact Registry:**
```bash
gcloud auth configure-docker us-central1-docker.pkg.dev
helm push aurora-kubectl-agent-1.0.3.tgz oci://us-central1-docker.pkg.dev/your-project/helm
```

### Install from OCI Registry

```bash
helm install aurora-kubectl-agent \
  oci://ghcr.io/your-org/aurora-kubectl-agent \
  --version 1.0.3 \
  --namespace aurora --create-namespace \
  --set aurora.agentToken="your-token" \
  --set aurora.backendUrl="https://your-aurora-instance.com" \
  --set aurora.wsEndpoint="wss://your-aurora-instance.com/kubectl-agent" \
  --set agent.image.repository="your-registry/aurora-kubectl-agent"
```

## Network Requirements

The agent requires **outbound** connectivity only:

- HTTPS (443) to Aurora backend
- WebSocket (wss://) to Aurora backend

**Firewall rules:** Allow egress to your Aurora instance domain on ports 80 and 443.

## Security Considerations

1. **RBAC Permissions**: By default, the agent has read-only access to most cluster resources. Review and customize `rbac.rules` in values.yaml.

2. **Token Security**: Store agent tokens securely. Tokens are stored in Kubernetes secrets.

3. **Network Policies**: Consider implementing network policies to restrict agent pod traffic.

4. **Pod Security**: Agent runs as non-root user (UID 1000) with dropped capabilities.

## Troubleshooting

### Agent not connecting

Check logs:
```bash
kubectl logs -n aurora -l app=aurora-kubectl-agent
```

Common issues:
- Invalid token
- Network connectivity to backend
- Firewall blocking outbound traffic

### Health checks failing

```bash
# Check liveness
kubectl exec -n aurora deployment/aurora-kubectl-agent -- \
  curl http://localhost:8080/health

# Check readiness (requires active connection)
kubectl exec -n aurora deployment/aurora-kubectl-agent -- \
  curl http://localhost:8080/ready
```

### View agent events

```bash
kubectl get events -n aurora --field-selector involvedObject.name=aurora-kubectl-agent
```

## Support

- GitHub: https://github.com/Arvo-AI
- Email: info@arvoai.ca
