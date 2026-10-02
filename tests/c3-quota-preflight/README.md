# OK-176 C3 disposable Talos/OpenEBS preflight

Test-only fixture; it never changes a production StorageClass. Render refuses an
existing output file and does not apply anything. All commands that contact the
cluster below belong to the test operator; rendering and checker self-tests are offline.

Every workload Pod uses `hostUsers: false`. Writers run as UID/GID 1000 with
all capabilities dropped and privilege escalation disabled. The build and
prepare-volume init containers also run in the Pod's user namespace; prepare-volume
chowns the mounted volume to the writer. The separate privileged observer uses
host users (the default), with read-only host mounts, and only reads quota evidence.

Prerequisite: the disposable worker must support Kubernetes user namespaces
(UserNamespacesSupport enabled, compatible kubelet/containerd/runc) and have a
nonzero `user.max_user_namespaces` sysctl. Talos 1.9.6 defaults it to zero: Pod
sandbox creation then fails with ENOSPC before the writer starts. The test operator
already set 11255 on the disposable worker for this investigation. This fixture
makes no Talos or cluster configuration changes.

The noquota PVC uses the test-only `ok176-c3-xfs-noquota` StorageClass,
defined in `workload.yaml.in` because the pinned chart renders only one class.
It uses the same `openebs.io/local` Hostpath provisioner, BasePath
`/var/openebs/ok176-c3`, node topology, Delete reclaim policy and
WaitForFirstConsumer binding as `ok176-c3-xfs`, with XFSQuota omitted (disabled).
Capture requires a `spec.local.path` under that BasePath; the checker requires
the noquota FS record to equal the quota volumes' XFS record and its 160 MiB
write to complete beyond 128Mi. This isolates the quota setting on the same
filesystem and provisioner.

HEADROOM records must occur exactly once before and after for each volume.
Only noquota measures parent-filesystem headroom, which must remain at least
1GiB in both snapshots on the proven shared XFS filesystem. Project-quota
statfs headroom must initially equal the declared 128/256/32 MiB capacity
for quota128/quota256/control and must not increase afterward; the 32Mi
control PVC still writes only 16 MiB. Invalid or missing measurements fail.

Observed 2026-10-02: local-path on Talos stores volumes under `/opt`, whose
filesystem does not support idmapped mounts on this worker. With
`hostUsers: false`, prepare-volume fails with runc's
`failed to set MOUNT_ATTR_IDMAP ... invalid argument`. These workloads cannot
use that class; local-path evidence is rejected.

Noquota project-ID/report readback is deliberately not required. A directory
without a quota assignment can use project 0, which the bounded-volume
readback rule rejects. The complete 160 MiB write on the same XFS filesystem
is the control's enforcement evidence; this avoids treating a missing project-0
report row as a failed capture. Observer readback remains required for both
bounded volumes.

The in-Pod readback init container is removed: unprivileged `xfs_quota -x report`
cannot supply reliable quota readback and must not create a pass. The observer's
before capture is the only READBACK/XFS_REPORT source. For quota128/quota256,
`quota_writer.c` opens the payload, reads FS_IOC_FSGETXATTR, sets only fsx_projid
to zero with FS_IOC_FSSETXATTR, then rereads the same file descriptor before
writing. Direct ioctl calls preserve the exact errno (unlike shell xfs_io status).
GET/reread failures terminate the writer without a valid reassignment record.

Pipe-delimited evidence additions:

Writer log records are wrapped by capture as `WRITER|label|podUID|originalRecord`.
The label comes from the fixed Pod inventory and the UID from an independent
API read, not the writer log. The checker requires both to match captured
status and the embedded record label. Runtime IDENTITY, USERNS, and (for quota
writers) REASSIGN must occur in that order before RESULT. Writer-originated
records without this source wrapper fail.

- `STATUS|phase|label|podUID|restarts|node|PV|PVpath|StorageClass|exit|finishedAt|podPhase|hostUsers`:
  both independently captured snapshots must end in literal `false`; true,
  absent, empty or a missing field fails.
- `IDENTITY|label|uid|gid|capEff`: every writer must report `1000|1000|0`.
- `USERNS|label|uidInside|uidOutside|uidCount|gidInside|gidOutside|gidCount`:
  exactly one UID and GID mapping is read from /proc/self. Each must start at
  inside zero, map to a nonzero parent ID, and cover UID/GID 1000. An initial
  namespace map contradicts `hostUsers: false` and fails.
- `REASSIGN|label|returnCode|errno|projectBefore|projectAfter`: exactly one per
  quota writer; require returnCode -1 and EINVAL(22), unchanged nonzero project
  ID equal to the observer READBACK. This deliberately fails on unexpected
  errors such as ENOTTY rather than mistaking an unsupported ioctl for protection.

Missing, duplicate, malformed or contradictory new records fail. The checker
retains the existing 128/256 bounds, distinct projects, control ordering,
noquota overfill, UID/restart, filesystem and headroom assertions. Written
bytes must also reach the capacity tolerance; invalid unlink flags and non-UTC
finishedAt values fail, and control completion is compared as parsed instants.

Test operator run sequence (from `ok-storage`; supply the disposable kubeconfig).
The old fixture must be removed before applying the new manifest. Capture its
PV inventory, events/logs and quota readback first as required by
`cleanup-resources.txt`; namespace/PVC deletion is destructive and remains with
the test operator. Inspect/remove any old fixture PVs and provisioner quota state
according to their reclaim policy before continuing; do not reuse old payloads.

```sh
set -Eeuo pipefail
mkdir -p artifacts
workload_kubeconfig=${WORKLOAD_ACCESS_FILE:?set WORKLOAD_ACCESS_FILE}
out=artifacts/ok176-c3-userns-render-final.yaml
capture=artifacts/ok176-c3-userns.capture
# Once old-fixture evidence and PV cleanup planning are complete:
kubectl --kubeconfig "$workload_kubeconfig" delete namespace ok176-c3
kubectl --kubeconfig "$workload_kubeconfig" wait --for=delete namespace/ok176-c3 --timeout=180s
# Complete old PV/quota cleanup from the inventory before applying.
# Skip rendering here if the reviewed fresh output already exists.
make c3-quota-preflight-render \
  C3_NODE_NAME=ok-176-c3-workers-jvmn2-vvcvr \
  C3_WRITER_BUILDER_IMAGE=gcc@sha256:82549aa8f90ada3236a8be70c74543132a76662ef33f0c3271ed802b81584a82 \
  C3_RENDERED_MANIFEST="$out"
test ! -e "$capture"
kubectl --kubeconfig "$workload_kubeconfig" apply -f "$out"
# Ready means each writer finished its init containers and is at the held gate.
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 wait --for=condition=Ready \
  pod/ok176-c3-quota-128 pod/ok176-c3-quota-256 pod/ok176-c3-control \
  pod/ok176-c3-noquota pod/ok176-c3-quota-observer --timeout=300s
bash tests/c3-quota-preflight/capture-status.sh \
  --kubeconfig "$workload_kubeconfig" --phase before --output "$capture"
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 patch configmap ok176-c3-gate \
  --type merge -p '{"data":{"start":"go"}}'
for pod in ok176-c3-quota-128 ok176-c3-quota-256; do
  kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 wait \
    --for=jsonpath='{.status.phase}'=Succeeded "pod/$pod" --timeout=300s
  test "$(kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 get pod "$pod" \
    -o jsonpath='{.status.containerStatuses[?(@.name=="writer")].state.terminated.exitCode}')" = 0
done
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 patch configmap ok176-c3-control-gate \
  --type merge -p '{"data":{"start":"go"}}'
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 wait \
  --for=jsonpath='{.status.phase}'=Succeeded \
  pod/ok176-c3-quota-128 pod/ok176-c3-quota-256 pod/ok176-c3-control \
  pod/ok176-c3-noquota --timeout=300s
bash tests/c3-quota-preflight/capture-status.sh \
  --kubeconfig "$workload_kubeconfig" --phase after --output "$capture"
make c3-quota-preflight-check C3_RESULTS="$capture"
# Preserve capture, rendered manifest, PV inventory, events/logs and quota readback.
# Test operator then performs teardown per cleanup-resources.txt, including
# fixture PVs/quota state, cluster-scoped chart objects and disposable cluster.
```

## Provisioner-restart and worker-reboot persistence run

The [2026-10-02 evidence bundle](evidence/2026-10-02-persistence/README.md)
contains the published initial and persistence captures, sanitized run logs,
the intermediate failure disclosure, and offline re-validation commands.

Offline `make verify` requires Python 3 with PyYAML for structural renderer
checks. Phase rendering itself uses only the Python standard library and does
not download the chart or invoke Helm. Existing-volume init containers adjust
only `/data/nested` ownership; they never recurse into retained payloads.

Run this only after the initial capture above passes. It reuses the four
original PVCs and their retained files. For a retry, the test operator first discards
the failed capture and removes the failed phase Pods/ConfigMaps and quota64 PVC/PV;
the new baseline and restart checkpoint both require quota64 to be absent. P1 creates one additional 64 MiB PVC;
P2 reuses only the four original PVCs. Neither phase deletes a PVC, namespace,
or original writer Pod. Each phase uses its own Pods and gate ConfigMaps so a
released gate or completed Pod cannot be mistaken for the other phase.

`capture-persist.sh` is read-only. It creates the baseline capture exclusively,
then accepts exactly P1-restarted, P1-before, P1-after, P2-before, and P2-after
in order. The restart checkpoint is captured before the P1 manifest is applied;
it requires the healthy provisioner Deployment UID to stay fixed, its sole Ready
Pod UID to change, and the quota64 PVC to remain absent. Each
step is built in a temporary file and installed atomically. A failed API read,
malformed log, changed Pod UID, or checker rejection leaves the last valid
capture unchanged. Observer records are bound to the independently read
observer Pod UID; writer records are bound to the independently read writer Pod
UID. The privileged observer is the only source of host mount, quota, retained
file, cleanup, SHA-256, and boot-ID evidence.

Before the initial status snapshot of each phase, capture verifies both gate ConfigMaps
still contain `start=hold`, verifies every Running writer actually mounts its
assigned gate read-only at `/gate`, and reads the worker boot ID while those Pods
are still held. It then reads the status snapshot again and requires it to be
byte-identical to the pre-boot held-state check, so the persisted `BOOT|before`
really precedes the recorded statuses without losing the held-state proof. Every
`STATUS` is immediately followed by a `MOUNT` record
binding that Pod UID to a `/data` mount without `subPath` and the expected PVC. Each
baseline/after observer snapshot includes `OBSERVER_SPEC`, which fixes the
privileged image and the exact read-only `/data`, `/host-var`, `/host-boot-id`,
and `/dev` hostPath sources and types. P1's before and after boot IDs must equal
the baseline; P2's before and after IDs must equal each other and differ from P1.

Set the values below from the already-proven disposable fixture. The Kubernetes
node name, Talos node address, and Talos endpoint are intentionally separate.

```sh
set -Eeuo pipefail
mkdir -p artifacts
workload_kubeconfig=${WORKLOAD_ACCESS_FILE:?set WORKLOAD_ACCESS_FILE}
talos_config=${TALOS_ACCESS_FILE:?set TALOS_ACCESS_FILE}
worker_node=ok-176-c3-workers-jvmn2-vvcvr
worker_ip=192.0.2.20
talos_endpoint=192.0.2.10
builder_image='gcc@sha256:82549aa8f90ada3236a8be70c74543132a76662ef33f0c3271ed802b81584a82'
initial_capture=artifacts/ok176-c3-userns.capture
persist_capture=artifacts/ok176-c3-persist.capture
p1_manifest=artifacts/ok176-c3-p1.yaml
p2_manifest=artifacts/ok176-c3-p2.yaml
observer_manifest=artifacts/ok176-c3-observer.yaml

test -s "$initial_capture"
test ! -e "$persist_capture"
test ! -e "$p1_manifest"
test ! -e "$p2_manifest"
test ! -e "$observer_manifest"

make c3-quota-preflight-persist-render \
  C3_PHASE=observer C3_NODE_NAME="$worker_node" \
  C3_WRITER_BUILDER_IMAGE="$builder_image" \
  C3_RENDERED_MANIFEST="$observer_manifest"
```

Create the distinct persistence observer before the baseline. Its read-only
`/host-var` mount proves the host `/var` XFS `prjquota` mount; `/host-boot-id`
provides the worker boot ID. The observer must be Running on the sole worker,
and the host-wide user-namespace sysctl must remain nonzero.

```sh
kubectl --kubeconfig "$workload_kubeconfig" apply -f "$observer_manifest"
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 wait \
  --for=condition=Ready pod/ok176-c3-persist-observer --timeout=180s
userns_max=$(kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 exec \
  ok176-c3-persist-observer -c observer -- cat /proc/sys/user/max_user_namespaces)
case "$userns_max" in ''|*[!0-9]*) echo "invalid user.max_user_namespaces: $userns_max" >&2; exit 1;; esac
test "$userns_max" -gt 0

bash tests/c3-quota-preflight/capture-persist.sh \
  --kubeconfig "$workload_kubeconfig" --initial "$initial_capture" \
  --phase baseline --output "$persist_capture"
```

Restart the OpenEBS Hostpath provisioner and wait for its rollout before P1.
This is a controller restart; the original four Succeeded writer Pods and all
PVCs remain in place.

```sh
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 rollout restart \
  deployment/ok176-c3-localpv-provisioner
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 rollout status \
  deployment/ok176-c3-localpv-provisioner --timeout=180s
bash tests/c3-quota-preflight/capture-persist.sh \
  --kubeconfig "$workload_kubeconfig" --initial "$initial_capture" \
  --phase P1 --point restarted --output "$persist_capture"
make c3-quota-preflight-persist-render \
  C3_PHASE=p1 C3_NODE_NAME="$worker_node" \
  C3_WRITER_BUILDER_IMAGE="$builder_image" \
  C3_RENDERED_MANIFEST="$p1_manifest"
```

Only after that checkpoint, apply P1, wait until all five writers are Ready at
their held gates, and capture their independent before status. This also reads
the actual quota64 PVC request and records its normalized value as 67108864
bytes. Release the three quota writers first and require all three to succeed
before releasing the control/noquota gate. The checker requires the later
control/noquota completion timestamps to be at or after every bounded writer and
requires noquota post-cleanup headroom of at least 1 GiB.

```sh
kubectl --kubeconfig "$workload_kubeconfig" apply -f "$p1_manifest"
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 wait --for=condition=Ready \
  pod/ok176-c3-p1-quota128 pod/ok176-c3-p1-quota256 pod/ok176-c3-p1-quota64 \
  pod/ok176-c3-p1-control pod/ok176-c3-p1-noquota --timeout=300s
bash tests/c3-quota-preflight/capture-persist.sh \
  --kubeconfig "$workload_kubeconfig" --initial "$initial_capture" \
  --phase P1 --point before --output "$persist_capture"

kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 patch \
  configmap ok176-c3-p1-gate --type merge -p '{"data":{"start":"go"}}'
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 wait \
  --for=jsonpath='{.status.phase}'=Succeeded \
  pod/ok176-c3-p1-quota128 pod/ok176-c3-p1-quota256 \
  pod/ok176-c3-p1-quota64 --timeout=300s
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 patch \
  configmap ok176-c3-p1-control-gate --type merge -p '{"data":{"start":"go"}}'
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 wait \
  --for=jsonpath='{.status.phase}'=Succeeded \
  pod/ok176-c3-p1-quota128 pod/ok176-c3-p1-quota256 pod/ok176-c3-p1-quota64 \
  pod/ok176-c3-p1-control pod/ok176-c3-p1-noquota --timeout=300s

bash tests/c3-quota-preflight/capture-persist.sh \
  --kubeconfig "$workload_kubeconfig" --initial "$initial_capture" \
  --phase P1 --point after --output "$persist_capture"
make c3-quota-preflight-persist-check C3_INITIAL_RESULTS="$initial_capture" \
  C3_RESULTS="$persist_capture" C3_THROUGH=P1
for pod in ok176-c3-quota-128 ok176-c3-quota-256 ok176-c3-control ok176-c3-noquota; do
  test "$(kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 get pod "$pod" \
    -o jsonpath='{.status.phase}')" = Succeeded
done
```

Delete only the P1 Pods. Do not delete any PVC. Keep both phase ConfigMaps, the new quota64 PVC, every
original PVC, and every original Succeeded writer Pod. Then reboot the sole
worker with Talos, wait for reboot completion and Kubernetes node readiness, and
recreate the `restartPolicy: Never` observer so P2 has a new independently bound
observer Pod. The capture checker requires P1's boot ID to match the baseline
and P2's boot ID to differ from P1, isolating the two events.

```sh
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 delete pod \
  ok176-c3-p1-quota128 ok176-c3-p1-quota256 ok176-c3-p1-quota64 \
  ok176-c3-p1-control ok176-c3-p1-noquota
talosctl --talosconfig "$talos_config" -n "$worker_ip" -e "$talos_endpoint" reboot --wait=true
kubectl --kubeconfig "$workload_kubeconfig" wait --for=condition=Ready \
  "node/$worker_node" --timeout=300s

kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 delete \
  pod/ok176-c3-persist-observer --wait=true
kubectl --kubeconfig "$workload_kubeconfig" apply -f "$observer_manifest"
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 wait \
  --for=condition=Ready pod/ok176-c3-persist-observer --timeout=180s
userns_max=$(kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 exec \
  ok176-c3-persist-observer -c observer -- cat /proc/sys/user/max_user_namespaces)
case "$userns_max" in ''|*[!0-9]*) echo "invalid user.max_user_namespaces: $userns_max" >&2; exit 1;; esac
test "$userns_max" -gt 0

for pod in ok176-c3-quota-128 ok176-c3-quota-256 ok176-c3-control ok176-c3-noquota; do
  test "$(kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 get pod "$pod" \
    -o jsonpath='{.status.phase}')" = Succeeded
done
```

P2 has four writers and no quota64 writer or quota64 evidence expansion. Use the same separated gate order,
then capture and check the complete persistence evidence.

```sh
make c3-quota-preflight-persist-render \
  C3_PHASE=p2 C3_NODE_NAME="$worker_node" \
  C3_WRITER_BUILDER_IMAGE="$builder_image" \
  C3_RENDERED_MANIFEST="$p2_manifest"
kubectl --kubeconfig "$workload_kubeconfig" apply -f "$p2_manifest"
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 wait --for=condition=Ready \
  pod/ok176-c3-p2-quota128 pod/ok176-c3-p2-quota256 \
  pod/ok176-c3-p2-control pod/ok176-c3-p2-noquota --timeout=300s
bash tests/c3-quota-preflight/capture-persist.sh \
  --kubeconfig "$workload_kubeconfig" --initial "$initial_capture" \
  --phase P2 --point before --output "$persist_capture"

kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 patch \
  configmap ok176-c3-p2-gate --type merge -p '{"data":{"start":"go"}}'
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 wait \
  --for=jsonpath='{.status.phase}'=Succeeded \
  pod/ok176-c3-p2-quota128 pod/ok176-c3-p2-quota256 --timeout=300s
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 patch \
  configmap ok176-c3-p2-control-gate --type merge -p '{"data":{"start":"go"}}'
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 wait \
  --for=jsonpath='{.status.phase}'=Succeeded \
  pod/ok176-c3-p2-quota128 pod/ok176-c3-p2-quota256 \
  pod/ok176-c3-p2-control pod/ok176-c3-p2-noquota --timeout=300s

bash tests/c3-quota-preflight/capture-persist.sh \
  --kubeconfig "$workload_kubeconfig" --initial "$initial_capture" \
  --phase P2 --point after --output "$persist_capture"
make c3-quota-preflight-persist-check C3_INITIAL_RESULTS="$initial_capture" \
  C3_RESULTS="$persist_capture"

for pod in ok176-c3-quota-128 ok176-c3-quota-256 ok176-c3-control ok176-c3-noquota; do
  test "$(kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 get pod "$pod" \
    -o jsonpath='{.status.phase}')" = Succeeded
done
kubectl --kubeconfig "$workload_kubeconfig" -n ok176-c3 delete pod \
  ok176-c3-p2-quota128 ok176-c3-p2-quota256 \
  ok176-c3-p2-control ok176-c3-p2-noquota
```

The observer proves that the retained quota128 payload SHA-256 and size, the
control/noquota retained sizes, the absent quota256 payload, PV/PVC identities,
quota rows, `/var` mount, and boot ID survive each phase. It also proves every
phase probe is absent after its writer reports `CLEANUP|label|0` or, for
quota128 open-time rejection, `CLEANUP|quota128|absent`.

Quota128 phase writers explicitly attempt project reassignment on
`/data/nested/payload`, opened `O_RDONLY`, before creating the phase probe.
`REASSIGN_TARGET` binds that path to the rejected ioctl: EINVAL and unchanged
project ID matching the observer are required. ENOSPC/EDQUOT at probe creation
is recorded as a zero-written, zero-allocated RESULT and absent CLEANUP with
exit 0; every other open error remains fatal. Write-time quota rejection is
also accepted with at most 1 MiB written/allocated. The retained payload hash
and size must still match the baseline. Phase Pod deletion is the only cleanup
in a successful run; retain the namespace and every PVC for
the separately approved final teardown.

No teardown command is automated here. A successful offline self-test proves
checker discrimination, not live enforcement; the full live rerun remains
owned by the test operator. Run `make verify` for the negative-first complete sample test.
Include the workload-defined `ok176-c3-xfs-noquota` StorageClass in the
test operator's cleanup inventory along with the chart-defined class.
