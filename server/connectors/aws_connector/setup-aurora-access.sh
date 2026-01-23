#!/bin/bash

#
# Aurora AWS Setup Script
# This script creates an IAM user with the necessary permissions for Aurora
#

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}Aurora AWS Setup Script${NC}"
echo -e "${BLUE}======================================${NC}"
echo

# Function to check if AWS CLI is installed and configured
check_aws_cli() {
    if ! command -v aws &> /dev/null; then
        echo -e "${RED}Error: AWS CLI is not installed.${NC}"
        echo "Please install the AWS CLI first: https://aws.amazon.com/cli/"
        exit 1
    fi

    # Check if AWS credentials are configured
    if ! aws sts get-caller-identity &> /dev/null; then
        echo -e "${RED}Error: AWS CLI is not configured.${NC}"
        echo "Please run 'aws configure' to set up your credentials."
        exit 1
    fi
}

# Function to generate a random suffix for unique names
generate_suffix() {
    echo $(date +%s%N | md5sum | head -c 6)
}

# Main setup function
main() {
    echo -e "${YELLOW}Checking AWS CLI configuration...${NC}"
    check_aws_cli

    # Get AWS account ID
    ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
    echo -e "${GREEN}✓ AWS Account ID: ${ACCOUNT_ID}${NC}"

    # Set variables
    USERNAME="aurora-user-$(generate_suffix)"
    POLICY_NAME="AuroraManagerPolicy-$(generate_suffix)"

    echo
    echo -e "${YELLOW}Creating IAM user: ${USERNAME}${NC}"

    # Create IAM user
    if aws iam create-user --user-name "$USERNAME" &> /dev/null; then
        echo -e "${GREEN}✓ IAM user created successfully${NC}"
    else
        echo -e "${RED}✗ Failed to create IAM user${NC}"
        exit 1
    fi

    # Create the policy document
    echo -e "${YELLOW}Creating IAM policy...${NC}"
    
    POLICY_DOCUMENT='{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EKSFullAccess",
      "Effect": "Allow",
      "Action": "eks:*",
      "Resource": "*"
    },
    {
      "Sid": "LambdaAndServerlessAccess",
      "Effect": "Allow",
      "Action": [
        "lambda:*",
        "apigateway:*",
        "logs:*",
        "states:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ECSAndContainerAccess",
      "Effect": "Allow",
      "Action": [
        "ecs:*",
        "ecr:*",
        "ec2:*",
        "elasticloadbalancing:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IAMRoleManagement",
      "Effect": "Allow",
      "Action": [
        "iam:*",
        "sts:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ComputeAndProvisioning",
      "Effect": "Allow",
      "Action": [
        "cloudformation:*",
        "autoscaling:*",
        "application-autoscaling:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "StorageAndDataAccess",
      "Effect": "Allow",
      "Action": [
        "s3:*",
        "glue:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "RDSAccess",
      "Effect": "Allow",
      "Action": "rds:*",
      "Resource": "*"
    },
    {
      "Sid": "BillingAndCostOptimization",
      "Effect": "Allow",
      "Action": [
        "ce:*",
        "cur:*",
        "budgets:*",
        "aws-portal:*",
        "servicequotas:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "MonitoringLoggingAndEvents",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:*",
        "logs:*",
        "events:*",
        "xray:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SecurityAndCompliance",
      "Effect": "Allow",
      "Action": [
        "guardduty:*",
        "inspector2:*",
        "securityhub:*",
        "kms:*",
        "secretsmanager:*",
        "config:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CIandDeployment",
      "Effect": "Allow",
      "Action": [
        "codebuild:*",
        "codepipeline:*",
        "codedeploy:*",
        "codecommit:*",
        "ecr:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SystemsManagerAndAutomation",
      "Effect": "Allow",
      "Action": [
        "ssm:*",
        "ssm-contacts:*",
        "ssm-incidents:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ApplicationIntegration",
      "Effect": "Allow",
      "Action": [
        "sns:*",
        "sqs:*",
        "events:*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "MachineLearning",
      "Effect": "Allow",
      "Action": [
        "sagemaker:*"
      ],
      "Resource": "*"
    }
  ]
}'

    # Save policy to temporary file
    TEMP_POLICY_FILE="/tmp/aurora-policy-$$.json"
    echo "$POLICY_DOCUMENT" > "$TEMP_POLICY_FILE"

    # Create the policy
    POLICY_ARN=$(aws iam create-policy \
        --policy-name "$POLICY_NAME" \
        --policy-document "file://$TEMP_POLICY_FILE" \
        --description "Aurora Manager Policy with comprehensive permissions" \
        --query 'Policy.Arn' \
        --output text 2>/dev/null)

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓ IAM policy created successfully${NC}"
    else
        echo -e "${RED}✗ Failed to create IAM policy${NC}"
        # Clean up
        aws iam delete-user --user-name "$USERNAME" &> /dev/null
        rm -f "$TEMP_POLICY_FILE"
        exit 1
    fi

    # Clean up temp file
    rm -f "$TEMP_POLICY_FILE"

    # Attach policy to user
    echo -e "${YELLOW}Attaching policy to user...${NC}"
    if aws iam attach-user-policy \
        --user-name "$USERNAME" \
        --policy-arn "$POLICY_ARN" &> /dev/null; then
        echo -e "${GREEN}✓ Policy attached successfully${NC}"
    else
        echo -e "${RED}✗ Failed to attach policy${NC}"
        # Clean up
        aws iam delete-policy --policy-arn "$POLICY_ARN" &> /dev/null
        aws iam delete-user --user-name "$USERNAME" &> /dev/null
        exit 1
    fi

    # Create access key
    echo -e "${YELLOW}Creating access keys...${NC}"
    ACCESS_KEY_OUTPUT=$(aws iam create-access-key --user-name "$USERNAME" --output json)

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓ Access keys created successfully${NC}"
    else
        echo -e "${RED}✗ Failed to create access keys${NC}"
        # Clean up
        aws iam detach-user-policy --user-name "$USERNAME" --policy-arn "$POLICY_ARN" &> /dev/null
        aws iam delete-policy --policy-arn "$POLICY_ARN" &> /dev/null
        aws iam delete-user --user-name "$USERNAME" &> /dev/null
        exit 1
    fi

    # Extract credentials
    ACCESS_KEY_ID=$(echo "$ACCESS_KEY_OUTPUT" | jq -r '.AccessKey.AccessKeyId')
    SECRET_ACCESS_KEY=$(echo "$ACCESS_KEY_OUTPUT" | jq -r '.AccessKey.SecretAccessKey')

    # Get default region
    DEFAULT_REGION=$(aws configure get region 2>/dev/null || echo "us-east-1")

    echo
    echo -e "${GREEN}======================================${NC}"
    echo -e "${GREEN}✓ Aurora AWS setup completed successfully!${NC}"
    echo -e "${GREEN}======================================${NC}"
    echo

    # Output credentials in JSON format
    echo -e "${YELLOW}Copy the JSON below and paste it into Aurora:${NC}"
    echo
    echo -e "${BLUE}{${NC}"
    echo -e "${BLUE}  \"aws_access_key_id\": \"${ACCESS_KEY_ID}\",${NC}"
    echo -e "${BLUE}  \"aws_secret_access_key\": \"${SECRET_ACCESS_KEY}\",${NC}"
    echo -e "${BLUE}  \"aws_account_id\": \"${ACCOUNT_ID}\",${NC}"
    echo -e "${BLUE}  \"default_region\": \"${DEFAULT_REGION}\"${NC}"
    echo -e "${BLUE}}${NC}"
    echo

    # Save cleanup instructions
    echo -e "${YELLOW}To revoke Aurora's access later, run:${NC}"
    echo "aws iam detach-user-policy --user-name $USERNAME --policy-arn $POLICY_ARN"
    echo "aws iam delete-access-key --user-name $USERNAME --access-key-id $ACCESS_KEY_ID"
    echo "aws iam delete-user --user-name $USERNAME"
    echo "aws iam delete-policy --policy-arn $POLICY_ARN"
    echo

    echo -e "${GREEN}Setup complete! Return to Aurora and paste the JSON credentials.${NC}"
}

# Run main function
main 