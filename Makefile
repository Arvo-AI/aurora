.PHONY: help dev down logs rebuild-server restart prod prod-build prod-logs prod-down clean nuke build-no-cache dev-fresh prod-clean prod-nuke prod-build-no-cache prod-fresh prod-local init prod-local-build prod-local-logs prod-local-down prod-local-clean prod-local-nuke deploy-build deploy

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
	@echo "Production (local testing with prod builds):"
	@echo "  make prod               - Alias for prod-local (pulls prebuilt images)"
	@echo "  make prod-build         - Alias for prod-local-build (build from source)"
	@echo "  make prod-build-no-cache - Build all production containers without using cache"
	@echo "  make prod-logs          - Alias for prod-local-logs"
	@echo "  make prod-down          - Alias for prod-local-down"
	@echo "  make prod-clean         - Alias for prod-local-clean"
	@echo "  make prod-nuke          - Alias for prod-local-nuke"
	@echo "  make prod-fresh         - Full production cleanup + rebuild without cache + start"
	@echo ""
	@echo "Local Production (for testing/evaluation):"
	@echo "  make init              - First-time setup (generates secrets, initializes Vault)"
	@echo "  make prod-local         - Pull prebuilt images and start (no build needed)"
	@echo "  make prod-local-build   - Build from source and start"
	@echo "  make prod-local-logs    - Show logs for production-local containers"
	@echo "  make prod-local-down    - Stop production-local containers"
	@echo "  make prod-local-clean   - Stop and remove production-local volumes"
	@echo "  make prod-local-nuke    - Full cleanup: containers, volumes, images"
	@echo ""
	@echo "Kubernetes Deployment:"
	@echo "  make deploy-build      - Build and push images for K8s deployment (reads values.generated.yaml)"
	@echo "  make deploy            - Run deploy-build then deploy with Helm"

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
	@for ep in $$(docker network inspect aurora_default -f '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null); do docker network disconnect -f aurora_default $$ep 2>/dev/null; done; true
	docker compose down --remove-orphans

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

# Production commands (alias to prod-local for consistency)
prod: prod-local

prod-build: prod-local-build

prod-logs: prod-local-logs

prod-down: prod-local-down

prod-build-no-cache:
	@echo "Building all production containers without cache..."
	docker compose -f docker-compose.prod-local.yml build --no-cache

prod-clean: prod-local-clean

prod-nuke: prod-local-nuke

prod-fresh:
	@echo "Performing full fresh production rebuild..."
	docker compose -f docker-compose.prod-local.yml down -v --rmi local --remove-orphans
	@echo "Building without cache..."
	docker compose -f docker-compose.prod-local.yml build --no-cache
	@echo "Starting production containers..."
	docker compose -f docker-compose.prod-local.yml up -d
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
	@echo "Pulling prebuilt images from GHCR..."
	@docker pull ghcr.io/arvo-ai/aurora-server:latest
	@docker pull ghcr.io/arvo-ai/aurora-frontend:latest
	@echo "Tagging images for docker compose..."
	@docker tag ghcr.io/arvo-ai/aurora-server:latest aurora_server:latest
	@docker tag ghcr.io/arvo-ai/aurora-server:latest aurora_celery-worker:latest
	@docker tag ghcr.io/arvo-ai/aurora-server:latest aurora_celery-beat:latest
	@docker tag ghcr.io/arvo-ai/aurora-server:latest aurora_chatbot:latest
	@docker tag ghcr.io/arvo-ai/aurora-frontend:latest aurora_frontend:latest
	@echo "Starting Aurora in production-local mode..."
	@docker compose -f docker-compose.prod-local.yml up -d
	@echo ""
	@echo "✓ Aurora is starting! Services will be available at:"
	@echo "  - Frontend: http://localhost:3000"
	@echo "  - Backend API: http://localhost:5080"
	@echo "  - Chatbot WebSocket: ws://localhost:5006"
	@echo "  - Vault UI: http://localhost:8200"
	@echo ""
	@echo "View logs with: make prod-local-logs"

prod-local-build:
	@if [ ! -f .env ]; then \
		echo "Error: .env file not found."; \
		echo "Please run 'make init' first to set up your environment."; \
		exit 1; \
	fi
	@echo "Building from source and starting Aurora in production mode..."
	@docker compose -f docker-compose.prod-local.yml up --build -d
	@echo ""
	@echo "✓ Aurora is starting (built from source)!"
	@echo "  - Frontend: http://localhost:3000"
	@echo "  - Backend API: http://localhost:5080"
	@echo "  - Chatbot WebSocket: ws://localhost:5006"

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

# Kubernetes deployment commands
deploy-build:
	@echo "Building and pushing images for Kubernetes deployment..."
	@if [ ! -f deploy/helm/aurora/values.generated.yaml ]; then \
		echo "Error: values.generated.yaml not found. Copy values.yaml to values.generated.yaml and configure it."; \
		exit 1; \
	fi
	@echo "Extracting image registry and build args from values.generated.yaml..."
	@set -e; \
	IMAGE_REGISTRY=$$(yq '.image.registry' deploy/helm/aurora/values.generated.yaml); \
	if [ -z "$$IMAGE_REGISTRY" ] || [ "$$IMAGE_REGISTRY" = "null" ] || [ "$$IMAGE_REGISTRY" = "your-registry" ]; then \
		echo "Error: image.registry not configured in values.generated.yaml"; \
		exit 1; \
	fi; \
	GIT_SHA=$$(git rev-parse --short HEAD); \
	NEXT_PUBLIC_VARS=$$(yq '.config | keys | .[] | select(test("^NEXT_PUBLIC_"))' deploy/helm/aurora/values.generated.yaml); \
	BUILD_ARGS=""; \
	for var in $$NEXT_PUBLIC_VARS; do \
		value=$$(yq ".config.$$var" deploy/helm/aurora/values.generated.yaml); \
		if [ -n "$$value" ] && [ "$$value" != "null" ]; then \
			BUILD_ARGS="$$BUILD_ARGS --build-arg $$var=$$value"; \
		fi; \
	done; \
	echo "Using git SHA tag: $$GIT_SHA"; \
	echo "Building backend image: $$IMAGE_REGISTRY/aurora-server:$$GIT_SHA"; \
	docker buildx build --platform linux/amd64 -t $$IMAGE_REGISTRY/aurora-server:$$GIT_SHA -f server/Dockerfile --target prod ./server --push; \
	echo "Building frontend image: $$IMAGE_REGISTRY/aurora-frontend:$$GIT_SHA"; \
	docker buildx build --platform linux/amd64 -t $$IMAGE_REGISTRY/aurora-frontend:$$GIT_SHA \
		-f client/Dockerfile --target prod \
		$$BUILD_ARGS \
		./client --push; \
	ENABLE_POD_ISOLATION=$$(yq '.config.ENABLE_POD_ISOLATION' deploy/helm/aurora/values.generated.yaml); \
	if [ "$$ENABLE_POD_ISOLATION" = "true" ]; then \
		echo "Pod isolation enabled, building terminal image: $$IMAGE_REGISTRY/aurora-terminal:$$GIT_SHA"; \
		docker buildx build --platform linux/amd64 -t $$IMAGE_REGISTRY/aurora-terminal:$$GIT_SHA \
			-f server/Dockerfile-user-terminal \
			./server --push; \
		echo "Updating TERMINAL_IMAGE in values.generated.yaml..."; \
		yq -i ".config.TERMINAL_IMAGE = \"$$IMAGE_REGISTRY/aurora-terminal:$$GIT_SHA\"" deploy/helm/aurora/values.generated.yaml; \
	else \
		echo "Pod isolation disabled, skipping terminal image build"; \
	fi; \
	echo "Images built and pushed successfully with tag: $$GIT_SHA"; \
	echo "Updating values.generated.yaml with new tag..."; \
	yq -i ".image.tag = \"$$GIT_SHA\"" deploy/helm/aurora/values.generated.yaml

deploy: deploy-build
	@echo "Deploying to Kubernetes with Helm..."
	@helm upgrade --install aurora-oss ./deploy/helm/aurora \
		--namespace aurora --create-namespace \
		--reset-values \
		-f deploy/helm/aurora/values.generated.yaml
	@echo ""
	@echo "✓ Deployment complete!"
	@echo "Next: Initialize Vault (first time only) and verify deployment."
	@echo "  kubectl get pods -n aurora"

%:
	@:
