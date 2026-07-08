# Snapshot Semantics

Per the ADR-Platform-009 review, ok-storage distinguishes two levels of
snapshot consistency. Consumers must know which one they get by default,
and what they must do themselves for the other.

## Platform-level: crash-consistent

Longhorn snapshots (`ok-storage-block`, `ok-storage-shared`) are
**crash-consistent** by default: the volume looks exactly as it would after
a sudden power loss. This is what the storage contract guarantees.

Crash-consistent is sufficient for most stateless-adjacent workloads and
filesystem-level recovery, but it does **not** guarantee that an
application's in-flight transactions are safe.

## Workload-level: application-consistent

Application-consistent snapshots (e.g. a database that has flushed its
buffers and quiesced writes before the snapshot is taken) are the
responsibility of the **workload**, not the platform:

- Databases should use their own logical backup/dump tooling, or
  pre/post-snapshot hooks (e.g. `pg_start_backup` / `pg_stop_backup`-style
  patterns) before triggering a Longhorn snapshot.
- ok-apps manifests that need this should document it explicitly rather
  than assume the platform snapshot is sufficient.

## Why this split

This mirrors "OpenKubes owns the contracts, not the components": the
platform commits to a durable, well-defined guarantee (crash-consistency)
that is implementation-independent. Application-consistency depends on
knowledge only the workload has, and is therefore explicitly out of scope
for `ok-storage`.

## Current gap: Kubernetes-native snapshot/restore needs a backup target

Found during OK-55 verification: Longhorn's CSI driver maps the
Kubernetes-native `VolumeSnapshot` API to a Longhorn **backup** (an upload
to an external S3/NFS backup target), not to a local, in-cluster snapshot.
Without a backup target configured, creating a `VolumeSnapshot` fails
(`missing input parameter`) — there is nowhere to upload to.

`storageclasses/ok-storage-block-snapshot-class.yaml` is committed and
will work once a backup target exists, but is not functional today.

Until then, `ok-storage-block`'s "snapshot/restore" guarantee is verified
via CSI **volume cloning** instead (`tests/verify-clone.yaml`): a new,
independent PVC populated from a point-in-time copy of an existing one.
This proves the same underlying capability (point-in-time data
duplication) without depending on an external backup target, but it is
not a full substitute — a clone stays on the same cluster and doesn't
protect against cluster-level loss, whereas a real backup would.

Configuring a Longhorn backup target (S3 or NFS) is tracked as follow-up
work, similar in spirit to the deferred Ceph migration: a known, deliberate
gap, not an oversight.
