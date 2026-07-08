# ok-storage

**ok-storage** is the persistent storage layer of [OpenKubes](https://github.com/openkubes/openkubes).

It owns the storage **contract** consumed by every OpenKubes capability — ok-cluster,
ok-apps, and any Crossplane Composition that requests a PVC — never a specific
implementation.

> OpenKubes owns the contracts, not the components.
> ok-storage defines what a volume guarantees. Longhorn is today's implementation.
> See [ADR-Platform-009](https://github.com/openkubes/openkubes/blob/main/architecture/decisions/ADR-Platform-009-storage-contract.md)
> for the full decision record.

## The Contract

| StorageClass | Access Mode | Guarantees |
|---|---|---|
| `ok-storage-block` (default) | RWO | Replicated (>=2), survives single node failure, snapshot/restore, online expansion |
| `ok-storage-shared` | RWX | Shared storage required by KubeVirt live migration and multi-pod workloads, same durability as block |
| `ok-storage-local` | RWO | Node-local, non-replicated — scratch, cache, reproducible data only |

No manifest in any `ok-*` repo may reference an implementation-specific
StorageClass (e.g. `longhorn`, `rook-ceph-block`). The three names above are
the only stable interface.

## Current Implementation: Longhorn v1

Chosen for the current hardware: two bare-metal storage nodes (`ok-infra`,
`ok-gpu`), which rules out Ceph's 3-monitor quorum requirement. Longhorn
fulfills the full contract at `numberOfReplicas: 2`.

- Redundancy lives on exactly **one** layer — the RKE2 host cluster.
  Workload (Talos) clusters do not run their own Longhorn; nesting would be
  4x write amplification for no benefit.
- RWX (`ok-storage-shared`) is served via Longhorn's built-in share-manager
  and is what makes KubeVirt live migration possible.
- Both storage nodes are fully partitioned (no free NVMe), so Longhorn's
  data path is `/var/lib/longhorn` on the root filesystem with an explicit
  storage reservation — see [`values/longhorn-values.yaml`](values/longhorn-values.yaml).
- HA parameters are version-controlled here, not tribal knowledge.

### Migration path: Rook/Ceph

Ceph is **deferred, not rejected**. A 2-node Ceph cluster is an anti-pattern
(split-brain risk on quorum); Ceph becomes the preferred implementation once
a third storage node exists. Because consumers only ever reference the
`ok-storage-*` contract names, that swap is transparent to every workload.

## Usage

```bash
make prereqs        # install open-iscsi + nfs-common on ok-infra, ok-gpu
make install        # deploy Longhorn with the version-controlled HA values
make apply-classes  # apply ok-storage-block / -shared / -local
make status         # Longhorn nodes, volumes, and the contract StorageClasses
```

### Uninstall

`deletingConfirmationFlag` is left at its default (`false`) — Longhorn's own
safety guard against accidental deletion. `make uninstall` will fail with
`BackoffLimitExceeded` until you explicitly confirm:

```bash
kubectl --kubeconfig ~/.kube/ok-infra.yaml -n longhorn-system \
  patch settings.longhorn.io deleting-confirmation-flag --type merge -p '{"value":"true"}'
make uninstall
```

StorageClasses are untouched by `make uninstall` (`reclaimPolicy: Retain`);
run `make clean` separately if you also want those removed.

Consume it like any other StorageClass:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: example
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: ok-storage-block
  resources:
    requests:
      storage: 10Gi
```

## Repository Structure

```
ok-storage/
├── Makefile                     # install / uninstall / status lifecycle targets
├── storageclasses/
│   ├── ok-storage-block.yaml     # RWO, replicated, default
│   ├── ok-storage-shared.yaml    # RWX, live migration
│   └── ok-storage-local.yaml     # RWO, node-local, scratch
├── values/
│   └── longhorn-values.yaml      # version-controlled Longhorn HA parameters
├── scripts/
│   └── prereqs.sh                # open-iscsi + nfs-common host preflight
└── docs/
    └── snapshot-semantics.md     # crash- vs application-consistent snapshots
```

## Part of OpenKubes

```
OpenKubes
├── ok-local      — Local development (Multipass)
├── ok-cluster    — Cluster Lifecycle Engine
├── ok-linux      — OS profiles, Image Factory, MachineConfig
├── ok-storage    — Persistent Storage Contract  ← you are here
├── ok-gitops     — GitOps bootstrap (ArgoCD)
└── ok-apps       — Platform applications
```

- [OpenKubes](https://github.com/openkubes/openkubes)
- [OK-Cluster](https://github.com/openkubes/ok-cluster)
- [OK-Linux](https://github.com/openkubes/ok-linux)

## License

Apache 2.0 — see [LICENSE](LICENSE)
