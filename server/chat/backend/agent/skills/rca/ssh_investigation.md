---
id: ssh_investigation
name: SSH Investigation for VMs
category: rca_provider
connection_check:
  method: always
index: "SSH investigation commands for VMs"
rca_priority: 15
metadata:
  author: aurora
  version: "1.0"
---

## SSH Investigation (for VMs)

If you need to SSH into a VM for deeper investigation:

1. Generate SSH key if needed: `terminal_exec('test -f ~/.ssh/aurora_key || ssh-keygen -t rsa -b 4096 -f ~/.ssh/aurora_key -N ""')`
2. Get public key: `terminal_exec('cat ~/.ssh/aurora_key.pub')`
3. Add key to VM (provider-specific)
4. SSH with command: `terminal_exec('ssh -i ~/.ssh/aurora_key -o StrictHostKeyChecking=no USER@IP "COMMAND"')`

### Default SSH Users by Provider

- **GCP**: USER=admin
- **AWS**: USER=ec2-user (Amazon Linux) or ubuntu (Ubuntu)
- **Azure**: USER=azureuser
- **OVH**: USER=debian or ubuntu
- **Scaleway**: USER=root
