# Aurora kubectl Agent (Helm)

Deploys a read-only kubectl agent that connects your Kubernetes cluster to Aurora's Kubernetes Connector feature in the UI.

## Prerequisites

- Kubernetes 1.19+, Helm 3
- Egress to Aurora backend (e.g., `https://your-aurora-instance.com`)
- An agent token from Aurora
- Container image pushed to an accessible registry

## Configuration

Before installing, you need to configure:

1. **Aurora backend URLs** - Set `aurora.backendUrl` and `aurora.wsEndpoint`
2. **Agent token** - Set `aurora.agentToken` (get from Aurora UI)
3. **Container image** - Set `agent.image.repository` to your registry

### Example values.yaml

```yaml
aurora:
  backendUrl: "https://your-aurora-instance.com"
  wsEndpoint: "wss://your-aurora-instance.com/kubectl-agent"
  agentToken: "your-generated-token"

agent:
  image:
    repository: your-registry/aurora-kubectl-agent
    tag: "1.0.3"
```

## Install

```bash
# Install with custom values file
helm install aurora-kubectl-agent ./kubectl-agent/chart \
  --namespace default --create-namespace \
  -f values.yaml

# Or set values via command line
helm install aurora-kubectl-agent ./kubectl-agent/chart \
  --namespace default --create-namespace \
  --set aurora.agentToken="<YOUR_TOKEN>" \
  --set aurora.backendUrl="https://your-aurora-instance.com" \
  --set aurora.wsEndpoint="wss://your-aurora-instance.com/kubectl-agent" \
  --set agent.image.repository="your-registry/aurora-kubectl-agent"
```

## Verify
- Pods: `kubectl get pods -n default -l app=aurora-kubectl-agent` (replace 'default' with your namespace)
- Logs: `kubectl logs -n default -l app=aurora-kubectl-agent --tail=50` (replace 'default' with your namespace)

## Key Configuration Values

### Required
- `aurora.agentToken` - Authentication token (get from Aurora UI → Connectors → Kubernetes)
- `aurora.backendUrl` - Aurora backend URL (e.g., `https://your-aurora-instance.com`)
- `aurora.wsEndpoint` - WebSocket endpoint (e.g., `wss://your-aurora-instance.com/kubectl-agent`)
- `agent.image.repository` - Container registry path

### Optional
- `aurora.clusterId` - Custom cluster identifier (auto-generated if empty)
- `agent.replicaCount` - Number of replicas (default: 1)
- `rbac.create` - Create RBAC resources (default: true)
- `rbac.rules` - Custom RBAC permissions

See `values.yaml` for complete configuration options.

## Upgrade / Remove
- Upgrade: `helm upgrade aurora-kubectl-agent ./kubectl-agent/chart -n default --reuse-values` (replace 'default' with your namespace)
- Uninstall: `helm uninstall aurora-kubectl-agent -n default` (replace 'default' with your namespace)

Support: info@arvoai.ca

