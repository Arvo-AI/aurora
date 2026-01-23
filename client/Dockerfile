# ============================================
# Development Stage
# ============================================
FROM oven/bun:1.2.22-alpine AS dev

WORKDIR /app

# Install dependencies first (will be overridden by volume mount in dev)
COPY package.json bun.lock ./
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install

# Expose port for Next.js
EXPOSE 3000

# Use bun dev with polling for file watching in Docker
# The --turbo flag enables Turbopack for faster reloads
CMD ["bun", "run", "dev"]

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

ARG NEXT_PUBLIC_ENABLE_SLACK
ENV NEXT_PUBLIC_ENABLE_SLACK=$NEXT_PUBLIC_ENABLE_SLACK

ARG NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH
ARG NEXT_PUBLIC_ENABLE_CONFLUENCE
ENV NEXT_PUBLIC_ENABLE_CONFLUENCE=$NEXT_PUBLIC_ENABLE_CONFLUENCEENV NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH=$NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH

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

# Expose port and run Next.js
EXPOSE 3000
CMD ["bun", "run", "start"]
