# Developer-workspace persistent-capacity preflight (experimental)

**Status: C3 is an investigative candidate only; an observed project-ID escape
currently blocks adoption for an untrusted workspace.** This is a record of a
gated disposable-Talos preflight for OK-176, not an accepted storage profile,
contract amendment, production recommendation, or proof that enforcement works. No new
StorageClass is defined here. The existing classes and their guarantees remain
those in [ADR-Platform-009](https://github.com/openkubes/openkubes/blob/main/architecture/decisions/ADR-Platform-009-storage-contract.md). Envelope and workspace context is in [ADR-Platform-017](https://github.com/openkubes/openkubes/blob/main/architecture/decisions/ADR-Platform-017-constraint-envelopes.md) and [ADR-Platform-039](https://github.com/openkubes/openkubes/blob/main/architecture/decisions/ADR-Platform-039-developer-workspace-contract.md), whose human decision remains a separate OK-178 gate.

## Why C3, and what it does not mean

C3 is OpenEBS Dynamic LocalPV Hostpath v4.6.0 with XFS project hard quotas.
It is the smallest candidate to test first: it applies a hard per-volume limit
without adding a guest disk or running distributed storage inside a guest. The
pinned source uses a privileged, node-affined helper Pod and `xfs_quota` to
assign project IDs and set `bsoft`/`bhard`; its `openebs/linux-utils:4.6.0`
helper image bundles `xfsprogs`, `quota-tools`, and `util-linux`. The backing
BasePath must still be on XFS with project quotas enabled (`prjquota`/`pquota`)
and a working kernel quota implementation.

The chart defaults soft and hard size margins to 0%; the preflight must set
both explicitly to 0%, making each limit equal to the PVC request. These are
size margins, not a time-grace setting. Upstream documents quota resize as
unsupported. Treat a resized claim as unsupported: reject it or migrate data
into a newly sized claim; do not represent filesystem growth as verified quota
expansion. Migration may require downtime and temporarily double the storage
allocation while old and new claims coexist; neither cost is covered by the
capacity proof.

This is a **capacity** experiment, not durability evidence. A guest-local quota
volume remains local to its node/filesystem and does not acquire replica,
snapshot/restore, node-failure, or online-expansion guarantees. ADR-009 limits
`ok-storage-local` to scratch, cache, and reproducible data. Do not label this
proof `ok-storage-block`, do not introduce a nested distributed guest-storage
layer, and do not use an experimental class for persistent human workspaces.
The provisioner namespace needs the privilege for host-path setup; the developer
workspace/agent itself does **not** receive that privilege or host access.

C3 was chosen to investigate before Rawfile LocalPV v0.15.3 because Rawfile is
explicitly pre-v1 and adds privileged loop-device, `/dev`, and bidirectional
mount-propagation lifecycle. That choice is not an adoption recommendation:
neither option establishes VM backing durability.

## Observed security result — project-ID escape (adoption blocked)

In the disposable Talos preflight, a writer running as UID 1000 with no effective
capabilities changed an owned file's XFS project ID from 2 to 0 using
`xfs_io -r -c 'chproj 0'`. This is a real observed escape from project
assignment; UID/GID restriction, dropped capabilities, and
`allowPrivilegeEscalation: false` did not prevent it. The same writer then
wrote 160 MiB to that file inside the 128Mi PVC without error, so the hard limit
did not bound it. Until a mitigation is tested against this negative control, do
not claim C3 enforces runtime storage bounds against an untrusted workspace. C3 remains under
investigation only.

Candidate mitigation gates, not selected or proven:

- **User namespaces:** Kubernetes documents `spec.hostUsers: false` and states
  that within a user namespace the kernel restricts project-ID changes. In one
  disposable Talos 1.9.6 / Kubernetes 1.34.1 run, the same UID 1000 writer with
  `hostUsers: false` on a fresh 128Mi C3 PVC got `EINVAL` from `chproj 0`, kept
  its project ID, and its 160 MiB write stopped with `ENOSPC` at 128 MiB. Talos
  defaults `user.max_user_namespaces` to 0, so the worker needed that sysctl
  raised before such pods could start. The full 128/256 harness then passed
  with every writer at `hostUsers: false`: both reassignments returned `EINVAL`,
  both writers stopped with `ENOSPC` at exactly 128 and 256 MiB, and the
  same-XFS no-quota control wrote 160 MiB. The bound also held after an
  OpenEBS provisioner restart and after a Talos worker reboot: project IDs and
  hard limits were unchanged, retained data was intact, a full volume refused
  new files, and a volume provisioned after the restart got its own project ID
  and stopped at exactly its size. This only protects workloads that are required to run with
  `hostUsers: false`. The cluster's `local-path` class (under `/opt`) cannot
  be idmap-mounted on this worker, so such workloads cannot use it.
- **Targeted seccomp:** blocking project-ID-changing filesystem ioctls (such as
  the `FS_IOC_FSSETXATTR` path used to set XFS inode attributes) is another
  candidate. It needs integration proof that the workload cannot clear or
  replace its project ID through this or an alternate path, while ordinary
  writes still work and the privileged provisioner/helper lifecycle is unaffected.

Neither proposal is a workaround already implemented here; keep the over-limit
write after project-ID clearing as a required fail-closed test.

## Proof stages and launch horizon

These stages are deliberately distinct; completion of one does not authorize
production adoption. Disposable-cluster testing is authorized, with creation and
teardown owned by the test operator:

1. **Optional mechanism-only fixture:** if a direct-Docker genuine-XFS fixture
   is used, it can test kernel project-quota behavior. It does not run kind or
   the actual OpenEBS provisioner/helper Pod, so it is not C3 integration proof
   and is not a prerequisite to the Talos test. Do not substitute ext4, sparse-
   file size, or admission quota for a hard quota.
2. **Disposable Talos 1.9.6 integration:** the test operator has read back
   `diskQuotaSupport=true` and `/var` as XFS with `prjquota` in the current
   disposable guest, and added the provisioner-directory bind mount without a
   MachineConfig change. This establishes those mount prerequisites only. The
   hard-limit-overrun probe exposed the project-ID escape; the user-namespace
   mitigation and subsequent provisioner-restart/worker-reboot checks are recorded
   in the [2026-10-02 evidence bundle](../tests/c3-quota-preflight/evidence/2026-10-02-persistence/README.md).
   Cleanup/re-provision and orphan-cleanup proof remain separate obligations. The
   test operator owns cluster creation and teardown; record outcomes only after
   the relevant checks run.
3. **OK-178 launch readiness (future gate):** OK-178's horizon is a usable,
   publicly explainable developer-workspace capability end to end: OK-175
   implementation, OK-176 enforcement/lifecycle evidence, OK-177 second runtime,
   the human ADR-039 decision, living architecture/operator guidance, a clean-
   environment OpenCode quickstart, public docs/blog, and cross-links. This
   preflight is one input only; it does not meet the Epic, accept a storage
   contract, or claim production readiness.

The provider-neutral OK-176 decision remains open: admission-time PVC sizing
alone is not runtime enforcement. A portable workspace contract may express
persistent lifecycle semantics; an implementation profile must not offer that
profile until a conforming implementation enforces runtime capacity, unless a
human-approved contract decision says otherwise. The OK-176 decision rejects
admission-only PVC sizing as proof of enforced persistent bounds. Related work
is tracked as OK-174, OK-175, OK-177, and OK-178.

## Risk controls and test matrix

These are **required preflight checks or proposed controls, not completed results**.
The currently pinned upstream behavior is not a guarantee that every control is
implemented: in particular, the quota-ID allocator must not be described as
collision-proof. OpenEBS serializes its helper quota operations with `flock`,
but that does not serialize against kubelet quota-ID allocation. Its cleanup
script suppresses errors while resetting project/quota state; helper Job success
alone is not cleanup proof. Read quota state back.

| Case | Preflight evidence / control | Boundary or failure to expose |
|---|---|---|
| Hard byte limit | Two PVC sizes above backend minimum; write beyond request; measure allocated bytes (including checkout) and capture write, flush, and close status with bounded memory. Require `EDQUOT`/`ENOSPC` within stated tolerance. | A PVC request or `Bound` status is not proof. Include an independent volume on the same backing filesystem and verify it can still write with parent headroom reserved. |
| Negative control | Run the equivalent overfill test on non-enforcing local-path and require the check to fail as an enforcement claim. | Prevent a harness that passes regardless of backend behavior. |
| Filesystem-wide capacity | Track parent free bytes and inodes; reserve aggregate headroom/admission against multiple claims and other host users. Exercise byte and inode exhaustion separately. | Per-project limit does not prevent the parent filesystem filling first. Parent reserve/admission enforcement is not implemented here; define an operational stop threshold and fail closed before exhaustion. |
| Quota-ID collision/concurrency | Read existing project IDs and kubelet quota state; run concurrent claim provisioning; verify unique, disjoint assignments and correct per-volume limits before/after. | IDs share a filesystem namespace. The current `report | tail | awk + 1` allocator plus helper lock is not proof of coordination with kubelet or other allocators. If uniqueness cannot be demonstrated, stop; allocator hardening/coordination is an upstream or implementation follow-up, not an assumed workaround. |
| Escape and accounting edges | Test writes as an unprivileged workload user, nested directories, and open-unlinked files; attempt to clear an owned file's project ID and then exceed the hard limit. Confirm bytes/inodes remain charged and limits cannot be bypassed. | UID 1000/capabilities-dropped already changed project ID 2 to 0 in the disposable preflight. It then wrote 160 MiB into the 128Mi PVC. With `hostUsers: false` the change failed (EINVAL) and the full 128/256 harness passed. C3 without mandatory user namespaces is not approved for an untrusted runtime. |
| Cleanup and reuse | Stop writers before deleting claims. Read back project assignment and quota removal; repeat delete/re-provision and concurrent allocation. Inspect residual directories/IDs. | Upstream cleanup suppresses reset errors. Do not rely on Job success, or delete active data. Retention/reclaim behavior must be explicit; no destructive cleanup without a separate approved disposable fixture. |
| Resize | Request growth and verify the profile rejects it or follows a separately tested migration to a new claim. | Upstream says quota resize is unsupported; do not imply online expansion. |
| Restart/reboot | Repeat enforcement after provisioner restart and guest worker reboot; check quota records, mount flags, and existing data. | A mechanism-only direct-Docker fixture cannot show Talos boot behavior, runtime-restart persistence, or VM lifecycle recovery. |
| Durability / launch operability | Separately inspect VM disk backing, node affinity, recovery, snapshots, and operator workflow against their governing contracts. | Capacity proof is not VM backing durability, ADR-009 block conformance, or OK-178 launch readiness. |

### Project-ID isolation choice (unproven)

First try **coordinated ID allocation** only if the test can prove disjoint IDs
against kubelet and all existing project IDs, including under concurrent PVC
provisioning. Its advantage is no extra filesystem/device and simpler setup;
its cost is cross-allocator coordination and stronger cleanup/readback obligations
than the current helper lock provides. Do not claim the upstream `max + 1`
allocator is robust. If coordination cannot be demonstrated, stop rather than
silently sharing IDs.

A **dedicated XFS filesystem** is the stronger isolation alternative: it avoids
sharing that filesystem's project-ID namespace with kubelet and can contain
parent byte/inode exhaustion. It costs an explicitly owned disk/backing fixture,
mount lifecycle, reserved capacity, and additional approval/operations; it is
not an automatic workaround or a production design. Either path remains an
unproven option until tested.

## Source pins

- OpenEBS Dynamic LocalPV v4.6.0, commit
  [`7426127d566ac249dde04cad9e22a66f80049e0f`](https://github.com/openebs/dynamic-localpv-provisioner/tree/7426127d566ac249dde04cad9e22a66f80049e0f):
  [quota script](https://github.com/openebs/dynamic-localpv-provisioner/blob/7426127d566ac249dde04cad9e22a66f80049e0f/cmd/provisioner-localpv/app/quota_scripts.go),
  [helper behavior](https://github.com/openebs/dynamic-localpv-provisioner/blob/7426127d566ac249dde04cad9e22a66f80049e0f/cmd/provisioner-localpv/app/helper_hostpath.go),
  [Talos guidance](https://github.com/openebs/dynamic-localpv-provisioner/blob/7426127d566ac249dde04cad9e22a66f80049e0f/docs/installation/platforms/talos.md),
  [quota defaults and resize limit](https://github.com/openebs/dynamic-localpv-provisioner/blob/7426127d566ac249dde04cad9e22a66f80049e0f/docs/tutorials/hostpath/xfs_quota/enable-xfs-quota.md).
- OpenEBS helper image [`linux-utils:4.6.0`](https://github.com/openebs/linux-utils/blob/v4.6.0/dockerfiles/linux-utils/Dockerfile) bundles quota utilities; host XFS kernel and mount support are still required.
- Rawfile LocalPV v0.15.3, commit
  [`3010a8c6ac01023ee2a8d26872ce193f3c6b39d7`](https://github.com/openebs/rawfile-localpv/tree/3010a8c6ac01023ee2a8d26872ce193f3c6b39d7):
  [architecture](https://github.com/openebs/rawfile-localpv/blob/3010a8c6ac01023ee2a8d26872ce193f3c6b39d7/docs/architecture.md),
  [install/cleanup guidance](https://github.com/openebs/rawfile-localpv/blob/3010a8c6ac01023ee2a8d26872ce193f3c6b39d7/docs/install-guide.md),
  [pre-v1 warning](https://github.com/openebs/rawfile-localpv/blob/3010a8c6ac01023ee2a8d26872ce193f3c6b39d7/README.md).
- Talos v1.9.6 source: [worker quota default](https://github.com/siderolabs/talos/blob/v1.9.6/pkg/machinery/config/generate/worker.go#L71), [mount option](https://github.com/siderolabs/talos/blob/v1.9.6/internal/pkg/mount/v2/mount.go#L48), [EPHEMERAL mount](https://github.com/siderolabs/talos/blob/v1.9.6/internal/app/machined/pkg/runtime/v1alpha1/v1alpha1_sequencer_tasks.go#L1708). The disposable test has separately read back `diskQuotaSupport=true` and XFS `prjquota` on `/var`; this does not prove C3 hard-limit enforcement or escape protection.
- Kubernetes documents [project quota behavior and user-namespace requirement](https://kubernetes.io/docs/concepts/storage/ephemeral-storage/) and [Pod user namespaces (`hostUsers: false`)](https://kubernetes.io/docs/concepts/workloads/pods/user-namespaces/). Its quota text describes storage-use tracking; it does not itself prove this C3 PVC hard-limit profile.

## Decision record boundary

This document records a reversible experiment selection only. Promote a decision
to an ADR if, after proof and human review, the platform changes storage
contract guarantees or adopts a workspace storage profile. In particular, do
not change ADR-009's `ok-storage-*` guarantees or claim that this candidate
fulfills them based on this preflight.
