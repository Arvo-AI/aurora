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
Per-service values in .Values.scheduling.<service> override the global defaults.
*/}}
{{- define "aurora.scheduling" -}}
{{- $svc := .service -}}
{{- $ctx := .global -}}
{{- $tol := $ctx.Values.tolerations -}}
{{- $ns  := $ctx.Values.nodeSelector -}}
{{- $aff := $ctx.Values.affinity -}}
{{- if and $ctx.Values.scheduling (index $ctx.Values.scheduling $svc) -}}
  {{- $override := index $ctx.Values.scheduling $svc -}}
  {{- if $override.tolerations -}}{{- $tol = $override.tolerations -}}{{- end -}}
  {{- if $override.nodeSelector -}}{{- $ns = $override.nodeSelector -}}{{- end -}}
  {{- if $override.affinity -}}{{- $aff = $override.affinity -}}{{- end -}}
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
