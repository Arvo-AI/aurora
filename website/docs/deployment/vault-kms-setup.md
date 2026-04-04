---
sidebar_position: 3
---

# Vault Auto-Unseal with KMS

Auto-unseal eliminates manual Vault unsealing after pod restarts by delegating key decryption to a cloud KMS.

## Should You Use Auto-Unseal?

| Scenario | Recommendation |
|----------|----------------|
| Production Kubernetes | **Yes** — pods restart frequently |
| Development / testing | Optional — manual unseal is fine |
| Air-gapped / no internet | No — use manual (Shamir) unsealing |

## Supported Providers

| | AWS KMS | GCP Cloud KMS |
|---|---------|---------------|
| **Cost** | ~$1/mo | ~$0.06/mo |
| **Setup Time** | 15-20 min | 25-35 min |
| **Best For** | EKS, EC2 | GKE, Compute Engine |
| **Auth Method** | IRSA / Node Role | Workload Identity / SA Key |

## How It Works

```
Pod Restart → Vault Sealed → KMS Decrypt → Auto-Unseal → Ready (10-30s)
```

Vault starts sealed, calls KMS to decrypt the master key using the pod's cloud identity, and unseals automatically.

:::danger
- **KMS unavailable = Vault outage.** If the KMS key is unreachable, Vault cannot unseal.
- **KMS key deleted = permanent data loss.** Enable key deletion protection.
:::

---

## AWS KMS Setup

### 1. Create KMS Key

```bash
export AWS_REGION="us-east-1"
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export CLUSTER_NAME="aurora-cluster"
export AWS_PAGER=""

KMS_KEY_ID=$(aws kms create-key \
  --description "Vault auto-unseal" \
  --region "$AWS_REGION" \
  --query 'KeyMetadata.KeyId' --output text)

aws kms create-alias --alias-name alias/vault-unseal-key --target-key-id "$KMS_KEY_ID" --region "$AWS_REGION"
aws kms enable-key-rotation --key-id "$KMS_KEY_ID" --region "$AWS_REGION"

echo "Key ID: $KMS_KEY_ID"
```

### 2. Grant IAM Access

Create the policy:

```bash
cat > /tmp/vault-kms-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["kms:Encrypt", "kms:Decrypt", "kms:DescribeKey"],
    "Resource": "arn:aws:kms:${AWS_REGION}:${AWS_ACCOUNT_ID}:key/${KMS_KEY_ID}"
  }]
}
EOF

aws iam create-policy --policy-name VaultKMSUnseal --policy-document file:///tmp/vault-kms-policy.json
```

Pick **one** option to attach it:

**Option A — IRSA (recommended for EKS):**
```bash
# Enable OIDC provider (once per cluster)
eksctl utils associate-iam-oidc-provider --region "$AWS_REGION" --cluster "$CLUSTER_NAME" --approve

# Create the role (--role-only prevents eksctl from creating a K8s ServiceAccount that conflicts with Helm)
eksctl create iamserviceaccount \
  --name aurora-oss-vault --namespace aurora-oss \
  --cluster "$CLUSTER_NAME" --region "$AWS_REGION" \
  --attach-policy-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:policy/VaultKMSUnseal" \
  --approve --role-only --role-name VaultKMSUnsealRole
```

**Option B — Node role (simpler):**
```bash
NODEGROUP=$(aws eks list-nodegroups --cluster-name "$CLUSTER_NAME" --region "$AWS_REGION" --query 'nodegroups[0]' --output text)
NODE_ROLE=$(aws eks describe-nodegroup --cluster-name "$CLUSTER_NAME" --nodegroup-name "$NODEGROUP" \
  --region "$AWS_REGION" --query 'nodegroup.nodeRole' --output text | cut -d'/' -f2)

aws iam attach-role-policy --role-name "$NODE_ROLE" \
  --policy-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:policy/VaultKMSUnseal"
```

### 3. Configure Helm Values

Add to `deploy/helm/aurora/values.generated.yaml`:

**EKS with IRSA or Node Role (recommended — no static credentials):**
```yaml
vault:
  seal:
    type: "awskms"
    awskms:
      region: "us-east-1"
      kms_key_id: "alias/vault-unseal-key"

# Only if using IRSA (Option A):
serviceAccount:
  vault:
    annotations:
      eks.amazonaws.com/role-arn: "arn:aws:iam::<ACCOUNT_ID>:role/VaultKMSUnsealRole"
```

**Non-EKS clusters (on-prem, GKE, AKS, etc.) using AWS KMS with static credentials:**

If your cluster isn't on EKS, there's no IRSA or EC2 instance role to provide AWS credentials automatically. Create a Kubernetes Secret with the AWS credentials, then enable the `credentials` flag:

```bash
kubectl create secret generic vault-aws-kms -n aurora-oss \
  --from-literal=access_key=AKIA... \
  --from-literal=secret_key=...
```

```yaml
vault:
  seal:
    type: "awskms"
    awskms:
      region: "us-east-1"
      kms_key_id: "alias/vault-unseal-key"
      credentials: true  # mounts vault-aws-kms Secret into Vault pod
```

The chart mounts the Secret at `/vault/aws/` and configures Vault to read credentials from `file:///vault/aws/access_key` and `file:///vault/aws/secret_key`, keeping the values out of the ConfigMap.

### 4. Reset & Reinitialize Vault

```bash
# Delete existing Vault data (required when switching from manual to KMS)
kubectl scale statefulset aurora-oss-vault -n aurora-oss --replicas=0
kubectl delete pvc vault-data-aurora-oss-vault-0 -n aurora-oss

# If eksctl created a conflicting ServiceAccount, delete it
kubectl delete serviceaccount aurora-oss-vault -n aurora-oss --ignore-not-found

# Deploy with new config
helm upgrade aurora-oss ./deploy/helm/aurora --namespace aurora-oss --reset-values \
  -f deploy/helm/aurora/values.generated.yaml
```

Wait for the Vault pod to be running (it will show `0/1 Ready` — that's expected, it's uninitialized):

```bash
kubectl wait --for=jsonpath='{.status.phase}'=Running pod/aurora-oss-vault-0 -n aurora-oss --timeout=120s

# Confirm Vault is up and waiting for init (should return 501 = uninitialized)
kubectl exec -n aurora-oss aurora-oss-vault-0 -- vault status 2>&1 | head -3
```

If you see `Seal Type: awskms`, the KMS config is working. Now **run each command one at a time** (heredocs break if batch-pasted in zsh):

```bash
kubectl -n aurora-oss exec -it statefulset/aurora-oss-vault -- \
  vault operator init -recovery-shares=1 -recovery-threshold=1
```

Save the Recovery Key and Root Token. Then:

```bash
kubectl -n aurora-oss exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && echo "<ROOT_TOKEN>" | vault login -'
```

```bash
kubectl -n aurora-oss exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && vault secrets enable -path=aurora kv-v2'
```

```bash
kubectl -n aurora-oss exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && vault policy write aurora-app - <<POLICY
path "aurora/data/users/*" { capabilities = ["create","read","update","delete","list"] }
path "aurora/metadata/users/*" { capabilities = ["list","read","delete"] }
path "aurora/metadata/" { capabilities = ["list"] }
path "aurora/metadata/users" { capabilities = ["list"] }
POLICY'
```

```bash
kubectl -n aurora-oss exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && vault token create -policy=aurora-app -ttl=0'
```

Update values and redeploy:

```bash
yq -i '.secrets.backend.VAULT_TOKEN = "<APP_TOKEN>"' deploy/helm/aurora/values.generated.yaml

helm upgrade aurora-oss ./deploy/helm/aurora --namespace aurora-oss --reset-values \
  -f deploy/helm/aurora/values.generated.yaml
```

### 5. Verify

```bash
kubectl exec -n aurora-oss statefulset/aurora-oss-vault -- vault status
# Seal Type should be: awskms
# Sealed should be: false
```

Test that auto-unseal survives a pod restart:

```bash
kubectl delete pod aurora-oss-vault-0 -n aurora-oss
kubectl wait --for=jsonpath='{.status.phase}'=Running pod/aurora-oss-vault-0 -n aurora-oss --timeout=120s
kubectl exec -n aurora-oss aurora-oss-vault-0 -- vault status
```

If it shows `Seal Type: awskms` and `Sealed: false` without any manual unseal command, KMS auto-unseal is working.

### AWS KMS Troubleshooting

| Error | Fix |
|-------|-----|
| `not authorized to perform kms:Decrypt` | Attach `VaultKMSUnseal` policy to IRSA role or node role |
| `NoCredentialProviders` | IRSA annotation missing or node role has no policy |
| `KMSKeyNotFoundException` | Wrong key — verify: `aws kms describe-key --key-id alias/vault-unseal-key` |
| `UPGRADE FAILED: conflict with "eksctl"` | Delete the SA: `kubectl delete sa aurora-oss-vault -n aurora-oss` then re-run helm upgrade |
| `no IAM OIDC provider associated` | Run: `eksctl utils associate-iam-oidc-provider --region $AWS_REGION --cluster <NAME> --approve` |

---

## GCP Cloud KMS Setup

### 1. Create Key Ring and Key

```bash
export GCP_PROJECT="your-project-id"
export GCP_REGION="us-central1"

gcloud kms keyrings create vault-keyring \
  --location "$GCP_REGION" --project "$GCP_PROJECT"

gcloud kms keys create vault-unseal-key \
  --location "$GCP_REGION" --keyring vault-keyring \
  --purpose encryption --project "$GCP_PROJECT"
```

### 2. Grant IAM Access

**GKE with Workload Identity (recommended):**
```bash
gcloud iam service-accounts create vault-kms-sa --display-name "Vault KMS"

gcloud kms keys add-iam-policy-binding vault-unseal-key \
  --location "$GCP_REGION" --keyring vault-keyring \
  --member "serviceAccount:vault-kms-sa@${GCP_PROJECT}.iam.gserviceaccount.com" \
  --role roles/cloudkms.cryptoKeyEncrypterDecrypter \
  --project "$GCP_PROJECT"

gcloud iam service-accounts add-iam-policy-binding \
  vault-kms-sa@${GCP_PROJECT}.iam.gserviceaccount.com \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:${GCP_PROJECT}.svc.id.goog[aurora-oss/aurora-oss-vault]"
```

**On-premises (service account key):**
```bash
gcloud iam service-accounts keys create /tmp/vault-kms-key.json \
  --iam-account vault-kms-sa@${GCP_PROJECT}.iam.gserviceaccount.com

kubectl create secret generic vault-gcp-kms -n aurora-oss \
  --from-file=credentials.json=/tmp/vault-kms-key.json
rm /tmp/vault-kms-key.json
```

### 3. Configure Helm Values

```yaml
vault:
  seal:
    type: "gcpckms"
    gcpckms:
      project: "your-project-id"
      region: "us-central1"
      key_ring: "vault-keyring"
      crypto_key: "vault-unseal-key"
      # For on-prem only:
      # credentials: "/vault/gcp/credentials.json"

# GKE Workload Identity:
serviceAccount:
  vault:
    annotations:
      iam.gke.io/gcp-service-account: "vault-kms-sa@your-project-id.iam.gserviceaccount.com"
```

### 4. Reset & Reinitialize

Same process as AWS — delete Vault PVC, redeploy, run `vault operator init -recovery-shares=1 -recovery-threshold=1`, set up KV engine and policy, update VAULT_TOKEN, redeploy. See the AWS section above for the exact commands.

### 5. Verify

```bash
kubectl exec -n aurora-oss statefulset/aurora-oss-vault -- vault status
# Seal Type: gcpckms | Sealed: false
```

---

## Migrating from Manual to Auto-Unseal

If Vault is already running with manual (Shamir) seals:

1. Back up Vault data
2. Update Helm values with the seal configuration
3. Restart Vault with `-migrate` flag
4. Provide existing unseal keys when prompted

See [HashiCorp Seal Migration docs](https://developer.hashicorp.com/vault/docs/concepts/seal#seal-migration) for details.
