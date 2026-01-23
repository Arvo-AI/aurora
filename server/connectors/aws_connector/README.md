# AWS Connector

IAM Role with External ID for cross-account access.

## Setup

### 1. Create IAM Role in AWS

1. Go to [IAM > Roles](https://console.aws.amazon.com/iam/home#/roles) > **Create role**
2. Trusted entity: **AWS account** > **Another AWS account**
   - Account ID: Aurora's AWS Account ID (displayed in onboarding UI)
   - Check **Require external ID** and enter the External ID (displayed in onboarding UI)
3. Attach policy: `PowerUserAccess` (or `ReadOnlyAccess` for read-only)
4. Name: `AuroraRole`
5. Copy the **Role ARN** after creation

### 2. Configure `.env`

```bash
AWS_ACCESS_KEY_ID=your-aurora-aws-access-key
AWS_SECRET_ACCESS_KEY=your-aurora-aws-secret-key
AWS_DEFAULT_REGION=us-east-1
```

> These are Aurora's own AWS credentials for STS calls, not the user's account credentials.

## Troubleshooting

**"Aurora cannot assume this role"** — Verify the trust policy has correct Aurora Account ID and External ID

**"Unable to determine Aurora's AWS account ID"** — Set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in `.env`
