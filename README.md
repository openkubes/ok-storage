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
| `ok-storage-block` (default) | RWO | Replicated (>=2), survives single node failure, snapshot/restore*, online expansion |
| `ok-storage-shared` | RWX | Shared storage required by KubeVirt live migration and multi-pod workloads, same durability as block |
| `ok-storage-local` | RWO | Node-local, non-replicated — scratch, cache, reproducible data only |

No manifest in any `ok-*` repo may reference an implementation-specific
StorageClass (e.g. `longhorn`, `rook-ceph-block`). The three names above are
the only stable interface.

\* Snapshot/restore requires a configured Longhorn backup target (S3/NFS),
which does not exist yet — see the Testing section below for the current
verification approach and status.

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

## Testing

`tests/` contains verification manifests for each contract StorageClass —
not part of the contract itself, just proof it actually works on this
cluster. Safe to apply and delete any time; nothing here is meant to be
left running.

**`ok-storage-block`** — confirms a replicated RWO volume schedules across
both nodes:

```bash
kubectl --kubeconfig ~/.kube/ok-infra.yaml apply -f tests/verify-block.yaml
kubectl --kubeconfig ~/.kube/ok-infra.yaml wait --for=condition=Ready pod/ok-storage-test --timeout=60s
kubectl --kubeconfig ~/.kube/ok-infra.yaml exec ok-storage-test -- sh -c 'echo hello > /data/hello.txt && cat /data/hello.txt'

# verify replica placement (expect one on each node)
PV=$(kubectl --kubeconfig ~/.kube/ok-infra.yaml get pvc ok-storage-test -o jsonpath='{.spec.volumeName}')
kubectl --kubeconfig ~/.kube/ok-infra.yaml -n longhorn-system get replicas.longhorn.io \
  -l longhornvolume=$PV -o custom-columns=NAME:.metadata.name,NODE:.spec.nodeID,STATE:.status.currentState

kubectl --kubeconfig ~/.kube/ok-infra.yaml delete -f tests/verify-block.yaml
```

**`ok-storage-shared`** — confirms two pods can mount the same volume at
once (real shared access, not just independent mounts):

```bash
kubectl --kubeconfig ~/.kube/ok-infra.yaml apply -f tests/verify-shared.yaml
kubectl --kubeconfig ~/.kube/ok-infra.yaml wait --for=condition=Ready pod/ok-storage-test-a pod/ok-storage-test-b --timeout=120s

kubectl --kubeconfig ~/.kube/ok-infra.yaml exec ok-storage-test-a -- sh -c 'echo "written by pod A" > /data/shared.txt'
kubectl --kubeconfig ~/.kube/ok-infra.yaml exec ok-storage-test-b -- cat /data/shared.txt   # expect: written by pod A

kubectl --kubeconfig ~/.kube/ok-infra.yaml delete -f tests/verify-shared.yaml
```

**Data duplication via CSI clone** (`ok-storage-block`) — confirms a
point-in-time copy of a volume's data lands in a new, independent PVC:

```bash
# 1. Create the source PVC + pod, write data
kubectl --kubeconfig ~/.kube/ok-infra.yaml apply -f tests/verify-clone.yaml
kubectl --kubeconfig ~/.kube/ok-infra.yaml wait --for=condition=Ready pod/ok-storage-test-src --timeout=60s
kubectl --kubeconfig ~/.kube/ok-infra.yaml exec ok-storage-test-src -- sh -c 'echo "before clone" > /data/state.txt'

# 2. Clone it into a new, independent PVC + pod
kubectl --kubeconfig ~/.kube/ok-infra.yaml apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ok-storage-test-cloned
spec:
  storageClassName: ok-storage-block
  dataSource:
    name: ok-storage-test-src
    kind: PersistentVolumeClaim
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: ok-storage-test-cloned
spec:
  containers:
    - name: test
      image: busybox:1.36
      command: ["sleep", "3600"]
      volumeMounts:
        - name: data
          mountPath: /data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: ok-storage-test-cloned
EOF
kubectl --kubeconfig ~/.kube/ok-infra.yaml wait --for=condition=Ready pod/ok-storage-test-cloned --timeout=60s

# 3. Change the source data, to prove the clone is independent, not a live link
kubectl --kubeconfig ~/.kube/ok-infra.yaml exec ok-storage-test-src -- sh -c 'echo "AFTER clone -- should not appear in the clone" > /data/state.txt'

# 4. Confirm: clone has the pre-change content, source has the post-change content
kubectl --kubeconfig ~/.kube/ok-infra.yaml exec ok-storage-test-cloned -- cat /data/state.txt   # expect: before clone
kubectl --kubeconfig ~/.kube/ok-infra.yaml exec ok-storage-test-src -- cat /data/state.txt       # expect: AFTER clone ...

# 5. Clean up
kubectl --kubeconfig ~/.kube/ok-infra.yaml delete pod/ok-storage-test-cloned pvc/ok-storage-test-cloned
kubectl --kubeconfig ~/.kube/ok-infra.yaml delete -f tests/verify-clone.yaml
```

> **Snapshot/restore via the Kubernetes-native `VolumeSnapshot` API is
> not yet usable.** Longhorn's CSI driver maps `VolumeSnapshot` to a
> Longhorn *backup* (upload to an external S3/NFS target), not a local
> snapshot — it fails with `missing input parameter` until a backup
> target is configured. `storageclasses/ok-storage-block-snapshot-class.yaml`
> is committed and ready for when that's set up, but is not functional
> today. See [`docs/snapshot-semantics.md`](docs/snapshot-semantics.md).
> The clone test above verifies the same underlying guarantee (point-in-time
> data duplication into an independent volume) without needing a backup
> target. Configuring a backup target is tracked as follow-up work.

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
│   ├── ok-storage-block.yaml             # RWO, replicated, default
│   ├── ok-storage-shared.yaml            # RWX, live migration
│   ├── ok-storage-local.yaml             # RWO, node-local, scratch
│   └── ok-storage-block-snapshot-class.yaml  # VolumeSnapshotClass for snapshot/restore
├── values/
│   └── longhorn-values.yaml      # version-controlled Longhorn HA parameters
├── scripts/
│   └── prereqs.sh                # open-iscsi + nfs-common host preflight
├── tests/
│   ├── verify-block.yaml             # ok-storage-block verification (see Testing)
│   ├── verify-shared.yaml            # ok-storage-shared verification (see Testing)
│   └── verify-clone.yaml             # ok-storage-block data-duplication (see Testing)
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
