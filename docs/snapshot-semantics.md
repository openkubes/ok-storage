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
