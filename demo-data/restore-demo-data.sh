#!/bin/bash
#
# restore-demo-data.sh - Restore demo incident data for Aurora demo branch
#
# This script is called automatically by the demo-init service in docker-compose.yaml.
# It's idempotent - safe to run multiple times, will only restore data once.

set -euo pipefail

# Inside container, demo-data is mounted at /demo-data
SCRIPT_DIR="/demo-data"

echo "Demo Data Restore - Starting..."

# Database connection parameters
PGHOST="${POSTGRES_HOST:-postgres}"
PGPORT="${POSTGRES_PORT:-5432}"
PGUSER="${POSTGRES_USER:-aurora}"
PGDATABASE="${POSTGRES_DB:-aurora_db}"
export PGPASSWORD="${POSTGRES_PASSWORD:-}"

# Wait for postgres to be ready
echo "Waiting for PostgreSQL to be ready..."
RETRIES=0
MAX_RETRIES=30
while ! pg_isready -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" >/dev/null 2>&1; do
    RETRIES=$((RETRIES + 1))
    if [ "$RETRIES" -ge "$MAX_RETRIES" ]; then
        echo "ERROR: PostgreSQL did not become ready after ${MAX_RETRIES}s"
        exit 1
    fi
    sleep 1
done
echo "PostgreSQL is ready"

# Check if demo data already initialized
echo "Checking if demo data already loaded..."
DEMO_INCIDENT_EXISTS=$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -tAc \
    "SELECT EXISTS(SELECT 1 FROM incidents WHERE id = 'a867c4b3-f09c-4a4f-a2bc-1c390d967b37')" 2>/dev/null || echo "false")

if [ "$DEMO_INCIDENT_EXISTS" = "t" ]; then
    echo "Demo data already loaded. Skipping restore."
    echo "To reset demo data, run: make down && make dev"
    exit 0
fi

echo "Demo data not found. Restoring from snapshot..."

# Import SQL dump
echo "[1/3] Importing PostgreSQL data..."
if psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" < "$SCRIPT_DIR/aurora_db.sql" >/dev/null 2>&1; then
    echo "       Done"
else
    echo "       WARNING: SQL import had errors (may be harmless)"
fi

# Import additional demo incidents
if [ -f "$SCRIPT_DIR/demo-incident-2.sql" ]; then
    echo "       Importing demo incident 2..."
    if psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" < "$SCRIPT_DIR/demo-incident-2.sql" >/dev/null 2>&1; then
        echo "       Done"
    else
        echo "       WARNING: Demo incident 2 import had errors"
    fi
fi

# Extract Weaviate data
echo "[2/3] Restoring Weaviate vector data..."
if [ -d "/var/lib/weaviate" ]; then
    # Running inside a container with Weaviate volume mounted
    tar xzf "$SCRIPT_DIR/weaviate_data.tar.gz" -C /var/lib/weaviate/ 2>/dev/null && echo "       Done" || echo "       WARNING: Weaviate extraction failed"
else
    # Running from host - need to use docker to extract into volume
    echo "       Extracting into weaviate volume via docker..."
    docker run --rm \
        -v aurora_weaviate_data:/dest \
        -v "$SCRIPT_DIR:/backup:ro" \
        alpine tar xzf /backup/weaviate_data.tar.gz -C /dest 2>/dev/null && echo "       Done" || echo "       WARNING: Weaviate extraction failed"
fi

# Create marker table
echo "[3/3] Setting demo_initialized marker..."
psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" <<EOF >/dev/null 2>&1
CREATE TABLE IF NOT EXISTS demo_metadata (
    initialized BOOLEAN DEFAULT TRUE,
    restored_at TIMESTAMP DEFAULT NOW(),
    incident_id VARCHAR(255) DEFAULT 'a867c4b3-f09c-4a4f-a2bc-1c390d967b37'
);
INSERT INTO demo_metadata (initialized) VALUES (TRUE);
EOF
echo "       Done"

echo ""
echo "Demo data restore complete!"
echo "  - Demo incident 1: 'Database connection pool exhausted - payment-service'"
echo "    Source: PagerDuty | Cloud: GCP/GKE | Tools: Splunk, GitHub, Terminal"
echo "  - Demo incident 2: 'Memory leak causing OOMKilled pods - checkout-service'"
echo "    Source: Datadog | Cloud: AWS/EKS | Tools: kubectl, AWS CLI, GitHub, Web Search"
echo "  - Access: Sign up at http://localhost:3000 to view the incidents"
echo ""
