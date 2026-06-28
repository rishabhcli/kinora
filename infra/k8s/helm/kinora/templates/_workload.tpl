{{/* ---------------------------------------------------------------------- */}}
{{/* kinora.deployment — renders a Deployment for one backend role.          */}}
{{/* Context: dict "root" $ "role" <name> "cfg" <role values>.               */}}
{{/* Every backend role uses the same image + config + secret; only command, */}}
{{/* replicas, resources, probes, and ports differ.                          */}}
{{/* ---------------------------------------------------------------------- */}}
{{- define "kinora.deployment" -}}
{{- $root := .root -}}
{{- $role := .role -}}
{{- $cfg := .cfg -}}
{{- $isFrontend := eq $role "frontend" -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "kinora.fullname" $root }}-{{ $role }}
  labels:
    {{- include "kinora.labels" $root | nindent 4 }}
    {{- include "kinora.componentLabel" (dict "role" $role) | nindent 4 }}
spec:
  {{- if not (and $cfg.autoscaling $cfg.autoscaling.enabled) }}
  replicas: {{ $cfg.replicas }}
  {{- end }}
  selector:
    matchLabels:
      {{- include "kinora.roleSelectorLabels" (dict "root" $root "role" $role) | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "kinora.roleSelectorLabels" (dict "root" $root "role" $role) | nindent 8 }}
      annotations:
        # Roll pods when the config/secret content changes.
        checksum/config: {{ include (print $root.Template.BasePath "/configmap.yaml") $root | sha256sum }}
    spec:
      serviceAccountName: {{ include "kinora.serviceAccountName" $root }}
      {{- with $root.Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      securityContext:
        {{- toYaml $root.Values.podSecurityContext | nindent 8 }}
      containers:
        - name: {{ $role }}
          {{- if $isFrontend }}
          image: "{{ $root.Values.frontendImage.repository }}:{{ $root.Values.frontendImage.tag }}"
          imagePullPolicy: {{ $root.Values.frontendImage.pullPolicy }}
          {{- else }}
          image: "{{ $root.Values.image.repository }}:{{ $root.Values.image.tag }}"
          imagePullPolicy: {{ $root.Values.image.pullPolicy }}
          command:
            {{- toYaml $cfg.command | nindent 12 }}
          {{- end }}
          securityContext:
            {{- toYaml $root.Values.containerSecurityContext | nindent 12 }}
          {{- if gt (int $cfg.port) 0 }}
          ports:
            - name: http
              containerPort: {{ $cfg.port }}
              protocol: TCP
          {{- end }}
          {{- if not $isFrontend }}
          envFrom:
            - configMapRef:
                name: {{ include "kinora.configMapName" $root }}
            - secretRef:
                name: {{ include "kinora.secretName" $root }}
          {{- end }}
          {{- /* ---- Probes ---- */}}
          {{- if $cfg.httpProbe }}
          livenessProbe:
            httpGet:
              path: {{ $cfg.httpProbe.path }}
              port: {{ $cfg.httpProbe.port }}
            initialDelaySeconds: {{ $root.Values.probes.liveness.initialDelaySeconds }}
            periodSeconds: {{ $root.Values.probes.liveness.periodSeconds }}
            timeoutSeconds: {{ $root.Values.probes.liveness.timeoutSeconds }}
            failureThreshold: {{ $root.Values.probes.liveness.failureThreshold }}
          readinessProbe:
            httpGet:
              path: {{ $cfg.httpProbe.path }}
              port: {{ $cfg.httpProbe.port }}
            initialDelaySeconds: {{ $root.Values.probes.readiness.initialDelaySeconds }}
            periodSeconds: {{ $root.Values.probes.readiness.periodSeconds }}
            timeoutSeconds: {{ $root.Values.probes.readiness.timeoutSeconds }}
            failureThreshold: {{ $root.Values.probes.readiness.failureThreshold }}
          startupProbe:
            httpGet:
              path: {{ $cfg.httpProbe.path }}
              port: {{ $cfg.httpProbe.port }}
            periodSeconds: {{ $root.Values.probes.startup.periodSeconds }}
            failureThreshold: {{ $root.Values.probes.startup.failureThreshold }}
          {{- else if $cfg.tcpProbe }}
          livenessProbe:
            tcpSocket:
              port: {{ $cfg.tcpProbe.port }}
            initialDelaySeconds: {{ $root.Values.probes.liveness.initialDelaySeconds }}
            periodSeconds: {{ $root.Values.probes.liveness.periodSeconds }}
          readinessProbe:
            tcpSocket:
              port: {{ $cfg.tcpProbe.port }}
            initialDelaySeconds: {{ $root.Values.probes.readiness.initialDelaySeconds }}
            periodSeconds: {{ $root.Values.probes.readiness.periodSeconds }}
          {{- else if $cfg.execProbe }}
          livenessProbe:
            exec:
              command:
                {{- toYaml $cfg.execProbe.command | nindent 16 }}
            initialDelaySeconds: {{ $root.Values.probes.liveness.initialDelaySeconds }}
            periodSeconds: {{ $root.Values.probes.liveness.periodSeconds }}
            timeoutSeconds: {{ $root.Values.probes.liveness.timeoutSeconds }}
            failureThreshold: {{ $root.Values.probes.liveness.failureThreshold }}
          {{- end }}
          resources:
            {{- toYaml $cfg.resources | nindent 12 }}
          {{- if $root.Values.tmpVolume.enabled }}
          volumeMounts:
            - name: tmp
              mountPath: /tmp
          {{- end }}
      {{- if $root.Values.tmpVolume.enabled }}
      volumes:
        - name: tmp
          emptyDir:
            sizeLimit: {{ $root.Values.tmpVolume.sizeLimit }}
      {{- end }}
{{- end -}}
