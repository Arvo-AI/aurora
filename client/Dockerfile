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
# BUILDPLATFORM pin keeps bun on the build host's arch so the native Linux SWC
# binary resolves correctly for the target instead of shipping a darwin one.
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

# We copy the full node_modules from the builder rather than re-installing with
# `bun install --production`: bun implicitly enables frozen-lockfile under
# --production and bails on the subtree difference (oven-sh/bun#5792, #19088).
# The real trim lives in a future `output: "standalone"` migration.
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
