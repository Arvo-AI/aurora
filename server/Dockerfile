# ============================================
# Build GitHub MCP Server from source (pinned)
# ============================================
FROM golang:1.25-bookworm AS github-mcp-build

ARG GITHUB_MCP_VERSION=v1.0.3
RUN git clone --depth 1 --branch ${GITHUB_MCP_VERSION} \
        https://github.com/github/github-mcp-server.git /src
WORKDIR /src
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /github-mcp-server ./cmd/github-mcp-server

# ============================================
# Base Stage - Common dependencies
# ============================================
FROM python:3.12-slim AS base

ENV PYTHONPATH="/app"
# Set uv cache directory to a writable location for non-root users
ENV UV_CACHE_DIR="/tmp/.cache/uv"

# Set the working directory
WORKDIR /app 

# Disable HTTP pipelining to prevent CDN hash mismatches
RUN echo 'Acquire::http::Pipeline-Depth "0";' > /etc/apt/apt.conf.d/99fixhash && \
    apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    g++ \
    python3-dev \
    postgresql \
    postgresql-client \
    wget \
    unzip \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    openssh-client \
    dnsutils \
    --no-install-recommends && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Download and install uv to a system-wide location (accessible by non-root users)
ADD https://astral.sh/uv/0.8.22/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv && \
    mv /root/.local/bin/uvx /usr/local/bin/uvx 2>/dev/null || true

# Add Docker GPG key and repository
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && \
    chmod a+r /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" \
        | tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker CLI, Buildx **and** Compose plugin in one shot
RUN apt-get update && apt-get install -y \
        docker-ce-cli \
        docker-buildx-plugin \
        docker-compose-plugin \
        --no-install-recommends && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Terraform (multi-arch support)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        TERRAFORM_ARCH="amd64"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        TERRAFORM_ARCH="arm64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    wget -q https://releases.hashicorp.com/terraform/1.7.5/terraform_1.7.5_linux_${TERRAFORM_ARCH}.zip && \
    unzip terraform_1.7.5_linux_${TERRAFORM_ARCH}.zip && \
    mv terraform /usr/local/bin/ && \
    rm terraform_1.7.5_linux_${TERRAFORM_ARCH}.zip

# Build Terraform provider filesystem mirror for all supported providers.
# Each provider is a separate layer so Docker caches them independently.
RUN mkdir -p /opt/terraform-providers /tmp/_mirror-src

RUN cd /tmp/_mirror-src && \
    printf 'terraform {\n  required_providers {\n    p = {\n      source = "ovh/ovh"\n      version = ">= 0.36.0"\n    }\n  }\n}\n' \
      > main.tf && \
    terraform providers mirror -platform=linux_$(dpkg --print-architecture) /opt/terraform-providers && \
    rm main.tf

RUN cd /tmp/_mirror-src && \
    printf 'terraform {\n  required_providers {\n    p = {\n      source = "scaleway/scaleway"\n      version = ">= 2.0"\n    }\n  }\n}\n' \
      > main.tf && \
    terraform providers mirror -platform=linux_$(dpkg --print-architecture) /opt/terraform-providers && \
    rm main.tf

RUN cd /tmp/_mirror-src && \
    printf 'terraform {\n  required_providers {\n    p = {\n      source = "hashicorp/google"\n      version = "~> 4.0"\n    }\n  }\n}\n' \
      > main.tf && \
    terraform providers mirror -platform=linux_$(dpkg --print-architecture) /opt/terraform-providers && \
    rm main.tf

RUN cd /tmp/_mirror-src && \
    printf 'terraform {\n  required_providers {\n    p = {\n      source = "hashicorp/azurerm"\n      version = "~> 3.0"\n    }\n  }\n}\n' \
      > main.tf && \
    terraform providers mirror -platform=linux_$(dpkg --print-architecture) /opt/terraform-providers && \
    rm main.tf

RUN cd /tmp/_mirror-src && \
    printf 'terraform {\n  required_providers {\n    p = {\n      source = "hashicorp/aws"\n      version = "~> 5.0"\n    }\n  }\n}\n' \
      > main.tf && \
    terraform providers mirror -platform=linux_$(dpkg --print-architecture) /opt/terraform-providers && \
    rm -rf /tmp/_mirror-src

# Configure Terraform to use local mirror with network fallback
RUN cat > /etc/terraform.rc <<'EOF'
provider_installation {
  filesystem_mirror {
    path = "/opt/terraform-providers"
  }
  direct {}
}

plugin_cache_dir = "/app/terraform_workdir/.plugin-cache"

disable_checkpoint = true
disable_checkpoint_signature = true
plugin_cache_may_break_dependency_lock_file = true
EOF

# Airgapped variant: mirror only, no network fallback
RUN cat > /etc/terraform-airgapped.rc <<'EOF'
provider_installation {
  filesystem_mirror {
    path = "/opt/terraform-providers"
  }
}

plugin_cache_dir = "/app/terraform_workdir/.plugin-cache"

disable_checkpoint = true
disable_checkpoint_signature = true
plugin_cache_may_break_dependency_lock_file = true
EOF

# Install Google Cloud SDK
RUN curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y google-cloud-sdk google-cloud-sdk-gke-gcloud-auth-plugin && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install AWS CLI v2 (multi-arch support)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        AWS_CLI_ARCH="x86_64"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        AWS_CLI_ARCH="aarch64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_CLI_ARCH}.zip" -o "awscliv2.zip" && \
    unzip -q awscliv2.zip && \
    ./aws/install > /dev/null 2>&1 && \
    rm -rf awscliv2.zip aws/

# Install Azure CLI (manual download method)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        AZURE_CLI_ARCH="x64"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        AZURE_CLI_ARCH="aarch64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl -fsSL "https://aka.ms/InstallAzureCLIDeb" -o azure-cli-install.sh && \
    chmod +x azure-cli-install.sh && \
    ./azure-cli-install.sh && \
    rm azure-cli-install.sh

# Install OVHcloud CLI (multi-arch support)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        OVH_CLI_ARCH="x86_64"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        OVH_CLI_ARCH="arm64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl -fsSL "https://github.com/ovh/ovhcloud-cli/releases/download/v0.9.0/ovhcloud-cli_Linux_${OVH_CLI_ARCH}.tar.gz" -o ovhcloud.tar.gz && \
    tar -xzf ovhcloud.tar.gz && \
    mv ovhcloud /usr/local/bin/ && \
    chmod +x /usr/local/bin/ovhcloud && \
    rm ovhcloud.tar.gz

# Install Scaleway CLI (multi-arch support)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        SCW_CLI_ARCH="amd64"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        SCW_CLI_ARCH="arm64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl -fsSL "https://github.com/scaleway/scaleway-cli/releases/download/v2.48.0/scaleway-cli_2.48.0_linux_${SCW_CLI_ARCH}" -o /usr/local/bin/scw && \
    chmod +x /usr/local/bin/scw

# Install Tailscale CLI (for local dev SSH to tailnet devices)
RUN curl -fsSL https://tailscale.com/install.sh | sh

# Install Node.js for MCP servers
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy GitHub MCP Server binary from build stage
COPY --from=github-mcp-build /github-mcp-server /usr/local/bin/github-mcp-server

# Install kubectl for orchestrator to manage terminal pods
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        KUBECTL_ARCH="amd64"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        KUBECTL_ARCH="arm64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    KUBECTL_VERSION=$(curl -fsSL --retry 3 --retry-delay 2 https://dl.k8s.io/release/stable.txt 2>/dev/null || echo "v1.31.0") && \
    curl -fsSL --retry 5 --retry-delay 3 \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${KUBECTL_ARCH}/kubectl" \
      -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl

# Upgrade pip & install base deps
RUN --mount=type=cache,target=/root/.cache/pip \
    uv pip install --upgrade pip setuptools wheel --system

# Copy dependency files first for caching
COPY requirements.txt /app/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/root/.cache/uv \
    uv pip install -r requirements.txt --system

# ============================================
# Development Stage
# ============================================
FROM base AS dev

# In development, code is mounted as volume
# No need to copy source code here
EXPOSE 5006
CMD ["python", "main_chatbot.py"]

# ============================================
# Production Stage
# ============================================
FROM base AS prod

RUN useradd -m -u 1000 -s /bin/bash app && \
    mkdir -p /tmp/.cache/uv && \
    chown -R app:app /app /tmp/.cache

# Copy project sources with root:root ownership + world-read so the `app`
# user can read+execute but cannot self-modify the image contents.
COPY --chmod=0555 ./chat /app/chat/
COPY --chmod=0555 ./config /app/config/
COPY --chmod=0555 ./connectors /app/connectors/
COPY --chmod=0555 ./guardrails /app/guardrails/
COPY --chmod=0555 ./routes /app/routes/
COPY --chmod=0555 ./services /app/services/
COPY --chmod=0555 ./utils /app/utils/
COPY --chmod=0444 ./celery_config.py /app/celery_config.py
COPY --chmod=0444 ./main_chatbot.py /app/main_chatbot.py
COPY --chmod=0444 ./main_compute.py /app/main_compute.py
COPY --chmod=0444 ./mcp_server.py /app/mcp_server.py
COPY --chmod=0444 ./rbac_model.conf /app/rbac_model.conf

USER app

EXPOSE 5006
# Entrypoint for chat container:
CMD ["python", "main_chatbot.py"]
