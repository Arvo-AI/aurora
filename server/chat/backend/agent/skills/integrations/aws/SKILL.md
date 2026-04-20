---
name: aws
id: aws
description: "AWS integration for managing EC2, RDS, S3, Lambda, EKS, CloudWatch, IAM, and other services via CLI and Terraform"
category: cloud_provider
connection_check:
  method: provider_in_preference
tools:
  - cloud_exec
  - iac_tool
index: "AWS — EC2, RDS, S3, Lambda, EKS, CloudWatch, IAM, Terraform IaC"
rca_priority: 10
allowed-tools: cloud_exec, iac_tool
metadata:
  author: aurora
  version: "1.0"
---

# AWS Integration

## Overview
Amazon Web Services cloud provider for managing compute, storage, databases, containers, serverless, networking, and observability.

## Instructions

### CLI COMMANDS (use cloud_exec with 'aws')

**CRITICAL: Always use cloud_exec('aws', 'COMMAND') — NOT terminal_exec!**
Authentication and credentials are auto-configured. The `aws` CLI is available.
Additional CLIs: `kubectl`, `eksctl`, `sam`, `cdk`, `helm`, `terraform`.

**MULTI-ACCOUNT SUPPORT:**
- First call without `account_id` fans out to ALL connected accounts and returns `results_by_account`.
- Identify the relevant account from the results.
- ALL subsequent calls MUST include `account_id='<ACCOUNT_ID>'` to target that account.
- Never keep querying all accounts after you know which one matters.

**Discovery Commands:**
- Caller identity: `cloud_exec('aws', 'sts get-caller-identity')`
- List regions: `cloud_exec('aws', 'ec2 describe-regions --output table', account_id='<ACCOUNT_ID>')`
- List services: `cloud_exec('aws', 'service-quotas list-services', account_id='<ACCOUNT_ID>')`

**EC2 (Compute):**
- List instances: `cloud_exec('aws', 'ec2 describe-instances --query "Reservations[].Instances[].{ID:InstanceId,Type:InstanceType,State:State.Name,Name:Tags[?Key==`Name`].Value|[0]}" --output table', account_id='<ACCOUNT_ID>')`
- Start/stop: `cloud_exec('aws', 'ec2 start-instances --instance-ids <ID>', account_id='<ACCOUNT_ID>')`
- Describe: `cloud_exec('aws', 'ec2 describe-instances --instance-ids <ID>', account_id='<ACCOUNT_ID>')`
- Security groups: `cloud_exec('aws', 'ec2 describe-security-groups --group-ids <SG_ID>', account_id='<ACCOUNT_ID>')`

**EKS (Kubernetes):**
- List clusters: `cloud_exec('aws', 'eks list-clusters', account_id='<ACCOUNT_ID>')`
- Describe cluster: `cloud_exec('aws', 'eks describe-cluster --name <NAME>', account_id='<ACCOUNT_ID>')`
- Get kubeconfig: `cloud_exec('aws', 'eks update-kubeconfig --name <NAME> --region <REGION>', account_id='<ACCOUNT_ID>')`
- Then kubectl: `cloud_exec('aws', 'kubectl get pods -n <NAMESPACE> -o wide', account_id='<ACCOUNT_ID>')`

**S3 (Storage):**
- List buckets: `cloud_exec('aws', 's3 ls', account_id='<ACCOUNT_ID>')`
- List objects: `cloud_exec('aws', 's3 ls s3://<BUCKET>/', account_id='<ACCOUNT_ID>')`
- Copy: `cloud_exec('aws', 's3 cp <SRC> <DST>', account_id='<ACCOUNT_ID>')`

**RDS (Databases):**
- List instances: `cloud_exec('aws', 'rds describe-db-instances --query "DBInstances[].{ID:DBInstanceIdentifier,Engine:Engine,Status:DBInstanceStatus}" --output table', account_id='<ACCOUNT_ID>')`
- Describe: `cloud_exec('aws', 'rds describe-db-instances --db-instance-identifier <ID>', account_id='<ACCOUNT_ID>')`

**Lambda (Serverless):**
- List functions: `cloud_exec('aws', 'lambda list-functions --query "Functions[].{Name:FunctionName,Runtime:Runtime}" --output table', account_id='<ACCOUNT_ID>')`
- Invoke: `cloud_exec('aws', 'lambda invoke --function-name <NAME> /dev/stdout', account_id='<ACCOUNT_ID>')`
- Get logs: `cloud_exec('aws', 'logs filter-log-events --log-group-name /aws/lambda/<NAME> --start-time <EPOCH_MS> --limit 50', account_id='<ACCOUNT_ID>')`

**CloudWatch (Monitoring):**
- Query logs: `cloud_exec('aws', 'logs filter-log-events --log-group-name <GROUP> --start-time <EPOCH_MS> --filter-pattern "<PATTERN>"', account_id='<ACCOUNT_ID>')`
- List log groups: `cloud_exec('aws', 'logs describe-log-groups', account_id='<ACCOUNT_ID>')`
- Get metrics: `cloud_exec('aws', 'cloudwatch get-metric-statistics --namespace <NS> --metric-name <METRIC> --dimensions Name=<DIM>,Value=<VAL> --start-time <ISO> --end-time <ISO> --period 300 --statistics Average', account_id='<ACCOUNT_ID>')`
- List alarms: `cloud_exec('aws', 'cloudwatch describe-alarms --state-value ALARM', account_id='<ACCOUNT_ID>')`

**IAM:**
- List roles: `cloud_exec('aws', 'iam list-roles --query "Roles[].{Name:RoleName,Arn:Arn}" --output table', account_id='<ACCOUNT_ID>')`
- Get role policy: `cloud_exec('aws', 'iam list-attached-role-policies --role-name <ROLE>', account_id='<ACCOUNT_ID>')`

**Networking:**
- List VPCs: `cloud_exec('aws', 'ec2 describe-vpcs --output table', account_id='<ACCOUNT_ID>')`
- List subnets: `cloud_exec('aws', 'ec2 describe-subnets --filters "Name=vpc-id,Values=<VPC_ID>" --output table', account_id='<ACCOUNT_ID>')`
- List load balancers: `cloud_exec('aws', 'elbv2 describe-load-balancers --output table', account_id='<ACCOUNT_ID>')`
- Route tables: `cloud_exec('aws', 'ec2 describe-route-tables --filters "Name=vpc-id,Values=<VPC_ID>"', account_id='<ACCOUNT_ID>')`

### TERRAFORM FOR AWS
Use iac_tool — provider.tf is AUTO-GENERATED, just write the resource!

**PREREQUISITE:** Get account ID first:
`cloud_exec('aws', "sts get-caller-identity --query 'Account' --output text", account_id='<ACCOUNT_ID>')`

**EC2 INSTANCE EXAMPLE:**
```hcl
resource "aws_instance" "vm" {
  ami           = "ami-0c55b159cbfafe1f0"
  instance_type = "t3.micro"
  subnet_id     = "<SUBNET_ID>"

  tags = {
    Name = "my-vm"
  }
}
```

**EKS CLUSTER:**
```hcl
resource "aws_eks_cluster" "cluster" {
  name     = "my-cluster"
  role_arn = "<EKS_ROLE_ARN>"

  vpc_config {
    subnet_ids = ["<SUBNET_1>", "<SUBNET_2>"]
  }
}
```

**S3 BUCKET:**
```hcl
resource "aws_s3_bucket" "bucket" {
  bucket = "my-bucket-unique-name"
}
```

**RDS INSTANCE:**
```hcl
resource "aws_db_instance" "db" {
  allocated_storage    = 20
  engine               = "postgres"
  engine_version       = "15"
  instance_class       = "db.t3.micro"
  db_name              = "mydb"
  username             = "admin"
  manage_master_user_password = true
  skip_final_snapshot  = true
}
```

**Common AWS Terraform resources:**
- `aws_instance` — EC2 virtual machines
- `aws_security_group` — Firewall rules
- `aws_eks_cluster`, `aws_eks_node_group` — Kubernetes
- `aws_s3_bucket` — Object storage
- `aws_db_instance` — RDS databases
- `aws_lambda_function` — Serverless functions
- `aws_vpc`, `aws_subnet` — Networking
- `aws_lb`, `aws_lb_target_group` — Load balancers
- `aws_iam_role`, `aws_iam_policy` — IAM

DO NOT write terraform{} or provider{} blocks — they are auto-generated!

### CRITICAL RULES
- ALWAYS target a specific account_id after the first fan-out call
- Use `--output table` or `--query` with JMESPath for readable output
- AMI IDs are region-specific — look them up or use data sources
- Default region: us-east-1 unless user specifies otherwise
- For EKS: always run `update-kubeconfig` before kubectl commands
- Get real values (VPC IDs, subnet IDs, AMI IDs) from CLI before writing Terraform

### ON ANY AWS ERROR
1. Permission denied → Check IAM role/policy: `cloud_exec('aws', 'iam get-role --role-name <ROLE>', account_id='<ACCOUNT_ID>')`
2. Service not enabled → Not applicable for AWS (services are always available)
3. CLI syntax error → Use `cloud_exec('aws', '<SERVICE> help')` to check correct subcommand
4. Terraform failure → Run `cloud_exec('aws', ...)` to verify resources exist, then fix the manifest
