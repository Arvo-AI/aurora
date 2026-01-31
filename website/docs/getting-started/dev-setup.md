---
sidebar_position: 2
---

# Development Setup

Set up Aurora for local development and contributing to the project.

## Prerequisites

### Required Software

| Software | Version | Purpose |
|----------|---------|---------|
| Docker | 24.0+ | Container runtime |
| Docker Compose | 2.20+ (v2) | Container orchestration |
| Node.js | 18.x+ | Frontend development |
| Python | 3.11+ | Backend development |
| Make | Any | Build automation |
| Git | Any | Version control |

### Verify Installation

```bash
docker --version          # Docker version 24.0.0+
docker compose version    # Docker Compose version v2.20.0+
node --version           # v18.0.0+
python3 --version        # Python 3.11+
make --version           # GNU Make 3.81+
```

## Fork and Clone

### 1. Fork the Repository

1. Go to [github.com/Arvo-AI/aurora](https://github.com/Arvo-AI/aurora)
2. Click **Fork** in the top right
3. This creates a copy under your GitHub account

### 2. Clone Your Fork

```bash
git clone https://github.com/YOUR-USERNAME/aurora.git
cd aurora
```

### 3. Add Upstream Remote

```bash
git remote add upstream https://github.com/Arvo-AI/aurora.git
```

Verify remotes:
```bash
git remote -v
# origin    https://github.com/YOUR-USERNAME/aurora.git (fetch)
# origin    https://github.com/YOUR-USERNAME/aurora.git (push)
# upstream  https://github.com/Arvo-AI/aurora.git (fetch)
# upstream  https://github.com/Arvo-AI/aurora.git (push)
```

## Environment Setup

### 1. Create Environment File

```bash
cp .env.example .env
```

### 2. Configure Required Variables

Edit `.env` and add at minimum:

```bash
# Generate secure secrets
POSTGRES_PASSWORD=$(openssl rand -hex 32)
FLASK_SECRET_KEY=$(openssl rand -hex 32)
AUTH_SECRET=$(openssl rand -hex 32)

# Add your LLM API key (at least one required)
OPENROUTER_API_KEY=sk-or-v1-your-key-here
```

Or use the init script:
```bash
make init
# Then add your LLM API key to .env
```

## Start Development Stack

```bash
make dev
```

This command:
- Builds all Docker images
- Starts all services with hot-reload enabled
- Mounts source directories for live code changes

### Access Points

| Service | URL | Description |
|---------|-----|-------------|
| Frontend | http://localhost:3000 | Next.js with hot reload |
| Backend API | http://localhost:5080 | Flask API |
| Chatbot | ws://localhost:5006 | WebSocket server |
| Vault UI | http://localhost:8200 | Secrets management |
| SeaweedFS | http://localhost:8888 | File browser |

## Development Workflow

### Starting and Stopping

```bash
# Start all services
make dev

# Stop all services
make down

# Restart all services
make restart

# View logs (all services)
make logs

# View logs (specific service)
make logs aurora-server
make logs frontend
```

### Rebuilding

```bash
# Rebuild specific service after code changes
make rebuild-server

# Rebuild all containers
make build

# Rebuild without cache (clean build)
make build-no-cache

# Full clean rebuild
make dev-fresh
```

### Cleanup

```bash
# Stop containers, keep data
make down

# Stop containers, remove volumes (data loss!)
make clean

# Full cleanup: containers, volumes, images
make nuke
```

## Frontend Development

The frontend is located in `client/` and uses:
- **Next.js 15** with App Router
- **TypeScript** in strict mode
- **Tailwind CSS** for styling
- **shadcn/ui** component library
- **Auth.js** for authentication

### Directory Structure

```
client/
├── src/
│   ├── app/           # Next.js App Router pages
│   ├── components/    # React components
│   │   └── ui/        # shadcn/ui components
│   └── lib/           # Utilities
├── public/            # Static assets
├── package.json
└── tailwind.config.ts
```

### Running Commands

```bash
cd client

# Install dependencies
npm install

# Run linter
npm run lint

# Fix lint errors
npm run lint -- --fix

# Build for production
npm run build

# Type check
npx tsc --noEmit
```

### Path Aliases

The frontend uses `@/*` as a path alias to `./src/*`:

```typescript
// Instead of
import { Button } from '../../../components/ui/button'

// Use
import { Button } from '@/components/ui/button'
```

### Code Style

- Use functional components with hooks
- TypeScript strict mode enabled
- ESLint with `next/core-web-vitals` config
- Prettier for formatting (if configured)

## Backend Development

The backend is located in `server/` and uses:
- **Flask** for the REST API
- **Flask-SocketIO** for WebSocket (chatbot)
- **Celery** for background tasks
- **LangChain/LangGraph** for AI agents
- **psycopg2** for PostgreSQL

### Directory Structure

```
server/
├── main_compute.py      # Flask API entry point
├── main_chatbot.py      # WebSocket chatbot entry point
├── routes/              # Flask blueprints
├── connectors/          # Cloud provider integrations
├── agents/              # LangGraph agent definitions
├── utils/               # Shared utilities
├── requirements.txt     # Python dependencies
└── Dockerfile
```

### Rebuilding After Changes

Backend changes require rebuilding the container:

```bash
make rebuild-server
```

### Viewing Logs

```bash
# API server logs
docker logs -f aurora-server

# Celery worker logs
docker logs -f aurora-celery_worker-1

# Chatbot logs
docker logs -f aurora-chatbot
```

### Code Style

- Python 3.11+ features allowed
- Use type hints where practical
- Flask blueprints for route organization
- Logging at INFO level (use `logging.info()`)
- No print statements in production code

## Database

Aurora uses PostgreSQL for persistent storage.

### Access Database

```bash
docker exec -it aurora-postgres-1 psql -U aurora -d aurora_db
```

### Common Queries

```sql
-- List tables
\dt

-- Describe table
\d users

-- Exit
\q
```

## Testing Changes

### Frontend

```bash
cd client
npm run lint
npm run build
```

### Backend

```bash
# Rebuild and check logs
make rebuild-server
docker logs -f aurora-server
```

### Full Stack

```bash
# Restart everything
make restart
make logs
```

## Keeping Fork Updated

Before starting new work, sync with upstream:

```bash
git fetch upstream
git checkout main
git merge upstream/main
git push origin main
```

## Creating a Feature Branch

```bash
git checkout main
git pull upstream main
git checkout -b feature/your-feature-name
```

### Branch Naming

| Prefix | Use For |
|--------|---------|
| `feature/` | New features |
| `bugfix/` | Bug fixes |
| `hotfix/` | Urgent production fixes |
| `docs/` | Documentation updates |
| `refactor/` | Code refactoring |
| `test/` | Adding tests |
| `chore/` | Maintenance tasks |

## Submitting Changes

1. Commit your changes with clear messages
2. Push to your fork: `git push origin feature/your-feature-name`
3. Open a Pull Request against `Arvo-AI/aurora:main`
4. Fill out the PR template

See [CONTRIBUTING.md](https://github.com/arvo-ai/aurora/blob/main/CONTRIBUTING.md) for detailed guidelines.

## IDE Setup

### VS Code

Recommended extensions:
- ESLint
- Prettier
- Python
- Pylance
- Docker
- Tailwind CSS IntelliSense

### Settings

`.vscode/settings.json`:
```json
{
  "editor.formatOnSave": true,
  "editor.defaultFormatter": "esbenp.prettier-vscode",
  "[python]": {
    "editor.defaultFormatter": "ms-python.python"
  },
  "typescript.preferences.importModuleSpecifier": "non-relative"
}
```

## Troubleshooting

### "Module not found" in Frontend

```bash
cd client
rm -rf node_modules
npm install
```

### Backend Changes Not Reflected

```bash
make rebuild-server
```

### Database Connection Errors

```bash
# Check if postgres is running
docker ps | grep postgres

# Check postgres logs
docker logs aurora-postgres-1
```

### Port Conflicts

```bash
# Find what's using a port
lsof -i :3000
lsof -i :5080

# Kill the process or change Aurora's ports
```
