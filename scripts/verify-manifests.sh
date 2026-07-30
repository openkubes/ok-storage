#!/usr/bin/env bash

set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
snapshot_class="${repo_dir}/storageclasses/ok-storage-block-snapshot-class.yaml"

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
