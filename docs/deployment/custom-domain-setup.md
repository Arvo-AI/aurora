# Custom Domain Setup for Demo VMs

## Quick Setup (5 minutes)

### 1. DNS Configuration

Add this A record to your `aurora-ai.net` DNS:

```
demo2.aurora-ai.net    A    35.239.95.255
```

**Where to add it:**
- Go to your domain registrar or DNS provider (e.g., Cloudflare, GoDaddy, Namecheap, Google Domains)
- Find DNS settings for `aurora-ai.net`
- Add new A record as shown above
- Wait 5-10 minutes for DNS propagation

### 2. VM Configuration

SSH into the VM and add the DOMAIN variable:

```bash
# SSH to VM-2
gcloud compute ssh aurora-demo-vm-2 --zone=us-central1-a --project=sublime-flux-414616

# Add DOMAIN to .env
echo "DOMAIN=demo2.aurora-ai.net" >> /opt/aurora-demo/.env

# Restart services
cd /opt/aurora-demo
docker compose -f docker-compose.prod-local.yml up -d
```

### 3. Test

After DNS propagates (5-10 minutes):

```bash
# Test HTTP (will redirect to HTTPS)
curl -I http://demo2.aurora-ai.net

# Test HTTPS (Caddy auto-provisions SSL certificate)
curl -I https://demo2.aurora-ai.net
```

## What Changed

**Before:**
- Frontend: `http://35.239.95.255:3000`
- Backend: `http://35.239.95.255:5080`

**After:**
- Frontend: `https://demo2.aurora-ai.net`
- Backend: `https://demo2.aurora-ai.net/api`
- WebSocket: `wss://demo2.aurora-ai.net/ws`

## How It Works

1. **DNS**: Points `demo2.aurora-ai.net` to your VM's IP
2. **Caddy**: Sits in front of your containers, handles HTTPS
3. **Let's Encrypt**: Caddy automatically gets SSL certificates
4. **Routing**: 
   - `/` → frontend container (port 3000)
   - `/api/*` → backend container (port 5080)
   - `/ws*` → chatbot container (port 5006)

## Troubleshooting

**DNS not resolving?**
```bash
# Check DNS propagation
dig demo2.aurora-ai.net
nslookup demo2.aurora-ai.net
```

**SSL certificate issues?**
```bash
# Check Caddy logs
docker logs aurora-caddy
```

**Port 80/443 not accessible?**
```bash
# Check GCP firewall rules
gcloud compute firewall-rules list --project=sublime-flux-414616 | grep default-allow-http
```

If you need to open ports:
```bash
gcloud compute firewall-rules create allow-http-https \
  --allow tcp:80,tcp:443 \
  --source-ranges 0.0.0.0/0 \
  --target-tags http-server,https-server \
  --project=sublime-flux-414616
```

## For VM-1 (Primary)

Same steps but use:
- Domain: `demo1.aurora-ai.net`
- VM: `aurora-demo-vm`
- IP: Get with `gcloud compute instances describe aurora-demo-vm --zone=us-central1-a --format='get(networkInterfaces[0].accessConfigs[0].natIP)'`
