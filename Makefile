.PHONY: help verify prereqs install uninstall apply-classes status backup-target clean gpu-demo-apply gpu-demo-verify gpu-demo-remove local-quota-apply local-quota-verify local-quota-remove c3-quota-preflight-self-test c3-quota-preflight-render c3-quota-preflight-check c3-quota-preflight-persist-self-test c3-quota-preflight-persist-render c3-quota-preflight-persist-check

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
	PYTHONDONTWRITEBYTECODE=1 python3 scripts/local-quota-storage.py self-test
	$(MAKE) c3-quota-preflight-self-test
	$(MAKE) c3-quota-preflight-persist-self-test

## c3-quota-preflight-self-test: offline negative-first tests for the C3 quota evidence checker
c3-quota-preflight-self-test:
	PYTHONDONTWRITEBYTECODE=1 python3 tests/c3-quota-preflight/c3_quota_preflight.py self-test

## c3-quota-preflight-render: render the pinned test-only OpenEBS 4.6.0 fixture; never applies it
c3-quota-preflight-render:
	@test -n "$(C3_NODE_NAME)" || (echo "FAIL: set C3_NODE_NAME to the disposable worker hostname" >&2; exit 2)
	@test -n "$(C3_WRITER_BUILDER_IMAGE)" || (echo "FAIL: set digest-pinned C3_WRITER_BUILDER_IMAGE" >&2; exit 2)
	@test -n "$(C3_RENDERED_MANIFEST)" || (echo "FAIL: set C3_RENDERED_MANIFEST output path" >&2; exit 2)
	PYTHONDONTWRITEBYTECODE=1 python3 tests/c3-quota-preflight/c3_render.py \
		--node-name "$(C3_NODE_NAME)" --writer-builder-image "$(C3_WRITER_BUILDER_IMAGE)" \
		--output "$(C3_RENDERED_MANIFEST)"

## c3-quota-preflight-check: check captured disposable-Talos/OpenEBS C3 workload output (no cluster mutation)
c3-quota-preflight-check:
	@test -n "$(C3_RESULTS)" || (echo "FAIL: set C3_RESULTS to captured writer output" >&2; exit 2)
	PYTHONDONTWRITEBYTECODE=1 python3 tests/c3-quota-preflight/c3_quota_preflight.py check-results \
		--results "$(C3_RESULTS)"

## c3-quota-preflight-persist-self-test: negative-first restart/reboot checker and phase render tests
c3-quota-preflight-persist-self-test:
	PYTHONDONTWRITEBYTECODE=1 python3 tests/c3-quota-preflight/c3_persist.py --self-test
	PYTHONDONTWRITEBYTECODE=1 python3 tests/c3-quota-preflight/c3_render.py --self-test

## c3-quota-preflight-persist-render: render p1, p2, or observer for the existing fixture; never applies
c3-quota-preflight-persist-render:
	@test -n "$(C3_PHASE)" || (echo "FAIL: set C3_PHASE to p1, p2, or observer" >&2; exit 2)
	@test -n "$(C3_NODE_NAME)" || (echo "FAIL: set C3_NODE_NAME to the disposable worker hostname" >&2; exit 2)
	@test -n "$(C3_WRITER_BUILDER_IMAGE)" || (echo "FAIL: set digest-pinned C3_WRITER_BUILDER_IMAGE" >&2; exit 2)
	@test -n "$(C3_RENDERED_MANIFEST)" || (echo "FAIL: set C3_RENDERED_MANIFEST output path" >&2; exit 2)
	PYTHONDONTWRITEBYTECODE=1 python3 tests/c3-quota-preflight/c3_render.py \
		--phase "$(C3_PHASE)" --node-name "$(C3_NODE_NAME)" \
		--writer-builder-image "$(C3_WRITER_BUILDER_IMAGE)" --output "$(C3_RENDERED_MANIFEST)"

C3_THROUGH ?= P2
## c3-quota-preflight-persist-check: check baseline and completed phases through C3_THROUGH (default P2)
c3-quota-preflight-persist-check:
	@test -n "$(C3_INITIAL_RESULTS)" || (echo "FAIL: set C3_INITIAL_RESULTS to initial-run capture" >&2; exit 2)
	@test -n "$(C3_RESULTS)" || (echo "FAIL: set C3_RESULTS to persistence capture" >&2; exit 2)
	PYTHONDONTWRITEBYTECODE=1 python3 tests/c3-quota-preflight/c3_persist.py \
		--initial "$(C3_INITIAL_RESULTS)" --capture "$(C3_RESULTS)" --through "$(C3_THROUGH)"

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

## local-quota-apply: install the non-contract quota profile in an explicit guest cluster
local-quota-apply:
	LOCAL_QUOTA_APPLY=$(LOCAL_QUOTA_APPLY) python3 scripts/local-quota-storage.py apply --kubeconfig "$(LOCAL_QUOTA_GUEST_KUBECONFIG)"

## local-quota-verify: operator-only proof in disposable/authorized environments; creates privileged hostPath observer; NOT tenant-callable readiness (tenants consume StorageClass only)
local-quota-verify:
	LOCAL_QUOTA_VERIFY="$(LOCAL_QUOTA_VERIFY)" LOCAL_QUOTA_EXPECTED_CONTEXT="$(LOCAL_QUOTA_EXPECTED_CONTEXT)" python3 scripts/local-quota-storage.py verify --kubeconfig "$(LOCAL_QUOTA_GUEST_KUBECONFIG)"

## local-quota-remove: remove the unused quota profile (namespace retained)
local-quota-remove:
	LOCAL_QUOTA_REMOVE=$(LOCAL_QUOTA_REMOVE) python3 scripts/local-quota-storage.py remove --kubeconfig "$(LOCAL_QUOTA_GUEST_KUBECONFIG)"
