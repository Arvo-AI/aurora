---
sidebar_position: 2
---

# Services Reference

Detailed reference for all Aurora services.

## Service Ports

| Service | Port | Protocol | Description |
|---------|------|----------|-------------|
| Frontend | 3000 | HTTP | Next.js web application |
| REST API | 5080 | HTTP | Flask API server |
| Chatbot | 5006 | WebSocket | Real-time chat |
| PostgreSQL | 5432 | TCP | Primary database |
| Redis | 6379 | TCP | Task queue / cache |
| Weaviate | 8080 | HTTP | Vector database |
| Vault | 8200 | HTTP | Secrets management |
| SeaweedFS S3 | 8333 | HTTP | Object storage API |
| SeaweedFS UI | 8888 | HTTP | File browser |
| SeaweedFS Master | 9333 | HTTP | Cluster status |

## Frontend (aurora-frontend)

Next.js 15 application serving the web UI.

### Key Features
- Server-side rendering
- Auth.js authentication
- Tailwind CSS styling
- shadcn/ui components

### Environment Variables
```bash
NEXTAUTH_URL=http://localhost:3000
AUTH_SECRET=<secret>
```

### Development
```bash
cd client
npm run dev    # Start dev server
npm run lint   # Run linter
npm run build  # Production build
```

## REST API (aurora-server)

Flask application providing the HTTP API.

### Key Features
- Flask blueprints for route organization
- psycopg2 for PostgreSQL
- LangChain/LangGraph for agents

### Entry Point
```
server/main_compute.py
```

### Environment Variables
```bash
FLASK_SECRET_KEY=<secret>
FLASK_ENV=production
POSTGRES_HOST=postgres
REDIS_HOST=redis
```

### Rebuild
```bash
make rebuild-server
```

## Chatbot (aurora-chatbot)

WebSocket server for real-time chat.

### Key Features
- Flask-SocketIO
- Real-time message streaming
- Agent response handling

### Entry Point
```
server/main_chatbot.py
```

### Connection
```javascript
const socket = io('ws://localhost:5006');
```

## Celery Workers (aurora-celery_worker)

Background task processing.

### Key Features
- Redis as message broker
- Async task execution
- Retry handling

### View Logs
```bash
docker logs -f aurora-celery_worker-1
```

### Common Tasks
- Investigation runs
- Connector data sync
- Scheduled jobs

## PostgreSQL

Primary relational database.

### Connection
```bash
Host: postgres (in Docker) / localhost (external)
Port: 5432
Database: aurora_db
User: postgres
```

### Access
```bash
docker exec -it aurora-postgres-1 psql -U postgres -d aurora_db
```

## Redis

In-memory data store.

### Uses
- Celery task broker
- Session storage
- Pub/sub for real-time updates

### Access
```bash
docker exec -it aurora-redis-1 redis-cli
```

## Weaviate

Vector database for semantic search.

### Uses
- Log embeddings
- Document search
- Similarity queries

### API
```
http://localhost:8080/v1
```

## Vault

Secrets management.

### Uses
- User credential storage
- API key management
- Secure secret retrieval

### Access
```bash
# CLI
docker exec -it vault vault kv list aurora/users/

# UI
http://localhost:8200
```

## SeaweedFS

S3-compatible object storage.

### Uses
- File uploads
- Investigation artifacts
- Attachment storage

### Access
```bash
# S3 API
http://localhost:8333

# File browser
http://localhost:8888
```

## Health Checks

```bash
# API health
curl http://localhost:5080/health

# Vault health
curl http://localhost:8200/v1/sys/health

# All containers
docker compose ps
```
