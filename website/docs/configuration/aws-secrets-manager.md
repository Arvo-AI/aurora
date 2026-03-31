---
sidebar_position: 3
---

# AWS Secrets Manager Configuration

Aurora supports AWS Secrets Manager as an alternative secrets backend to HashiCorp Vault. This is useful for EKS deployments where your security team requires AWS-native secrets management.

## How It Works

- **Backend selection**: Set `SECRETS_BACKEND=aws_secrets_manager` to switch from Vault
- **Secret references**: Stored in the database as `awssm:{region}:{prefix}/{secret_name}`, resolved at runtime
- **Authentication**: Uses the standard boto3 credential chain (env vars, IRSA, instance profile)
- **No rotation config needed**: Aurora handles credential refresh at the application level (OAuth token rotation, STS re-assumption). Do **not** configure AWS SM auto-rotation.

## Prerequisites

1. An AWS account with Secrets Manager access
2. An IAM policy granting the required permissions (see below)
3. For EKS: an IAM Role for Service Accounts (IRSA) or pod identity

### Required IAM Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:CreateSecret",
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:DeleteSecret"
      ],
      "Resource": "arn:aws:secretsmanager:*:*:secret:aurora/users/*"
    }
  ]
}
```

Restrict the `Resource` ARN to your specific region and account for production use.

## Docker Compose Setup

### 1. Add to `.env`

```bash
SECRETS_BACKEND=aws_secrets_manager
AWS_SM_REGION=us-east-1
AWS_SM_PREFIX=aurora/users

# AWS credentials (for Docker Compose — not needed with IRSA on EKS)
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
```

### 2. Build and start Aurora

```bash
make dev-build && make dev        # development
make prod-local                   # production (build from source)
make prod-prebuilt                # production (prebuilt images)
```

Vault containers will still start but won't be used. They consume minimal resources (~128Mi).

## Helm / EKS Setup (IRSA)

IRSA (IAM Roles for Service Accounts) is the recommended authentication method for EKS. It injects temporary AWS credentials into pods automatically — no static keys needed.

### 1. Create the IAM Role

```bash
eksctl create iamserviceaccount \
  --name aurora-backend \
  --namespace aurora \
  --cluster YOUR_CLUSTER \
  --attach-policy-arn arn:aws:iam::YOUR_ACCOUNT:policy/AuroraSecretsManagerPolicy \
  --approve
```

### 2. Install the EBS CSI Driver

Aurora's stateful services (Postgres, Redis, Weaviate) require persistent volumes. A fresh EKS cluster needs the EBS CSI driver and a default StorageClass:

```bash
# Create IRSA for the driver
eksctl create iamserviceaccount \
  --name ebs-csi-controller-sa \
  --namespace kube-system \
  --cluster YOUR_CLUSTER \
  --attach-policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy \
  --approve

# Install the addon
EBS_ROLE_ARN=$(kubectl get sa ebs-csi-controller-sa -n kube-system \
  -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}')

eksctl create addon \
  --name aws-ebs-csi-driver \
  --cluster YOUR_CLUSTER \
  --service-account-role-arn "$EBS_ROLE_ARN" \
  --force

# Create a default StorageClass
kubectl apply -f - <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer
parameters:
  type: gp3
EOF
```

### 3. Configure values.yaml

```yaml
services:
  vault:
    enabled: false   # No Vault pods needed

config:
  SECRETS_BACKEND: "aws_secrets_manager"
  AWS_SM_REGION: "us-east-1"
  AWS_SM_PREFIX: "aurora/users"
  ENABLE_POD_ISOLATION: "true"   # Required for IRSA token mounting

serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::YOUR_ACCOUNT:role/aurora-backend
```

### 4. Deploy

```bash
helm upgrade --install aurora ./deploy/helm/aurora -f values.generated.yaml
```

## Configuration Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SECRETS_BACKEND` | No | `vault` | Set to `aws_secrets_manager` |
| `AWS_SM_REGION` | Yes | — | AWS region for Secrets Manager |
| `AWS_SM_PREFIX` | No | `aurora/users` | Path prefix for secret names |
| `AWS_ACCESS_KEY_ID` | No* | — | AWS access key (*not needed with IRSA) |
| `AWS_SECRET_ACCESS_KEY` | No* | — | AWS secret key (*not needed with IRSA) |

## Secret Reference Format

Secrets are stored in the database with the reference format:

```text
awssm:{region}:{prefix}/{secret_name}
```

Example: `awssm:us-east-1:aurora/users/aurora-dev-ed952f31-b494-4b98-97a4-b68ac5d8cb1d-datadog-token`

## Troubleshooting

### "AWS Secrets Manager backend is not available"

1. Check `AWS_SM_REGION` is set
2. Verify AWS credentials are available (env vars or IRSA)
3. Test connectivity from inside the container:

```bash
# Docker Compose
docker exec aurora-server python -c "
import boto3
client = boto3.client('secretsmanager', region_name='us-east-1')
r = client.create_secret(Name='aurora/users/test-hello', SecretString='it works')
print('OK:', r['Name'])
client.delete_secret(SecretId='aurora/users/test-hello', ForceDeleteWithoutRecovery=True)
"

# EKS
kubectl exec -n aurora <server-pod> -- python -c "
import boto3
client = boto3.client('secretsmanager', region_name='us-east-1')
r = client.create_secret(Name='aurora/users/test-hello', SecretString='it works')
print('OK:', r['Name'])
client.delete_secret(SecretId='aurora/users/test-hello', ForceDeleteWithoutRecovery=True)
"
```

### "AccessDeniedException"

Your IAM policy is missing required permissions. Ensure all four actions (`CreateSecret`, `GetSecretValue`, `PutSecretValue`, `DeleteSecret`) are allowed on the `aurora/users/*` resource.

### IRSA: "no IAM OIDC provider associated with cluster"

Your EKS cluster doesn't have an OIDC provider. Create one before setting up the service account:

```bash
eksctl utils associate-iam-oidc-provider --region us-east-1 --cluster YOUR_CLUSTER --approve
```

To avoid this, create the cluster with `eksctl create cluster --with-oidc`.

### IRSA: credentials not working despite role annotation

Two common causes:

1. **`ENABLE_POD_ISOLATION` is not `"true"`** — Without this, `automountServiceAccountToken` is `false` and the IRSA token file is never mounted into the pod. Verify with:
   ```bash
   kubectl exec -n aurora <server-pod> -- env | grep AWS_WEB_IDENTITY
   ```
   If empty, set `ENABLE_POD_ISOLATION: "true"` in your values.

2. **Trust policy mismatch** — The IAM role's trust policy must allow the Helm-created ServiceAccounts (e.g., `aurora-aurora-oss-server`), not just the eksctl-created one. Use a wildcard in the trust policy condition:
   ```json
   "StringLike": {
     "oidc.eks.<region>.amazonaws.com/id/<OIDC_ID>:sub": "system:serviceaccount:aurora:*"
   }
   ```

### Migrating from Vault

There is no automatic migration tool. Existing `vault:` references in the database will fail if you switch backends without re-connecting integrations. To migrate:

1. Deploy with `SECRETS_BACKEND=aws_secrets_manager`
2. Have users disconnect and reconnect their integrations (this stores new credentials in AWS SM)
3. Old `vault:` references become orphaned and can be cleaned up

## See Also

- [Vault Configuration](./vault.md) — default secrets backend
