# ============================================
# Builder Stage - compiles native wheels into a venv that gets copied forward
# ============================================
FROM python:3.12 AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/root/.cache/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        python3-dev \
        curl \
        ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ADD https://astral.sh/uv/0.8.22/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv && \
    mv /root/.local/bin/uvx /usr/local/bin/uvx 2>/dev/null || true

RUN uv venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install -r /tmp/requirements.txt --python /opt/venv/bin/python

# ============================================
# Production Stage - runtime only, no compilers, no -dev headers
# ============================================
FROM python:3.12-slim AS prod

ENV PYTHONPATH="/app" \
    PATH="/opt/venv/bin:$PATH" \
    UV_CACHE_DIR="/tmp/.cache/uv"

WORKDIR /app

# Runtime apt deps only.
# - psycopg2-binary statically links libpq, so libpq-dev / libpq5 are not needed.
# - Aurora's Postgres runs in its own container; the postgresql server pkg is dropped.
# - postgresql-client kept defensively (~20 MB): runbook parser classifies psql
#   command hints, and IaC/terminal tools may shell out to psql from runbooks.
RUN echo 'Acquire::http::Pipeline-Depth "0";' > /etc/apt/apt.conf.d/99fixhash && \
    apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client \
        ca-certificates \
        curl \
        gnupg \
        lsb-release \
        wget \
        unzip \
        openssh-client \
        dnsutils \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Add Docker GPG key and repository
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && \
    chmod a+r /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" \
        | tee /etc/apt/sources.list.d/docker.list > /dev/null

# Docker CLI + Buildx + Compose plugin
RUN apt-get update && apt-get install -y --no-install-recommends \
        docker-ce-cli \
        docker-buildx-plugin \
        docker-compose-plugin \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Terraform (multi-arch)
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

# Google Cloud SDK + GKE auth plugin
RUN curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y --no-install-recommends \
        google-cloud-sdk \
        google-cloud-sdk-gke-gcloud-auth-plugin \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# AWS CLI v2 (multi-arch)
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

# Azure CLI (manual download)
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

# OVHcloud CLI (multi-arch)
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

# Scaleway CLI (multi-arch)
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

# Tailscale CLI (used by tailscale_ssh tool to join the tailnet)
RUN curl -fsSL https://tailscale.com/install.sh | sh

# Node.js 20.x (for MCP servers)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# kubectl (orchestrator manages terminal pods)
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

# Pre-built Python venv from builder stage
COPY --from=builder /opt/venv /opt/venv

# Application source
COPY ./chat /app/chat/
COPY ./config /app/config/
COPY ./connectors /app/connectors/
COPY ./routes /app/routes/
COPY ./services /app/services/
COPY ./utils /app/utils/
COPY ./celery_config.py /app/celery_config.py
COPY ./main_chatbot.py /app/main_chatbot.py
COPY ./main_compute.py /app/main_compute.py
COPY ./mcp_server.py /app/mcp_server.py
COPY ./rbac_model.conf /app/rbac_model.conf

EXPOSE 5006
CMD ["python", "main_chatbot.py"]

# ============================================
# Development Stage - same runtime, source mounted as volume by compose
# ============================================
FROM prod AS dev

EXPOSE 5006
CMD ["python", "main_chatbot.py"]
