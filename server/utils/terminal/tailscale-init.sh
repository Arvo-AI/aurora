#!/bin/bash
# Tailscale initialization script for terminal pods
# Called when TS_AUTH_KEY environment variable is present

set -e

if [ -z "$TS_AUTH_KEY" ]; then
    echo "No TS_AUTH_KEY set, skipping Tailscale setup"
    exit 0
fi

STATE_DIR="/home/appuser/.local/share/tailscale"
SOCKET_PATH="/tmp/tailscaled.sock"
LOG_FILE="/tmp/tailscale-init.log"

echo "Starting Tailscale initialization..." | tee "$LOG_FILE"

# Ensure state directory exists
mkdir -p "$STATE_DIR"

# Start tailscaled in userspace networking mode (no NET_ADMIN needed)
echo "Starting tailscaled in userspace mode..." | tee -a "$LOG_FILE"
tailscaled --state="${STATE_DIR}/tailscaled.state" \
           --socket="${SOCKET_PATH}" \
           --tun=userspace-networking \
           --statedir="$STATE_DIR" \
           2>&1 | tee -a "$LOG_FILE" &

TAILSCALED_PID=$!

# Wait for tailscaled to be ready
echo "Waiting for tailscaled to start..." | tee -a "$LOG_FILE"
for i in {1..30}; do
    if [ -S "$SOCKET_PATH" ]; then
        echo "tailscaled socket ready" | tee -a "$LOG_FILE"
        break
    fi
    sleep 1
done

if [ ! -S "$SOCKET_PATH" ]; then
    echo "ERROR: tailscaled socket not found after 30 seconds" | tee -a "$LOG_FILE"
    exit 1
fi

# Determine hostname for this terminal
# Use USER_HASH (pre-computed hash of user_id) for consistent one-device-per-user
# Falls back to USER_ID hash if USER_HASH not set, or generic name as last resort
HOSTNAME="aurora-terminal"
if [ -n "$USER_HASH" ]; then
    # Preferred: Use pre-computed user hash (first 8 chars)
    HOSTNAME="aurora-${USER_HASH:0:8}"
elif [ -n "$USER_ID" ]; then
    # Fallback: Compute hash from user_id
    USER_HASH=$(echo -n "$USER_ID" | sha256sum | cut -c1-8)
    HOSTNAME="aurora-${USER_HASH}"
fi

# Join tailnet with persistent auth key (one device per user)
# --force-reauth ensures we take over any existing device with this hostname
echo "Joining tailnet as ${HOSTNAME}..." | tee -a "$LOG_FILE"
tailscale --socket="${SOCKET_PATH}" up \
    --authkey="$TS_AUTH_KEY" \
    --hostname="$HOSTNAME" \
    --accept-routes \
    --force-reauth \
    2>&1 | tee -a "$LOG_FILE"

# Verify connection
echo "Checking Tailscale status..." | tee -a "$LOG_FILE"
tailscale --socket="${SOCKET_PATH}" status 2>&1 | tee -a "$LOG_FILE"

echo "Tailscale initialization complete" | tee -a "$LOG_FILE"

# Keep tailscaled running (script exits, but daemon continues)
wait $TAILSCALED_PID 2>/dev/null || true
