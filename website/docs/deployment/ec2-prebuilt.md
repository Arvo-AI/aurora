---
sidebar_position: 2
---

# EC2 Prebuilt Setup

Minimal steps to run Aurora with prebuilt images on a single EC2 instance.

## Instance

| | Minimum | Recommended |
|---|--------|-------------|
| **Type** | t3.large | t3.xlarge |
| **vCPU** | 2 | 4 |
| **RAM** | 8 GB | 16 GB |
| **Storage** | 30 GB gp3 | 40 GB gp3 |
| **OS** | Ubuntu 24.04 LTS | — |

**Security group (inbound):** allow 22 (SSH), 3000 (frontend), 5080 (API), 5006 (WebSocket); source `0.0.0.0/0` or your IP.

---

## Commands on EC2

```bash
# 1. Install Docker + Compose v2
sudo apt-get update && sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker

# 2. Install make, clone and init
sudo apt-get install -y make
git clone https://github.com/arvo-ai/aurora.git && cd aurora
make init

# 3. Edit .env: set host to your EC2 public IP or domain, and add one LLM key
nano .env
# Set: NEXT_PUBLIC_BACKEND_URL=http://YOUR_IP:5080
#      NEXT_PUBLIC_WEBSOCKET_URL=ws://YOUR_IP:5006
#      OPENROUTER_API_KEY=sk-or-v1-xxx  (or OPENAI_API_KEY / ANTHROPIC_API_KEY)

# 4. Start (prebuilt)
make prod-prebuilt

# 5. Vault token after first boot (optional but recommended)
# vault-init is a one-shot container (exits after init); get token from its logs:
docker logs aurora-vault-init 2>&1 | grep "Root Token:"
# Add VAULT_TOKEN=... to .env, then: make down && make prod-prebuilt
```

---

## If you see "permission denied" on docker

Your shell doesn’t have the `docker` group yet. Run `newgrp docker` (then `make prod-prebuilt` again), or open a new SSH session.

## Frontend not loading (http://&lt;IP&gt;:3000)

1. **Security group** — In AWS: EC2 → Security Groups → your instance's SG → Inbound rules. Add (or fix): Type Custom TCP, Port 3000, Source `0.0.0.0/0`. Save.
2. **On the VM:** check the frontend is running and listening:
   ```bash
   docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "frontend|3000"
   curl -s -o /dev/null -w "%{http_code}" http://localhost:3000
   ```
   If `curl` returns `200`, the app is up; the problem is network (security group or firewall). If the frontend container is exited, run `make prod-logs` and fix any errors.
3. **Ubuntu firewall:** `sudo ufw status`. If active, allow ports then reload: `sudo ufw allow 3000 && sudo ufw allow 5080 && sudo ufw allow 5006 && sudo ufw reload`.

## Optional

- **Pin version:** `VERSION=v1.2.3 make prod-prebuilt`
- **Logs:** `make prod-logs`
- **Stop:** `make down`

Frontend: `http://<EC2>:3000` · API: `http://<EC2>:5080`
