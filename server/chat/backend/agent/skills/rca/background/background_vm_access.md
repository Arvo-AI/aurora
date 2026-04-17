VM ACCESS - Automatic SSH Keys Available, use OpenSSH terminal_exec to SSH into VMs:
For OVH/Scaleway VMs configured via Aurora UI: Keys auto-mounted at ~/.ssh/id_<provider>_<vm_id>
SSH command: terminal_exec('ssh -i ~/.ssh/id_scaleway_<VM_ID> -o StrictHostKeyChecking=no -o BatchMode=yes root@IP "command"')
Or simpler: terminal_exec('ssh root@IP "command"') - keys in ~/.ssh/ tried automatically
Users: GCP=admin | AWS=ec2-user/ubuntu | Azure=azureuser | OVH=debian/ubuntu/root | Scaleway=root
