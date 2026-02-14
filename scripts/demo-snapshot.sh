#!/bin/bash
#
# demo-snapshot.sh - Save a read-only snapshot of the Aurora platform state.
#
# This script backs up all persistent data (database, vault, weaviate,
# seaweedfs, memgraph) WITHOUT modifying or stopping any running services.
#
# Usage:
#   ./scripts/demo-snapshot.sh [snapshot-name]
#
# Examples:
#   ./scripts/demo-snapshot.sh                      # Creates timestamped snapshot
#   ./scripts/demo-snapshot.sh incident-demo-ready  # Creates named snapshot

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

# Snapshot name: use argument or timestamp
SNAPSHOT_NAME="${1:-$(date +%Y%m%d_%H%M%S)}"
SNAPSHOT_DIR="$SNAPSHOTS_DIR/$SNAPSHOT_NAME"

# ── Preflight checks ──────────────────────────────────────────────────────

if [ -d "$SNAPSHOT_DIR" ]; then
    echo "Error: Snapshot '$SNAPSHOT_NAME' already exists at $SNAPSHOT_DIR"
    echo "Choose a different name or delete the existing snapshot first."
    exit 1
fi

# Verify postgres container is running (needed for pg_dump)
if ! docker inspect aurora-postgres >/dev/null 2>&1; then
    echo "Error: aurora-postgres container is not running."
    echo "Start the platform with 'make dev' first."
    exit 1
fi

# Verify all volumes exist
MISSING_VOLUMES=()
for vol in "${VOLUMES[@]}"; do
    full_name="${VOLUME_PREFIX}_${vol}"
    if ! docker volume inspect "$full_name" >/dev/null 2>&1; then
        MISSING_VOLUMES+=("$full_name")
    fi
done

if [ ${#MISSING_VOLUMES[@]} -gt 0 ]; then
    echo "Warning: The following volumes do not exist and will be skipped:"
    for mv in "${MISSING_VOLUMES[@]}"; do
        echo "  - $mv"
    done
    echo ""
fi

# ── Create snapshot ───────────────────────────────────────────────────────

mkdir -p "$SNAPSHOT_DIR"
echo "Creating snapshot '$SNAPSHOT_NAME'..."
echo "  Destination: $SNAPSHOT_DIR"
echo ""

# 1. Database: pg_dump (consistent logical backup while DB is running)
echo "[1/3] Backing up PostgreSQL database..."
if docker exec aurora-postgres pg_dump -U aurora aurora_db > "$SNAPSHOT_DIR/aurora_db.sql" 2>/dev/null; then
    DB_SIZE=$(du -h "$SNAPSHOT_DIR/aurora_db.sql" | cut -f1)
    echo "       Done ($DB_SIZE)"
else
    echo "       FAILED - pg_dump returned an error"
    rm -rf "$SNAPSHOT_DIR"
    exit 1
fi

# 2. Docker volumes: tar with read-only mount
echo "[2/3] Backing up Docker volumes..."
BACKED_UP=0
for vol in "${VOLUMES[@]}"; do
    full_name="${VOLUME_PREFIX}_${vol}"

    # Skip volumes that don't exist
    if ! docker volume inspect "$full_name" >/dev/null 2>&1; then
        echo "       Skipping $vol (volume not found)"
        continue
    fi

    printf "       %-30s" "$vol"

    # Mount volume as read-only (:ro), tar into snapshot dir
    if docker run --rm \
        -v "${full_name}:/source:ro" \
        -v "$SNAPSHOT_DIR:/backup" \
        alpine tar czf "/backup/${vol}.tar.gz" -C /source . 2>/dev/null; then
        FILE_SIZE=$(du -h "$SNAPSHOT_DIR/${vol}.tar.gz" | cut -f1)
        echo "$FILE_SIZE"
        BACKED_UP=$((BACKED_UP + 1))
    else
        echo "FAILED"
    fi
done
echo "       Backed up $BACKED_UP/${#VOLUMES[@]} volumes"

# 3. Environment config
echo "[3/3] Saving .env configuration..."
if [ -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env" "$SNAPSHOT_DIR/.env"
    echo "       Done"
else
    echo "       No .env file found, skipping"
fi

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
TOTAL_SIZE=$(du -sh "$SNAPSHOT_DIR" | cut -f1)
FILE_COUNT=$(find "$SNAPSHOT_DIR" -type f | wc -l | tr -d ' ')
echo "Snapshot complete: $SNAPSHOT_DIR"
echo "  Files: $FILE_COUNT"
echo "  Total size: $TOTAL_SIZE"
echo ""
echo "To restore this snapshot later, run:"
echo "  ./scripts/demo-restore.sh $SNAPSHOT_NAME"
