# ============================================
# Development Stage
# ============================================
FROM oven/bun:1.2.22-alpine AS dev

WORKDIR /app

# Install dependencies first (will be overridden by volume mount in dev)
COPY package.json bun.lock ./
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Expose port for Next.js
EXPOSE 3000

# Use the same entrypoint as prod to generate env-config.js at startup
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["bun", "run", "dev"]

# ============================================
# Production Builder Stage
# ============================================
# Pin builder to BUILDPLATFORM so bun resolves the correct linux SWC binary
# for the target architecture (e.g. linux-arm64-musl on Apple Silicon /
# ARM K8s nodes; linux-x64-musl on amd64 CI). Avoids shipping the 124 MB
# darwin-arm64 binary into the image.
FROM --platform=$BUILDPLATFORM oven/bun:1.2.22-alpine AS builder
ARG TARGETPLATFORM
ARG BUILDPLATFORM
ENV NEXT_TELEMETRY_DISABLED=1

# Declare build arguments for NEXT_PUBLIC_ variables

ARG NEXT_PUBLIC_BACKEND_URL
ENV NEXT_PUBLIC_BACKEND_URL=$NEXT_PUBLIC_BACKEND_URL


ARG NEXT_PUBLIC_WEBSOCKET_URL
ENV NEXT_PUBLIC_WEBSOCKET_URL=$NEXT_PUBLIC_WEBSOCKET_URL

# Feature flags (single NEXT_PUBLIC_ENABLE_* vars for both backend and frontend)
ARG NEXT_PUBLIC_ENABLE_OVH
ENV NEXT_PUBLIC_ENABLE_OVH=$NEXT_PUBLIC_ENABLE_OVH

ARG NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH
ENV NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH=$NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH

ARG NEXT_PUBLIC_ENABLE_SCALEWAY
ENV NEXT_PUBLIC_ENABLE_SCALEWAY=$NEXT_PUBLIC_ENABLE_SCALEWAY

ARG NEXT_PUBLIC_ENABLE_SHAREPOINT
ENV NEXT_PUBLIC_ENABLE_SHAREPOINT=$NEXT_PUBLIC_ENABLE_SHAREPOINT

WORKDIR /app

# Optimize Docker layer caching by copying package files and installing dependencies first
COPY package.json bun.lock ./
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install

# Copy the entire project after installing dependencies
COPY . .

# Build the Next.js app
RUN bun run build

# ============================================
# Production Runner Stage
# ============================================
FROM oven/bun:1.2.22-alpine AS prod

ENV NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production
WORKDIR /app

# Copy built application from the builder stage.
# NOTE: We copy the full node_modules from builder rather than running
# `bun install --production` here. Bun treats --production as implicitly
# frozen-lockfile, then reports the prod-only subtree as a "change" vs.
# the full lockfile and refuses to install (oven-sh/bun#5792, #19088, #20913).
# None of CI=, --no-frozen-lockfile, or unsetting NODE_ENV override it.
# A proper fix is either regenerating bun.lock as prod-only (breaks dev) or
# switching to `output: "standalone"` in next.config.ts (separate migration).
#
# Ownership is set via --chown on each COPY rather than a trailing
# `chown -R 1000:1000 /app`. A recursive chown after the copies rewrites
# every file's metadata, which creates a full COW duplicate of node_modules
# + .next (~1.5 GB of pure waste in a separate layer).
COPY --chown=1000:1000 --from=builder /app/.next ./.next
COPY --chown=1000:1000 --from=builder /app/public ./public
COPY --chown=1000:1000 --from=builder /app/node_modules ./node_modules
COPY --chown=1000:1000 --from=builder /app/package.json ./package.json

COPY --chown=1000:1000 docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Expose port and run Next.js
EXPOSE 3000
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["bun", "run", "start"]
