# Use the official Python image as the base
FROM python:3.11-slim-bookworm

# Development Dockerfile for Chatbot
# This includes cloud CLI tools (gcloud, aws, az, terraform, helm) for local development
# when ENABLE_POD_ISOLATION=false (commands run directly in this container)
# 
# For production (Kubernetes), use Dockerfile-chatbot.dockerfile which is leaner
# since commands execute in isolated terminal pods (ENABLE_POD_ISOLATION=true)

ENV PYTHONPATH="/app"

# Set the working directory
WORKDIR /app

# Use HTTPS mirror with retries to handle transient mirror issues
RUN echo 'Acquire::Retries "3";' > /etc/apt/apt.conf.d/80-retries && \
    echo 'deb https://deb.debian.org/debian bookworm main' > /etc/apt/sources.list && \
    echo 'deb https://deb.debian.org/debian-security bookworm-security main' >> /etc/apt/sources.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
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
    git \
    jq \
    vim \
    nano \
    bash-completion \
    python3-venv \
    dnsutils && \
    rm -rf /var/lib/apt/lists/*

# Download the latest installer
ADD https://astral.sh/uv/0.8.22/install.sh /uv-installer.sh

# Run the installer then remove it
RUN sh /uv-installer.sh && rm /uv-installer.sh

# Ensure the installed binary is on the `PATH`
ENV PATH="/root/.local/bin/:$PATH"

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


# Install Node.js for MCP servers
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install kubectl for orchestrator to manage terminal pods
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        KUBECTL_ARCH="amd64"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        KUBECTL_ARCH="arm64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl -fsSL --http1.1 --tls-max 1.2 \
      https://dl.k8s.io/release/$(curl -fsSL --http1.1 --tls-max 1.2 https://dl.k8s.io/release/stable.txt)/bin/linux/${KUBECTL_ARCH}/kubectl \
      -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl

# Install Google Cloud SDK
RUN curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y google-cloud-sdk google-cloud-sdk-gke-gcloud-auth-plugin && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install AWS CLI v2 (multi-arch support)
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        AWS_CLI_ARCH="x86_64"; \
    elif [ "$ARCH" = "arm64" ]; then \
        AWS_CLI_ARCH="aarch64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_CLI_ARCH}.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    ./aws/install && \
    rm -rf awscliv2.zip aws/

# Install Azure CLI
RUN curl -fsSL "https://aka.ms/InstallAzureCLIDeb" -o azure-cli-install.sh && \
    chmod +x azure-cli-install.sh && \
    ./azure-cli-install.sh && \
    rm azure-cli-install.sh

# Install Terraform (multi-arch support)
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        TERRAFORM_ARCH="amd64"; \
    elif [ "$ARCH" = "arm64" ]; then \
        TERRAFORM_ARCH="arm64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    wget -q https://releases.hashicorp.com/terraform/1.7.5/terraform_1.7.5_linux_${TERRAFORM_ARCH}.zip && \
    unzip terraform_1.7.5_linux_${TERRAFORM_ARCH}.zip && \
    mv terraform /usr/local/bin/ && \
    rm terraform_1.7.5_linux_${TERRAFORM_ARCH}.zip

# Install Helm (multi-arch support)
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        HELM_ARCH="amd64"; \
    elif [ "$ARCH" = "arm64" ]; then \
        HELM_ARCH="arm64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl -fsSL https://get.helm.sh/helm-v3.14.0-linux-${HELM_ARCH}.tar.gz -o helm.tar.gz && \
    tar -xzf helm.tar.gz && \
    mv linux-${HELM_ARCH}/helm /usr/local/bin/ && \
    rm -rf helm.tar.gz linux-${HELM_ARCH}/

# Install eksctl (multi-arch support)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        EKSCTL_ARCH="amd64"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        EKSCTL_ARCH="arm64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    curl --silent --location "https://github.com/weaveworks/eksctl/releases/latest/download/eksctl_Linux_${EKSCTL_ARCH}.tar.gz" | tar xz -C /tmp && \
    mv /tmp/eksctl /usr/local/bin && \
    chmod +x /usr/local/bin/eksctl

# Install OVH CLI (multi-arch support)

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
    mv ovhcloud /usr/local/bin/ovhcloud && \
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

# Install Ansible
RUN uv pip install --no-cache-dir ansible --system

# Install Pulumi (multi-arch support)
RUN curl -fsSL https://get.pulumi.com | sh && \
    mv /root/.pulumi/bin/* /usr/local/bin/

# Upgrade pip & install base deps
RUN uv pip install --upgrade pip setuptools wheel --system

# Copy dependency files first for caching
COPY requirements.txt .

# Install dependencies for chatbot
RUN uv pip install -r requirements.txt --system

# Copy the project source
COPY . .

EXPOSE 5006

# Entrypoint for chat container:
CMD ["uv", "run", "main_chatbot.py"]
