# ============================================
# Development Stage
# ============================================
FROM oven/bun:1.2.22-alpine AS dev

# Install Node.js to run the dev server. Bun's JSC engine has known memory
# retention under long-running Next.js dev loads (oven-sh/bun#27514, #29302),
# causing OOM-kills in Docker Desktop. Bun is still used for `install` because
# the leak only matters for long-lived processes.
RUN apk add --no-cache nodejs npm

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
CMD ["npm", "run", "dev"]

# ============================================
# Production Builder Stage
# ============================================
FROM oven/bun:1.2.22-alpine AS builder

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

WORKDIR /app

# Copy built application from the builder stage
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh && chown -R 1000:1000 /app

USER bun

# Expose port and run Next.js
EXPOSE 3000
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["bun", "run", "start"]
