# Multi-Account AWS Onboarding Guide

This guide explains how to connect multiple AWS accounts to Aurora using
cross-account IAM roles and STS AssumeRole.  It is designed for organisations
with many accounts (e.g. managed via AWS Organizations / Control Tower).

---

## How It Works

Aurora uses a single set of "base" AWS credentials that only have permission to
call `sts:AssumeRole`.  For every customer account it accesses, Aurora assumes
an IAM role **inside that account** using a unique **External ID** to prevent
confused-deputy attacks.

```
Aurora base credentials (sts:AssumeRole only)
  └─> sts:AssumeRole(RoleArn, ExternalId)
        └─> Temporary read-only credentials (1-hour sessions)
```

Each account's role is granted the AWS-managed **ReadOnlyAccess** policy, so
Aurora can read inventory data without ever making write calls.

---

## Prerequisites

| Requirement | Who Provides It |
|---|---|
| Aurora's AWS Account ID | Displayed on the Aurora AWS Onboarding page |
| External ID (UUID) | Auto-generated per Aurora workspace |
| CloudFormation template | Downloaded from Aurora |

---

## Step-by-Step

### 1. Download the CloudFormation Template

1. Log in to Aurora and navigate to **Connectors > AWS**.
2. Click **Download CloudFormation Template**.  The template has your
   workspace's External ID and Aurora's account ID pre-filled.

The template creates a single IAM role (`AuroraReadOnlyRole` by default) with:

- Trust policy allowing Aurora's account to assume it, only with the correct
  External ID.
- The `ReadOnlyAccess` AWS-managed policy attached.

### 2. Deploy to a Single Account

If you have one or a few accounts, deploy the template manually:

```bash
aws cloudformation deploy \
  --template-file aurora-cross-account-role.yaml \
  --stack-name aurora-access \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      AuroraAccountId=<AURORA_ACCOUNT_ID> \
      ExternalId=<YOUR_EXTERNAL_ID> \
  --region us-east-1
```

### 3. Deploy Across an Organization (StackSets)

If you use **AWS Organizations** or **Control Tower**, deploy to all member
accounts at once using CloudFormation StackSets:

```bash
# Create the StackSet (run from the management/delegated-admin account)
aws cloudformation create-stack-set \
  --stack-set-name aurora-access \
  --template-body file://aurora-cross-account-role.yaml \
  --parameters \
      ParameterKey=AuroraAccountId,ParameterValue=<AURORA_ACCOUNT_ID> \
      ParameterKey=ExternalId,ParameterValue=<YOUR_EXTERNAL_ID> \
  --capabilities CAPABILITY_NAMED_IAM \
  --permission-model SERVICE_MANAGED \
  --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false

# Deploy to all accounts in the organization
aws cloudformation create-stack-instances \
  --stack-set-name aurora-access \
  --deployment-targets OrganizationalUnitIds=<ROOT_OU_ID> \
  --regions us-east-1 \
  --operation-preferences MaxConcurrentPercentage=100,FailureTolerancePercentage=10
```

With `--auto-deployment Enabled=true`, any new accounts added to the OU will
automatically get the Aurora role.

If you use **Control Tower**, you can alternatively register the template as a
Service Catalog product distributed via Account Factory.

### 4. Register Accounts in Aurora

After the roles are created in your AWS accounts:

1. Go to the Aurora AWS Onboarding page.
2. Click **Bulk Register Accounts**.
3. Paste your account IDs, one per line:

```
123456789012,us-east-1
234567890123,eu-west-1
345678901234,us-west-2
```

Format: `ACCOUNT_ID,REGION` (region defaults to `us-east-1` if omitted).  You
can also specify a custom role name as a third field if you changed the
default: `ACCOUNT_ID,REGION,ROLE_NAME`.

Aurora validates each account by attempting `sts:AssumeRole`.  Successfully
validated accounts are connected immediately; failed accounts show the error
inline so you can investigate.

### 5. Verify

After bulk registration:

- The connected accounts table on the onboarding page shows each account's
  status.
- Discovery automatically fans out across all connected accounts.
- Chat commands also fan out; results are tagged with the source account ID.

---

## What ReadOnlyAccess Covers

The AWS-managed `ReadOnlyAccess` policy grants `Describe*`, `Get*`, `List*`,
and `BatchGet*` actions across nearly all AWS services.  Key inclusions:

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
| **External ID** | A UUID v4 unique to your Aurora workspace.  Prevents other Aurora tenants from assuming your role (confused-deputy protection). |
| **No write access** | The role only has `ReadOnlyAccess`.  Aurora's session policy can further restrict in read-only mode. |
| **Short-lived credentials** | STS sessions last at most 1 hour.  Aurora proactively refreshes them before expiry. |
| **Per-account isolation** | Each AWS account has its own role.  Compromising one role does not affect other accounts. |
| **Audit trail** | Every `AssumeRole` call is logged in the target account's CloudTrail.  Session names include `aurora-<workspace_id>` for traceability. |

---

## Disconnecting

- **Single account**: Click the trash icon next to the account in the connected
  accounts table.  This removes Aurora's connection; the IAM role still exists
  in your AWS account until you delete the CloudFormation stack.
- **All accounts**: Click **Disconnect All**.

To fully remove access, delete the CloudFormation stack (or StackSet) in your
AWS accounts.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Access denied" on bulk register | Role not yet created, or External ID mismatch | Verify the CFN stack deployed successfully and uses the correct External ID |
| Some accounts fail, others succeed | IAM role propagation delay | Wait 5 minutes and retry the failed accounts |
| Discovery finds no resources | Resource Explorer not enabled | Run `aws resource-explorer-2 create-index --type AGGREGATOR` in your primary region |
| Template deploy fails | `CAPABILITY_NAMED_IAM` not specified | Add `--capabilities CAPABILITY_NAMED_IAM` to your deploy command |
