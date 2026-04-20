---
name: aws
id: aws
description: "AWS integration — EC2, EKS, RDS, S3, Lambda, CloudWatch, IAM, VPC, ELB via CLI"
category: cloud_provider
connection_check:
  method: provider_in_preference
tools:
  - cloud_exec
index: "AWS — EC2, EKS, RDS, S3, Lambda, CloudWatch, IAM, VPC"
rca_priority: 10
allowed-tools: cloud_exec
metadata:
  author: aurora
  version: "2.0"
---

# AWS Integration

## Overview
Full Amazon Web Services access via `cloud_exec('aws', 'COMMAND')`.
Available CLIs: `aws`, `kubectl`, `eksctl`, `helm`.
Authentication is automatic — never ask users for credentials.

## Multi-Account Support (CRITICAL)
- First `cloud_exec('aws', ...)` call (without `account_id`) fans out to ALL connected accounts and returns `results_by_account`.
- Inspect results to identify which account(s) are relevant.
- ALL subsequent calls MUST include `account_id='<ACCOUNT_ID>'`.
- NEVER keep querying all accounts after you've identified the right one.

```python
# Step 1: fan-out discovery
cloud_exec('aws', 'ec2 describe-instances --query "Reservations[].Instances[].{ID:InstanceId,State:State.Name}" --output json')
# Step 2: target specific account
cloud_exec('aws', 'ec2 describe-instances --instance-ids i-abc123', account_id='123456789012')
```

## CLI Reference

### Identity & Discovery
```python
cloud_exec('aws', 'sts get-caller-identity', account_id='<ACCT>')
cloud_exec('aws', 'ec2 describe-regions --output table', account_id='<ACCT>')
cloud_exec('aws', 'organizations describe-account --account-id <ACCT>', account_id='<ACCT>')
```

### EC2 (Compute)
```python
cloud_exec('aws', 'ec2 describe-instances --query "Reservations[].Instances[].{ID:InstanceId,Type:InstanceType,State:State.Name,Name:Tags[?Key==`Name`].Value|[0],AZ:Placement.AvailabilityZone}" --output table', account_id='<ACCT>')
cloud_exec('aws', 'ec2 describe-instances --instance-ids <ID> --output json', account_id='<ACCT>')
cloud_exec('aws', 'ec2 start-instances --instance-ids <ID>', account_id='<ACCT>')
cloud_exec('aws', 'ec2 stop-instances --instance-ids <ID>', account_id='<ACCT>')
cloud_exec('aws', 'ec2 terminate-instances --instance-ids <ID>', account_id='<ACCT>')
cloud_exec('aws', 'ec2 describe-instance-status --instance-ids <ID>', account_id='<ACCT>')
# Filter by tag:
cloud_exec('aws', 'ec2 describe-instances --filters "Name=tag:Environment,Values=production" --output table', account_id='<ACCT>')
# Filter by state:
cloud_exec('aws', 'ec2 describe-instances --filters "Name=instance-state-name,Values=running" --output table', account_id='<ACCT>')
```

### EKS (Kubernetes)
```python
cloud_exec('aws', 'eks list-clusters', account_id='<ACCT>')
cloud_exec('aws', 'eks describe-cluster --name <CLUSTER>', account_id='<ACCT>')
# MANDATORY before any kubectl: get kubeconfig
cloud_exec('aws', 'eks update-kubeconfig --name <CLUSTER> --region <REGION>', account_id='<ACCT>')
# Then kubectl works:
cloud_exec('aws', 'kubectl get pods -n <NS> -o wide', account_id='<ACCT>')
cloud_exec('aws', 'kubectl describe pod <POD> -n <NS>', account_id='<ACCT>')
cloud_exec('aws', 'kubectl logs <POD> -n <NS> --since=1h --tail=200', account_id='<ACCT>')
cloud_exec('aws', 'kubectl get events -n <NS> --sort-by=.lastTimestamp', account_id='<ACCT>')
cloud_exec('aws', 'kubectl top pods -n <NS>', account_id='<ACCT>')
cloud_exec('aws', 'kubectl get hpa -n <NS>', account_id='<ACCT>')
cloud_exec('aws', 'kubectl rollout history deployment/<DEPLOY> -n <NS>', account_id='<ACCT>')
# Node pool info:
cloud_exec('aws', 'eks list-nodegroups --cluster-name <CLUSTER>', account_id='<ACCT>')
cloud_exec('aws', 'eks describe-nodegroup --cluster-name <CLUSTER> --nodegroup-name <NG>', account_id='<ACCT>')
# Enable control plane logging:
cloud_exec('aws', 'eks update-cluster-config --name <CLUSTER> --logging \'{"clusterLogging": [{"types": ["api", "audit", "scheduler"], "enabled": true}]}\'', account_id='<ACCT>')
```

### S3 (Storage)
```python
cloud_exec('aws', 's3 ls', account_id='<ACCT>')
cloud_exec('aws', 's3 ls s3://<BUCKET>/ --recursive --summarize', account_id='<ACCT>')
cloud_exec('aws', 's3 cp <LOCAL> s3://<BUCKET>/<KEY>', account_id='<ACCT>')
cloud_exec('aws', 's3 rm s3://<BUCKET>/<KEY>', account_id='<ACCT>')
cloud_exec('aws', 's3api get-bucket-policy --bucket <BUCKET>', account_id='<ACCT>')
cloud_exec('aws', 's3api get-bucket-versioning --bucket <BUCKET>', account_id='<ACCT>')
```

### RDS (Databases)
```python
cloud_exec('aws', 'rds describe-db-instances --query "DBInstances[].{ID:DBInstanceIdentifier,Engine:Engine,Version:EngineVersion,Status:DBInstanceStatus,Class:DBInstanceClass,Storage:AllocatedStorage}" --output table', account_id='<ACCT>')
cloud_exec('aws', 'rds describe-db-instances --db-instance-identifier <ID>', account_id='<ACCT>')
cloud_exec('aws', 'rds describe-db-clusters --output table', account_id='<ACCT>')
cloud_exec('aws', 'rds describe-events --source-identifier <ID> --source-type db-instance --duration 1440', account_id='<ACCT>')
# Performance Insights:
cloud_exec('aws', 'pi get-resource-metrics --service-type RDS --identifier db-<RESOURCE_ID> --metric-queries "[{\"Metric\":\"db.load.avg\"}]" --start-time <ISO> --end-time <ISO> --period-in-seconds 300', account_id='<ACCT>')
```

### Lambda (Serverless)
```python
cloud_exec('aws', 'lambda list-functions --query "Functions[].{Name:FunctionName,Runtime:Runtime,Memory:MemorySize,Timeout:Timeout}" --output table', account_id='<ACCT>')
cloud_exec('aws', 'lambda get-function --function-name <NAME>', account_id='<ACCT>')
cloud_exec('aws', 'lambda invoke --function-name <NAME> --payload \'{"key":"value"}\' /dev/stdout', account_id='<ACCT>')
cloud_exec('aws', 'lambda get-function-configuration --function-name <NAME>', account_id='<ACCT>')
cloud_exec('aws', 'lambda list-event-source-mappings --function-name <NAME>', account_id='<ACCT>')
```

### CloudWatch Logs
```python
# List log groups:
cloud_exec('aws', 'logs describe-log-groups --query "logGroups[].{Name:logGroupName,Stored:storedBytes}" --output table', account_id='<ACCT>')
# Filter log events (simple):
cloud_exec('aws', 'logs filter-log-events --log-group-name <GROUP> --start-time <EPOCH_MS> --filter-pattern "ERROR" --limit 50', account_id='<ACCT>')
# Tail recent logs:
cloud_exec('aws', 'logs tail <GROUP> --since 1h --format short', account_id='<ACCT>')
```

### CloudWatch Logs Insights (PREFERRED for complex queries)
Use `start-query` + `get-query-results` for powerful log analysis:
```python
# Start an Insights query (returns queryId):
cloud_exec('aws', 'logs start-query --log-group-name <GROUP> --start-time <EPOCH_SEC> --end-time <EPOCH_SEC> --query-string "fields @timestamp, @message | filter @message like /ERROR/ | sort @timestamp desc | limit 50"', account_id='<ACCT>')
# Then fetch results (may need to wait a few seconds):
cloud_exec('aws', 'logs get-query-results --query-id <QUERY_ID>', account_id='<ACCT>')
```
Common Insights query patterns:
- Error frequency: `stats count(*) by bin(5m) | filter @message like /ERROR/`
- Top error messages: `filter @message like /ERROR/ | stats count(*) as cnt by @message | sort cnt desc | limit 20`
- Latency percentiles: `filter @type = "REPORT" | stats avg(@duration), pct(@duration, 95), max(@duration) by bin(5m)`
- Lambda cold starts: `filter @type = "REPORT" | filter @initDuration > 0 | stats count(*) as coldStarts by bin(10m)`

### CloudWatch Metrics & Alarms
```python
cloud_exec('aws', 'cloudwatch describe-alarms --state-value ALARM --output table', account_id='<ACCT>')
cloud_exec('aws', 'cloudwatch get-metric-statistics --namespace AWS/EC2 --metric-name CPUUtilization --dimensions Name=InstanceId,Value=<ID> --start-time <ISO> --end-time <ISO> --period 300 --statistics Average Maximum', account_id='<ACCT>')
cloud_exec('aws', 'cloudwatch list-metrics --namespace AWS/RDS --metric-name FreeableMemory', account_id='<ACCT>')
# Common namespaces: AWS/EC2, AWS/RDS, AWS/ELB, AWS/Lambda, AWS/EKS, AWS/S3, AWS/SQS, AWS/SNS
```

### IAM
```python
cloud_exec('aws', 'iam list-roles --query "Roles[].{Name:RoleName,Arn:Arn}" --output table', account_id='<ACCT>')
cloud_exec('aws', 'iam get-role --role-name <ROLE>', account_id='<ACCT>')
cloud_exec('aws', 'iam list-attached-role-policies --role-name <ROLE>', account_id='<ACCT>')
cloud_exec('aws', 'iam get-policy --policy-arn <ARN>', account_id='<ACCT>')
cloud_exec('aws', 'iam simulate-principal-policy --policy-source-arn <ROLE_ARN> --action-names s3:GetObject --resource-arns "arn:aws:s3:::bucket/*"', account_id='<ACCT>')
```

### Networking (VPC)
```python
cloud_exec('aws', 'ec2 describe-vpcs --query "Vpcs[].{ID:VpcId,CIDR:CidrBlock,Name:Tags[?Key==`Name`].Value|[0]}" --output table', account_id='<ACCT>')
cloud_exec('aws', 'ec2 describe-subnets --filters "Name=vpc-id,Values=<VPC>" --query "Subnets[].{ID:SubnetId,AZ:AvailabilityZone,CIDR:CidrBlock}" --output table', account_id='<ACCT>')
cloud_exec('aws', 'ec2 describe-security-groups --group-ids <SG> --output json', account_id='<ACCT>')
cloud_exec('aws', 'ec2 describe-route-tables --filters "Name=vpc-id,Values=<VPC>"', account_id='<ACCT>')
cloud_exec('aws', 'ec2 describe-nat-gateways --filter "Name=vpc-id,Values=<VPC>"', account_id='<ACCT>')
```

### Load Balancers
```python
cloud_exec('aws', 'elbv2 describe-load-balancers --query "LoadBalancers[].{Name:LoadBalancerName,DNS:DNSName,State:State.Code,Type:Type}" --output table', account_id='<ACCT>')
cloud_exec('aws', 'elbv2 describe-target-groups --load-balancer-arn <ARN>', account_id='<ACCT>')
cloud_exec('aws', 'elbv2 describe-target-health --target-group-arn <TG_ARN>', account_id='<ACCT>')
cloud_exec('aws', 'elbv2 describe-listeners --load-balancer-arn <ARN>', account_id='<ACCT>')
```

### Other Services
```python
# SQS:
cloud_exec('aws', 'sqs list-queues', account_id='<ACCT>')
cloud_exec('aws', 'sqs get-queue-attributes --queue-url <URL> --attribute-names All', account_id='<ACCT>')
# SNS:
cloud_exec('aws', 'sns list-topics', account_id='<ACCT>')
# Route 53:
cloud_exec('aws', 'route53 list-hosted-zones', account_id='<ACCT>')
cloud_exec('aws', 'route53 list-resource-record-sets --hosted-zone-id <ZONE>', account_id='<ACCT>')
# CloudFormation:
cloud_exec('aws', 'cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE', account_id='<ACCT>')
cloud_exec('aws', 'cloudformation describe-stack-events --stack-name <STACK> --max-items 20', account_id='<ACCT>')
# ECR:
cloud_exec('aws', 'ecr describe-repositories', account_id='<ACCT>')
cloud_exec('aws', 'ecr describe-images --repository-name <REPO> --query "imageDetails[].{Tags:imageTags,Pushed:imagePushedAt,Size:imageSizeInBytes}" --output table', account_id='<ACCT>')
# ECS:
cloud_exec('aws', 'ecs list-clusters', account_id='<ACCT>')
cloud_exec('aws', 'ecs describe-services --cluster <CLUSTER> --services <SVC>', account_id='<ACCT>')
cloud_exec('aws', 'ecs list-tasks --cluster <CLUSTER> --service-name <SVC>', account_id='<ACCT>')
```

## RCA / Investigation Workflow

When investigating an AWS incident:

1. **Identify the account**: First fan-out call to find which account has the affected resources
2. **Get cluster credentials** (if EKS): `eks update-kubeconfig --name <CLUSTER> --region <REGION>`
3. **Check resource state**: `ec2 describe-instances`, `eks describe-cluster`, `rds describe-db-instances`
4. **Check pods/containers** (if K8s): `kubectl get pods -o wide`, `kubectl describe pod`, `kubectl logs`
5. **Check events**: `kubectl get events --sort-by=.lastTimestamp`, `rds describe-events`
6. **Check logs**: CloudWatch `filter-log-events` or Insights `start-query` for patterns
7. **Check metrics**: `cloudwatch get-metric-statistics` for CPU, memory, disk, network
8. **Check alarms**: `cloudwatch describe-alarms --state-value ALARM`
9. **Check recent deployments**: `kubectl rollout history`, `cloudformation describe-stack-events`
10. **Check networking**: Security groups, NACLs, route tables, target health
11. **Compare healthy vs unhealthy**: `kubectl top pods`, instance metrics side-by-side

## Error Recovery

1. **Permission denied** → Check IAM: `iam get-role`, `iam list-attached-role-policies`, `iam simulate-principal-policy`
2. **Resource not found** → Verify region: `ec2 describe-regions`, check account_id
3. **CLI syntax** → `cloud_exec('aws', '<SERVICE> help')` for subcommand reference

### Context7 lookup on failure
For CLI errors:
`mcp_context7_get_library_docs(context7CompatibleLibraryID='/websites/aws_amazon_cli', topic='eks update-kubeconfig')`

## Region Mapping
- US (default): us-east-1
- Canada: ca-central-1
- EU/Belgium: eu-west-1
- UK/London: eu-west-2
- Singapore/SEA: ap-southeast-1
- Tokyo/Japan: ap-northeast-1
