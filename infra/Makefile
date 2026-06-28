# Kinora infra — local validation harness. Mirrors the checks in
# .github/workflows/infra-*.yml so you can run the whole gate locally before
# pushing. Tools that are absent are reported (not fatal) so partial runs work.
#
# Usage (from infra/):
#   make validate     # everything that's installed
#   make tf-fmt tf-validate hadolint yamllint helm-lint k8s-validate \
#        compose-config prom-check   # individual targets
#
# NOTHING here applies cloud infrastructure. Terraform runs init -backend=false
# + validate only; no providers are contacted with real credentials.

SHELL := /bin/bash
TF_DIR := terraform
HELM_CHART := k8s/helm/kinora
DOCKER := docker

.PHONY: help validate tf-fmt tf-fmt-fix tf-validate hadolint yamllint shellcheck \
        helm-lint k8s-validate kustomize-validate compose-config prom-check \
        am-check dashboards clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

validate: tf-fmt tf-validate hadolint yamllint helm-lint k8s-validate \
          kustomize-validate compose-config prom-check am-check dashboards ## Run every available check

# ---------------------------------------------------------------------------- #
# Terraform
# ---------------------------------------------------------------------------- #
tf-fmt: ## terraform fmt -check -recursive
	terraform -chdir=$(TF_DIR) fmt -check -recursive

tf-fmt-fix: ## terraform fmt -recursive (writes)
	terraform -chdir=$(TF_DIR) fmt -recursive

tf-validate: ## init -backend=false + validate every module + env root
	@set -euo pipefail; \
	for d in $$(find $(TF_DIR) -type f -name versions.tf -exec dirname {} \; | sort -u); do \
	  echo "== validate $$d =="; \
	  terraform -chdir="$$d" init -backend=false -input=false -no-color >/dev/null; \
	  terraform -chdir="$$d" validate -no-color; \
	done

# ---------------------------------------------------------------------------- #
# Docker / Dockerfiles
# ---------------------------------------------------------------------------- #
hadolint: ## hadolint the Dockerfiles (skips if hadolint absent)
	@if command -v hadolint >/dev/null 2>&1; then \
	  hadolint --config docker/.hadolint.yaml docker/backend.Dockerfile docker/desktop.Dockerfile && echo "hadolint OK"; \
	else echo "hadolint absent — skipped (runs in CI)"; fi

compose-config: ## docker compose config -q (base + observability layer)
	$(DOCKER) compose -f docker-compose.yml config -q
	$(DOCKER) compose -f docker-compose.yml -f docker-compose.observability.yml config -q
	@echo "compose config OK"

# ---------------------------------------------------------------------------- #
# YAML / shell
# ---------------------------------------------------------------------------- #
yamllint: ## yamllint infra/ (skips if yamllint absent)
	@if command -v yamllint >/dev/null 2>&1; then \
	  yamllint -c .yamllint.yaml . && echo "yamllint OK"; \
	else echo "yamllint absent — skipped (runs in CI)"; fi

shellcheck: ## shellcheck helper scripts (skips if shellcheck absent)
	@if command -v shellcheck >/dev/null 2>&1; then \
	  if compgen -G "scripts/*.sh" >/dev/null; then shellcheck scripts/*.sh && echo "shellcheck OK"; \
	  else echo "no helper scripts"; fi; \
	else echo "shellcheck absent — skipped"; fi

# ---------------------------------------------------------------------------- #
# Kubernetes
# ---------------------------------------------------------------------------- #
helm-lint: ## helm lint the chart (default + dev + prod) (skips if helm absent)
	@if command -v helm >/dev/null 2>&1; then \
	  helm lint $(HELM_CHART); \
	  helm lint $(HELM_CHART) -f $(HELM_CHART)/values.yaml -f $(HELM_CHART)/values-dev.yaml; \
	  helm lint $(HELM_CHART) -f $(HELM_CHART)/values.yaml -f $(HELM_CHART)/values-prod.yaml; \
	else echo "helm absent — skipped (runs in CI)"; fi

k8s-validate: ## helm template | kubeconform (skips if tools absent)
	@if command -v helm >/dev/null 2>&1 && command -v kubeconform >/dev/null 2>&1; then \
	  helm template kinora $(HELM_CHART) | kubeconform -strict -ignore-missing-schemas -summary -; \
	  helm template kinora $(HELM_CHART) -f $(HELM_CHART)/values.yaml -f $(HELM_CHART)/values-prod.yaml | kubeconform -strict -ignore-missing-schemas -summary -; \
	else echo "helm/kubeconform absent — skipped (runs in CI)"; fi

kustomize-validate: ## kubectl kustomize overlays | kubeconform (skips if tools absent)
	@if command -v kubectl >/dev/null 2>&1 && command -v kubeconform >/dev/null 2>&1; then \
	  for ov in dev prod; do \
	    echo "== kustomize $$ov =="; \
	    kubectl kustomize k8s/kustomize/overlays/$$ov | kubeconform -strict -ignore-missing-schemas -summary -; \
	  done; \
	else echo "kubectl/kubeconform absent — skipped (runs in CI)"; fi

# ---------------------------------------------------------------------------- #
# Observability
# ---------------------------------------------------------------------------- #
prom-check: ## promtool check rules + config (dockerized)
	$(DOCKER) run --rm --entrypoint promtool -v "$(CURDIR)/prometheus:/etc/prometheus" \
	  prom/prometheus:latest check config /etc/prometheus/prometheus.yml

am-check: ## amtool check alertmanager config (dockerized)
	$(DOCKER) run --rm --entrypoint amtool -v "$(CURDIR)/observability/alertmanager:/c" \
	  prom/alertmanager:latest check-config /c/alertmanager.yml

dashboards: ## validate Grafana dashboard JSON
	@for f in observability/grafana/dashboards/*.json; do \
	  python3 -c "import json,sys; json.load(open('$$f')); print('OK: $$f')"; \
	done

clean: ## remove transient terraform caches/locks
	find $(TF_DIR) -name '.terraform' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	find $(TF_DIR) -name '.terraform.lock.hcl' -delete 2>/dev/null || true
	@echo "cleaned terraform caches"
