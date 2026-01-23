# Aurora kubectl Agent

A lightweight agent that enables secure, read-only kubectl access from customer Kubernetes clusters to Aurora.

## What is this?

Aurora is an AI-powered cloud infrastructure management platform. This **kubectl agent** enables the **Kubernetes Connector** feature in the Aurora UI, allowing Aurora to securely access and monitor your Kubernetes clusters without requiring:

- Inbound network access to your cluster
- Sharing your kubeconfig or credentials
- Installing complex software

**How it works:**
1. You install this lightweight agent in your Kubernetes cluster
2. The agent connects outbound to your Aurora instance via WebSocket
3. Aurora's Kubernetes Connector can then execute read-only kubectl commands through the agent
4. All commands are authenticated and logged
5. Your cluster appears in the Aurora UI under the Kubernetes Connector section

**Use cases:**
- Connect Aurora to your Kubernetes clusters via the UI
- Infrastructure monitoring and observability
- Multi-cluster management from Aurora
- Kubernetes troubleshooting and diagnostics
- Secure cluster access for Aurora's AI features

## Overview

The kubectl agent connects outbound to Aurora via WebSocket, allowing Aurora to execute read-only kubectl commands in customer clusters without requiring inbound network access or direct credential sharing.

## Architecture

- **Agent** (`src/`): Python WebSocket client that runs in customer clusters
- **Helm Chart** (`chart/`): Kubernetes deployment manifests for easy installation
- **RBAC**: Configurable read-only permissions (view pods, deployments, services, etc.)
- **Security**: Runs as non-root, drops all capabilities, read-only filesystem

## Quick Start

### Prerequisites

**Before you start, you need:**

1. **Aurora Backend** - A running Aurora instance
   - If you don't have Aurora set up yet, see the [main Aurora repository](https://github.com/Arvo-AI)
   - Note your Aurora instance URL (e.g., `https://your-aurora-instance.com`)

2. **Kubernetes Cluster** - Version 1.19 or newer
   - You need cluster-admin access to install the agent
   - The cluster must allow outbound HTTPS connections

3. **Helm 3.x** - [Install Helm](https://helm.sh/docs/intro/install/)

4. **Container Registry Access** - To host the agent image
   - Docker Hub, GitHub Container Registry, GCR, or any container registry

5. **Agent Token** - Get this from the Kubernetes Connector in Aurora UI:
   - Log into your Aurora web UI
   - Navigate to **Connectors** → **Kubernetes** (or Settings → Kubernetes Clusters)
   - Click "Add Cluster" or "Connect Cluster"
   - Copy the generated agent token

### Step 1: Build the Agent Image

Build and push the Docker image to your container registry:

```bash
cd kubectl-agent/src/

# Build the image
docker build -t your-registry/aurora-kubectl-agent:1.0.3 .

# Push to your registry
docker push your-registry/aurora-kubectl-agent:1.0.3
```

**Registry examples:**
- Docker Hub: `docker.io/your-username/aurora-kubectl-agent:1.0.3`
- GitHub: `ghcr.io/your-org/aurora-kubectl-agent:1.0.3`
- GCR: `gcr.io/your-project/aurora-kubectl-agent:1.0.3`

### Step 2: Configure the Chart

Create a `values.yaml` file with your configuration:

```yaml
aurora:
  # Your Aurora instance URL (replace with your actual URL)
  backendUrl: "https://your-aurora-instance.com"
  
  # WebSocket endpoint (same URL, just change https to wss)
  wsEndpoint: "wss://your-aurora-instance.com/kubectl-agent"
  
  # Token from Aurora UI (Connectors → Kubernetes → Add Cluster)
  agentToken: "your-generated-token-here"

agent:
  image:
    # The image you built and pushed in Step 1
    repository: your-registry/aurora-kubectl-agent
    tag: "1.0.3"
```

### Step 3: Install via Helm

```bash
# Install the agent
helm install aurora-kubectl-agent ./kubectl-agent/chart \
  --namespace aurora --create-namespace \
  -f values.yaml
```

### Step 4: Verify Installation

Check that the agent is running and connected:

```bash
# Check pod status (should show "Running")
kubectl get pods -n aurora -l app=aurora-kubectl-agent

# Check agent logs (should show "Connected to backend")
kubectl logs -n aurora -l app=aurora-kubectl-agent --tail=50

# Verify connection (should return "ready")
kubectl exec -n aurora deployment/aurora-kubectl-agent -- \
  curl -s http://localhost:8080/ready
```

If everything is working, you should see the cluster appear in your Aurora UI under **Connectors → Kubernetes** with a green "Connected" status.

### Troubleshooting

**Agent not connecting?**
- Verify your Aurora backend URL is accessible from the cluster
- Check firewall rules allow outbound HTTPS (port 443)
- Verify the agent token is correct
- Check logs: `kubectl logs -n aurora -l app=aurora-kubectl-agent`

**Pod not starting?**
- Verify the container image exists and is accessible
- Check: `kubectl describe pod -n aurora -l app=aurora-kubectl-agent`

See `chart/README.md` for full Helm installation options and `DEPLOYMENT.md` for advanced configurations.

## Development

### Requirements

- Python 3.11+
- kubectl
- Kubernetes cluster for testing
- Aurora backend instance for connection

### Local Testing

```bash
cd src/
pip install -r requirements.txt

export NEXT_PUBLIC_WEBSOCKET_URL="wss://your-aurora-instance.com/kubectl-agent"
export AURORA_AGENT_TOKEN="your-token"
export AGENT_VERSION="dev"

python agent.py
```

### Configuration Options

The agent is configured via environment variables:

- `NEXT_PUBLIC_WEBSOCKET_URL` - WebSocket endpoint for Aurora backend (required)
- `AURORA_AGENT_TOKEN` - Authentication token (required)
- `AGENT_VERSION` - Agent version string (optional, defaults to chart version)
- `LOG_LEVEL` - Logging level: debug, info, warn, error (default: info)
- `LOG_FORMAT` - Log format: json or text (default: json)

## Security

- Agent runs with read-only RBAC permissions by default
- Only establishes outbound connections (no inbound ports required)
- Token-based authentication
- All commands are logged

## Support

**Questions or issues?**
- Email: info@arvoai.ca
- GitHub: https://github.com/Arvo-AI

**Looking for Aurora documentation?**
- Main repository: https://github.com/Arvo-AI
- Website: https://www.arvoai.ca
