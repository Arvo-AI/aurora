# Enabling VM-1 Deployment

VM-1 is fully configured and ready to deploy. All GCP secrets are created and the workflow is prepared.

## Prerequisites (Already Done ✅)

- ✅ VM-1 GCP secrets created
- ✅ Service account has access to secrets
- ✅ Workflow updated with VM-1 deployment steps
- ✅ No hardcoded values

## To Enable VM-1 Deployment

### 1. Set up DNS

Add this A record to your `aurora-ai.net` DNS:

```
Type: A
Name: demo1
Value: <VM-1-PUBLIC-IP>
TTL: Auto or 300
```

Get VM-1's IP:
```bash
gcloud compute instances describe aurora-demo-vm \
  --zone=us-central1-a \
  --project=sublime-flux-414616 \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

### 2. Uncomment VM-1 in Workflow

Edit `.github/workflows/deploy-demo-vms.yml`:

**Lines to uncomment: 102-150** (VM-1 deployment and health check sections)

Find these commented sections:
- `# - name: Deploy to VM-1 (primary demo VM)`
- `# - name: Wait for VM-1 services to stabilize`
- `# - name: Health check VM-1`

Remove the `#` from all lines in these three sections.

### 3. Update Deployment Summary (Optional)

Line 213: Uncomment to show VM-1 in summary:
```yaml
# echo "  - $VM1_NAME: https://${{ env.VM1_DOMAIN }}" (currently disabled)
```
Change to:
```yaml
echo "  - $VM1_NAME: https://${{ env.VM1_DOMAIN }}"
```

And remove line 216:
```yaml
echo "Note: VM-1 deployment is currently disabled"
```

### 4. Test

1. Commit and push to `demo` branch
2. Watch GitHub Actions workflow
3. Verify both VMs deploy successfully
4. Access:
   - VM-1: `https://demo1.aurora-ai.net`
   - VM-2: `https://demo2.aurora-ai.net`

## GCP Secrets Configuration

All secrets are already created:

### VM-1 Secrets
- `demo-vm1-name` = `aurora-demo-vm`
- `demo-vm1-domain` = `demo1.aurora-ai.net`
- `demo-vm1-frontend-url` = `https://demo1.aurora-ai.net`
- `demo-vm1-backend-url` = `https://demo1.aurora-ai.net`
- `demo-vm1-websocket-url` = `wss://demo1.aurora-ai.net/ws`

### VM-2 Secrets  
- `demo-vm2-name` = `aurora-demo-vm-2`
- `demo-vm2-domain` = `demo2.aurora-ai.net`
- `demo-vm2-frontend-url` = `https://demo2.aurora-ai.net`
- `demo-vm2-backend-url` = `https://demo2.aurora-ai.net`
- `demo-vm2-websocket-url` = `wss://demo2.aurora-ai.net/ws`

### Shared Secrets
- `demo-gcp-zone` = `us-central1-a`

## What the Workflow Does

When VM-1 is uncommented, it will:

1. ✅ SSH to VM-1 using Workload Identity
2. ✅ Fix git permissions (chown)
3. ✅ Pull latest code from `demo` branch
4. ✅ Configure domain URLs in `.env` from GCP secrets
5. ✅ Rebuild frontend with new environment
6. ✅ Restart all services
7. ✅ Health check via HTTPS domain
8. ✅ Display deployment summary

No manual intervention needed after uncommenting!
