#!/usr/bin/env bash

set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
snapshot_class="${repo_dir}/storageclasses/ok-storage-block-snapshot-class.yaml"
gpu_demo_class="${repo_dir}/demo/ok-storage-block-gpu-test.yaml"
gpu_demo_tool="${repo_dir}/scripts/gpu-demo-storage.py"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

grep -qx 'kind: VolumeSnapshotClass' "${snapshot_class}" ||
  fail "snapshot manifest must remain a VolumeSnapshotClass"
grep -qx '  name: ok-storage-block-snapshot' "${snapshot_class}" ||
  fail "snapshot class contract name changed"
grep -qx 'driver: driver.longhorn.io' "${snapshot_class}" ||
  fail "current implementation must use the Longhorn CSI driver"

parameter_block="$(
  awk '
    /^parameters:$/ { in_parameters = 1; next }
    in_parameters && /^[^ ]/ { exit }
    in_parameters { print }
  ' "${snapshot_class}"
)"

[[ "${parameter_block}" == '  type: snap' ]] ||
  fail "Longhorn parameters must contain exactly: type: snap"

if grep -Ev '^[[:space:]]*#' "${snapshot_class}" |
  grep -Eqi '(^|[[:space:]])type:[[:space:]]*bak([[:space:]]|$)|backup[[:space:]_-]*target'; then
  fail "snapshot class must not select Longhorn backup mode or a backup target"
fi

printf 'PASS: ok-storage-block-snapshot selects exact local Longhorn snapshot mode\n'

grep -qx '  name: ok-storage-block-gpu-test' "${gpu_demo_class}" ||
  fail "GPU demo StorageClass name changed"
grep -qx 'reclaimPolicy: Delete' "${gpu_demo_class}" ||
  fail "GPU demo volumes must be deleted with their PVCs"
grep -qx '  numberOfReplicas: "1"' "${gpu_demo_class}" ||
  fail "GPU demo StorageClass must remain explicitly non-HA"
grep -qx '  nodeSelector: "openkubes-gpu-demo"' "${gpu_demo_class}" ||
  fail "GPU demo StorageClass must remain pinned by the dedicated Longhorn tag"
if grep -q 'storageclass.kubernetes.io/is-default-class' "${gpu_demo_class}"; then
  fail "GPU demo StorageClass must never become a default"
fi
grep -q 'GPU_DEMO_APPLY.*yes' "${gpu_demo_tool}" ||
  fail "GPU demo apply lifecycle must remain explicitly gated"
grep -q 'GPU_DEMO_REMOVE.*yes' "${gpu_demo_tool}" ||
  fail "GPU demo removal lifecycle must remain explicitly gated"

printf 'PASS: GPU demo StorageClass is explicit, one-replica, tagged and disposable\n'
