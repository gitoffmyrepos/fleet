{{- define "fleet.name" -}}fleet{{- end -}}
{{- define "fleet.labels" -}}
app.kubernetes.io/name: {{ include "fleet.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
