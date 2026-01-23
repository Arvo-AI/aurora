---
sidebar_position: 1
---

# Architecture Overview

Aurora is a containerized application consisting of multiple services orchestrated via Docker Compose.

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Frontend (Next.js)                       │
│                         http://localhost:3000                    │
└─────────────────────────────────────────────────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                              ▼
┌─────────────────────────────┐    ┌─────────────────────────────┐
│     REST API (Flask)        │    │   Chatbot (WebSocket)       │
│   http://localhost:5080     │    │   ws://localhost:5006       │
└─────────────────────────────┘    └─────────────────────────────┘
                    │                              │
                    └──────────────┬──────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Celery Workers                              │
│                   (Background Task Processing)                   │
└─────────────────────────────────────────────────────────────────┘
                                   │
        ┌─────────────┬────────────┼────────────┬─────────────┐
        ▼             ▼            ▼            ▼             ▼
┌─────────────┐ ┌─────────┐ ┌──────────┐ ┌─────────┐ ┌───────────┐
│  PostgreSQL │ │  Redis  │ │ Weaviate │ │  Vault  │ │ SeaweedFS │
│    :5432    │ │  :6379  │ │   :8080  │ │  :8200  │ │   :8333   │
└─────────────┘ └─────────┘ └──────────┘ └─────────┘ └───────────┘
```

## Core Components

### Frontend (Next.js)

- **Technology**: Next.js 15, TypeScript, Tailwind CSS, shadcn/ui
- **Port**: 3000
- **Authentication**: Auth.js
- **Purpose**: User interface for investigations, settings, and connectors

### REST API (Flask)

- **Technology**: Flask, Python 3.11
- **Port**: 5080
- **Entry point**: `server/main_compute.py`
- **Purpose**: HTTP API for CRUD operations, authentication, connector management

### Chatbot (WebSocket)

- **Technology**: Flask-SocketIO
- **Port**: 5006
- **Entry point**: `server/main_chatbot.py`
- **Purpose**: Real-time chat interface for natural language investigations

### Celery Workers

- **Technology**: Celery with Redis broker
- **Purpose**: Background task processing for long-running operations
- **Tasks**: Investigation runs, connector sync, async operations

## Data Stores

### PostgreSQL

- **Port**: 5432
- **Database**: `aurora_db`
- **Purpose**: Primary data store for users, investigations, connector configs

### Redis

- **Port**: 6379
- **Purpose**: Celery task queue, session cache, real-time subscriptions

### Weaviate

- **Port**: 8080
- **Purpose**: Vector database for semantic search over logs and documents

### HashiCorp Vault

- **Port**: 8200
- **Purpose**: Secrets management for user credentials and API keys

### SeaweedFS

- **Port**: 8333 (S3 API), 8888 (File Browser)
- **Purpose**: S3-compatible object storage for file uploads

## Agent Architecture

Aurora uses LangGraph for agent orchestration:

```
User Query
    │
    ▼
┌─────────────────────┐
│   Agent Supervisor  │
└─────────────────────┘
    │
    ├──► Cloud Connector Agents (GCP, AWS, Azure)
    ├──► Observability Agents (Datadog, Grafana)
    └──► Communication Agents (Slack, PagerDuty)
```

Each agent can:
- Query external systems using configured credentials
- Synthesize findings into natural language
- Chain together for complex investigations

## Directory Structure

```
aurora/
├── client/              # Next.js frontend
│   └── src/
│       ├── app/         # App router pages
│       └── components/  # React components
├── server/              # Python backend
│   ├── routes/          # Flask blueprints
│   ├── connectors/      # Cloud/tool integrations
│   ├── agents/          # LangGraph agents
│   └── utils/           # Shared utilities
├── kubectl-agent/       # Kubernetes agent (optional)
└── website/             # Documentation (Docusaurus)
```

## Docker Compose Files

| File | Purpose |
|------|---------|
| `docker-compose.yaml` | Development stack |
| `docker-compose.prod-local.yml` | Production-local testing |
| `prod.docker-compose.yml` | Production deployment |
