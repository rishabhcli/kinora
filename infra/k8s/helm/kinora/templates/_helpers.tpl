{{/* ---------------------------------------------------------------------- */}}
{{/* Naming helpers                                                          */}}
{{/* ---------------------------------------------------------------------- */}}

{{- define "kinora.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "kinora.fullname" -}}
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
{{- end -}}

{{- define "kinora.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common labels applied to every object. */}}
{{- define "kinora.labels" -}}
helm.sh/chart: {{ include "kinora.chart" . }}
{{ include "kinora.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: kinora
{{- end -}}

{{- define "kinora.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kinora.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Per-role selector labels (adds the role component). Use ONLY for
     spec.selector.matchLabels / Service selectors — never alongside
     kinora.labels in metadata (that would duplicate the name/instance keys). */}}
{{- define "kinora.roleSelectorLabels" -}}
{{ include "kinora.selectorLabels" .root }}
app.kubernetes.io/component: {{ .role }}
{{- end -}}

{{/* The component label only — pair with kinora.labels in metadata.labels. */}}
{{- define "kinora.componentLabel" -}}
app.kubernetes.io/component: {{ .role }}
{{- end -}}

{{- define "kinora.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "kinora.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* The Secret name a role's envFrom references. */}}
{{- define "kinora.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secret" (include "kinora.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "kinora.configMapName" -}}
{{- printf "%s-config" (include "kinora.fullname" .) -}}
{{- end -}}
