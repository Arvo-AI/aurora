#
# Aurora AWS Setup Script (PowerShell)
# This script creates an IAM user with the necessary permissions for Aurora
#

$ErrorActionPreference = "Stop"

# Color functions for output
function Write-ColorOutput($ForegroundColor) {
    $fc = $host.ui.RawUI.ForegroundColor
    $host.ui.RawUI.ForegroundColor = $ForegroundColor
    if ($args) {
        Write-Output $args
    }
    $host.ui.RawUI.ForegroundColor = $fc
}

Write-Host "======================================" -ForegroundColor Blue
Write-Host "Aurora AWS Setup Script" -ForegroundColor Blue
Write-Host "======================================" -ForegroundColor Blue
Write-Host

# Function to check if AWS CLI is installed and configured
function Test-AWSCLIConfiguration {
    Write-Host "Checking AWS CLI configuration..." -ForegroundColor Yellow
    
    # Check if AWS CLI is installed
    try {
        $awsVersion = aws --version 2>&1
        if (-not $?) {
            throw "AWS CLI not found"
        }
    }
    catch {
        Write-Host "Error: AWS CLI is not installed." -ForegroundColor Red
        Write-Host "Please install the AWS CLI first: https://aws.amazon.com/cli/"
        exit 1
    }

    # Check if AWS credentials are configured
    try {
        $callerIdentity = aws sts get-caller-identity 2>&1 | ConvertFrom-Json
        if (-not $callerIdentity.Account) {
            throw "Not configured"
        }
    }
    catch {
        Write-Host "Error: AWS CLI is not configured." -ForegroundColor Red
        Write-Host "Please run 'aws configure' to set up your credentials."
        exit 1
    }
}

# Function to generate a random suffix for unique names
function Get-RandomSuffix {
    $randomBytes = New-Object byte[] 4
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($randomBytes)
    return [System.BitConverter]::ToString($randomBytes).Replace("-", "").ToLower()
}

# Main setup function
function Start-AuroraSetup {
    Test-AWSCLIConfiguration

    # Get AWS account ID
    $accountId = (aws sts get-caller-identity --query Account --output text).Trim()
    Write-Host "✓ AWS Account ID: $accountId" -ForegroundColor Green

    # Set variables
    $suffix = Get-RandomSuffix
    $username = "aurora-user-$suffix"
    $policyName = "AuroraManagerPolicy-$suffix"

    Write-Host
    Write-Host "Creating IAM user: $username" -ForegroundColor Yellow

    # Create IAM user
    try {
        aws iam create-user --user-name $username 2>&1 | Out-Null
        Write-Host "✓ IAM user created successfully" -ForegroundColor Green
    }
    catch {
        Write-Host "✗ Failed to create IAM user" -ForegroundColor Red
        exit 1
    }

    # Create the policy document
    Write-Host "Creating IAM policy..." -ForegroundColor Yellow
    
    $policyDocument = @'
{
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
}
'@

    # Save policy to temporary file
    $tempPolicyFile = [System.IO.Path]::GetTempFileName()
    $policyDocument | Out-File -FilePath $tempPolicyFile -Encoding UTF8

    # Create the policy
    try {
        $policyArn = (aws iam create-policy `
            --policy-name $policyName `
            --policy-document "file://$tempPolicyFile" `
            --description "Aurora Manager Policy with comprehensive permissions" `
            --query 'Policy.Arn' `
            --output text).Trim()
        
        Write-Host "✓ IAM policy created successfully" -ForegroundColor Green
    }
    catch {
        Write-Host "✗ Failed to create IAM policy" -ForegroundColor Red
        # Clean up
        aws iam delete-user --user-name $username 2>&1 | Out-Null
        Remove-Item $tempPolicyFile -Force
        exit 1
    }

    # Clean up temp file
    Remove-Item $tempPolicyFile -Force

    # Attach policy to user
    Write-Host "Attaching policy to user..." -ForegroundColor Yellow
    try {
        aws iam attach-user-policy `
            --user-name $username `
            --policy-arn $policyArn 2>&1 | Out-Null
        Write-Host "✓ Policy attached successfully" -ForegroundColor Green
    }
    catch {
        Write-Host "✗ Failed to attach policy" -ForegroundColor Red
        # Clean up
        aws iam delete-policy --policy-arn $policyArn 2>&1 | Out-Null
        aws iam delete-user --user-name $username 2>&1 | Out-Null
        exit 1
    }

    # Create access key
    Write-Host "Creating access keys..." -ForegroundColor Yellow
    try {
        $accessKeyOutput = aws iam create-access-key --user-name $username --output json | ConvertFrom-Json
        Write-Host "✓ Access keys created successfully" -ForegroundColor Green
    }
    catch {
        Write-Host "✗ Failed to create access keys" -ForegroundColor Red
        # Clean up
        aws iam detach-user-policy --user-name $username --policy-arn $policyArn 2>&1 | Out-Null
        aws iam delete-policy --policy-arn $policyArn 2>&1 | Out-Null
        aws iam delete-user --user-name $username 2>&1 | Out-Null
        exit 1
    }

    # Extract credentials
    $accessKeyId = $accessKeyOutput.AccessKey.AccessKeyId
    $secretAccessKey = $accessKeyOutput.AccessKey.SecretAccessKey

    # Get default region
    $defaultRegion = aws configure get region 2>$null
    if (-not $defaultRegion) {
        $defaultRegion = "us-east-1"
    }

    Write-Host
    Write-Host "======================================" -ForegroundColor Green
    Write-Host "✓ Aurora AWS setup completed successfully!" -ForegroundColor Green
    Write-Host "======================================" -ForegroundColor Green
    Write-Host

    # Output credentials in JSON format
    Write-Host "Copy the JSON below and paste it into Aurora:" -ForegroundColor Yellow
    Write-Host
    Write-Host "{" -ForegroundColor Blue
    Write-Host "  `"aws_access_key_id`": `"$accessKeyId`"," -ForegroundColor Blue
    Write-Host "  `"aws_secret_access_key`": `"$secretAccessKey`"," -ForegroundColor Blue
    Write-Host "  `"aws_account_id`": `"$accountId`"," -ForegroundColor Blue
    Write-Host "  `"default_region`": `"$defaultRegion`"" -ForegroundColor Blue
    Write-Host "}" -ForegroundColor Blue
    Write-Host

    # Save cleanup instructions
    Write-Host "To revoke Aurora's access later, run:" -ForegroundColor Yellow
    Write-Host "aws iam detach-user-policy --user-name $username --policy-arn $policyArn"
    Write-Host "aws iam delete-access-key --user-name $username --access-key-id $accessKeyId"
    Write-Host "aws iam delete-user --user-name $username"
    Write-Host "aws iam delete-policy --policy-arn $policyArn"
    Write-Host

    Write-Host "Setup complete! Return to Aurora and paste the JSON credentials." -ForegroundColor Green
}

# Run main function
Start-AuroraSetup 