#!/usr/bin/env bash
# Collect what a human or an agent needs to explain a preview run: container
# state, resource use and every service's logs, with secrets redacted.
# Safe to run whether the stack is healthy, half-booted or already failed, so
# it never exits non-zero.
#
# Usage:
#   scripts/preview/evidence.sh
#
# Output: preview-evidence/{ps.txt,stats.txt,logs/<service>.log}
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

if [ ! -f "$PREVIEW_ENV_FILE" ]; then
  echo "No preview env file, so no stack was started. Nothing to collect."
  exit 0
fi

mkdir -p "$PREVIEW_EVIDENCE_DIR/logs"

info "Container state"
compose ps -a > "$PREVIEW_EVIDENCE_DIR/ps.txt" 2>&1 || true

# Memory and CPU per container: the number to watch when sizing a per-PR
# environment on a shared cluster later.
ids="$(docker ps -q --filter "label=com.docker.compose.project=$PREVIEW_PROJECT")"
if [ -n "$ids" ]; then
  # shellcheck disable=SC2086
  docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' $ids \
    > "$PREVIEW_EVIDENCE_DIR/stats.txt" 2>&1 || true
fi

info "Logs"
for service in $(compose config --services 2>/dev/null); do
  compose logs --no-color --no-log-prefix "$service" 2>&1 \
    | redact > "$PREVIEW_EVIDENCE_DIR/logs/$service.log" || true
done

info "Evidence written to $PREVIEW_EVIDENCE_DIR"
exit 0
