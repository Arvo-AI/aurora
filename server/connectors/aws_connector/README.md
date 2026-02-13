# AWS Connector

IAM Role with External ID for cross-account access.

## Prerequisites

Before setting up AWS integration, you need to configure Aurora's own AWS credentials. These credentials are used by Aurora to assume roles in your AWS account via STS (Security Token Service).

## Setup

### Step 1: Create an IAM User for Aurora

Aurora needs AWS credentials to call STS AssumeRole. Create an IAM user with the following policy:

1. Go to [IAM > Users](https://console.aws.amazon.com/iam/home#/users) > **Create user**
2. Name: `aurora-service-user` (or any name you prefer)
3. **Do not** enable console access (Aurora only needs programmatic access)
4. After creating the user, attach the following policy:

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

This policy allows Aurora to assume roles in any AWS account. The user only needs `sts:AssumeRole` permission - Aurora will inherit the permissions of the roles it assumes.

### Step 2: Create Access Keys

1. Go to the user you just created
2. Click on the **Security credentials** tab
3. Scroll down to **Access keys** section
4. Click **Create access key**
5. Select **Application running outside AWS** as the use case
6. Click **Next** and optionally add a description
7. Click **Create access key**
8. **Important**: Copy both the **Access key ID** and **Secret access key** immediately - you won't be able to see the secret key again!

### Step 3: Configure Aurora Environment

Add the credentials to your Aurora `.env` file:

```bash
AWS_ACCESS_KEY_ID=your-access-key-id-here
AWS_SECRET_ACCESS_KEY=your-secret-access-key-here
AWS_DEFAULT_REGION=us-east-1
```

After adding these credentials, **rebuild and restart Aurora**:

```bash
make down
make dev-build  # or make prod-local for production (build from source)
make dev        # or make prod-prebuilt / make prod for production (prebuilt images)
```

### Step 4: Create IAM Role in Your AWS Account

Now that Aurora has credentials configured, you can create the role that Aurora will assume:

1. Go to [IAM > Roles](https://console.aws.amazon.com/iam/home#/roles) > **Create role**
2. Trusted entity: **AWS account** > **Another AWS account**
   - Account ID: Aurora's AWS Account ID (this will be displayed in the onboarding UI after credentials are configured)
   - Check **Require external ID** and enter the External ID (displayed in onboarding UI)
3. Attach permission policies to the role:
   - For full access: `PowerUserAccess` (recommended for most use cases)
   - For read-only: `ReadOnlyAccess`
   - Or create custom policies for specific permissions
4. Name: `AuroraRole` (or any name you prefer)
5. Copy the **Role ARN** after creation (format: `arn:aws:iam::123456789012:role/AuroraRole`)

### Step 5: Complete Onboarding in Aurora UI

1. Navigate to the AWS onboarding page in Aurora
2. The UI will display:
   - Your External ID (unique to your workspace)
   - Aurora's AWS Account ID (dynamically retrieved from the credentials you configured)
   - A trust policy template with the correct Account ID
3. Copy the trust policy and use it when creating your IAM role (or use the AWS console wizard as described in Step 4)
4. Enter your Role ARN in the onboarding form
5. Click **Connect AWS Account**

> **Important**: After creating or updating an IAM role in AWS, changes can take **up to 5 minutes** to propagate across AWS services. If role assumption fails immediately after creating or updating the role, wait a few minutes and try again. This is normal AWS behavior and not an error with Aurora.

## How It Works

1. **Aurora's credentials** (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`) are used to authenticate with AWS STS
2. Aurora calls `sts:AssumeRole` with:
   - Your role ARN
   - The External ID (for security)
   - A session name
3. AWS returns temporary credentials that Aurora uses to access your AWS resources
4. These temporary credentials are cached and refreshed automatically

## Security Best Practices

- **External ID**: The External ID prevents the "confused deputy" problem - it ensures that only Aurora (with the correct External ID) can assume your role
- **Least Privilege**: Attach only the permissions your role needs. Aurora will inherit these permissions when it assumes the role
- **Role Permissions**: The role you create can have any permissions you want Aurora to have. Common choices:
  - `PowerUserAccess`: Full access except IAM management
  - `ReadOnlyAccess`: Read-only access to all services
  - Custom policies: Specific permissions for your use case

## Troubleshooting

**"Aurora cannot assume this role"**
- **IAM propagation delay**: AWS IAM changes (including role creation and trust policy updates) can take up to 5 minutes to propagate. If you just created or updated the role, wait a few minutes and try again before troubleshooting further.
- Verify the trust policy has the correct Aurora Account ID and External ID
- Ensure the role exists in your AWS account
- Check that the External ID matches exactly (case-sensitive)

**"Unable to determine Aurora's AWS account ID"**
- Ensure `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are set in `.env`
- Verify the credentials are valid by testing them with AWS CLI: `aws sts get-caller-identity`
- Rebuild and restart Aurora after adding credentials

**"Access denied when assuming role"**
- Verify the IAM user has `sts:AssumeRole` permission
- Check that the role's trust policy allows Aurora's account ID
- Ensure the External ID in the trust policy matches the one shown in the UI

## Account ID Detection

Aurora automatically detects its AWS account ID by calling `sts:get-caller-identity` using the configured credentials. This account ID is displayed in the onboarding UI and used in the trust policy template. The account ID is retrieved dynamically, so you don't need to configure it manually.
