.PHONY: help verify prereqs install uninstall apply-classes status backup-target clean gpu-demo-apply gpu-demo-verify gpu-demo-remove

KUBECONFIG_FILE ?= $(HOME)/.kube/ok-infra.yaml
LONGHORN_NAMESPACE ?= longhorn-system
LONGHORN_VERSION ?= v1.7.0
HOSTS ?= ok-infra ok-gpu

## help: show available targets (default target -- bare `make` is a no-op by design)
help:
	@echo "ok-storage -- available targets:"
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /'

.DEFAULT_GOAL := help

## verify: run offline contract and manifest guards
verify:
	./scripts/verify-manifests.sh
	PYTHONDONTWRITEBYTECODE=1 python3 scripts/gpu-demo-storage.py self-test

## prereqs: install open-iscsi + nfs-common on every RKE2 host node
prereqs:
	@for h in $(HOSTS); do \
		echo "==> $$h"; \
		ssh root@$$h 'bash -s' < scripts/prereqs.sh; \
	done

## install: deploy Longhorn via Helm using the version-controlled HA values
install:
	helm repo add longhorn https://charts.longhorn.io
	helm repo update
	helm upgrade --install longhorn longhorn/longhorn \
		--kubeconfig $(KUBECONFIG_FILE) \
		--namespace $(LONGHORN_NAMESPACE) \
		--create-namespace \
		--version $(LONGHORN_VERSION) \
		-f values/longhorn-values.yaml
	$(MAKE) apply-classes

## apply-classes: apply the ok-storage-* contract StorageClasses.
## Also best-effort deletes the raw "longhorn" StorageClass the Helm chart
## creates -- but Longhorn's own controller recreates it on its next
## reconcile (observed: back within ~20s), so this does NOT durably remove
## it. Real enforcement of "reference ok-storage-*, never longhorn
## directly" (ADR-Platform-009) is code review / repo discipline, not a
## technical block. Left in as a courtesy for right-after-install checks.
apply-classes:
	kubectl --kubeconfig $(KUBECONFIG_FILE) apply -f storageclasses/
	kubectl --kubeconfig $(KUBECONFIG_FILE) delete storageclass longhorn --ignore-not-found

## status: show Longhorn nodes, volumes, and the contract StorageClasses
status:
	kubectl --kubeconfig $(KUBECONFIG_FILE) -n $(LONGHORN_NAMESPACE) get nodes.longhorn.io
	kubectl --kubeconfig $(KUBECONFIG_FILE) -n $(LONGHORN_NAMESPACE) get volumes.longhorn.io
	kubectl --kubeconfig $(KUBECONFIG_FILE) get storageclass ok-storage-block ok-storage-shared ok-storage-local

## uninstall: remove Longhorn (contract StorageClasses are left in place)
uninstall:
	helm uninstall longhorn --kubeconfig $(KUBECONFIG_FILE) -n $(LONGHORN_NAMESPACE) || true

## clean: remove the ok-storage-* StorageClasses (does not touch Longhorn itself)
clean:
	kubectl --kubeconfig $(KUBECONFIG_FILE) delete -f storageclasses/ --ignore-not-found

## gpu-demo-apply: explicitly enable the non-HA, ok-gpu-only demo StorageClass
gpu-demo-apply:
	GPU_DEMO_APPLY=$(GPU_DEMO_APPLY) python3 scripts/gpu-demo-storage.py apply \
		--kubeconfig $(KUBECONFIG_FILE)

## gpu-demo-verify: read-only verification of the GPU demo node tag and StorageClass
gpu-demo-verify:
	python3 scripts/gpu-demo-storage.py verify --kubeconfig $(KUBECONFIG_FILE)

## gpu-demo-remove: remove the unused GPU demo StorageClass and only its Longhorn tag
gpu-demo-remove:
	GPU_DEMO_REMOVE=$(GPU_DEMO_REMOVE) python3 scripts/gpu-demo-storage.py remove \
		--kubeconfig $(KUBECONFIG_FILE)
