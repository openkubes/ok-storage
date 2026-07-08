.PHONY: prereqs install uninstall apply-classes status backup-target clean

KUBECONFIG_FILE ?= $(HOME)/.kube/ok-infra.yaml
LONGHORN_NAMESPACE ?= longhorn-system
LONGHORN_VERSION ?= v1.7.0
HOSTS ?= ok-infra ok-gpu

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

## apply-classes: apply the ok-storage-* contract StorageClasses
apply-classes:
	kubectl --kubeconfig $(KUBECONFIG_FILE) apply -f storageclasses/

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
