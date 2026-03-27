---
sidebar_position: 6
---

# Installing Docker

How to install Docker Engine and Docker Compose on a VM, including environments where `curl` and `wget` are blocked.

---

## Quick Install (Unrestricted Internet)

If the VM has unrestricted outbound internet access:

### Ubuntu / Debian

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

### CentOS / RHEL / Amazon Linux

```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker
```

### Verify

```bash
docker --version          # 24.0+ required
docker compose version    # v2 required
```

---

## Enterprise / Restricted Install (No curl or wget)

Use this path when the VM's security policy blocks `curl`, `wget`, or the Docker convenience script. The GPG key is downloaded on a machine with internet access and transferred to the VM via `scp`.

### Debian 12 (Bookworm) — amd64

**On your local machine** (with internet):

```bash
# Download the Docker GPG key
curl -fsSL https://download.docker.com/linux/debian/gpg -o docker.asc

# Transfer to the VM
scp docker.asc user@VM_IP:/tmp/
```

You can also download `https://download.docker.com/linux/debian/gpg` in a browser and save it as `docker.asc`.

**On the VM:**

```bash
# Install the GPG key
sudo mkdir -p /etc/apt/keyrings
sudo mv /tmp/docker.asc /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add the Docker repository
sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: bookworm
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF

# Install Docker
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Add your user to the docker group
sudo usermod -aG docker $USER
newgrp docker
```

### Debian 12 (Bookworm) — arm64

Identical to amd64. The Docker repo automatically serves the correct architecture. Use the same GPG key and the same `docker.sources` entry — `apt` resolves the right `arm64` packages.

### Ubuntu 22.04 / 24.04 — amd64 / arm64

**On your local machine:**

```bash
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o docker.asc
scp docker.asc user@VM_IP:/tmp/
```

**On the VM:**

```bash
sudo mkdir -p /etc/apt/keyrings
sudo mv /tmp/docker.asc /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Detect the Ubuntu codename automatically
CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")

sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $CODENAME
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

sudo usermod -aG docker $USER
newgrp docker
```

### CentOS / RHEL 8+ / Amazon Linux 2023 — amd64 / arm64

**On your local machine:**

```bash
curl -fsSL https://download.docker.com/linux/centos/gpg -o docker.asc
scp docker.asc user@VM_IP:/tmp/
```

**On the VM:**

```bash
sudo rpm --import /tmp/docker.asc

sudo tee /etc/yum.repos.d/docker-ce.repo > /dev/null <<'EOF'
[docker-ce-stable]
name=Docker CE Stable
baseurl=https://download.docker.com/linux/centos/$releasever/$basearch/stable
enabled=1
gpgcheck=1
gpgkey=file:///tmp/docker.asc
EOF

sudo yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker

sudo usermod -aG docker $USER
newgrp docker
```

:::note Amazon Linux 2023
Amazon Linux 2023 uses the CentOS/RHEL repos. Use `baseurl=https://download.docker.com/linux/centos/9/$basearch/stable` (hardcode `9` instead of `$releasever`).
:::

---

## Full Airgap (No Outbound Internet at All)

If the VM cannot reach `download.docker.com` or any package mirrors, you must download `.deb` or `.rpm` files on a connected machine and transfer them.

### Debian / Ubuntu

**On your local machine**, download the five packages from [download.docker.com](https://download.docker.com/linux/debian/dists/bookworm/pool/stable/amd64/) (adjust the path for your OS, release, and architecture):

- `containerd.io`
- `docker-ce-cli`
- `docker-ce`
- `docker-buildx-plugin`
- `docker-compose-plugin`

```bash
# Transfer all .deb files to the VM
scp *.deb user@VM_IP:/tmp/

# On the VM
cd /tmp
sudo dpkg -i ./containerd.io_*.deb ./docker-ce-cli_*.deb ./docker-ce_*.deb \
  ./docker-buildx-plugin_*.deb ./docker-compose-plugin_*.deb
sudo apt-get install -f -y

sudo usermod -aG docker $USER
newgrp docker
```

### CentOS / RHEL

Download the five `.rpm` packages from [download.docker.com](https://download.docker.com/linux/centos/9/x86_64/stable/Packages/) (adjust for architecture).

```bash
scp *.rpm user@VM_IP:/tmp/

# On the VM
cd /tmp
sudo yum localinstall -y ./containerd.io-*.rpm ./docker-ce-cli-*.rpm ./docker-ce-*.rpm \
  ./docker-buildx-plugin-*.rpm ./docker-compose-plugin-*.rpm
sudo systemctl enable --now docker

sudo usermod -aG docker $USER
newgrp docker
```

---

## Common Issues

### `apt update` fails with TLS errors

The VM's firewall or proxy is blocking HTTPS to `download.docker.com`. Options:

1. Allowlist `download.docker.com` in the firewall/proxy
2. Set up an internal apt mirror
3. Use the [full airgap method](#full-airgap-no-outbound-internet-at-all)

### `apt update` fails with GPG errors

The GPG key wasn't installed correctly. Verify:

```bash
file /etc/apt/keyrings/docker.asc
# Should say "PGP public key block"
```

If it says "HTML document" or similar, the download was intercepted by a proxy. Re-download the key or transfer it via a different method.

### Permission denied after `newgrp`

Log out and SSH back in. The docker group membership only takes effect in new login sessions.

### Docker installed but `docker compose` not found

You have Docker but not the Compose plugin. Install it:

```bash
# Debian/Ubuntu
sudo apt-get install -y docker-compose-plugin

# CentOS/RHEL
sudo yum install -y docker-compose-plugin
```
