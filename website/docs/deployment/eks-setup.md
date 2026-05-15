---
sidebar_position: 6
---

# EKS Cluster Setup for Aurora

How to set up an AWS EKS cluster ready for Aurora. If you already have a cluster, skip to [Verify Your Cluster](#verify-your-cluster) to make sure it meets the requirements.

## Prerequisites

Install these tools first:

| Tool | Install |
|------|---------|
| `aws` CLI | [docs.aws.amazon.com/cli](https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html) |
| `eksctl` | `brew install eksctl` or [eksctl.io/installation](https://eksctl.io/installation/) |
| `kubectl` | [kubernetes.io/docs/tasks/tools](https://kubernetes.io/docs/tasks/tools/) |

Configure the AWS CLI:
```bash
aws configure
# Enter: Access Key ID, Secret Access Key, region (e.g. us-east-1), output format (json)
```

Verify your identity and permissions before proceeding:
```bash
aws sts get-caller-identity
# You should see your Account, UserId, and Arn. If this fails, your credentials are wrong.

# Check you can create EKS clusters (should return cluster list, even if empty)
aws eks list-clusters --region us-east-1
```

If either command fails with `AccessDenied`, you need an IAM user/role with **AdministratorAccess** or at minimum: `eks:*`, `ec2:*`, `iam:*`, `cloudformation:*`, `s3:*`. Talk to your AWS admin.

## Step 1: Create the Cluster

Aurora needs at least **4 CPU cores** and **12GB RAM** allocatable.

### New VPC (simplest)

```bash
# 2x t3.large = 4 vCPU, 16GB RAM total
# Takes 15-20 minutes — don't interrupt it
eksctl create cluster \
  --name aurora-cluster \
  --region us-east-1 \
  --node-type t3.large \
  --nodes 2

# Verify kubectl is connected
kubectl get nodes
```

### Existing VPC

```bash
# List subnets in the VPC
aws ec2 describe-subnets --region us-east-1 \
  --filters "Name=vpc-id,Values=<YOUR_VPC_ID>" \
  --query 'Subnets[*].[SubnetId,AvailabilityZone,Tags[?Key==`Name`].Value|[0]]' --output table

# Create cluster in existing VPC
# Pick 2 PUBLIC subnets from DIFFERENT AZs (e.g. us-east-1b and us-east-1d — don't mix public/private)
eksctl create cluster \
  --name aurora-cluster \
  --region us-east-1 \
  --node-type t3.large \
  --nodes 2 \
  --vpc-public-subnets <SUBNET_1>,<SUBNET_2>

# For private-only subnets:
# --vpc-private-subnets <PRIVATE_SUBNET_1>,<PRIVATE_SUBNET_2>
```

## Step 2: Install EBS CSI Driver

EKS does **not** ship with a working storage driver. Without this, all database pods (Postgres, Redis, Vault, Weaviate) will be stuck in `Pending`.

```bash
export AWS_REGION="us-east-1"
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 1. Enable OIDC provider (needed for IAM roles)
eksctl utils associate-iam-oidc-provider \
  --region "$AWS_REGION" --cluster aurora-cluster --approve

# 2. Create IAM role for the CSI driver
eksctl create iamserviceaccount \
  --name ebs-csi-controller-sa \
  --namespace kube-system \
  --cluster aurora-cluster \
  --region "$AWS_REGION" \
  --attach-policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy \
  --approve --role-only \
  --role-name AmazonEKS_EBS_CSI_DriverRole

# 3. Install the EBS CSI addon
eksctl create addon --name aws-ebs-csi-driver \
  --cluster aurora-cluster --region "$AWS_REGION" \
  --service-account-role-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:role/AmazonEKS_EBS_CSI_DriverRole" \
  --force

# 4. Create a gp3 StorageClass (replaces the broken default gp2)
cat <<EOF | kubectl apply -f -
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

# 5. Remove default from the old gp2
kubectl patch storageclass gp2 \
  -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"false"}}}'
```

**Verify:**
```bash
kubectl get pods -n kube-system | grep ebs      # should be Running
kubectl get storageclass                          # gp3 should be (default)
```

## Step 3: Configure S3 Storage

Aurora stores uploaded files in S3. Choose one of the two approaches below.

### Option A: IRSA (recommended for EKS)

IAM Roles for Service Accounts injects short-lived credentials into pods automatically — no static keys to manage or rotate.

```bash
export AWS_REGION="us-east-1"
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AURORA_BUCKET="aurora-storage-${AWS_ACCOUNT_ID}"

# 1. Create bucket
aws s3 mb s3://$AURORA_BUCKET --region "$AWS_REGION"

# 2. Create an IAM role for Aurora with S3 access
#    (eksctl wires up the OIDC trust policy automatically)
eksctl create iamserviceaccount \
  --name aurora-irsa \
  --namespace aurora-oss \
  --cluster aurora-cluster \
  --region "$AWS_REGION" \
  --attach-policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess \
  --approve --override-existing-serviceaccounts

# 3. Get the role ARN
ROLE_ARN=$(kubectl get sa aurora-irsa -n aurora-oss \
  -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}')
echo "Use this in your Helm values: $ROLE_ARN"
```

:::tip Least-privilege policy
The command above uses `AmazonS3FullAccess` for simplicity. For production, create a scoped policy that only grants access to your specific bucket (see the [AWS connector README](https://github.com/Arvo-AI/aurora/blob/main/server/connectors/aws_connector/README.md) for an example policy).
:::

**Update the IRSA trust policy** to cover all Aurora backend pods (server, chatbot, celery-worker, celery-beat):

```bash
OIDC_URL=$(aws eks describe-cluster --name aurora-cluster --region "$AWS_REGION" \
  --query 'cluster.identity.oidc.issuer' --output text | sed 's|https://||')

ROLE_NAME=$(echo $ROLE_ARN | cut -d'/' -f2)

cat <<TRUST > /tmp/trust-policy.json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::${AWS_ACCOUNT_ID}:oidc-provider/${OIDC_URL}"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringLike": {
        "${OIDC_URL}:sub": "system:serviceaccount:aurora-oss:*-aurora-oss-*",
        "${OIDC_URL}:aud": "sts.amazonaws.com"
      }
    }
  }]
}
TRUST

aws iam update-assume-role-policy --role-name "$ROLE_NAME" \
  --policy-document file:///tmp/trust-policy.json
```

Then in your `values.generated.yaml`:

```yaml
serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: "<ROLE_ARN from above>"

config:
  STORAGE_BUCKET: "aurora-storage-<ACCOUNT_ID>"
  STORAGE_REGION: "us-east-1"

secrets:
  backend:
    STORAGE_ACCESS_KEY: ""   # intentionally empty — IRSA provides credentials
    STORAGE_SECRET_KEY: ""   # intentionally empty — IRSA provides credentials
```

### Option B: Static IAM credentials

If you prefer static credentials (simpler setup, but keys must be rotated manually):

```bash
AURORA_BUCKET="aurora-storage-${AWS_ACCOUNT_ID}"

# Create bucket
aws s3 mb s3://$AURORA_BUCKET --region "$AWS_REGION"

# Create an IAM user for Aurora
aws iam create-user --user-name aurora-s3

# Create a least-privilege policy scoped to the Aurora bucket only
aws iam put-user-policy --user-name aurora-s3 \
  --policy-name AuroraS3Access \
  --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {
      \"Effect\": \"Allow\",
      \"Action\": [
        \"s3:ListBucket\",
        \"s3:GetBucketLocation\"
      ],
      \"Resource\": \"arn:aws:s3:::${AURORA_BUCKET}\"
    },
    {
      \"Effect\": \"Allow\",
      \"Action\": [
        \"s3:GetObject\",
        \"s3:PutObject\",
        \"s3:DeleteObject\"
      ],
      \"Resource\": \"arn:aws:s3:::${AURORA_BUCKET}/*\"
    }
  ]
}"

aws iam create-access-key --user-name aurora-s3
```

**Save the `AccessKeyId` and `SecretAccessKey` from the output** — you'll need them when deploying Aurora.

## Verify Your Cluster

Whether you created a new cluster or are using an existing one, run the Aurora preflight check:

```bash
# From the Aurora repo
./deploy/preflight.sh
```

This validates: kubectl connection, storage driver, StorageClass, node resources, and ingress. Fix any `FAIL` items, then proceed to the [Kubernetes Deployment](./kubernetes) guide.

## Troubleshooting

### `eksctl create cluster` fails with quota errors

"Maximum number of VPCs/addresses reached":
- Delete unused VPCs/EIPs: `aws ec2 describe-vpcs --region us-east-1`
- Use a different region (e.g. `us-west-2`)
- Request a quota increase (AWS Console → Service Quotas → VPC)

### EBS CSI controller in `CrashLoopBackOff`

```bash
kubectl logs -n kube-system -l app=ebs-csi-controller --all-containers --tail=10
```

If you see `UnauthorizedOperation`, attach the EBS policy to the node role:
```bash
# Find the node role name
NODE_ROLE=$(aws eks describe-nodegroup --cluster-name aurora-cluster \
  --nodegroup-name $(aws eks list-nodegroups --cluster-name aurora-cluster \
    --query 'nodegroups[0]' --output text --region "$AWS_REGION") \
  --region "$AWS_REGION" --query 'nodegroup.nodeRole' --output text | cut -d'/' -f2)

aws iam attach-role-policy --role-name "$NODE_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy

# Restart the controller
kubectl delete pods -n kube-system -l app=ebs-csi-controller
```

### PVCs stuck in `Pending`

```bash
kubectl get pvc -n aurora-oss
kubectl get storageclass
```

If StorageClass is `gp2` with provisioner `kubernetes.io/aws-ebs`, that's the broken in-tree driver. Follow Step 2 above to install the CSI driver and create gp3.

After fixing, delete stuck PVCs to force recreation:
```bash
kubectl delete pvc --all -n aurora-oss
kubectl delete pods --all -n aurora-oss
```

## Tear Down

To delete everything:

```bash
# Delete Aurora first
helm uninstall aurora-oss -n aurora-oss
kubectl delete namespace aurora-oss

# Delete the S3 bucket
aws s3 rb s3://aurora-storage-${AWS_ACCOUNT_ID} --force --region "$AWS_REGION"

# Delete the IAM user
aws iam delete-access-key --user-name aurora-s3 \
  --access-key-id $(aws iam list-access-keys --user-name aurora-s3 --query 'AccessKeyMetadata[0].AccessKeyId' --output text)
aws iam delete-user-policy --user-name aurora-s3 \
  --policy-name AuroraS3Access
aws iam delete-user --user-name aurora-s3

# Delete the EKS cluster (takes ~10 minutes)
eksctl delete cluster --name aurora-cluster --region "$AWS_REGION"
```
