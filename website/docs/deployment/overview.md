---
sidebar_position: 0
---

# Deployment Overview

Aurora supports three deployment targets. The deployment wizard (`./deploy/deploy.sh`) is the single entrypoint — it asks where you're deploying and routes to the right tool.

```
                  ┌─────────────────────────────────┐
                  │       ./deploy/deploy.sh        │
                  │   "What environment are you     │
                  │    deploying Aurora on?"        │
                  └───────────────┬─────────────────┘
                                  │
            ┌─────────────────────┼─────────────────────┐
            ▼                     ▼                     ▼
   ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
   │   Personal   │     │ VM or server │     │  Kubernetes  │
   │   computer   │     │              │     │   cluster    │
   └──────┬───────┘     └──────┬───────┘     └──────┬───────┘
          │                    │                     │
          ▼                    ▼                     │
   make init && make dev   aurora-deploy.sh          │
   (or prod-prebuilt)      (installs Docker,         │
                           configures, starts)       │
                                                     ▼
                              ┌─────────────────────────────────┐
                              │  Does the cluster environment   │
                              │  have internet access?          │
                              └───────────────┬─────────────────┘
                                              │
                            ┌─────────────────┴─────────────────┐
                            ▼                                   ▼
               ┌────────────────────────┐          ┌────────────────────────┐
               │          YES           │          │           NO           │
               └───────────┬────────────┘          └───────────┬────────────┘
                           │                                   │
                ┌──────────┴──────────┐           ┌────────────┴────────────┐
                ▼                     ▼           ▼                         ▼
     ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
     │ Build & push     │ │ Prebuilt images  │ │ Download bundle  │ │ On the bastion   │
     │                  │ │                  │ │                  │ │                  │
     │ k8s-deploy.sh    │ │ deploy-k8s-      │ │ Wizard downloads │ │ deploy-k8s-      │
     │                  │ │ airgap.sh        │ │ bundle, then     │ │ airgap.sh        │
     │ Builds from      │ │ --connected      │ │ transfer to      │ │ --airgap         │
     │ source, pushes   │ │                  │ │ bastion and      │ │                  │
     │ directly to      │ │ Pulls from GHCR, │ │ re-run wizard    │ │ Loads tarball,   │
     │ your registry    │ │ pushes to your   │ │                  │ │ pushes to reg,   │
     │                  │ │ private registry │ │                  │ │ helm deploy,     │
     │                  │ │                  │ │                  │ │ vault setup      │
     └──────────────────┘ └──────────────────┘ └──────────────────┘ └──────────────────┘
```

## Quick Start

**On a fresh VM** (zero dependencies):

```bash
curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/deploy/bootstrap.sh | bash
```

**From a cloned repo:**

```bash
./deploy/deploy.sh
```

## Deployment Guides

| Target | Guide | When to Use |
|--------|-------|-------------|
| **Personal computer** | [Docker Compose Setup](./docker-compose) | Local development and testing |
| **VM or server** | [VM Deployment](./vm-deployment) | Single-server production (standard or air-tight) |
| **Kubernetes** | [Kubernetes Deployment](./kubernetes) | Multi-node production with Helm |
| **Kubernetes (air-gapped)** | [Air-Gapped Kubernetes](./kubernetes-airgap) | Private registry or physically isolated clusters |

## Supporting Guides

| Guide | Description |
|-------|-------------|
| [EKS Cluster Setup](./eks-setup) | AWS-specific: CSI driver, S3 bucket, node groups |
| [Installing Docker](./install-docker) | Manual Docker install for all OS/arch, including offline |
| [Vault KMS Auto-Unseal](./vault-kms-setup) | Production Vault: auto-unseal with AWS KMS or GCP Cloud KMS |
| [Vault KMS — GCP](./vault-kms-gcp) | GCP-specific KMS setup |
