---
sidebar_position: 4
---

# GCP Cloud KMS Auto-Unseal Setup

Cost: ~$0.06/month

## Prerequisites

- GCP project with Cloud KMS API enabled
- `gcloud` CLI configured
- Vault running on GKE, Compute Engine, **or on-premises**

## Step 1: Enable Cloud KMS API

```bash
gcloud services enable cloudkms.googleapis.com
```

## Step 2: Create Key Ring and Key

You **choose** the key ring and key names, then create them in GCP. They are not pre-existing values.

```bash
# Set variables first (required). Replace your-project-id with your GCP project ID.
export PROJECT_ID="your-project-id"
export LOCATION="us-central1"
export KEYRING="vault-keyring"
export KEY="vault-unseal-key"

# Confirm they are set
echo "Project: $PROJECT_ID Location: $LOCATION KeyRing: $KEYRING Key: $KEY"

# Create key ring (container for keys; one per region)
gcloud kms keyrings create "$KEYRING" --location="$LOCATION"

# Create crypto key (the actual encryption key Vault will use)
gcloud kms keys create "$KEY" --location="$LOCATION" --keyring="$KEYRING" --purpose=encryption
```

If you get `Error parsing [keyring]` or `Failed to find attribute [location]`, the variables are not set—run the `export` lines above in the same shell, then rerun the create commands.

Use the same `KEYRING` and `KEY` values in your Helm config (Step 4).

## Step 3: Configure Authentication

Choose based on where Vault runs:

### Option A: GKE with Workload Identity (recommended for GKE)

```bash
# Create service account
gcloud iam service-accounts create vault-kms \
  --display-name "Vault KMS Service Account"

# Grant KMS permissions (both key and keyring level required)
gcloud kms keys add-iam-policy-binding $KEY --location=$LOCATION --keyring=$KEYRING \
  --member "serviceAccount:vault-kms@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role "roles/cloudkms.cryptoKeyEncrypterDecrypter"

gcloud kms keyrings add-iam-policy-binding $KEYRING --location=$LOCATION \
  --member "serviceAccount:vault-kms@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role "roles/cloudkms.cryptoKeyEncrypterDecrypter"

# Bind to Kubernetes service account
gcloud iam service-accounts add-iam-policy-binding \
  vault-kms@${PROJECT_ID}.iam.gserviceaccount.com \
  --role "roles/iam.workloadIdentityUser" \
  --member "serviceAccount:${PROJECT_ID}.svc.id.goog[aurora/aurora-oss-vault]"
```

### Option B: Compute Engine

```bash
# Grant KMS permissions (both key and keyring level required)
gcloud kms keys add-iam-policy-binding $KEY --location=$LOCATION --keyring=$KEYRING \
  --member "serviceAccount:YOUR_COMPUTE_SA@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role "roles/cloudkms.cryptoKeyEncrypterDecrypter"

gcloud kms keyrings add-iam-policy-binding $KEYRING --location=$LOCATION \
  --member "serviceAccount:YOUR_COMPUTE_SA@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role "roles/cloudkms.cryptoKeyEncrypterDecrypter"
```

### Option C: On-Premises / Non-GCP Kubernetes (Service Account Key)

For on-prem clusters, you need a service account key file:

```bash
# Create service account
gcloud iam service-accounts create vault-kms \
  --display-name "Vault KMS Service Account"

# Grant KMS permissions (both key and keyring level required)
gcloud kms keys add-iam-policy-binding $KEY --location=$LOCATION --keyring=$KEYRING \
  --member "serviceAccount:vault-kms@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role "roles/cloudkms.cryptoKeyEncrypterDecrypter"

gcloud kms keyrings add-iam-policy-binding $KEYRING --location=$LOCATION \
  --member "serviceAccount:vault-kms@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role "roles/cloudkms.cryptoKeyEncrypterDecrypter"

# Create and download key file
gcloud iam service-accounts keys create vault-kms-key.json \
  --iam-account vault-kms@${PROJECT_ID}.iam.gserviceaccount.com
```

**Store the key securely in Kubernetes:**

```bash
# Create namespace if needed
kubectl create namespace aurora

# Create secret from key file
kubectl create secret generic vault-gcp-kms \
  --from-file=credentials.json=vault-kms-key.json \
  -n aurora

# Delete local key file
rm vault-kms-key.json
```

**Security considerations for service account keys:**

1. **Enable etcd encryption at rest** (protects secrets stored in Kubernetes):
   - On **GKE, EKS, AKS**: encryption at rest is enabled by default; no action needed.
   - On **self-managed clusters**: see [Encrypting secret data at rest](https://kubernetes.io/docs/tasks/administer-cluster/encrypt-data/).

2. **Rotate service account keys** (recommended quarterly):
   ```bash
   # Create new key
   gcloud iam service-accounts keys create new-vault-kms-key.json \
     --iam-account vault-kms@${PROJECT_ID}.iam.gserviceaccount.com
   
   # Update Kubernetes secret
   kubectl create secret generic vault-gcp-kms \
     --from-file=credentials.json=new-vault-kms-key.json \
     -n aurora --dry-run=client -o yaml | kubectl apply -f -
   
   # Restart Vault to pick up new key
   kubectl rollout restart statefulset/aurora-oss-vault -n aurora
   
   # Verify Vault unseals successfully, then delete old key
   gcloud iam service-accounts keys list \
     --iam-account vault-kms@${PROJECT_ID}.iam.gserviceaccount.com
   
   gcloud iam service-accounts keys delete OLD_KEY_ID \
     --iam-account vault-kms@${PROJECT_ID}.iam.gserviceaccount.com
   
   # Delete local key file
   rm new-vault-kms-key.json
   ```

## Step 4: Configure Helm Values

:::warning Important File
You must edit the file `deploy/helm/aurora/values.generated.yaml`. This is the file that `make deploy` reads. You cannot use a different filename.
:::

**If you don't have this file yet:**
```bash
cp deploy/helm/aurora/values.yaml deploy/helm/aurora/values.generated.yaml
```

**Add the vault seal configuration** to `deploy/helm/aurora/values.generated.yaml`. Replace the placeholder values with your actual values from Step 2 (`$PROJECT_ID`, `$LOCATION`, `$KEYRING`, `$KEY`).

**For GKE with Workload Identity:**

Add this section to `values.generated.yaml`:

```yaml
vault:
  seal:
    type: "gcpckms"
    gcpckms:
      project: "your-project-id"        # Replace with $PROJECT_ID
      region: "us-central1"              # Replace with $LOCATION
      key_ring: "vault-keyring"          # Replace with $KEYRING
      crypto_key: "vault-unseal-key"     # Replace with $KEY
  
  serviceAccount:
    annotations:
      iam.gke.io/gcp-service-account: vault-kms@your-project-id.iam.gserviceaccount.com
      # Replace your-project-id with $PROJECT_ID
```

**For On-Premises (with service account key):**

Add this section to `values.generated.yaml`:

```yaml
vault:
  seal:
    type: "gcpckms"
    gcpckms:
      project: "your-project-id"        # Replace with $PROJECT_ID
      region: "us-central1"              # Replace with $LOCATION
      key_ring: "vault-keyring"          # Replace with $KEYRING
      crypto_key: "vault-unseal-key"     # Replace with $KEY
      credentials: "/vault/gcp/credentials.json"
```

The Helm template automatically mounts the `vault-gcp-kms` secret when `credentials` is set. Ensure you created the secret in Step 3 (Option C).

**Example:** If your `$PROJECT_ID` is `my-gcp-project`, `$LOCATION` is `us-west1`, `$KEYRING` is `vault-keyring`, and `$KEY` is `vault-unseal-key`, your config would be:

```yaml
vault:
  seal:
    type: "gcpckms"
    gcpckms:
      project: "my-gcp-project"
      region: "us-west1"
      key_ring: "vault-keyring"
      crypto_key: "vault-unseal-key"
```

## Step 5: Verify Secret Exists (On-Prem Only)

**If using Option C (service account key), verify the secret exists:**

```bash
kubectl get secret vault-gcp-kms -n aurora

# Should show:
# NAME              TYPE     DATA   AGE
# vault-gcp-kms     Opaque   1      5m
```

If the secret doesn't exist, go back to Step 3 (Option C) and create it.

## Step 6: Deploy

After adding the seal configuration to `values.generated.yaml`, deploy:

```bash
make deploy
```

This updates Vault's configuration with the KMS seal settings and mounts the credentials secret (if using Option C).

## Step 7: Reset Existing Vault (if needed)

**If Vault was already initialized** with Shamir seals and you want to use KMS instead:

```bash
# Scale down Vault to 0 replicas (stops the pod)
kubectl scale statefulset aurora-oss-vault -n aurora --replicas=0

# Wait for pod to terminate
kubectl wait --for=delete pod -l app.kubernetes.io/name=aurora-oss-vault -n aurora --timeout=60s

# Delete Vault's data volume (WARNING: This deletes all Vault data)
# Finds and deletes the PVC automatically
kubectl delete pvc -n aurora $(kubectl get pvc -n aurora -o name | grep vault-data)

# Scale back up (will recreate PVC and start fresh)
kubectl scale statefulset aurora-oss-vault -n aurora --replicas=1

# Wait for pod to be ready
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=aurora-oss-vault -n aurora --timeout=120s
```

## Step 8: Initialize Vault

After deployment (or reset), initialize Vault (only needed once):

```bash
# Initialize Vault (use recovery-shares, not key-shares for auto-unseal)
kubectl -n aurora exec -it statefulset/aurora-oss-vault -- \
  vault operator init -recovery-shares=1 -recovery-threshold=1
```

**Save the output securely** — you'll get recovery keys (not unseal keys; auto-unseal handles unsealing automatically). Recovery keys are only needed if KMS becomes unavailable.

## Step 9: Configure Vault for Aurora

After initialization, set up the KV mount and application policy:

```bash
# Login with root token (from Step 8 output)
kubectl -n aurora exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && echo "<ROOT_TOKEN>" | vault login -'

# Enable KV v2 secrets engine at path 'aurora'
kubectl -n aurora exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && vault secrets enable -path=aurora kv-v2'

# Create Aurora application policy
kubectl -n aurora exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && vault policy write aurora-app - <<EOF
# Aurora application policy
path "aurora/data/users/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "aurora/metadata/users/*" {
  capabilities = ["list", "read", "delete"]
}
path "aurora/metadata/" {
  capabilities = ["list"]
}
path "aurora/metadata/users" {
  capabilities = ["list"]
}
EOF'

# Create token with aurora-app policy
kubectl -n aurora exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && vault token create -policy=aurora-app -ttl=0'
```

**Update `values.generated.yaml`** with the token from the last command:

```yaml
secrets:
  VAULT_TOKEN: "<TOKEN_FROM_ABOVE>"
```

:::danger Secure This Token
The `VAULT_TOKEN` grants access to all secrets stored in Vault. If this token is compromised, an attacker can read/modify all application secrets. Store `values.generated.yaml` securely and never commit it to version control.
:::

Then redeploy:

```bash
make deploy
```

Vault will auto-unseal on startup using the KMS configuration.

## Verify

```bash
# Check seal type
kubectl exec -n aurora statefulset/aurora-oss-vault -- vault status

# Should show:
# Seal Type: gcpckms
# Sealed: false
```

Test auto-unseal:
```bash
kubectl rollout restart statefulset/aurora-oss-vault -n aurora
kubectl logs -n aurora statefulset/aurora-oss-vault -f
# Watch for automatic unseal
```

## Troubleshooting

| Error | Fix |
|-------|-----|
| `PERMISSION_DENIED` | Check IAM binding on key |
| `NOT_FOUND` | Verify project/region/keyring/key names |
| `INVALID_ARGUMENT` | Check key is encryption type, not signing |
| `Workload Identity not working` | Verify GKE cluster has WI enabled |
| `cannot read credentials file` | Ensure `vault-gcp-kms` secret exists and redeploy |
| `could not find default credentials` | Check credentials path or secret mount |

### Debug Workload Identity (GKE)

```bash
kubectl exec -n aurora statefulset/aurora-oss-vault -- \
  curl -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email
```

### Debug Service Account Key (On-Prem)

```bash
# Verify secret exists
kubectl get secret vault-gcp-kms -n aurora

# Verify mount in pod
kubectl exec -n aurora statefulset/aurora-oss-vault -- ls -la /vault/gcp/

# Test connectivity to GCP
kubectl exec -n aurora statefulset/aurora-oss-vault -- \
  curl -s https://cloudkms.googleapis.com/ -o /dev/null -w "%{http_code}"
# Should return 404 (API reachable but no path)
```

## Cost Breakdown

- Software key: $0.06/month
- HSM key: $1.00/month (if needed for compliance)
- Operations: $0.03 per 10,000 requests (10,000 free/month)
- Typical cluster: ~$0.06/month total

## Security Best Practices

1. **Enable key rotation:**
   ```bash
   gcloud kms keys update $KEY --location=$LOCATION --keyring=$KEYRING \
     --rotation-period=90d \
     --next-rotation-time $(date -u +%Y-%m-%dT%H:%M:%SZ -d "+90 days")
   ```

2. **Use Organization Policy** to prevent key deletion

3. **Enable Cloud Audit Logs** for KMS operations

4. **Use separate keys** for prod/staging environments

5. **For on-prem:** Rotate service account keys quarterly
