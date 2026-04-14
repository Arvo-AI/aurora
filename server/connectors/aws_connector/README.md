# AWS Connector

Cross-account IAM roles with STS AssumeRole for single or multi-account setups.

---

## How It Works

Aurora uses a single set of "base" AWS credentials that only have permission to
call `sts:AssumeRole`. For every account it accesses, Aurora assumes an IAM role
**inside that account** using a unique **External ID** to prevent confused-deputy
attacks.

```text
Aurora base credentials (sts:AssumeRole only)
  └─> sts:AssumeRole(RoleArn, ExternalId)
        └─> Temporary credentials (1-hour sessions, auto-refreshed)
```

---

## Prerequisites

| Requirement | Who Provides It |
|---|---|
| Aurora's AWS Account ID | Displayed on the Aurora AWS Onboarding page |
| External ID (UUID) | Auto-generated per Aurora workspace |
| CloudFormation template | Hosted publicly by Aurora (auto-included in Quick-Create links) |

---

## Operator Setup (Aurora's Own AWS Credentials)

Before users can onboard their accounts, the Aurora operator must configure
Aurora's own AWS credentials. These are used solely to call `sts:AssumeRole`.

Choose **one** of the two paths below depending on your deployment type.

---

### Path A: Static Credentials (Docker Compose / non-Kubernetes)

#### 1. Create an IAM User for Aurora

1. Go to [IAM > Users](https://console.aws.amazon.com/iam/home#/users) > **Create user**
2. Name: `aurora-service-user` (or any name you prefer)
3. **Do not** enable console access (Aurora only needs programmatic access)
4. Attach the following policy:

```json
{
	"Version": "2012-10-17",
	"Statement": [
		{
			"Effect": "Allow",
			"Action": [
				"sts:AssumeRole"
			],
			"Resource": "*"
		}
	]
}
```

#### 2. Create Access Keys

1. Go to the user you just created
2. Click the **Security credentials** tab
3. Scroll to **Access keys** > **Create access key**
4. Select **Application running outside AWS**
5. Copy both the **Access key ID** and **Secret access key**

#### 3. Configure Aurora Environment

Add to your `.env`:

```bash
AWS_ACCESS_KEY_ID=your-access-key-id-here
AWS_SECRET_ACCESS_KEY=your-secret-access-key-here
AWS_DEFAULT_REGION=us-east-1
```

Rebuild and restart:

```bash
make down
make dev-build  # or make prod-local for production
make dev        # or make prod-prebuilt / make prod for production
```

---

### Path B: IRSA / Pod Identity (Kubernetes on EKS)

When running on EKS, use IAM Roles for Service Accounts (IRSA) instead of
static credentials. The EKS pod identity webhook injects short-lived
credentials automatically — no `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY`
needed.

#### 1. Create an IAM Role for Aurora's Backend

Create an IAM role trusted by your cluster's OIDC provider. The role needs
**three sets of permissions** to fully support all Aurora features:

| Permission | Why |
|---|---|
| `sts:AssumeRole` on `AuroraReadOnly-*` roles | AWS connector — assumes cross-account customer roles |
| `s3:*` on your storage bucket | File storage (omit if using static `STORAGE_ACCESS_KEY`/`SECRET_KEY`) |
| `secretsmanager:*` on `aurora/users/*` | Secrets backend (omit if using Vault) |

> **The `sts:AssumeRole` permission is required for the AWS connector to work.**
> Without it, Aurora's backend cannot assume cross-account roles and users will
> see "not authorized to perform: sts:AssumeRole" errors when connecting AWS accounts.

Example inline policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AWSConnector",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "*"
    },
    {
      "Sid": "Storage",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                 "s3:ListMultipartUploadParts", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::<YOUR_BUCKET>/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": "arn:aws:s3:::<YOUR_BUCKET>"
    },
    {
      "Sid": "SecretsManager",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue", "secretsmanager:CreateSecret",
                 "secretsmanager:PutSecretValue", "secretsmanager:UpdateSecret",
                 "secretsmanager:DeleteSecret", "secretsmanager:DescribeSecret",
                 "secretsmanager:ListSecrets"],
      "Resource": "arn:aws:secretsmanager:<REGION>:<YOUR_ACCOUNT_ID>:secret:aurora/users/*"
    }
  ]
}
```

The role's trust policy must allow your cluster's OIDC provider for **all
backend service accounts** (server, chatbot, celery-worker, celery-beat).
Use `StringLike` with a wildcard to cover all of them in one statement:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/oidc.eks.<REGION>.amazonaws.com/id/<OIDC_ID>"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "oidc.eks.<REGION>.amazonaws.com/id/<OIDC_ID>:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "oidc.eks.<REGION>.amazonaws.com/id/<OIDC_ID>:sub": "system:serviceaccount:<NAMESPACE>:<RELEASE_NAME>-aurora-oss-*"
      }
    }
  }]
}
```

> **All four backend pods need IRSA access.** Scoping the trust policy to only
> `aurora-oss-server` will cause `AssumeRoleWithWebIdentity` failures in the
> chatbot and celery workers when they attempt AWS operations (storage, secrets,
> AWS connector credential refresh).

#### 2. Annotate the Service Accounts via Helm

In your `values.yaml` override file:

```yaml
serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::<ACCOUNT_ID>:role/<your-aurora-irsa-role>

config:
  STORAGE_BUCKET: "<your-bucket>"
  STORAGE_ENDPOINT_URL: ""        # empty = real AWS S3
  STORAGE_REGION: "<region>"
  SECRETS_BACKEND: "aws_secrets_manager"   # if not using Vault
  AWS_SM_REGION: "<region>"
  AWS_SM_PREFIX: "aurora/users"

secrets:
  backend:
    STORAGE_ACCESS_KEY: ""   # intentionally empty — IRSA provides credentials
    STORAGE_SECRET_KEY: ""   # intentionally empty — IRSA provides credentials
```

Do **not** set `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY` — their presence
overrides the IRSA credential chain.

---

### Step 4. Host the CloudFormation Template

Aurora hosts the CloudFormation template publicly on its own AWS account so
that end-users never need to upload or host it themselves. The template URL
is hardcoded in the backend (`server/routes/aws/onboarding.py`).

1. **Create an S3 bucket**:

```bash
aws s3 mb s3://aurora-cfn-templates-<YOUR_AURORA_ACCOUNT_ID> --region <YOUR_REGION>
```

2. **Upload the template**:

```bash
aws s3 cp server/connectors/aws_connector/aurora-cross-account-role.yaml \
  s3://aurora-cfn-templates-<YOUR_AURORA_ACCOUNT_ID>/aurora-cross-account-role.yaml
```

3. **Make the template publicly readable** (the template contains no secrets —
   only parameter placeholders). The bucket policy is scoped to a single object:

```bash
aws s3api put-public-access-block \
  --bucket aurora-cfn-templates-<YOUR_AURORA_ACCOUNT_ID> \
  --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false

aws s3api put-bucket-policy \
  --bucket aurora-cfn-templates-<YOUR_AURORA_ACCOUNT_ID> \
  --policy '{
    "Version": "2012-10-17",
    "Statement": [{
      "Sid": "PublicReadCFNTemplate",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::aurora-cfn-templates-<YOUR_AURORA_ACCOUNT_ID>/aurora-cross-account-role.yaml"
    }]
  }'
```

4. **Update the constant** in `server/routes/aws/onboarding.py`:

```python
CLOUDFORMATION_TEMPLATE_URL = "https://aurora-cfn-templates-<YOUR_AURORA_ACCOUNT_ID>.s3.<YOUR_REGION>.amazonaws.com/aurora-cross-account-role.yaml"
```

5. Rebuild Aurora (`make rebuild-server`).

---

## Onboarding AWS Accounts

Once the operator has configured Aurora's credentials and hosted the template,
users can connect their AWS accounts via the onboarding UI.

### Option A: Quick-Create Link (Recommended)

Aurora hosts the CloudFormation template publicly, so the onboarding page
always shows a ready-to-use **Quick-Create Link**.

1. Navigate to **Connectors > AWS** in Aurora.
2. Click the Quick-Create button.
3. Log into the target AWS account in your browser.
4. The AWS Console shows the stack with all parameters pre-filled.
5. Check the IAM capabilities acknowledgement box and click **Create stack**.
6. Copy the Role ARN from the stack outputs and paste it in Aurora.

### Option B: Manual Setup

1. Navigate to **Connectors > AWS** in Aurora.
2. Copy the **External ID** and **Trust Policy** shown in the UI.
3. In your AWS account, go to [IAM > Roles](https://console.aws.amazon.com/iam/home#/roles) > **Create role**.
4. Trusted entity: **AWS account** > **Another AWS account**.
   - Account ID: Aurora's AWS Account ID (shown in the UI)
   - Check **Require external ID** and paste the External ID
5. Attach permission policies:
   - For read-only: `ReadOnlyAccess`
   - For full access: `PowerUserAccess`
   - Or create custom policies for specific permissions
6. Name: `AuroraReadOnlyRole` (or any name you prefer)
7. Copy the **Role ARN** and paste it in the Aurora onboarding form.

### Option C: Download CloudFormation Template

If you prefer to review or modify the template before deploying:

1. Click **Download CloudFormation Template** in the onboarding UI.
   The template has your workspace's External ID and Aurora's account ID pre-filled.
2. Deploy via CLI:

```bash
aws cloudformation deploy \
  --template-file aurora-cross-account-role.yaml \
  --stack-name aurora-role \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      AuroraAccountId=<AURORA_ACCOUNT_ID> \
      ExternalId=<YOUR_EXTERNAL_ID> \
  --region us-east-1
```

> `deploy` creates or updates the stack, so re-running is safe.

---

## Multi-Account / Organization Deployment

For organizations with many accounts (AWS Organizations, Control Tower):

### StackSets

Deploy to all member accounts at once from the management account:

```bash
aws cloudformation create-stack-set \
  --stack-set-name aurora-role \
  --template-body file://aurora-cross-account-role.yaml \
  --parameters \
      ParameterKey=AuroraAccountId,ParameterValue=<AURORA_ACCOUNT_ID> \
      ParameterKey=ExternalId,ParameterValue=<YOUR_EXTERNAL_ID> \
  --capabilities CAPABILITY_NAMED_IAM \
  --permission-model SERVICE_MANAGED \
  --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false

aws cloudformation create-stack-instances \
  --stack-set-name aurora-role \
  --deployment-targets OrganizationalUnitIds=<ROOT_OU_ID> \
  --regions us-east-1 \
  --operation-preferences MaxConcurrentPercentage=100,FailureTolerancePercentage=10
```

With `--auto-deployment Enabled=true`, new accounts added to the OU
automatically get the Aurora role.

If you use **Control Tower**, you can alternatively register the template as a
Service Catalog product distributed via Account Factory.

### Bulk Register

After roles are created across your accounts:

1. Go to the Aurora AWS Onboarding page.
2. Open **More: bulk register, StackSets**.
3. Paste your account IDs, one per line:

```text
123456789012,us-east-1
234567890123,eu-west-1
345678901234,us-west-2
```

Format: `ACCOUNT_ID,REGION` (region defaults to `us-east-1` if omitted). You
can also specify a custom role name as a third field:
`ACCOUNT_ID,REGION,ROLE_NAME`.

Aurora validates each account by attempting `sts:AssumeRole`. Successfully
validated accounts are connected immediately; failed accounts show the error
inline.

---

## What ReadOnlyAccess Covers

The AWS-managed `ReadOnlyAccess` policy grants `Describe*`, `Get*`, `List*`,
and `BatchGet*` actions across nearly all AWS services. Key inclusions:

- **Compute**: EC2, ECS, EKS, Lambda
- **Storage**: S3 (read objects + list buckets), EBS
- **Database**: RDS, DynamoDB, Redshift, ElastiCache
- **Networking**: VPC, ELB, Route 53, CloudFront
- **Security**: IAM (read), SecurityHub, GuardDuty, Inspector
- **Monitoring**: CloudWatch, CloudTrail, X-Ray
- **Infrastructure-as-Code**: CloudFormation stack info

**What it does NOT allow**:

- Creating, modifying, or deleting any resources
- Accessing S3 object contents that require specific bucket policies
- KMS decryption (unless explicitly granted)
- Accessing secrets in Secrets Manager or SSM Parameter Store (SecureString)

For a full list, see the
[AWS documentation](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/ReadOnlyAccess.html).

---

## Security Model

| Control | Detail |
|---|---|
| **External ID** | A UUID v4 unique to your Aurora workspace. Prevents other Aurora tenants from assuming your role (confused-deputy protection). |
| **No write access** | The role only has `ReadOnlyAccess`. Aurora's session policy can further restrict in read-only mode. |
| **Short-lived credentials** | STS sessions last at most 1 hour. Aurora proactively refreshes them before expiry. |
| **Per-account isolation** | Each AWS account has its own role. Compromising one role does not affect other accounts. |
| **Audit trail** | Every `AssumeRole` call is logged in the target account's CloudTrail. Session names include `aurora-<workspace_id>` for traceability. |

---

## Account ID Detection

Aurora automatically detects its AWS account ID by calling
`sts:get-caller-identity` using the configured credentials. This account ID
is displayed in the onboarding UI and used in the trust policy template.

---

## Disconnecting

- **Single account**: Click the trash icon next to the account in the connected
  accounts table. This removes Aurora's connection; the IAM role still exists
  in your AWS account until you delete the CloudFormation stack.
- **All accounts**: Click **Disconnect All**.

To fully remove access, delete the CloudFormation stack (or StackSet) in your
AWS accounts.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Quick-Create link says "templateURL is required" | Template URL not configured | Update the `CLOUDFORMATION_TEMPLATE_URL` constant in `server/routes/aws/onboarding.py` |
| Quick-Create says role already exists | `AuroraReadOnlyRole` already in that account | Use the existing role (just register in Aurora), or delete the old stack first |
| "Access denied" on bulk register | Role not created, or External ID mismatch | Verify the CFN stack deployed successfully with the correct External ID |
| "Aurora cannot assume this role" | IAM propagation delay | Wait up to 5 minutes after role creation and retry |
| "Unable to determine Aurora's AWS account ID" | Credentials not configured | **Static creds:** ensure `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are set in `.env`. **IRSA:** verify the service account annotation (`eks.amazonaws.com/role-arn`) and that the OIDC trust policy matches the pod's service account. |
| "not authorized to perform: sts:AssumeRole" on AWS connector | IRSA role missing `sts:AssumeRole` permission | Add `sts:AssumeRole` on `arn:aws:iam::<account>:role/AuroraReadOnly-*` to the IRSA role's inline policy (see Path B above). Static-credential deployments: add `sts:AssumeRole` to the IAM user policy. |
| Some accounts fail, others succeed | IAM role propagation delay | Wait 5 minutes and retry the failed accounts |
| Discovery finds no resources | Resource Explorer not enabled | Run `aws resource-explorer-2 create-index --type AGGREGATOR` in your primary region |
| Template deploy fails | `CAPABILITY_NAMED_IAM` not specified | Add `--capabilities CAPABILITY_NAMED_IAM` to your deploy command |
