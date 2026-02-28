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

# Check if base demo data (aurora_db.sql) already loaded
echo "Checking if base demo data already loaded..."
DEMO_INCIDENT_EXISTS=$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -tAc \
    "SELECT EXISTS(SELECT 1 FROM incidents WHERE id = 'a867c4b3-f09c-4a4f-a2bc-1c390d967b37')" 2>/dev/null || echo "false")

# Import SQL dump (only if not already loaded)
echo "[1/3] Importing PostgreSQL data..."
if [ "$DEMO_INCIDENT_EXISTS" = "t" ]; then
    echo "       Base demo data already loaded, skipping aurora_db.sql"
else
    echo "       Base data not found. Restoring from snapshot..."
    if psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" < "$SCRIPT_DIR/aurora_db.sql" >/dev/null 2>&1; then
        echo "       Done"
    else
        echo "       WARNING: SQL import had errors (may be harmless)"
    fi
fi

# Always run supplemental incident files - each has its own internal idempotency check
if [ -f "$SCRIPT_DIR/demo-incident-1.sql" ]; then
    echo "       Importing demo incident 1 postmortem..."
    if psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" < "$SCRIPT_DIR/demo-incident-1.sql" >/dev/null 2>&1; then
        echo "       Done"
    else
        echo "       WARNING: Demo incident 1 postmortem import had errors"
    fi
fi

if [ -f "$SCRIPT_DIR/demo-incident-2.sql" ]; then
    echo "       Importing demo incident 2..."
    if psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" < "$SCRIPT_DIR/demo-incident-2.sql" >/dev/null 2>&1; then
        echo "       Done"
    else
        echo "       WARNING: Demo incident 2 import had errors"
    fi
fi

if [ -f "$SCRIPT_DIR/demo-incident-3.sql" ]; then
    echo "       Importing demo incident 3..."
    if psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" < "$SCRIPT_DIR/demo-incident-3.sql" >/dev/null 2>&1; then
        echo "       Done"
    else
        echo "       WARNING: Demo incident 3 import had errors"
    fi
fi

if [ -f "$SCRIPT_DIR/demo-incident-4.sql" ]; then
    echo "       Importing demo incident 4..."
    if psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" < "$SCRIPT_DIR/demo-incident-4.sql" >/dev/null 2>&1; then
        echo "       Done"
    else
        echo "       WARNING: Demo incident 4 import had errors"
    fi
fi

if [ -f "$SCRIPT_DIR/demo-incident-coroot.sql" ]; then
    echo "       Importing demo incident (Coroot)..."
    if psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" < "$SCRIPT_DIR/demo-incident-coroot.sql" >/dev/null 2>&1; then
        echo "       Done"
    else
        echo "       WARNING: Demo incident (Coroot) import had errors"
    fi
fi

if [ -f "$SCRIPT_DIR/demo-incident-jenkins-bb.sql" ]; then
    echo "       Importing demo incident (Jenkins+Bitbucket+CloudBees)..."
    if psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" < "$SCRIPT_DIR/demo-incident-jenkins-bb.sql" >/dev/null 2>&1; then
        echo "       Done"
    else
        echo "       WARNING: Demo incident (Jenkins+Bitbucket+CloudBees) import had errors"
    fi
fi

if [ -f "$SCRIPT_DIR/demo-incident-dt-bp-te.sql" ]; then
    echo "       Importing demo incident (Dynatrace+BigPanda+ThousandEyes)..."
    if psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" < "$SCRIPT_DIR/demo-incident-dt-bp-te.sql" >/dev/null 2>&1; then
        echo "       Done"
    else
        echo "       WARNING: Demo incident (Dynatrace+BigPanda+ThousandEyes) import had errors"
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
echo "  - Demo incident 3: 'High 502 error rate on notification-service'"
echo "    Source: Grafana | Cloud: AWS/EKS | Tools: kubectl, Splunk, GitHub, Web Search"
echo "  - Demo incident 4: 'Intermittent 503 errors on payment-gateway service'"
echo "    Source: Netdata | Cloud: Azure/AKS | Tools: kubectl, Splunk, GitHub, Web Search"
echo "  - Demo incident (Coroot): 'Database connectivity issues - catalog service'"
echo "    Source: Coroot | Shows exact code lines + PR fix | NVIDIA Demo Ready"
echo "  - Access: Sign up at http://localhost:3000 to view the incidents"
echo ""
