.PHONY: help dev down logs rebuild-server restart prod prod-build prod-logs prod-down clean nuke build-no-cache dev-fresh prod-clean prod-nuke prod-build-no-cache prod-fresh prod-local init prod-local-build prod-local-logs prod-local-down prod-local-clean prod-local-nuke

help:
	@echo "Available commands:"
	@echo "  make dev                - Build and start all containers in detached mode (Docker Compose)"
	@echo "  make (dev-)build        - Build all containers without starting them"
	@echo "  make build-no-cache     - Build all containers without using cache"
	@echo "  make down               - Stop and remove all containers"
	@echo "  make clean              - Stop containers and remove volumes"
	@echo "  make nuke               - Full cleanup: stop containers, remove volumes, images, and orphans"
	@echo "  make dev-fresh          - Full cleanup + rebuild without cache + start"
	@echo "  make logs               - Show logs for all containers (last 50 lines, follows)"
	@echo "  make logs <service>     - Show logs for specific service (e.g., make logs frontend)"
	@echo "  make rebuild-server     - Rebuild and restart the aurora-server container"
	@echo "  make restart            - Restart the Docker Compose stack"
	@echo ""
	@echo "Production:"
	@echo "  make prod               - Build and start all containers in production mode"
	@echo "  make prod-build         - Build all production containers without starting them"
	@echo "  make prod-build-no-cache - Build all production containers without using cache"
	@echo "  make prod-logs          - Show logs for all production containers (last 50 lines, follows)"
	@echo "  make prod-down          - Stop and remove all production containers"
	@echo "  make prod-clean         - Stop production containers and remove volumes"
	@echo "  make prod-nuke          - Full production cleanup: containers, volumes, images, orphans"
	@echo "  make prod-fresh         - Full production cleanup + rebuild without cache + start"
	@echo ""
	@echo "Local Production (for testing/evaluation):"
	@echo "  make init              - First-time setup (generates secrets, initializes Vault)"
	@echo "  make prod-local         - Build and start production-like stack locally"
	@echo "  make prod-local-build   - Build production-local containers without starting"
	@echo "  make prod-local-logs    - Show logs for production-local containers"
	@echo "  make prod-local-down    - Stop production-local containers"
	@echo "  make prod-local-clean   - Stop and remove production-local volumes"
	@echo "  make prod-local-nuke    - Full cleanup: containers, volumes, images"

rebuild-server:
	@echo "Stopping aurora-server container..."
	docker compose stop aurora-server
	@echo "Removing aurora-server container..."
	docker compose rm -f aurora-server
	@echo "Rebuilding aurora-server container..."
	docker compose build aurora-server
	@echo "Starting aurora-server container in detached mode..."
	docker compose up -d aurora-server
	@echo "aurora-server has been restarted and rebuilt!"

dev:
	@if [ ! -f .env ]; then \
		echo "Error: .env file not found."; \
		echo "Please run 'make init' first to set up your environment."; \
		exit 1; \
	fi
	docker compose up --build -d

dev-build: build
build:
	docker compose build

down:
	docker compose down

logs:
	@if [ -z "$(filter-out $@,$(MAKECMDGOALS))" ]; then \
		docker compose logs --tail 50 -f; \
	else \
		docker compose logs --tail 50 -f $(filter-out $@,$(MAKECMDGOALS)); \
	fi

restart:
	docker compose down
	docker compose up -d

# Build without cache
build-no-cache:
	@echo "Building all containers without cache..."
	docker compose build --no-cache

# Stop containers and remove volumes
clean:
	@echo "Stopping containers and removing volumes..."
	docker compose down -v

# Full cleanup: containers, volumes, images, and orphans
nuke:
	@echo "Performing full cleanup..."
	docker compose down -v --rmi local --remove-orphans
	@echo "Pruning dangling images..."
	docker image prune -f
	@echo "Cleanup complete!"

# Full cleanup + rebuild without cache + start
dev-fresh:
	@echo "Performing full fresh rebuild..."
	docker compose down -v --rmi local --remove-orphans
	@echo "Building without cache..."
	docker compose build --no-cache
	@echo "Starting containers..."
	docker compose up -d
	@echo "Fresh rebuild complete!"

# Production commands
prod:
	docker compose -f prod.docker-compose.yml up --build -d

prod-build:
	docker compose -f prod.docker-compose.yml build $(ARGS)

prod-logs:
	@if [ -z "$(filter-out $@,$(MAKECMDGOALS))" ]; then \
		docker compose -f prod.docker-compose.yml logs --tail 50 -f; \
	else \
		docker compose -f prod.docker-compose.yml logs --tail 50 -f $(filter-out $@,$(MAKECMDGOALS)); \
	fi

prod-down:
	docker compose -f prod.docker-compose.yml down

# Production build without cache
prod-build-no-cache:
	@echo "Building all production containers without cache..."
	docker compose -f prod.docker-compose.yml build --no-cache

# Stop production containers and remove volumes
prod-clean:
	@echo "Stopping production containers and removing volumes..."
	docker compose -f prod.docker-compose.yml down -v

# Full production cleanup: containers, volumes, images, and orphans
prod-nuke:
	@echo "Performing full production cleanup..."
	docker compose -f prod.docker-compose.yml down -v --rmi local --remove-orphans
	@echo "Pruning dangling images..."
	docker image prune -f
	@echo "Production cleanup complete!"

# Full production cleanup + rebuild without cache + start
prod-fresh:
	@echo "Performing full fresh production rebuild..."
	docker compose -f prod.docker-compose.yml down -v --rmi local --remove-orphans
	@echo "Building without cache..."
	docker compose -f prod.docker-compose.yml build --no-cache
	@echo "Starting production containers..."
	docker compose -f prod.docker-compose.yml up -d
	@echo "Fresh production rebuild complete!"

# Local Production commands (for testing/evaluation)
init:
	@echo "Setting up Aurora for local production testing..."
	@if [ ! -f .env ]; then \
		echo "Creating .env from .env.example..."; \
		cp .env.example .env; \
	fi
	@chmod +x scripts/generate-local-secrets.sh scripts/init-prod-vault.sh
	@echo "Generating secure secrets..."
	@./scripts/generate-local-secrets.sh
	@echo ""
	@echo "✓ Setup complete! Next steps:"
	@echo "  1. Edit .env and add your LLM API key (OPENROUTER_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY)"
	@echo "  2. Run: make dev (for development) or make prod-local (for production-like testing)"

prod-local:
	@if [ ! -f .env ]; then \
		echo "Error: .env file not found."; \
		echo "Please run 'make init' first to set up your environment."; \
		exit 1; \
	fi
	@if ! grep -q "^POSTGRES_PASSWORD=" .env || grep -q '^POSTGRES_PASSWORD=$$' .env; then \
		echo "Error: Secrets not generated. Run 'make init' first."; \
		exit 1; \
	fi
	@echo "Starting Aurora in production-local mode..."
	@docker compose -f docker-compose.prod-local.yml up --build -d
	@echo ""
	@echo "✓ Aurora is starting! Services will be available at:"
	@echo "  - Frontend: http://localhost:3000"
	@echo "  - Backend API: http://localhost:5000"
	@echo "  - Chatbot WebSocket: ws://localhost:5006"
	@echo "  - Vault UI: http://localhost:8200"
	@echo ""
	@echo "View logs with: make prod-local-logs"

prod-local-build:
	@echo "Building production-local containers..."
	@docker compose -f docker-compose.prod-local.yml build $(ARGS)

prod-local-logs:
	@if [ -z "$(filter-out $@,$(MAKECMDGOALS))" ]; then \
		docker compose -f docker-compose.prod-local.yml logs --tail 50 -f; \
	else \
		docker compose -f docker-compose.prod-local.yml logs --tail 50 -f $(filter-out $@,$(MAKECMDGOALS)); \
	fi

prod-local-down:
	@echo "Stopping production-local containers..."
	@docker compose -f docker-compose.prod-local.yml down

prod-local-clean:
	@echo "Stopping production-local containers and removing volumes..."
	@docker compose -f docker-compose.prod-local.yml down -v
	@echo "Note: .env file preserved. To remove it, delete manually."

prod-local-nuke:
	@echo "Performing full production-local cleanup..."
	@docker compose -f docker-compose.prod-local.yml down -v --rmi local --remove-orphans
	@echo "Pruning dangling images..."
	@docker image prune -f
	@echo "Production-local cleanup complete!"
	@echo "Note: .env file preserved. To remove it, delete manually."
%:
	@:
