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
Resolve a third-party image reference.
When thirdPartyImages.registry is set (air-gapped), returns <registry>/<image>.
When empty (default), returns the upstream public reference.

Usage: {{ include "aurora.thirdPartyImage" (dict "root" . "image" "postgres:15-alpine" "default" "postgres:15-alpine") }}
       {{ include "aurora.thirdPartyImage" (dict "root" . "image" "weaviate:1.27.6" "default" "cr.weaviate.io/semitechnologies/weaviate:1.27.6") }}
*/}}
{{- define "aurora.thirdPartyImage" -}}
{{- $registry := .root.Values.thirdPartyImages.registry -}}
{{- if $registry -}}
{{ printf "%s/%s" $registry .image }}
{{- else -}}
{{ .default }}
{{- end -}}
{{- end }}
