---
id: provider_aws
name: AWS RCA Investigation
category: rca_provider
connection_check:
  method: provider_in_preference
index: "AWS/EKS investigation commands"
rca_priority: 5
metadata:
  author: aurora
  version: "1.0"
---

## AWS/EKS Investigation

IMPORTANT: If multiple AWS accounts are connected, your FIRST `cloud_exec('aws', ...)` call (without account_id) fans out to ALL accounts. Check `results_by_account` in the response.
- Identify which account(s) have the issue based on the results.
- For ALL subsequent calls, pass `account_id='<ACCOUNT_ID>'` to target only the relevant account. Example: `cloud_exec('aws', 'ec2 describe-instances', account_id='123456789012')`
- Do NOT keep querying all accounts after you know where the problem is.
- Check caller identity: `cloud_exec('aws', 'sts get-caller-identity', account_id='<ACCOUNT_ID>')`
- Check cluster status: `cloud_exec('aws', 'eks describe-cluster --name CLUSTER_NAME', account_id='<ACCOUNT_ID>')`
- **IMPORTANT**: Get cluster credentials first: `cloud_exec('aws', 'eks update-kubeconfig --name CLUSTER_NAME --region REGION', account_id='<ACCOUNT_ID>')`
- Get pod details: `cloud_exec('aws', 'kubectl get pods -n NAMESPACE -o wide', account_id='<ACCOUNT_ID>')`
- Describe problematic pods: `cloud_exec('aws', 'kubectl describe pod POD_NAME -n NAMESPACE', account_id='<ACCOUNT_ID>')`
- Check pod logs: `cloud_exec('aws', 'kubectl logs POD_NAME -n NAMESPACE --since=1h', account_id='<ACCOUNT_ID>')`
- Check events: `cloud_exec('aws', 'kubectl get events -n NAMESPACE --sort-by=.lastTimestamp', account_id='<ACCOUNT_ID>')`
- Query CloudWatch logs: `cloud_exec('aws', 'logs filter-log-events --log-group-name LOG_GROUP --start-time TIMESTAMP', account_id='<ACCOUNT_ID>')`
- Check EC2 instances: `cloud_exec('aws', 'ec2 describe-instances --filters "Name=tag:Name,Values=*"', account_id='<ACCOUNT_ID>')`
- Check load balancers: `cloud_exec('aws', 'elbv2 describe-load-balancers', account_id='<ACCOUNT_ID>')`
- Check security groups: `cloud_exec('aws', 'ec2 describe-security-groups', account_id='<ACCOUNT_ID>')`
