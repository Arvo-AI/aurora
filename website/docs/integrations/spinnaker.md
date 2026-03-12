---
sidebar_position: 3
---

# Spinnaker

Aurora integrates with [Spinnaker](https://spinnaker.io/) to provide deployment pipeline visibility, automated incident correlation with CD events, and root cause analysis when deployments fail.

## What You Get

| Capability | Description |
|------------|-------------|
| **Deployment event tracking** | Pipeline executions are correlated with alerts automatically |
| **RCA with deployment context** | Auto-generated RCA includes pipeline stages, failure details, trigger info |
| **Application health visibility** | View cluster and server group health across all clouds |
| **Pipeline browsing** | View execution history and pipeline configs from Aurora chat |
| **Rollback triggering** | Trigger Spinnaker rollback pipelines directly from Aurora (with confirmation) |

:::tip Already have Spinnaker?
If you already have a running Spinnaker instance, skip straight to [Connecting to Spinnaker](#connecting-to-spinnaker). The [Kubernetes setup guide](#setting-up-spinnaker-on-kubernetes-sample-setup) below is only for deploying a fresh Spinnaker instance.
:::

## Prerequisites

- A running Spinnaker instance with **Gate** (API gateway) accessible from the Aurora server
- `NEXT_PUBLIC_ENABLE_SPINNAKER=true` in your `.env` file
- Aurora with Vault configured and unsealed

:::info Feature Flag
The Spinnaker connector is behind a feature flag. Set `NEXT_PUBLIC_ENABLE_SPINNAKER=true` in your `.env` and restart Aurora. The connector will appear under the **CI/CD** category on the Connectors page.
:::

---

## Connecting to Spinnaker

Aurora supports two authentication methods:

### Option A: Token / Basic Auth

For Spinnaker instances with basic authentication or API tokens.

1. Navigate to **Connectors** > **Spinnaker**
2. Select the **Token / Basic Auth** tab
3. Enter:
   - **Spinnaker Gate URL**: Full URL to your Gate API (e.g., `https://gate.spinnaker.example.com`)
   - **Username**: Your Spinnaker username
   - **Password / API Token**: Your password or API token
4. Click **Connect Spinnaker**

Aurora validates the credentials by calling Gate's `/credentials` endpoint. On success, credentials are encrypted and stored in Vault.

### Option B: X.509 Certificate

For Spinnaker instances configured with mutual TLS (mTLS) authentication.

1. Navigate to **Connectors** > **Spinnaker**
2. Select the **X.509 Certificate** tab
3. Enter:
   - **Spinnaker Gate URL**: Full URL to the X.509 endpoint (e.g., `https://gate.spinnaker.example.com:8085`)
   - **Client Certificate (PEM)**: Upload your `.crt` or `.pem` file
   - **Client Private Key (PEM)**: Upload your `.key` or `.pem` file
   - **CA Bundle (PEM)** *(optional)*: Upload the CA certificate if using a private/self-signed CA
4. Click **Connect Spinnaker**

Certificate and key data are encrypted and stored in Vault. Temp files are created at runtime for mTLS handshakes and cleaned up automatically.

---

## Webhook Configuration

After connecting, Aurora displays a **webhook URL** and an **Echo configuration snippet**. Configure Spinnaker Echo to send pipeline events to Aurora for real-time deployment tracking and incident correlation.

### Echo Configuration

Patch the SpinnakerService CR to add the Echo webhook profile. Replace `YOUR_AURORA_URL` with the URL where Aurora is reachable from the Spinnaker cluster, and `YOUR_USER_ID` with the value shown on the Spinnaker connector page.

```bash
kubectl patch spinnakerservice spinnaker -n spinnaker --type=merge -p '{
  "spec": {
    "spinnakerConfig": {
      "profiles": {
        "echo": {
          "rest": {
            "enabled": true,
            "endpoints": [
              {
                "wrap": false,
                "url": "YOUR_AURORA_URL/spinnaker/webhook/YOUR_USER_ID",
                "headers": {
                  "Content-Type": "application/json"
                }
              }
            ]
          }
        }
      }
    }
  }
}'
```

Echo will restart automatically. Verify the config was applied:

```bash
kubectl exec -n spinnaker deployment/spin-echo -- cat /opt/spinnaker/config/echo-local.yml | grep -A5 "rest:"
```

:::info Local Development
If Aurora is running locally (Docker) and Spinnaker is on a remote cluster (GKE, EKS, etc.), Aurora isn't directly reachable. Use [ngrok](https://ngrok.com/) to expose your local Aurora:

```bash
ngrok http 5080
# Use the ngrok URL as YOUR_AURORA_URL, e.g.:
# https://abc123.ngrok-free.dev
```
:::

Echo sends raw Orca pipeline events (starting, failed, succeeded, etc.) on every pipeline state change. Aurora normalizes these automatically — no custom template is needed.

### What Happens When Events Arrive

1. Echo sends pipeline lifecycle events (starting, stage changes, completion) to Aurora
2. Aurora normalizes and stores each deployment event in the database
3. For failed pipelines (`TERMINAL`, `CANCELED`, `STOPPED`), Aurora creates an incident
4. The alert correlator checks if the deployment matches any existing incidents
5. If **Automatic RCA on Deployment Failures** is enabled, Aurora triggers a background RCA investigation

---

## Using Spinnaker in Aurora Chat

Once connected, ask Aurora about your deployments in the chat:

- *"Show me recent Spinnaker deployments"*
- *"What happened with the api-gateway deployment?"*
- *"Check application health for payment-service"*
- *"What pipelines are configured for my-app?"*
- *"Roll back the last api-gateway deployment"* (requires confirmation)

The agent uses the `spinnaker_rca` tool with these actions:

| Action | Description |
|--------|-------------|
| `recent_pipelines` | List recent pipeline executions across applications |
| `pipeline_detail` | Get full execution details with stage-by-stage status |
| `application_health` | Get cluster and server group health |
| `list_pipeline_configs` | List available pipeline definitions |
| `execution_logs` | Get detailed logs/context for failed stages |
| `trigger_pipeline` | Trigger a pipeline (requires user confirmation) |

---

## Setting Up Spinnaker on Kubernetes (Sample Setup)

If you don't have a Spinnaker instance, follow this guide to deploy one on a Kubernetes cluster (GKE, EKS, AKS, or local).

### 1. Create a Kubernetes Cluster

**GKE example:**

```bash
gcloud container clusters create spinnaker-test \
  --zone us-central1-a \
  --num-nodes 3 \
  --machine-type e2-standard-4

gcloud container clusters get-credentials spinnaker-test \
  --zone us-central1-a
```

For other providers, ensure you have a cluster with at least 3 nodes and 4 vCPU / 16 GB RAM per node.

### 2. Install the Spinnaker Operator

```bash
# Create namespaces
kubectl create namespace spinnaker
kubectl create namespace spinnaker-operator

# Download and install the operator
# Check https://github.com/armory/spinnaker-operator/releases for the latest version
curl -L https://github.com/armory/spinnaker-operator/releases/latest/download/manifests.tgz | tar xz

kubectl apply -f deploy/crds/ -n spinnaker-operator
kubectl apply -f deploy/operator/cluster -n spinnaker-operator
```

Wait for the operator to be running:

```bash
kubectl get pods -n spinnaker-operator
# NAME                                  READY   STATUS    RESTARTS   AGE
# spinnaker-operator-xxxxx-xxxxx        2/2     Running   0          60s
```

### 3. Create a Service Account

```bash
kubectl create serviceaccount spinnaker-sa -n spinnaker

# Create a token secret (required on Kubernetes 1.24+)
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: spinnaker-sa-token
  namespace: spinnaker
  annotations:
    kubernetes.io/service-account.name: spinnaker-sa
type: kubernetes.io/service-account-token
EOF

# Grant cluster-admin (for testing; use a tighter role in production)
kubectl create clusterrolebinding spinnaker-admin \
  --clusterrole=cluster-admin \
  --serviceaccount=spinnaker:spinnaker-sa
```

### 4. Configure Persistent Storage

Spinnaker needs a storage backend for Front50 (its metadata store). This example uses **GCS**. You can also use S3, MinIO, or Azure Blob Storage.

**GCS setup:**

```bash
# Create a GCS bucket
export PROJECT_ID=$(gcloud config get-value project)
gsutil mb gs://spin-${PROJECT_ID}

# Create a service account with storage access
gcloud iam service-accounts create spinnaker-storage \
  --display-name "Spinnaker Storage"

gcloud projects add-iam-policy-binding ${PROJECT_ID} \
  --member="serviceAccount:spinnaker-storage@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/storage.admin"

# Create and download a key
gcloud iam service-accounts keys create key.json \
  --iam-account=spinnaker-storage@${PROJECT_ID}.iam.gserviceaccount.com

# Create Kubernetes secrets
kubectl create secret generic spinnaker-gcs-key \
  --from-file=key.json=key.json -n spinnaker

# The operator also needs the key
kubectl create secret generic spinnaker-gcs-key \
  --from-file=key.json=key.json -n spinnaker-operator

# Also create it with the volume ID name that the operator generates
kubectl create secret generic gcs-key \
  --from-file=key.json=key.json -n spinnaker
```

Mount the GCS key into the operator deployment:

```bash
kubectl patch deployment spinnaker-operator -n spinnaker-operator \
  --type='json' -p='[
  {"op": "add", "path": "/spec/template/spec/volumes", "value": [
    {"name": "gcs-key", "secret": {"secretName": "spinnaker-gcs-key"}}
  ]},
  {"op": "add", "path": "/spec/template/spec/containers/0/volumeMounts", "value": [
    {"name": "gcs-key", "mountPath": "/opt/spinnaker/credentials", "readOnly": true}
  ]},
  {"op": "add", "path": "/spec/template/spec/containers/1/volumeMounts", "value": [
    {"name": "gcs-key", "mountPath": "/opt/spinnaker/credentials", "readOnly": true}
  ]}
]'
```

Wait for the operator to restart:

```bash
kubectl rollout status deployment/spinnaker-operator -n spinnaker-operator
```

### 5. Deploy Spinnaker

Create the SpinnakerService custom resource:

```yaml title="spinnaker.yml"
apiVersion: spinnaker.io/v1alpha2
kind: SpinnakerService
metadata:
  name: spinnaker
  namespace: spinnaker
spec:
  expose:
    type: service
    service:
      type: ClusterIP
  spinnakerConfig:
    config:
      version: "1.32.4"
      persistentStorage:
        persistentStoreType: gcs
        gcs:
          jsonPath: /opt/spinnaker/credentials/key.json
          project: YOUR_GCP_PROJECT_ID
          bucket: spin-YOUR_GCP_PROJECT_ID
          rootFolder: front50
      providers:
        kubernetes:
          enabled: true
          primaryAccount: my-k8s
          accounts:
            - name: my-k8s
              serviceAccount: true
              namespaces: []
              kinds: []
      security:
        apiSecurity:
          overrideBaseUrl: http://localhost:8084
        uiSecurity:
          overrideBaseUrl: http://localhost:9000
    profiles:
      gate:
        cors:
          allowedOriginsPattern: ".*"
        server:
          port: 8084
      clouddriver:
        kubernetes:
          accounts:
            - name: my-k8s
              serviceAccount: true
              namespaces: []
              kinds: []
              rawResourcesEndpointConfig:
                kindExpressions: []
                omitKindExpressions: []
    service-settings:
      clouddriver:
        kubernetes:
          serviceAccountName: spinnaker-sa
          volumes:
            - id: gcs-key
              type: secret
              secretName: spinnaker-gcs-key
              mountPath: /opt/spinnaker/credentials
      front50:
        kubernetes:
          serviceAccountName: spinnaker-sa
          volumes:
            - id: gcs-key
              type: secret
              secretName: spinnaker-gcs-key
              mountPath: /opt/spinnaker/credentials
      gate:
        kubernetes:
          serviceAccountName: spinnaker-sa
      orca:
        kubernetes:
          serviceAccountName: spinnaker-sa
      echo:
        kubernetes:
          serviceAccountName: spinnaker-sa
      rosco:
        kubernetes:
          serviceAccountName: spinnaker-sa
```

Replace `YOUR_GCP_PROJECT_ID` with your actual project ID, then apply:

```bash
kubectl apply -f spinnaker.yml
```

:::tip Webhook Validation
If the operator's webhook blocks the apply, delete it:
```bash
kubectl delete validatingwebhookconfiguration spinnakervalidatingwebhook
```
Then re-apply.
:::

### 6. Wait for All Services

```bash
# Watch pods come up (takes 5-10 minutes)
kubectl get pods -n spinnaker -w
```

All 8 services should reach `1/1 Running`:

| Service | Role |
|---------|------|
| `spin-gate` | API gateway (port 8084) |
| `spin-deck` | Web UI (port 9000) |
| `spin-clouddriver` | Cloud provider integrations |
| `spin-front50` | Metadata/config persistence |
| `spin-orca` | Pipeline orchestration |
| `spin-echo` | Notifications and webhooks |
| `spin-rosco` | Image baking |
| `spin-redis` | Caching |

### 7. Access Spinnaker

Port-forward the UI and API:

```bash
kubectl port-forward svc/spin-deck -n spinnaker 9000:9000 &
kubectl port-forward svc/spin-gate -n spinnaker 8084:8084 &
```

- **Spinnaker UI**: http://localhost:9000
- **Gate API**: http://localhost:8084

Test the API:

```bash
curl http://localhost:8084/credentials
# [{"authorized":true,"name":"my-k8s","type":"kubernetes",...}]
```

### 8. Connect to Aurora

If Aurora is running in Docker on the same machine, use `host.docker.internal` to reach the port-forwarded Gate:

1. Go to **Connectors** > **Spinnaker**
2. **Gate URL**: `http://host.docker.internal:8084`
3. Enter any username/password (if Gate has no auth configured)
4. Click **Connect Spinnaker**

---

## Setting Up X.509 (mTLS) Authentication

For production environments, configure Gate with mutual TLS so clients authenticate with certificates.

### 1. Generate Certificates

```bash
mkdir -p spinnaker-certs && cd spinnaker-certs

# Create a Certificate Authority (CA)
openssl genrsa -out ca.key 4096
openssl req -x509 -new -key ca.key -days 365 -out ca.crt \
  -subj "/CN=SpinnakerCA/O=Aurora"

# Create the Gate server certificate
openssl genrsa -out gate-server.key 4096
openssl req -new -key gate-server.key -out gate-server.csr \
  -subj "/CN=spin-gate/O=Spinnaker"

# Create a SAN extension file (add all hostnames Gate will be accessed by)
cat > gate-san.ext <<'EOF'
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names
[alt_names]
DNS.1 = spin-gate
DNS.2 = spin-gate.spinnaker
DNS.3 = spin-gate.spinnaker.svc
DNS.4 = spin-gate.spinnaker.svc.cluster.local
DNS.5 = localhost
DNS.6 = host.docker.internal
IP.1 = 127.0.0.1
EOF

# Sign the server certificate
openssl x509 -req -in gate-server.csr -CA ca.crt -CAkey ca.key \
  -CAcreateserial -days 365 -out gate-server.crt -extfile gate-san.ext

# Create the client certificate (for Aurora)
openssl genrsa -out client.key 4096
openssl req -new -key client.key -out client.csr \
  -subj "/CN=aurora-client/O=Aurora"
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key \
  -CAcreateserial -days 365 -out client.crt
```

:::warning SAN Hostnames
The `gate-san.ext` file must include every hostname that will be used to access Gate. If Aurora's server container connects via `host.docker.internal`, include it. If using a real domain, add that instead. Without the correct SAN, you'll get `CERTIFICATE_VERIFY_FAILED: Hostname mismatch`.
:::

### 2. Create JKS Keystore and Truststore

Gate (Java) requires JKS format for SSL:

```bash
# Create PKCS12 from server cert
openssl pkcs12 -export -in gate-server.crt -inkey gate-server.key \
  -out gate-server.p12 -name gate -CAfile ca.crt -caname SpinnakerCA \
  -password pass:changeit

# Convert to JKS keystore
keytool -importkeystore \
  -srckeystore gate-server.p12 -srcstoretype PKCS12 -srcstorepass changeit \
  -destkeystore gate-keystore.jks -deststoretype JKS -deststorepass changeit \
  -noprompt

# Create truststore with the CA cert
keytool -import -file ca.crt -alias spinnakerCA \
  -keystore gate-truststore.jks -storepass changeit -noprompt
```

### 3. Create Kubernetes Secret

```bash
kubectl create secret generic gate-x509-certs -n spinnaker \
  --from-file=gate-keystore.jks \
  --from-file=gate-truststore.jks \
  --from-file=ca.crt
```

### 4. Configure Gate for mTLS

Patch the SpinnakerService CR to enable X.509 on Gate:

```bash
kubectl patch spinnakerservice spinnaker -n spinnaker --type='merge' -p='{
  "spec": {
    "spinnakerConfig": {
      "profiles": {
        "gate": {
          "server": {
            "port": 8085,
            "ssl": {
              "enabled": true,
              "keyStore": "/opt/gate-certs/gate-keystore.jks",
              "keyStorePassword": "changeit",
              "keyStoreType": "JKS",
              "trustStore": "/opt/gate-certs/gate-truststore.jks",
              "trustStorePassword": "changeit",
              "trustStoreType": "JKS",
              "clientAuth": "want"
            }
          },
          "default": {
            "apiPort": 8084
          },
          "x509": {
            "enabled": true,
            "subjectPrincipalRegex": "CN=(.*?)(?:,|$)"
          },
          "cors": {
            "allowedOriginsPattern": ".*"
          }
        }
      },
      "service-settings": {
        "gate": {
          "kubernetes": {
            "serviceAccountName": "spinnaker-sa",
            "volumes": [
              {
                "id": "gate-x509-certs",
                "mountPath": "/opt/gate-certs",
                "secretName": "gate-x509-certs",
                "type": "secret"
              }
            ]
          }
        }
      }
    }
  }
}'
```

This configures:
- **Port 8085** (`server.port`): HTTPS with mTLS — `clientAuth: want` means client certs are accepted but not required
- **Port 8084** (`default.apiPort`): Secondary API port
- **x509.subjectPrincipalRegex**: Extracts the username from the certificate CN

### 5. Fix Readiness Probe

The Gate readiness probe defaults to `http://localhost:8084/health`, but with SSL enabled the main port uses HTTPS. Update the probe:

```bash
# Get the current deployment, update the probe, reapply
kubectl get deployment spin-gate -n spinnaker -o json | \
  python3 -c "
import json,sys
d = json.load(sys.stdin)
d['spec']['template']['spec']['containers'][0]['readinessProbe']['exec']['command'] = \
  ['wget', '--no-check-certificate', '--spider', '-q', 'https://localhost:8085/health']
json.dump(d, sys.stdout)
" | kubectl apply -f -
```

### 6. Expose Port 8085 on the Service

The operator's service only exposes 8084 by default. Add port 8085:

```bash
kubectl patch svc spin-gate -n spinnaker --type='json' -p='[
  {"op": "add", "path": "/spec/ports/-", "value": {
    "name": "gate-x509", "port": 8085, "protocol": "TCP", "targetPort": 8085
  }}
]'
```

:::note
The Spinnaker operator may reset the service ports during reconciliation. If port 8085 disappears, re-run the patch command above.
:::

### 7. Test the Connection

```bash
# Port-forward the X.509 port
kubectl port-forward svc/spin-gate -n spinnaker 8085:8085 &

# Test with client certificate
curl -s \
  --cert client.crt \
  --key client.key \
  --cacert ca.crt \
  https://localhost:8085/credentials
# [{"authorized":true,"name":"my-k8s",...}]
```

### 8. Connect Aurora with X.509

1. Go to **Connectors** > **Spinnaker**
2. Select the **X.509 Certificate** tab
3. Enter:
   - **Gate URL**: `https://host.docker.internal:8085` (or your Gate hostname)
   - **Client Certificate**: Upload `client.crt`
   - **Client Private Key**: Upload `client.key`
   - **CA Bundle**: Upload `ca.crt`
4. Click **Connect Spinnaker**

---

## Creating a Test Pipeline

To verify the full integration, create a sample application and pipeline:

```bash
# Create an application
curl -s -X POST http://localhost:8084/applications/test-app/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "job": [{
      "type": "createApplication",
      "application": {
        "name": "test-app",
        "email": "test@example.com",
        "cloudProviders": "kubernetes",
        "instancePort": 80
      }
    }],
    "application": "test-app",
    "description": "Create test application"
  }'

# Create a pipeline
curl -s -X POST http://localhost:8084/pipelines \
  -H "Content-Type: application/json" \
  -d '{
    "name": "deploy-nginx",
    "application": "test-app",
    "stages": [{
      "type": "deployManifest",
      "name": "Deploy Nginx",
      "refId": "1",
      "requisiteStageRefIds": [],
      "account": "my-k8s",
      "cloudProvider": "kubernetes",
      "moniker": {"app": "test-app"},
      "namespaceOverride": "default",
      "manifests": [{
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "nginx-test", "namespace": "default"},
        "spec": {
          "replicas": 1,
          "selector": {"matchLabels": {"app": "nginx-test"}},
          "template": {
            "metadata": {"labels": {"app": "nginx-test"}},
            "spec": {
              "containers": [{
                "name": "nginx",
                "image": "nginx:latest",
                "ports": [{"containerPort": 80}]
              }]
            }
          }
        }
      }]
    }],
    "triggers": []
  }'

# Trigger the pipeline
curl -s -X POST http://localhost:8084/pipelines/test-app/deploy-nginx \
  -H "Content-Type: application/json" \
  -d '{"type": "manual"}'
```

Then in Aurora chat, ask: *"Show me recent Spinnaker deployments"* to verify the tool is working.

---

## Troubleshooting

| Error | Solution |
|-------|----------|
| **Spinnaker connector not visible** | Set `NEXT_PUBLIC_ENABLE_SPINNAKER=true` in `.env` and restart Aurora |
| **"Failed to store Spinnaker credentials"** | Vault is sealed. Unseal it: `docker exec aurora-vault vault operator unseal <unseal-key>`. Then restart the Aurora server |
| **"Unable to reach Spinnaker Gate API"** | Verify Gate is accessible from the Aurora server container. Use `host.docker.internal` instead of `localhost` for Docker setups |
| **"CERTIFICATE_VERIFY_FAILED: Hostname mismatch"** | The server certificate SAN doesn't include the hostname Aurora uses. Regenerate with the correct hostname in `gate-san.ext` |
| **"Failed to backup user file: key.json"** | The operator needs the GCS key mounted. Patch the operator deployment to mount the `spinnaker-gcs-key` secret at `/opt/spinnaker/credentials` |
| **Clouddriver stuck at ContainerCreating** | The secret name in the volume doesn't match. Create a secret named `gcs-key` as a copy of `spinnaker-gcs-key` |
| **Echo/Front50 pods cycling** | Set `security.apiSecurity.overrideBaseUrl` and `security.uiSecurity.overrideBaseUrl` in the SpinnakerService CR to stop continuous reconciliation |
| **Gate readiness probe failing after SSL** | The probe uses HTTP but Gate now uses HTTPS. Update the probe command to use `https://localhost:8085/health` |
| **Port 8085 not found on service** | The operator resets service ports. Re-run the `kubectl patch svc` command to add port 8085 |
| **clouddriver `RawResourcesEndpointConfig` error** | Add a `clouddriver` profile with `rawResourcesEndpointConfig` (see SpinnakerService CR example above) |
| **Webhook events not arriving** | Ensure the Aurora server is accessible from the Spinnaker cluster. For local setups, use `ngrok http 5080` to create a tunnel |
| **Celery: "unregistered task spinnaker.process_deployment"** | The Celery worker doesn't have the Spinnaker task registered. Ensure `routes.spinnaker.tasks` is in `celery_config.py`'s `include` list and restart the worker |
