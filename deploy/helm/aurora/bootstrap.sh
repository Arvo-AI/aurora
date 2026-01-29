#!/usr/bin/env bash
set -euo pipefail

OUTPUT_FILE="${1:-deploy/helm/aurora/values.generated.yaml}"

read -r -p "Registry (e.g. ghcr.io/org) [your-registry]: " REGISTRY
REGISTRY=${REGISTRY:-your-registry}

read -r -p "Frontend URL (https://aurora.example.com): " FRONTEND_URL
read -r -p "API URL (https://api.aurora.example.com): " API_URL
read -r -p "WebSocket URL (wss://ws.aurora.example.com): " WS_URL

read -r -p "S3 endpoint (https://s3.your-company.com): " S3_ENDPOINT
read -r -p "S3 bucket (aurora-storage): " S3_BUCKET
S3_BUCKET=${S3_BUCKET:-aurora-storage}

read -r -p "Postgres password: " POSTGRES_PASSWORD
read -r -p "Storage access key: " STORAGE_ACCESS_KEY
read -r -p "Storage secret key: " STORAGE_SECRET_KEY
read -r -p "Flask secret key: " FLASK_SECRET_KEY
read -r -p "Auth secret: " AUTH_SECRET
read -r -p "Vault token: " VAULT_TOKEN
read -r -p "OpenRouter API key (leave blank if using other LLM): " OPENROUTER_API_KEY

cat > "$OUTPUT_FILE" <<EOF
image:
  server: "${REGISTRY}/aurora-server:latest"
  frontend: "${REGISTRY}/aurora-frontend:latest"

config:
  NEXT_PUBLIC_BACKEND_URL: "${API_URL}"
  NEXT_PUBLIC_WEBSOCKET_URL: "${WS_URL}"
  FRONTEND_URL: "${FRONTEND_URL}"
  STORAGE_ENDPOINT_URL: "${S3_ENDPOINT}"
  STORAGE_BUCKET: "${S3_BUCKET}"

secrets:
  POSTGRES_PASSWORD: "${POSTGRES_PASSWORD}"
  STORAGE_ACCESS_KEY: "${STORAGE_ACCESS_KEY}"
  STORAGE_SECRET_KEY: "${STORAGE_SECRET_KEY}"
  FLASK_SECRET_KEY: "${FLASK_SECRET_KEY}"
  AUTH_SECRET: "${AUTH_SECRET}"
  VAULT_TOKEN: "${VAULT_TOKEN}"
  OPENROUTER_API_KEY: "${OPENROUTER_API_KEY}"
EOF

echo "Generated values file at ${OUTPUT_FILE}"
