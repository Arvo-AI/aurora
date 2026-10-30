{{- define "aurora.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "aurora.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end }}

{{- define "aurora.labels" -}}
app.kubernetes.io/name: {{ include "aurora.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: aurora
{{- end }}

{{/*
Image reference. "image" is server, frontend, postgres, redis, vault, searxng,
memgraph, minio, or minioInit. Aurora images inherit image.registry and
image.tag; other images stay public until image.images.<name>.registry is set.
A per-image tag or non-default repository beats image.digests, so a release
pin cannot replace a private build. frontendImage fills empty frontend fields.
*/}}
{{- define "aurora.image" -}}
{{- $name := .image -}}
{{- $ctx := .global -}}
{{- $img := $ctx.Values.image | default dict -}}
{{- $over := dict -}}
{{- if $img.images -}}
{{- $over = index $img.images $name | default dict -}}
{{- end -}}
{{- /* image.images.minio unset: keep services.minio.image, whose path already includes the host. */ -}}
{{- if and (eq $name "minio") (not $over.registry) (not $over.repository) (not $over.tag) (not $over.digest) -}}
{{- include "aurora.minioImage" $ctx -}}
{{- /* initImage is already a full ref; a structured override replaces it below. */ -}}
{{- else if and (eq $name "minioInit") (not $over.registry) (not $over.repository) (not $over.tag) (not $over.digest) -}}
{{- (($ctx.Values.services).minio).initImage | default "amazon/aws-cli:2.34.6" -}}
{{- else -}}
{{- $defaults := dict
      "server" (dict "repository" "aurora-server" "aurora" true)
      "frontend" (dict "repository" "aurora-frontend" "aurora" true)
      "postgres" (dict "repository" "postgres" "tag" "15-alpine")
      "redis" (dict "repository" "redis" "tag" "7-alpine")
      "vault" (dict "repository" "hashicorp/vault" "tag" "1.15")
      "searxng" (dict "repository" "searxng/searxng" "tag" "2025.5.8-7ca24eee4")
      "memgraph" (dict "repository" "memgraph/memgraph-mage" "tag" "3.8.1")
      "minio" (dict "repository" "pgsty/silo" "registry" "docker.io" "tag" "RELEASE.2026-09-16T00-00-00Z" "digest" "sha256:635197cb9f36d01bee221d34d1c7d7960f6a95c48b0b6c01d99cd13bdae51a46")
      "minioInit" (dict "repository" "amazon/aws-cli" "tag" "2.34.6")
-}}
{{- $builtin := index $defaults $name -}}
{{- if not $builtin -}}
{{- fail (printf "aurora.image: unknown image %q" $name) -}}
{{- end -}}
{{- $repo := $over.repository | default "" -}}
{{- $reg := $over.registry | default "" -}}
{{- $tag := $over.tag | default "" -}}
{{- $digest := $over.digest | default "" -}}
{{- /* Legacy frontendImage only fills fields image.images.frontend left empty. */ -}}
{{- if and (eq $name "frontend") $ctx.Values.frontendImage -}}
{{- $legacy := $ctx.Values.frontendImage -}}
{{- if not $reg -}}{{- $reg = $legacy.registry | default "" -}}{{- end -}}
{{- if not $repo -}}{{- $repo = $legacy.repository | default "" -}}{{- end -}}
{{- if and (not $tag) $legacy.tag -}}{{- $tag = $legacy.tag -}}{{- end -}}
{{- if and (not $digest) $legacy.digest -}}{{- $digest = $legacy.digest -}}{{- end -}}
{{- end -}}
{{- if not $repo -}}{{- $repo = $builtin.repository -}}{{- end -}}
{{- $customRepo := ne $repo $builtin.repository -}}
{{- /* Stock name with no tag of its own follows the release digest pin. */ -}}
{{- if and (not $digest) (not $tag) (not $customRepo) $img.digests -}}
{{- $digest = index $img.digests $name | default "" -}}
{{- end -}}
{{- /* Custom repo: silo's digest (or any other default) would point at the wrong image. */ -}}
{{- if and (not $digest) (not $tag) (not $customRepo) $builtin.digest -}}
{{- $digest = $builtin.digest -}}
{{- end -}}
{{- if not $reg -}}
{{- if $builtin.aurora -}}
{{- $reg = $img.registry | default "" -}}
{{- /* Default host only for the stock repo. A full repository already includes one. */ -}}
{{- else if and $builtin.registry (not $customRepo) -}}
{{- $reg = $builtin.registry -}}
{{- end -}}
{{- end -}}
{{- if $digest -}}
{{- if $reg -}}{{- printf "%s/%s@%s" $reg $repo $digest -}}{{- else -}}{{- printf "%s@%s" $repo $digest -}}{{- end -}}
{{- else -}}
{{- if not $tag -}}
{{- if $builtin.aurora -}}
{{- /* A renamed image is not the build image.tag tracks. */ -}}
{{- if $customRepo -}}{{- $tag = $ctx.Chart.AppVersion -}}{{- else -}}{{- $tag = $img.tag | default $ctx.Chart.AppVersion -}}{{- end -}}
{{- else -}}
{{- $tag = $builtin.tag -}}
{{- end -}}
{{- end -}}
{{- if $reg -}}{{- printf "%s/%s:%s" $reg $repo $tag -}}{{- else -}}{{- printf "%s:%s" $repo $tag -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Pull secrets for the named image. Aurora images use image.pullSecrets.
Third-party images do too only when pointed at a private registry — a secret
that cannot see the public registry fails those pulls. Per-image pullSecrets
replace the global list; frontendImage.pullSecrets is the legacy frontend list.
*/}}
{{- define "aurora.imagePullSecrets" -}}
{{- $name := .image -}}
{{- $ctx := .global -}}
{{- $img := $ctx.Values.image | default dict -}}
{{- $over := dict -}}
{{- if $img.images -}}
{{- $over = index $img.images $name | default dict -}}
{{- end -}}
{{- $secrets := $over.pullSecrets -}}
{{- if and (not $secrets) (eq $name "frontend") $ctx.Values.frontendImage -}}
{{- $secrets = $ctx.Values.frontendImage.pullSecrets -}}
{{- end -}}
{{- if not $secrets -}}
{{- $private := $over.registry | default "" -}}
{{- if and (not $private) (eq $name "frontend") $ctx.Values.frontendImage -}}
{{- $private = $ctx.Values.frontendImage.registry | default "" -}}
{{- end -}}
{{- if or (or (eq $name "server") (eq $name "frontend")) $private -}}
{{- $secrets = $img.pullSecrets -}}
{{- end -}}
{{- end -}}
{{- if $secrets }}
imagePullSecrets:
  {{- toYaml $secrets | nindent 2 }}
{{- end }}
{{- end }}

{{/*
MinIO image when image.images.minio is unset. Digest wins over tag.
Inline defaults are load-bearing: `helm upgrade --reuse-values` skips new chart
defaults, so upgrades from <=1.5.1 arrive with services.minio.image unset.
*/}}
{{- define "aurora.minioImage" -}}
{{- $defaultRepo := "docker.io/pgsty/silo" -}}
{{- $defaultTag := "RELEASE.2026-09-16T00-00-00Z" -}}
{{- $defaultDigest := "sha256:635197cb9f36d01bee221d34d1c7d7960f6a95c48b0b6c01d99cd13bdae51a46" -}}
{{- $img := .Values.services.minio.image | default dict -}}
{{- $repo := $img.repository | default $defaultRepo -}}
{{- $digest := $img.digest | default "" -}}
{{- $tag := $img.tag | default "" -}}
{{- /* Nothing pinned: the verified digest, and only for the repo it was taken from. */ -}}
{{- if and (not $digest) (not $tag) (eq $repo $defaultRepo) -}}
{{- $digest = $defaultDigest -}}
{{- end -}}
{{- if $digest -}}
{{- printf "%s@%s" $repo $digest -}}
{{- else -}}
{{- printf "%s:%s" $repo ($tag | default $defaultTag) -}}
{{- end -}}
{{- end -}}

{{/*
max_connections for the in-cluster Postgres; see services.postgres.maxConnections.
Inline default is load-bearing: `helm upgrade --reuse-values` skips new chart
defaults, so upgrades from <=1.5.1 arrive with the key unset. Without it here
the connection-budget guard below compares against postgres:15-alpine's stock
100 and hard-fails a stock single-replica install.
*/}}
{{- define "aurora.postgresMaxConnections" -}}
{{- /* Parenthesized so it is nil-safe when services.postgres is absent entirely. */ -}}
{{- (.Values.services.postgres).maxConnections | default 300 -}}
{{- end -}}

{{/*
Pod scheduling block (tolerations, nodeSelector, affinity).
Pass a dict with "service" (key into .Values.scheduling) and "global" (top-level context).
When scheduling.<service> is set, it fully replaces the global defaults for that service.
*/}}
{{- define "aurora.scheduling" -}}
{{- $svc := .service -}}
{{- $ctx := .global -}}
{{- $tol := $ctx.Values.tolerations -}}
{{- $ns  := $ctx.Values.nodeSelector -}}
{{- $aff := $ctx.Values.affinity -}}
{{- if and $ctx.Values.scheduling (index $ctx.Values.scheduling $svc) -}}
  {{- $override := index $ctx.Values.scheduling $svc -}}
  {{- $tol = $override.tolerations -}}
  {{- $ns = $override.nodeSelector -}}
  {{- $aff = $override.affinity -}}
{{- end -}}
{{- if $tol }}
tolerations:
  {{- toYaml $tol | nindent 2 }}
{{- end }}
{{- if $ns }}
nodeSelector:
  {{- toYaml $ns | nindent 2 }}
{{- end }}
{{- if $aff }}
affinity:
  {{- toYaml $aff | nindent 2 }}
{{- end }}
{{- end }}

{{/*
Pod-level securityContext.
Merges global .Values.podSecurityContext with per-service UID/GID defaults.
Per-service override in .Values.podSecurityContextOverrides replaces the entire block.
Usage: include "aurora.podSecurityContext" (dict "service" "server" "global" $ "defaults" (dict "runAsUser" 1000 ...))
*/}}
{{- define "aurora.podSecurityContext" -}}
{{- $svc := .service -}}
{{- $ctx := .global -}}
{{- $defaults := .defaults -}}
{{- if and $ctx.Values.podSecurityContextOverrides (index $ctx.Values.podSecurityContextOverrides $svc) }}
{{- toYaml (index $ctx.Values.podSecurityContextOverrides $svc) }}
{{- else }}
{{- $merged := merge (deepCopy $ctx.Values.podSecurityContext) $defaults }}
{{- toYaml $merged }}
{{- end }}
{{- end }}

{{/*
Container-level securityContext.
Merges global .Values.containerSecurityContext with per-service defaults.
Per-service override in .Values.containerSecurityContextOverrides replaces the entire block.
Usage: include "aurora.containerSecurityContext" (dict "service" "server" "global" $ "defaults" (dict))
*/}}
{{- define "aurora.containerSecurityContext" -}}
{{- $svc := .service -}}
{{- $ctx := .global -}}
{{- $defaults := .defaults -}}
{{- if and $ctx.Values.containerSecurityContextOverrides (index $ctx.Values.containerSecurityContextOverrides $svc) }}
{{- toYaml (index $ctx.Values.containerSecurityContextOverrides $svc) }}
{{- else }}
{{- $merged := merge (deepCopy $ctx.Values.containerSecurityContext) $defaults }}
{{- toYaml $merged }}
{{- end }}
{{- end }}

{{/*
Resolve the secret name for each group.
If existingSecret is set, use the user-provided name; otherwise use the chart-managed name.
*/}}
{{- define "aurora.secretName.db" -}}
{{- if .Values.secrets.db.existingSecret -}}
{{- .Values.secrets.db.existingSecret -}}
{{- else -}}
{{- include "aurora.fullname" . -}}-secrets-db
{{- end -}}
{{- end -}}

{{- define "aurora.secretName.backend" -}}
{{- if .Values.secrets.backend.existingSecret -}}
{{- .Values.secrets.backend.existingSecret -}}
{{- else -}}
{{- include "aurora.fullname" . -}}-secrets-backend
{{- end -}}
{{- end -}}

{{- define "aurora.secretName.app" -}}
{{- if .Values.secrets.app.existingSecret -}}
{{- .Values.secrets.app.existingSecret -}}
{{- else -}}
{{- include "aurora.fullname" . -}}-secrets-app
{{- end -}}
{{- end -}}

{{- define "aurora.secretName.llm" -}}
{{- if .Values.secrets.llm.existingSecret -}}
{{- .Values.secrets.llm.existingSecret -}}
{{- else -}}
{{- include "aurora.fullname" . -}}-secrets-llm
{{- end -}}
{{- end -}}
