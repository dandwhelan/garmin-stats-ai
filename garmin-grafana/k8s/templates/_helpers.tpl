{{- define "garmin-grafana.labels" -}}
app.kubernetes.io/name: {{ include "garmin-grafana.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
{{- define "garmin-grafana.name" -}}
{{ default .Chart.Name .Values.nameOverride }}
{{- end }}
