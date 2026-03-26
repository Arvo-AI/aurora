---
sidebar_position: 2
---

# VM Deployment

Deploy Aurora on a single VM using Docker Compose.

**Choose your deployment path:**

- [Standard Deployment](#standard-deployment) — the VM has unrestricted internet access
- [Secure Deployment (Air-Tight)](#secure-deployment-air-tight) — the VM has restricted or no outbound internet (enterprise, government, private infrastructure)

---

## Standard Deployment

This path covers every step from provisioning the VM to accessing Aurora in your browser, on any cloud provider with unrestricted internet.

### 1. Provision a VM

Create a VM on your cloud provider of choice (AWS EC2, GCP Compute Engine, Azure VM, DigitalOcean Droplet, Hetzner, etc.).

| Requirement | Value |
|-------------|-------|
| **OS** | Ubuntu 22.04 LTS or Debian 12 |
| **CPU** | 4 cores minimum, 8 recommended |
| **RAM** | 8 GB minimum, 32 GB recommended |
| **Disk** | **60 GB SSD** |

:::warning Disk Size
Aurora's Docker images, containers, and volumes require significant space.
:::

After creation, note the VM's **public/external IP address** — you'll need it later.

### 2. SSH Into the VM

```bash
ssh -i /path/to/your-key.pem YOUR_USERNAME@YOUR_VM_IP
```

Most cloud providers also offer a browser-based SSH console in their web UI.

### 3. Install Dependencies

Run these commands **one at a time** (not as a single pasted block — `newgrp` opens a sub-shell that prevents subsequent commands from running).

#### Ubuntu / Debian

```bash
# Update packages
sudo apt update && sudo apt upgrade -y

# Install system tools
sudo apt install -y make git jq cloud-guest-utils

# Install Docker
curl -fsSL https://get.docker.com | sh

# Add your user to the docker group
sudo usermod -aG docker $USER

# Apply the group change (opens a new shell — run this separately)
newgrp docker

# Verify Docker works (must print v2.x.x)
docker compose version
```

#### CentOS / RHEL / Amazon Linux

```bash
sudo yum update -y
sudo yum install -y make git jq cloud-utils-growpart

curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker

docker compose version
```

If `docker` gives "permission denied" after `newgrp`, log out and back in (`exit` then SSH again).

:::tip Restricted Environments
If `curl` is blocked or the Docker convenience script fails, see **[Installing Docker](./install-docker)** for manual installation instructions covering Debian, Ubuntu, CentOS/RHEL, Amazon Linux on both amd64 and arm64, including fully airgapped environments.
:::

:::caution Run Commands Individually
`newgrp docker` opens a new shell session. If you paste all commands at once, everything after `newgrp` will not execute in the current session. Run it separately, then continue with the remaining commands.
:::

### 4. Clone and Initialize

```bash
git clone https://github.com/arvo-ai/aurora.git
cd aurora
make init
```

`make init` creates `.env` from `.env.example` and generates random values for `POSTGRES_PASSWORD`, `FLASK_SECRET_KEY`, and `AUTH_SECRET`.

### 5. Configure .env

```bash
nano .env
```

#### Required Changes

**LLM API Key** — set at least one:

```bash
OPENROUTER_API_KEY=sk-or-v1-...     # Recommended — one key, many models
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_AI_API_KEY=AIza...
OPENAI_API_KEY=sk-...
```

**LLM Provider Mode** — must match whichever key you set (see [LLM Providers](/docs/integrations/llm-providers) for all options):

```bash
LLM_PROVIDER_MODE=openrouter   # for OPENROUTER_API_KEY (default)
LLM_PROVIDER_MODE=direct       # for direct provider keys (Anthropic, OpenAI, Google, etc.)
```

**Model selection** — when using `LLM_PROVIDER_MODE=direct`, all model env vars must use the same provider as your API key. If omitted, Aurora defaults to Anthropic models:

```bash
# Example: Google AI
MAIN_MODEL=google/gemini-3.1-pro-preview
RCA_MODEL=google/gemini-2.5-flash
SUMMARIZATION_MODEL=google/gemini-2.5-flash

# Example: Anthropic (default, no need to set explicitly)
MAIN_MODEL=anthropic/claude-sonnet-4.6
RCA_MODEL=anthropic/claude-haiku-4.5
```

See [LLM Providers](/docs/integrations/llm-providers#supported-models) for the full list of valid model names per provider.

**VM URLs** — replace `YOUR_VM_IP` with your VM's public IP (or internal/VPN IP — see note below):

```bash
FRONTEND_URL=http://YOUR_VM_IP:3000
NEXT_PUBLIC_BACKEND_URL=http://YOUR_VM_IP:5080
NEXT_PUBLIC_WEBSOCKET_URL=ws://YOUR_VM_IP:5006
SEARXNG_URL=http://YOUR_VM_IP:8082
```

Leave `BACKEND_URL=http://aurora-server:5080` as-is — that's for internal container-to-container communication.

:::tip Private/Internal Network
If accessing via VPN, private subnet, or reverse proxy, use that IP/hostname instead of the public IP (e.g., `10.8.0.1` for a WireGuard tunnel, `192.168.x.x` for a private subnet, `https://aurora.internal` for a reverse proxy). `BACKEND_URL` always stays as the internal Docker name regardless.
:::

Save and exit (`Ctrl+X`, `Y`, `Enter` in nano).

:::tip Static IPs
Most cloud providers assign ephemeral public IPs by default — they change if you stop and restart the VM. Reserve a static/elastic IP through your provider's console so you only need to configure the URLs once.
:::

### 6. Build and Start

Choose one:

**Option A — Build from source** (recommended for most deployments):
```bash
make prod-local
```
Builds all Docker images from the cloned source code. Slower on first run (several minutes) but ensures you have the latest code and all connectors.

**Option B — Pull prebuilt images** (faster, but uses published releases):
```bash
make prod-prebuilt
```
Pulls prebuilt images from GHCR instead of building locally. Faster to start, but uses the last published release.

### 7. Get and Set the Vault Token

After the stack is running, the `vault-init` sidecar initializes Vault and generates a root token. Extract it and write it into `.env`:

```bash
# Wait ~30 seconds for vault-init to finish, then:
VAULT_TOKEN=$(docker exec aurora-vault cat /vault/init/keys.json | jq -r '.root_token') \
  && sed -i "s|^VAULT_TOKEN=.*|VAULT_TOKEN=$VAULT_TOKEN|" .env

# Verify it was written
grep VAULT_TOKEN .env
```

If the command fails (container not ready yet), wait and retry. Check vault-init status with:

```bash
docker logs aurora-vault-init
```

### 8. Restart to Apply Vault Token

```bash
# Use whichever command you chose in step 6
make down && make prod-local      # if you built from source
make down && make prod-prebuilt   # if you pulled prebuilt images
```

### 9. Open Firewall Ports

Aurora needs three ports accessible from outside the VM:

| Port | Service |
|------|---------|
| 3000 | Frontend (Next.js) |
| 5080 | Backend API (Flask) |
| 5006 | WebSocket (Chatbot) |

How you open these depends on your cloud provider:

**AWS** — Edit the instance's **Security Group**: add inbound rules for TCP 3000, 5080, 5006.

**GCP** — Create a **Firewall Rule** under VPC Network > Firewall: allow ingress TCP 3000, 5080, 5006.

**Azure** — Edit the **Network Security Group** attached to the VM: add inbound security rules for TCP 3000, 5080, 5006.

**DigitalOcean** — Create or edit a **Cloud Firewall** and add inbound rules for TCP 3000, 5080, 5006, then attach it to your droplet.

**Any provider** — Find the network firewall / security group attached to your VM and allow inbound TCP on ports 3000, 5080, and 5006.

For the source IP range, use your own IP (e.g., `1.2.3.4/32` — find it at [whatismyip.com](https://www.whatismyip.com/)) to restrict access to just you, or `0.0.0.0/0` for public access.

:::warning Security
Setting source to `0.0.0.0/0` makes the instance accessible to anyone on the internet. Aurora has its own login system, but for a test/internal deployment, restrict to your own IP for safety. Consider enabling rate limiting (`RATE_LIMITING_ENABLED=true` in `.env`) if exposing publicly.
:::

**OS-level firewall** — Most cloud VMs don't enable an OS-level firewall by default. If yours does (check with `sudo ufw status` or `sudo firewall-cmd --state`), also open the ports there:

```bash
# Ubuntu/Debian (ufw)
sudo ufw allow 3000 && sudo ufw allow 5080 && sudo ufw allow 5006

# CentOS/RHEL (firewalld)
sudo firewall-cmd --permanent --add-port=3000/tcp --add-port=5080/tcp --add-port=5006/tcp
sudo firewall-cmd --reload
```

### 10. Access Aurora

Open in your browser:

```
http://YOUR_VM_IP:3000
```

You must include the `:3000` port — plain `http://YOUR_VM_IP/` (port 80) will not work.

### Verify Health

```bash
# From inside the VM
curl http://localhost:5080/health/liveness

# Check all containers are running
docker compose -f docker-compose.prod-local.yml ps
```

### Ongoing Operations

```bash
# View logs
make prod-logs

# Stop everything
make down

# Restart
make down && make prod-local

# Full cleanup (removes data volumes)
make prod-local-clean

# Nuclear option (removes everything including images)
make prod-local-nuke
```

### Deploying Code Updates

```bash
git pull
make down && make prod-local
```

The `NEXT_PUBLIC_*` environment variables are injected at container startup, not baked at build time. If you only change those values in `.env`, you can skip a full rebuild:

```bash
docker compose -f docker-compose.prod-local.yml up -d frontend
```

---

## Secure Deployment (Air-Tight)

Use this path when the target VM has restricted or no outbound internet access (enterprise, government, private infrastructure). All Docker images are pre-built and bundled into a single tarball on a machine with internet access, then transferred to the VM. Nothing is fetched from the internet during deployment.

**Prerequisites:**

- You have received the airtight bundle (a `.tar.gz` file, e.g. `aurora-airtight-4c92267-amd64.tar.gz`)
- Optionally, its `.sha256` checksum file for integrity verification
- The target VM meets the [hardware requirements](#1-provision-a-vm) (4+ CPU, 8+ GB RAM, 60 GB SSD)
- Docker and Docker Compose are installed on the VM (see [Installing Docker](./install-docker) for all OS/architecture combinations, including environments where `curl` and `wget` are blocked)
- You can SSH into the VM

### 1. Transfer the Bundle to the VM

You need the `.tar.gz` tarball on the VM. The `.sha256` checksum file is optional (for verifying the transfer). Use whatever transfer method your organization permits:

```bash
# SCP
scp aurora-airtight-4c92267-amd64.tar.gz user@10.0.0.5:~/

# GCS bucket
gcloud storage cp aurora-airtight-4c92267-amd64.tar.gz gs://my-bucket/
# Then on the VM:
gcloud storage cp gs://my-bucket/aurora-airtight-4c92267-amd64.tar.gz ~/

# Azure Blob / AWS S3 / internal file server — use your org's standard method
```

Replace the filename, user, and IP/bucket with your actual values.

### 2. (Optional) Verify the Bundle Integrity

If you also transferred the `.sha256` checksum file, verify the tarball wasn't corrupted:

```bash
cd ~
sha256sum -c aurora-airtight-4c92267-amd64.tar.gz.sha256
# Expected output: aurora-airtight-4c92267-amd64.tar.gz: OK
```

If the check fails, the file was corrupted — re-transfer it.

### 3. Get the Repository

The repo contains configuration files (`docker-compose.airtight.yml`, `Makefile`, `.env.example`) needed to run the stack. No images are pulled during this step.

**Option A — Clone (requires internet or GitHub access on the VM):**

```bash
git clone https://github.com/arvo-ai/aurora.git
cd aurora
```

**Option B — Release archive (no internet needed on the VM):**

Download the source archive (`.tar.gz` or `.zip`) from the [releases page](https://github.com/arvo-ai/aurora/releases) on a connected machine, transfer it to the VM alongside the image bundle, then extract:

```bash
VERSION=1.2.2  # replace with your release version (GitHub strips the 'v' prefix)
tar xzf aurora-$VERSION.tar.gz
cd aurora-$VERSION
```

:::warning Version must match the image bundle
The release archive version must match the version used to build the airtight image bundle. Mismatched versions can cause errors — the compose files, configs, and entrypoints in the source must match the images.
:::

Then initialize:

```bash
make init
```

### 4. Configure .env

```bash
nano .env
```

**LLM API Key** — set at least one:

```bash
OPENROUTER_API_KEY=sk-or-v1-...     # Recommended — one key, many models
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_AI_API_KEY=AIza...
OPENAI_API_KEY=sk-...
```

**LLM Provider Mode** — must match whichever key you set (see [LLM Providers](/docs/integrations/llm-providers) for all options):

```bash
LLM_PROVIDER_MODE=openrouter   # for OPENROUTER_API_KEY (default)
LLM_PROVIDER_MODE=direct       # for direct provider keys (Anthropic, OpenAI, Google, etc.)
```

**VM URLs** — replace `YOUR_VM_IP` with the VM's public IP (or internal/VPN IP):

```bash
FRONTEND_URL=http://YOUR_VM_IP:3000
NEXT_PUBLIC_BACKEND_URL=http://YOUR_VM_IP:5080
NEXT_PUBLIC_WEBSOCKET_URL=ws://YOUR_VM_IP:5006
SEARXNG_URL=http://YOUR_VM_IP:8082
```

Leave `BACKEND_URL=http://aurora-server:5080` as-is — that's for internal container-to-container communication.

:::tip Private/Internal Network
If accessing via VPN, private subnet, or reverse proxy, use that IP/hostname instead of the public IP (e.g., `10.8.0.1` for a WireGuard tunnel, `192.168.x.x` for a private subnet, `https://aurora.internal` for a reverse proxy). `BACKEND_URL` always stays as the internal Docker name regardless.
:::

Save and exit (`Ctrl+X`, `Y`, `Enter` in nano).

### 5. Load Images and Start

Pass the path to the tarball you transferred in step 1:

```bash
make prod-airtight AIRTIGHT_BUNDLE=~/aurora-airtight-4c92267-amd64.tar.gz
```

This loads every Docker image from the tarball into the local Docker daemon and starts the full Aurora stack. No outbound network calls are made. First run takes a few minutes while images are loaded.

On subsequent restarts (images already loaded):

```bash
make prod-airtight
```

### 6. Get and Set the Vault Token

```bash
# Wait ~30 seconds for vault-init to finish, then:
VAULT_TOKEN=$(docker exec aurora-vault cat /vault/init/keys.json | jq -r '.root_token') \
  && sed -i "s|^VAULT_TOKEN=.*|VAULT_TOKEN=$VAULT_TOKEN|" .env

grep VAULT_TOKEN .env
```

### 7. Restart to Apply Vault Token

```bash
make down && make prod-airtight
```

### 8. Open Firewall Ports

Same as the [standard deployment firewall step](#9-open-firewall-ports) — allow inbound TCP on ports 3000, 5080, and 5006.

### 9. Access Aurora

```
http://YOUR_VM_IP:3000
```

### Deploying Updates (Air-Tight)

Each new Aurora release requires a fresh bundle. On the connected machine:

```bash
git pull && make package-airtight
```

Transfer the new tarball and checksum to the VM, then:

```bash
make down
make prod-airtight AIRTIGHT_BUNDLE=~/aurora-airtight-<new-version>.tar.gz
```

The `.env` file stays on the VM and is never part of the bundle.

### Creating the Air-Tight Bundle

This section is for the person building the bundle on a machine with internet access.

```bash
git clone https://github.com/arvo-ai/aurora.git && cd aurora
make package-airtight
```

This builds all Aurora images, pulls all third-party images, and saves everything into `aurora-airtight-<version>-<arch>.tar.gz` with a SHA-256 checksum. The default target architecture is `linux/amd64`.

To target ARM servers:

```bash
PLATFORM=linux/arm64 make package-airtight
```

If building on Apple Silicon for an x86 server, the default `linux/amd64` cross-compiles automatically — no extra flags needed.

---

## Troubleshooting

### "no space left on device" During Build

The disk is full. Clean up and consider resizing:

```bash
docker image prune -a -f
docker builder prune -f
docker system df
```

If still not enough, resize the disk through your cloud provider's console and expand the partition:

```bash
sudo growpart /dev/sda 1
sudo resize2fs /dev/sda1
```

### Vault Sealed After VM Restart

The `vault-init` sidecar auto-unseals on startup using keys stored in a persistent Docker volume. If it didn't work:

```bash
docker restart aurora-vault-init
```

### "Connection Timed Out" in Browser

1. Verify the cloud firewall / security group allows TCP 3000, 5080, 5006
2. Verify the OS-level firewall isn't blocking traffic (`sudo ufw status` or `sudo firewall-cmd --state`)
3. Verify you're using the correct public IP: `curl -s ifconfig.me`
4. Verify the frontend is running: `docker ps | grep frontend`

### Public IP Changed

Cloud provider ephemeral IPs change when the VM is stopped and restarted. Update `.env` with the new IP and recreate the frontend:

```bash
nano .env   # update FRONTEND_URL, NEXT_PUBLIC_BACKEND_URL, NEXT_PUBLIC_WEBSOCKET_URL, SEARXNG_URL
docker compose -f docker-compose.prod-local.yml up -d frontend
```

To avoid this, reserve a static/elastic IP through your cloud provider.

### Containers Keep Restarting

Check logs for the failing container:

```bash
docker logs aurora-server --tail 50
docker logs aurora-celery_worker-1 --tail 50
```

Common causes: missing env vars, Vault not ready, database not initialized.
