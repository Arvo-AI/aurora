#!/bin/bash
#
# demo-restore.sh - Restore the Aurora platform from a snapshot.
#
# This script stops the platform, restores all persistent data from a
# snapshot, then restarts everything.
#
# Usage:
#   ./scripts/demo-restore.sh <snapshot-name>
#   ./scripts/demo-restore.sh --list              # List available snapshots
#
# Examples:
#   ./scripts/demo-restore.sh incident-demo-ready
#   ./scripts/demo-restore.sh 20260212_150000

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SNAPSHOTS_DIR="$PROJECT_DIR/demo-snapshots"

# Docker volume prefix used by docker-compose (project name)
VOLUME_PREFIX="aurora"

# All named volumes from docker-compose.yaml
VOLUMES=(
    "postgres-data"
    "vault-data"
    "vault-init"
    "weaviate_data"
    "seaweedfs_master_data"
    "seaweedfs_volume_data"
    "seaweedfs_filer_data"
    "terraform-workdir"
    "memgraph-data"
)

# ── Handle --list flag ────────────────────────────────────────────────────

if [ "${1:-}" = "--list" ]; then
    echo "Available snapshots:"
    echo ""
    if [ -d "$SNAPSHOTS_DIR" ]; then
        for dir in "$SNAPSHOTS_DIR"/*/; do
            [ -d "$dir" ] || continue
            name=$(basename "$dir")
            size=$(du -sh "$dir" | cut -f1)
            file_count=$(find "$dir" -type f | wc -l | tr -d ' ')
            has_db="no"
            [ -f "$dir/aurora_db.sql" ] && has_db="yes"
            printf "  %-30s  size: %s  files: %s  db_backup: %s\n" "$name" "$size" "$file_count" "$has_db"
        done
    else
        echo "  No snapshots directory found at $SNAPSHOTS_DIR"
    fi
    exit 0
fi

# ── Validate arguments ────────────────────────────────────────────────────

if [ -z "${1:-}" ]; then
    echo "Usage: ./scripts/demo-restore.sh <snapshot-name>"
    echo "       ./scripts/demo-restore.sh --list"
    echo ""
    echo "Run with --list to see available snapshots."
    exit 1
fi

SNAPSHOT_NAME="$1"
SNAPSHOT_DIR="$SNAPSHOTS_DIR/$SNAPSHOT_NAME"

if [ ! -d "$SNAPSHOT_DIR" ]; then
    echo "Error: Snapshot '$SNAPSHOT_NAME' not found at $SNAPSHOT_DIR"
    echo ""
    echo "Available snapshots:"
    if [ -d "$SNAPSHOTS_DIR" ]; then
        ls -1 "$SNAPSHOTS_DIR" 2>/dev/null | sed 's/^/  /'
    else
        echo "  (none)"
    fi
    exit 1
fi

if [ ! -f "$SNAPSHOT_DIR/aurora_db.sql" ]; then
    echo "Error: Snapshot '$SNAPSHOT_NAME' is missing aurora_db.sql"
    echo "This does not appear to be a valid snapshot."
    exit 1
fi

# ── Confirmation ──────────────────────────────────────────────────────────

echo "Restore snapshot: $SNAPSHOT_NAME"
echo "  Source: $SNAPSHOT_DIR"
echo ""
echo "WARNING: This will:"
echo "  1. Stop all Aurora containers"
echo "  2. Replace ALL persistent data (database, vault, weaviate, etc.)"
echo "  3. Restart all Aurora containers"
echo ""
echo "Any data created since the snapshot will be LOST."
echo ""
read -p "Type 'yes' to proceed: " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    echo "Aborted."
    exit 0
fi

echo ""

# ── Step 1: Stop the platform ────────────────────────────────────────────

echo "[1/4] Stopping Aurora platform..."
cd "$PROJECT_DIR"
docker compose down --timeout 30 2>/dev/null || docker-compose down --timeout 30 2>/dev/null
echo "       Done"

# ── Step 2: Restore Docker volumes ───────────────────────────────────────

echo "[2/4] Restoring Docker volumes..."
RESTORED=0
for vol in "${VOLUMES[@]}"; do
    full_name="${VOLUME_PREFIX}_${vol}"
    archive="$SNAPSHOT_DIR/${vol}.tar.gz"

    if [ ! -f "$archive" ]; then
        echo "       Skipping $vol (no archive in snapshot)"
        continue
    fi

    printf "       %-30s" "$vol"

    # Remove existing volume (safe because containers are stopped)
    docker volume rm "$full_name" 2>/dev/null || true

    # Create fresh volume
    docker volume create "$full_name" >/dev/null

    # Extract archive into volume
    if docker run --rm \
        -v "${full_name}:/dest" \
        -v "$SNAPSHOT_DIR:/backup:ro" \
        alpine tar xzf "/backup/${vol}.tar.gz" -C /dest 2>/dev/null; then
        echo "OK"
        RESTORED=$((RESTORED + 1))
    else
        echo "FAILED"
    fi
done
echo "       Restored $RESTORED volumes"

# ── Step 3: Start the platform ───────────────────────────────────────────

echo "[3/4] Starting Aurora platform..."
cd "$PROJECT_DIR"
docker compose up -d 2>/dev/null || docker-compose up -d 2>/dev/null
echo "       Done"

# ── Step 4: Restore database ─────────────────────────────────────────────

echo "[4/4] Restoring PostgreSQL database..."

# Wait for postgres to be ready
echo "       Waiting for PostgreSQL to accept connections..."
RETRIES=0
MAX_RETRIES=30
while ! docker exec aurora-postgres pg_isready -U aurora -d aurora_db >/dev/null 2>&1; do
    RETRIES=$((RETRIES + 1))
    if [ "$RETRIES" -ge "$MAX_RETRIES" ]; then
        echo "       Error: PostgreSQL did not become ready after ${MAX_RETRIES}s"
        echo "       Try running manually: docker exec -i aurora-postgres psql -U aurora -d aurora_db < $SNAPSHOT_DIR/aurora_db.sql"
        exit 1
    fi
    sleep 1
done
echo "       PostgreSQL is ready"

# Drop and recreate the database to ensure a clean restore.
# pg_dump produces a logical dump (SQL statements) so we need a clean slate.
echo "       Dropping existing data..."
docker exec aurora-postgres psql -U aurora -d postgres -c "DROP DATABASE IF EXISTS aurora_db;" >/dev/null 2>&1
docker exec aurora-postgres psql -U aurora -d postgres -c "CREATE DATABASE aurora_db OWNER aurora;" >/dev/null 2>&1

echo "       Loading snapshot data..."
if docker exec -i aurora-postgres psql -U aurora -d aurora_db < "$SNAPSHOT_DIR/aurora_db.sql" >/dev/null 2>&1; then
    echo "       Done"
else
    echo "       Warning: pg_restore reported errors (some may be harmless)"
fi

# ── Restore .env if present ──────────────────────────────────────────────

if [ -f "$SNAPSHOT_DIR/.env" ]; then
    if [ -f "$PROJECT_DIR/.env" ]; then
        # Don't silently overwrite - user may have changed secrets
        if ! diff -q "$SNAPSHOT_DIR/.env" "$PROJECT_DIR/.env" >/dev/null 2>&1; then
            echo ""
            echo "Note: Snapshot contains a .env file that differs from current."
            echo "  Snapshot .env saved at: $SNAPSHOT_DIR/.env"
            echo "  Current .env left unchanged. Review and update manually if needed."
        fi
    else
        cp "$SNAPSHOT_DIR/.env" "$PROJECT_DIR/.env"
        echo "Restored .env from snapshot"
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "Restore complete!"
echo ""
echo "  Platform should be available at:"
echo "    Frontend:  http://localhost:3000"
echo "    Backend:   http://localhost:5080"
echo "    Vault:     http://localhost:8200"
echo ""
echo "  It may take 30-60 seconds for all services to fully initialize."
echo "  Run 'make logs' to monitor startup progress."
