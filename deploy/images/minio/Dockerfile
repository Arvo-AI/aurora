# Rebuild of upstream MinIO from its GitHub release binary.
#
# MinIO deleted minio/minio and minio/mc from Docker Hub around 2026-09-11 and
# closed anonymous pulls on quay.io, so there is no public image left and
# dl.min.io now answers 410. GitHub release assets are the last live source.
# We pin the exact release the chart already shipped so existing minio-data
# PVCs stay readable — same binary means same on-disk format, no migration.
#
# This is a frozen stopgap: upstream is archived and will never issue another
# patch. New installs should use an external S3 bucket instead.
ARG MINIO_RELEASE=RELEASE.2025-04-22T22-12-26Z

# Fetch on the build host, never under emulation: the stage only curls and
# checksums, so cross-building for arm64 needs no QEMU.
FROM --platform=$BUILDPLATFORM alpine:3.21 AS fetch
ARG MINIO_RELEASE
ARG TARGETARCH
# Checksums published next to each release asset, pinned at authoring time so a
# swapped or tampered asset fails the build instead of shipping silently.
ARG SHA256_AMD64=53e2a2cb16c5366ea6fbbc479c19ddb4c6a0948273e752f740fb1fbf27bb817c
ARG SHA256_ARM64=6c2f3142c94240206123177f4ba1e360daa5d1e0a4962e90757ef4f92c3ab57c

# Deliberately unpinned: these only run in the throwaway fetch stage and never
# reach the final image, and pinned apk versions break when Alpine rebuilds.
RUN apk add --no-cache ca-certificates curl

RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) expected="$SHA256_AMD64" ;; \
      arm64) expected="$SHA256_ARM64" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 \
      --retry 3 --retry-delay 2 -o /tmp/minio \
      "https://github.com/minio/minio/releases/download/${MINIO_RELEASE}/minio.linux-${TARGETARCH}.${MINIO_RELEASE}"; \
    echo "${expected}  /tmp/minio" | sha256sum -c -; \
    chmod 0755 /tmp/minio

# The release binary is a static ELF (ET_EXEC, no dynamic interpreter), so it
# needs no libc — distroless/static keeps the attack surface near zero.
# Pinned by digest because `latest` is mutable; resolved from tag `latest`.
FROM gcr.io/distroless/static-debian12@sha256:d75cdd72874d4790092fcb1b058493ecf6bb5bf2b2b897045b00ff01d91843f2
ARG MINIO_RELEASE

LABEL org.opencontainers.image.title="aurora-minio" \
      org.opencontainers.image.description="Upstream MinIO ${MINIO_RELEASE} repackaged from its GitHub release binary, unmodified. Upstream Docker Hub images were deleted in September 2026." \
      org.opencontainers.image.version="${MINIO_RELEASE}" \
      org.opencontainers.image.source="https://github.com/minio/minio" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

COPY --from=fetch /tmp/minio /usr/bin/minio

# Matches the chart's podSecurityContext (runAsUser/fsGroup 1000) so the
# mounted PVC is writable; Kubernetes overrides this anyway.
USER 1000:1000

ENTRYPOINT ["/usr/bin/minio"]
