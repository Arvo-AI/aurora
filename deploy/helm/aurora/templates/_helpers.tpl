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
