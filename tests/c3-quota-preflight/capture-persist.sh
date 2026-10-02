#!/usr/bin/env bash
# Read-only persistence capture for the coordinator-run OK-176 C3 fixture.
# This script never creates, patches, or deletes a Kubernetes object.
set -Eeuo pipefail

namespace=ok176-c3
observer_pod=ok176-c3-persist-observer
fixture_root=/var/openebs/ok176-c3

tmp_files=()
lock_dir=
cleanup() {
  rm -f "${tmp_files[@]:-}"
  [[ -z "$lock_dir" ]] || rmdir "$lock_dir" 2>/dev/null || true
}
trap cleanup EXIT

die() { echo "FAIL: $*" >&2; exit 1; }
usage() {
  cat >&2 <<'EOF'
usage: capture-persist.sh --kubeconfig FILE --initial FILE \
  --phase baseline|P1|P2 [--point restarted|before|after] --output FILE

baseline takes no --point. P1 accepts restarted, before, and after; P2 accepts
before and after. Run exactly once in order: baseline, P1 restarted, P1 before,
P1 after, P2 before, P2 after.
EOF
  exit 2
}

kubeconfig= initial= phase= point= output=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kubeconfig|--initial|--phase|--point|--output)
      [[ $# -ge 2 ]] || usage
      case "$1" in
        --kubeconfig) kubeconfig=$2 ;;
        --initial) initial=$2 ;;
        --phase) phase=$2 ;;
        --point) point=$2 ;;
        --output) output=$2 ;;
      esac
      shift 2 ;;
    *) usage ;;
  esac
done

[[ -n "$kubeconfig" && -r "$kubeconfig" && -n "$initial" && -r "$initial" && -n "$output" ]] || usage
[[ "$initial" != "$output" ]] || die "initial and persistence captures must be different files"
case "$phase:$point" in
  baseline:) ;;
  P1:restarted) ;;
  P1:before|P1:after|P2:before|P2:after) ;;
  *) usage ;;
esac

command -v kubectl >/dev/null || die "kubectl is required"
command -v python3 >/dev/null || die "python3 is required"
command -v cmp >/dev/null || die "cmp is required"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
checker=$script_dir/c3_persist.py
[[ -r "$checker" ]] || die "persistence checker is missing: $checker"

output_dir=$(dirname -- "$output")
[[ -d "$output_dir" && -w "$output_dir" ]] || die "output directory is not writable: $output_dir"
candidate_lock=${output}.lock
mkdir -- "$candidate_lock" 2>/dev/null || die "capture is locked or stale lock exists: $candidate_lock"
lock_dir=$candidate_lock

new_tmp=$(mktemp "$output_dir/.capture-persist.new.XXXXXX")
tmp_files+=("$new_tmp")

kctl() { kubectl --kubeconfig "$kubeconfig" "$@"; }
one_line() {
  local description=$1 value=$2
  [[ -n "$value" && "$value" != *$'\n'* && "$value" != *$'\r'* ]] || die "invalid $description"
}
numeric() { [[ "$2" =~ ^[0-9]+$ ]] || die "$1 is not numeric: $2"; }
safe_field() {
  [[ -n "$2" && "$2" != *'|'* && "$2" != *$'\n'* && "$2" != *$'\r'* ]] || die "invalid $1"
}
expected_sc() {
  [[ "$1" == noquota ]] && printf '%s\n' ok176-c3-xfs-noquota || printf '%s\n' ok176-c3-xfs
}
pvc_name() {
  case "$1" in
    quota128) printf '%s\n' ok176-c3-quota-128 ;;
    quota256) printf '%s\n' ok176-c3-quota-256 ;;
    quota64) printf '%s\n' ok176-c3-quota-64 ;;
    control) printf '%s\n' ok176-c3-control ;;
    noquota) printf '%s\n' ok176-c3-noquota ;;
    *) die "unknown label $1" ;;
  esac
}
pod_name() {
  local lower=${1,,}
  case "$2" in
    quota128|quota256|quota64|control|noquota) printf 'ok176-c3-%s-%s\n' "$lower" "$2" ;;
    *) die "unknown label $2" ;;
  esac
}

declare -A pvc_uid pv_name pv_uid pv_path storage_class observer_path
load_volume() {
  local label=$1 pvc raw hostpath expected pvc_phase
  pvc=$(pvc_name "$label")
  raw=$(kctl -n "$namespace" get pvc "$pvc" -o jsonpath='{.metadata.uid}{"|"}{.spec.volumeName}{"|"}{.spec.storageClassName}{"|"}{.status.phase}')
  IFS='|' read -r pvc_uid[$label] pv_name[$label] storage_class[$label] pvc_phase <<<"$raw"
  safe_field "$label PVC UID" "${pvc_uid[$label]:-}"
  safe_field "$label PV name" "${pv_name[$label]:-}"
  expected=$(expected_sc "$label")
  [[ "${storage_class[$label]:-}" == "$expected" ]] || die "$label uses ${storage_class[$label]:-<empty>}, expected $expected"
  [[ "$pvc_phase" == Bound ]] || die "$label PVC is not Bound"

  raw=$(kctl get pv "${pv_name[$label]}" -o jsonpath='{.metadata.uid}{"|"}{.spec.local.path}{"|"}{.spec.hostPath.path}')
  IFS='|' read -r pv_uid[$label] pv_path[$label] hostpath <<<"$raw"
  safe_field "$label PV UID" "${pv_uid[$label]:-}"
  safe_field "$label PV path" "${pv_path[$label]:-}"
  [[ -z "$hostpath" ]] || die "$label unexpectedly uses spec.hostPath"
  [[ "${pv_path[$label]}" == "$fixture_root"/* && "${pv_path[$label]}" != *..* ]] ||
    die "$label PV path must be below $fixture_root"
  observer_path[$label]="/data${pv_path[$label]#"$fixture_root"}"
}

initial_matches_volume() {
  local label=$1
  awk -F'|' -v label="$label" -v pv="${pv_name[$label]}" -v path="${pv_path[$label]}" \
    -v sc="${storage_class[$label]}" '
      $1 == "STATUS" && $3 == label {
        seen[$2]++
        if ($7 != pv || $8 != path || $9 != sc) bad=1
      }
      END { exit !(seen["before"] == 1 && seen["after"] == 1 && !bad) }
    ' "$initial" || die "$label live PV identity differs from the initial capture"
}

baseline_field() {
  local label=$1 field=$2
  awk -F'|' -v label="$label" -v field="$field" \
    '$1 == "PVC" && $2 == "baseline" && $3 == label { if (++n == 1) value=$field }
     END { if (n == 1) print value; else exit 1 }' "$output"
}
matches_baseline() {
  local label=$1
  [[ "$(baseline_field "$label" 4)" == "$(pvc_name "$label")" &&
     "$(baseline_field "$label" 5)" == "${pvc_uid[$label]}" &&
     "$(baseline_field "$label" 6)" == "${pv_name[$label]}" &&
     "$(baseline_field "$label" 7)" == "${pv_uid[$label]}" &&
     "$(baseline_field "$label" 8)" == "${pv_path[$label]}" &&
     "$(baseline_field "$label" 9)" == "${storage_class[$label]}" ]] ||
    die "$label PVC/PV identity changed from baseline"
}

observer_uid= observer_node=
read_observer_status() {
  local pod_file raw
  pod_file=$(mktemp "$output_dir/.capture-persist.observer.XXXXXX")
  tmp_files+=("$pod_file")
  kctl -n "$namespace" get pod "$observer_pod" -o json >"$pod_file"
  raw=$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
m, spec, status = d.get("metadata", {}), d.get("spec", {}), d.get("status", {})
uid, node = m.get("uid", ""), spec.get("nodeName", "")
statuses = [item for item in status.get("containerStatuses", []) if item.get("name") == "observer"]
containers = [item for item in spec.get("containers", []) if item.get("name") == "observer"]
expected_mounts = {
    "base": ("/data", True),
    "host-var": ("/host-var", True),
    "host-dev": ("/dev", True),
    "host-boot-id": ("/host-boot-id", True),
}
expected_paths = {
    "base": ("/var/openebs/ok176-c3", "DirectoryOrCreate"),
    "host-var": ("/var", "Directory"),
    "host-dev": ("/dev", "Directory"),
    "host-boot-id": ("/proc/sys/kernel/random/boot_id", "File"),
}
if (not uid or not node or spec.get("hostUsers") is False
        or status.get("phase") != "Running" or len(statuses) != 1
        or statuses[0].get("ready") is not True or statuses[0].get("restartCount") != 0
        or len(containers) != 1 or len(spec.get("containers", [])) != 1):
    raise SystemExit("observer is not a unique Running Ready zero-restart container")
container = containers[0]
security = container.get("securityContext", {})
if container.get("image") != "openebs/linux-utils:4.6.0" or security.get("privileged") is not True:
    raise SystemExit("observer image or privileged setting is invalid")
mount_list = container.get("volumeMounts", [])
if (len(mount_list) != 4 or any("subPath" in item or "subPathExpr" in item
                                or item.get("mountPropagation") is not None
                                for item in mount_list)):
    raise SystemExit("observer volume mounts have duplicates or path modifiers")
mounts = {item.get("name"): (item.get("mountPath"), item.get("readOnly") is True)
          for item in mount_list}
if mounts != expected_mounts:
    raise SystemExit("observer volume mounts are not the exact read-only set")
volumes = {}
for item in spec.get("volumes", []):
    host = item.get("hostPath")
    if (not isinstance(host, dict) or set(item) != {"name", "hostPath"}
            or set(host) != {"path", "type"}):
        raise SystemExit("observer contains a non-hostPath volume")
    volumes[item.get("name")] = (host.get("path"), host.get("type"))
if volumes != expected_paths:
    raise SystemExit("observer hostPath sources or types are invalid")
print(uid + "|" + node)
' "$pod_file") || die "observer Pod status or spec is invalid"
  IFS='|' read -r observer_uid observer_node <<<"$raw"
  safe_field "observer Pod UID" "$observer_uid"
  safe_field "observer node" "$observer_node"
}

emit_observer_status_and_spec() {
  local capture_phase=$1
  printf 'OBSERVER_STATUS|%s|%s|%s\n' "$capture_phase" "$observer_uid" "$observer_node" >>"$new_tmp"
  printf 'OBSERVER_SPEC|%s|%s|/var/openebs/ok176-c3|/data|/var|/host-var|/proc/sys/kernel/random/boot_id|/host-boot-id|/dev|/dev|readonly|privileged\n' \
    "$capture_phase" "$observer_uid" >>"$new_tmp"
}

read_boot_id() {
  local capture_point=$1 capture_phase=$2 boot uid_after
  boot=$(kctl -n "$namespace" exec "$observer_pod" -c observer -- cat /host-boot-id)
  [[ "$boot" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || die "observer boot ID is malformed"
  uid_after=$(kctl -n "$namespace" get pod "$observer_pod" -o jsonpath='{.metadata.uid}')
  [[ "$uid_after" == "$observer_uid" ]] || die "observer Pod changed while boot ID was captured"
  printf 'BOOT|%s|%s|%s|%s\n' "$capture_point" "$capture_phase" "$observer_uid" "$boot" >>"$new_tmp"
  printf '%s\n' "$boot"
}

baseline_boot_id() {
  awk -F'|' '$1 == "OBSERVER" && $2 == "baseline" && $4 == "BOOT_ID" {value=$5; n++}
    END {if (n == 1) print value; else exit 1}' "$output"
}

phase_boot_id() {
  local capture_point=$1 capture_phase=$2
  awk -F'|' -v point="$capture_point" -v phase="$capture_phase" \
    '$1 == "BOOT" && $2 == point && $3 == phase {value=$5; n++}
     END {if (n == 1) print value; else exit 1}' "$output"
}

capture_provisioner() {
  local capture_phase=$1 deployment=ok176-c3-localpv-provisioner dep_file pod_file parsed deployment_uid selector pod_uid
  dep_file=$(mktemp "$output_dir/.capture-persist.deployment.XXXXXX")
  pod_file=$(mktemp "$output_dir/.capture-persist.provisioner-pods.XXXXXX")
  tmp_files+=("$dep_file" "$pod_file")
  kctl -n "$namespace" get deployment "$deployment" -o json >"$dep_file"
  parsed=$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
m, s, spec = d["metadata"], d.get("status", {}), d["spec"]
replicas = spec.get("replicas", 1)
labels = spec["selector"].get("matchLabels", {})
expressions = spec["selector"].get("matchExpressions", [])
healthy = (
    m.get("namespace") == "ok176-c3"
    and m.get("name") == "ok176-c3-localpv-provisioner"
    and m.get("uid")
    and not expressions and labels
    and s.get("observedGeneration") == m.get("generation")
    and s.get("replicas", 0) == replicas
    and s.get("updatedReplicas", 0) == replicas
    and s.get("readyReplicas", 0) == replicas
    and s.get("availableReplicas", 0) == replicas
)
if not healthy:
    raise SystemExit("deployment is not fully rolled out")
selector = ",".join(f"{key}={labels[key]}" for key in sorted(labels))
print(m["uid"] + "|" + selector)
' "$dep_file") || die "provisioner Deployment is not healthy"
  IFS='|' read -r deployment_uid selector <<<"$parsed"
  safe_field "provisioner Deployment UID" "$deployment_uid"
  safe_field "provisioner selector" "$selector"
  kctl -n "$namespace" get pods -l "$selector" -o json >"$pod_file"
  pod_uid=$(python3 -c '
import json, sys
items = json.load(open(sys.argv[1], encoding="utf-8")).get("items", [])
ready = []
for pod in items:
    meta, status = pod["metadata"], pod.get("status", {})
    containers = status.get("containerStatuses", [])
    if (not meta.get("deletionTimestamp") and status.get("phase") == "Running"
            and containers and all(item.get("ready") for item in containers)):
        ready.append(meta.get("uid", ""))
if len(ready) != 1 or not ready[0]:
    raise SystemExit("expected exactly one Running Ready provisioner Pod")
print(ready[0])
' "$pod_file") || die "provisioner selector did not resolve one Running Ready Pod"
  safe_field "provisioner Pod UID" "$pod_uid"
  if [[ "$capture_phase" == P1 ]]; then
    local baseline_deployment_uid baseline_pod_uid
    read -r baseline_deployment_uid baseline_pod_uid < <(
      awk -F'|' '$1 == "PROVISIONER" && $2 == "baseline" {deployment=$5; pod=$6; n++}
        END {if (n == 1) print deployment, pod; else exit 1}' "$output"
    ) || die "baseline provisioner record is missing or duplicated"
    [[ "$deployment_uid" == "$baseline_deployment_uid" ]] || die "provisioner Deployment identity changed after restart"
    [[ "$pod_uid" != "$baseline_pod_uid" ]] || die "provisioner Pod UID did not change after rollout restart"
  fi
  printf 'PROVISIONER|%s|%s|%s|%s|%s\n' "$capture_phase" "$namespace" "$deployment" "$deployment_uid" "$pod_uid" >>"$new_tmp"
}

require_baseline_shape() {
  local capture=${1:-$output}
  [[ -f "$capture" && ! -L "$capture" ]] || die "baseline capture must be a regular file"
  awk -F'|' '
    $1 == "OBSERVER_STATUS" && $2 == "baseline" { status++ }
    $1 == "OBSERVER_SPEC" && $2 == "baseline" { spec++ }
    $1 == "PVC" && $2 == "baseline" { pvc++ }
    $1 == "OBSERVER" && $2 == "baseline" && $4 == "HOSTFS" { hostfs++ }
    $1 == "OBSERVER" && $2 == "baseline" && $4 == "QUOTA" { quota++ }
    $1 == "OBSERVER" && $2 == "baseline" && $4 == "FILE" { file++ }
    $1 == "OBSERVER" && $2 == "baseline" && $4 == "BOOT_ID" { boot++ }
    $1 == "PROVISIONER" && $2 == "baseline" { provisioner++ }
    $2 == "P1" || $2 == "P2" { later++ }
    !($1 == "OBSERVER_STATUS" || $1 == "OBSERVER_SPEC" || $1 == "PVC" || $1 == "OBSERVER" || $1 == "PROVISIONER") { bad++ }
    END { exit !(NR == 16 && status == 1 && spec == 1 && pvc == 4 && hostfs == 1 && quota == 3 && file == 4 && boot == 1 && provisioner == 1 && later == 0 && bad == 0) }
  ' "$capture" || die "capture does not contain exactly one complete baseline"
}

require_before_shape() {
  local wanted=$1 count=$2
  require_baseline_shape_allow_later
  awk -F'|' -v phase="$wanted" -v count="$count" '
    $1 == "STATUS" && $2 == phase && $3 == "before" { before++ }
    $1 == "MOUNT" && $2 == phase && $3 == "before" { mounts++ }
    $1 == "PROVISIONER" && $2 == phase { provisioner++ }
    $1 == "CHECKPOINT" && $2 == phase { checkpoint++ }
    $1 == "GATE" && $2 == phase { gates++ }
    $1 == "BOOT" && $2 == "before" && $3 == phase { boot++ }
    $1 == "PVC_REQUEST" && $2 == phase { request++ }
    $2 == phase && !($1 == "STATUS" && $3 == "before") && !($1 == "MOUNT" && $3 == "before") &&
      $1 != "PROVISIONER" && $1 != "CHECKPOINT" && $1 != "GATE" && $1 != "PVC_REQUEST" { bad++ }
    END { exit !(before == count && mounts == count && gates == 2 && boot == 1 &&
      provisioner == (phase == "P1" ? 1 : 0) && checkpoint == (phase == "P1" ? 1 : 0) &&
      request == (phase == "P1" ? 1 : 0) && bad == 0) }
  ' "$output" || die "$wanted capture does not contain exactly its before statuses"
}

require_baseline_shape_allow_later() {
  local capture=${1:-$output}
  awk -F'|' '
    $1 == "OBSERVER_STATUS" && $2 == "baseline" { status++ }
    $1 == "OBSERVER_SPEC" && $2 == "baseline" { spec++ }
    $1 == "PVC" && $2 == "baseline" { pvc++ }
    $1 == "OBSERVER" && $2 == "baseline" && $4 == "HOSTFS" { hostfs++ }
    $1 == "OBSERVER" && $2 == "baseline" && $4 == "QUOTA" { quota++ }
    $1 == "OBSERVER" && $2 == "baseline" && $4 == "FILE" { file++ }
    $1 == "OBSERVER" && $2 == "baseline" && $4 == "BOOT_ID" { boot++ }
    $1 == "PROVISIONER" && $2 == "baseline" { provisioner++ }
    END { exit !(status == 1 && spec == 1 && pvc == 4 && hostfs == 1 && quota == 3 && file == 4 && boot == 1 && provisioner == 1) }
  ' "$capture" || die "capture baseline is missing, duplicated, or malformed"
}

require_restart_shape() {
  local capture=${1:-$output}
  require_baseline_shape_allow_later "$capture"
  awk -F'|' '
    $1 == "PROVISIONER" && $2 == "P1" { provisioner++ }
    $1 == "CHECKPOINT" && $2 == "P1" && $3 == "restarted" && $4 == "quota64" && $5 == "absent" { checkpoint++ }
    $2 == "P1" && $1 != "PROVISIONER" && $1 != "CHECKPOINT" { bad++ }
    $2 == "P2" { bad++ }
    END { exit !(provisioner == 1 && checkpoint == 1 && bad == 0) }
  ' "$capture" || die "P1 restart checkpoint is missing, duplicated, or malformed"
}

check_complete_phase() {
  local through=$1 capture=$2
  python3 "$checker" --initial "$initial" --capture "$capture" --through "$through" >/dev/null
}

prior_p1_tmp=
require_p1_complete_before_p2() {
  prior_p1_tmp=$(mktemp "$output_dir/.capture-persist.p1.XXXXXX")
  tmp_files+=("$prior_p1_tmp")
  awk -F'|' '$2 != "P2" && !($1 == "BOOT" && $3 == "P2")' "$output" >"$prior_p1_tmp"
  check_complete_phase P1 "$prior_p1_tmp" || die "prior P1 capture failed validation"
}

append_atomic() {
  local combined
  combined=$(mktemp "$output_dir/.capture-persist.combined.XXXXXX")
  tmp_files+=("$combined")
  cat -- "$output" "$new_tmp" >"$combined"
  "$@" "$combined"
  mv -fT -- "$combined" "$output"
}

emit_pvc() {
  local label=$1
  printf 'PVC|baseline|%s|%s|%s|%s|%s|%s|%s\n' "$label" "$(pvc_name "$label")" \
    "${pvc_uid[$label]}" "${pv_name[$label]}" "${pv_uid[$label]}" "${pv_path[$label]}" \
    "${storage_class[$label]}" >>"$new_tmp"
}

emit_gate_records() {
  local capture_phase=$1 lower gate cm_file value
  lower=${capture_phase,,}
  for gate in "ok176-c3-$lower-gate" "ok176-c3-$lower-control-gate"; do
    cm_file=$(mktemp "$output_dir/.capture-persist.gate.XXXXXX")
    tmp_files+=("$cm_file")
    kctl -n "$namespace" get configmap "$gate" -o json >"$cm_file"
    value=$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
if d.get("metadata", {}).get("namespace") != "ok176-c3" or d.get("metadata", {}).get("name") != sys.argv[2]:
    raise SystemExit("gate identity mismatch")
value = d.get("data", {}).get("start")
if value != "hold":
    raise SystemExit("gate start is not hold")
print(value)
' "$cm_file" "$gate") || die "$gate is missing data.start=hold"
    printf 'GATE|%s|%s|%s\n' "$capture_phase" "$gate" "$value" >>"$new_tmp"
  done
}

emit_quota64_request() {
  local pvc_file bytes
  pvc_file=$(mktemp "$output_dir/.capture-persist.quota64-pvc.XXXXXX")
  tmp_files+=("$pvc_file")
  kctl -n "$namespace" get pvc ok176-c3-quota-64 -o json >"$pvc_file"
  bytes=$(python3 -c '
import json, re, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
if d.get("metadata", {}).get("name") != "ok176-c3-quota-64":
    raise SystemExit("quota64 PVC identity mismatch")
quantity = d.get("spec", {}).get("resources", {}).get("requests", {}).get("storage", "")
match = re.fullmatch(r"([0-9]+)(Ki|Mi|Gi)?", quantity)
if not match:
    raise SystemExit("unsupported quota64 storage request")
scale = {None: 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}[match.group(2)]
print(int(match.group(1)) * scale)
' "$pvc_file") || die "quota64 PVC storage request is malformed"
  [[ "$bytes" == 67108864 ]] || die "quota64 PVC request is $bytes bytes, expected 67108864"
  printf 'PVC_REQUEST|P1|quota64|%s\n' "$bytes" >>"$new_tmp"
}

emit_file_record() {
  local capture_phase=$1 label=$2 filename=$3 state=$4 expected_size=$5 want_sha=$6 path size sha raw
  path=${observer_path[$label]}/nested/$filename
  if [[ "$state" == absent ]]; then
    kctl -n "$namespace" exec "$observer_pod" -c observer -- test ! -e "$path"
    printf 'OBSERVER|%s|%s|FILE|%s|%s|absent|0|-\n' "$capture_phase" "$observer_uid" "$label" "$filename" >>"$new_tmp"
    return
  fi
  size=$(kctl -n "$namespace" exec "$observer_pod" -c observer -- stat -c %s -- "$path")
  numeric "$label $filename size" "$size"
  [[ "$size" == "$expected_size" ]] || die "$label $filename size is $size, expected $expected_size"
  sha=-
  if [[ "$want_sha" == yes ]]; then
    raw=$(kctl -n "$namespace" exec "$observer_pod" -c observer -- sha256sum -- "$path")
    read -r sha _ <<<"$raw"
    [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || die "$label $filename SHA-256 is malformed"
  fi
  printf 'OBSERVER|%s|%s|FILE|%s|%s|present|%s|%s\n' "$capture_phase" "$observer_uid" "$label" "$filename" "$size" "$sha" >>"$new_tmp"
}

emit_observer_evidence() {
  local capture_phase=$1 quota_labels=$2 cleanup_labels=${3:-} probe=${4:-} expected_boot=${5:-} expected_uid=${6:-}
  local raw fs opts boot report label stat_output project hard expected
  read_observer_status
  [[ -z "$expected_uid" || "$observer_uid" == "$expected_uid" ]] || die "observer Pod changed during capture"
  emit_observer_status_and_spec "$capture_phase"

  raw=$(kctl -n "$namespace" exec "$observer_pod" -c observer -- findmnt -T /host-var -n -o FSTYPE,OPTIONS)
  one_line "observer findmnt output" "$raw"
  read -r fs opts <<<"$raw"
  [[ "$fs" == xfs ]] || die "host /var filesystem is $fs, expected xfs"
  [[ ",$opts," == *,prjquota,* || ",$opts," == *,pquota,* ]] || die "host /var mount lacks prjquota"
  printf 'OBSERVER|%s|%s|HOSTFS|/var|xfs|prjquota\n' "$capture_phase" "$observer_uid" >>"$new_tmp"

  boot=$(kctl -n "$namespace" exec "$observer_pod" -c observer -- cat /host-boot-id)
  [[ "$boot" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || die "observer boot ID is malformed"
  [[ -z "$expected_boot" || "$boot" == "$expected_boot" ]] || die "observer boot ID changed during capture"
  printf 'OBSERVER|%s|%s|BOOT_ID|%s\n' "$capture_phase" "$observer_uid" "$boot" >>"$new_tmp"

  report=$(kctl -n "$namespace" exec "$observer_pod" -c observer -- xfs_quota -x -c 'report -p -n -N' /data)
  [[ -n "$report" ]] || die "observer returned an empty XFS quota report"
  for label in $quota_labels; do
    stat_output=$(kctl -n "$namespace" exec "$observer_pod" -c observer -- xfs_io -c stat "${observer_path[$label]}")
    project=$(awk '/projid/ { if (++n == 1) value=$3 } END { if (n == 1) print value; else exit 1 }' <<<"$stat_output") ||
      die "observer did not return one project ID for $label"
    [[ "$project" =~ ^[1-9][0-9]*$ ]] || die "$label project ID is not positive"
    hard=$(awk -v id="$project" '$1 == id || $1 == "#" id { if (++n == 1) value=$4 } END { if (n == 1) print value; else exit 1 }' <<<"$report") ||
      die "XFS quota report has no unique row for $label project $project"
    case "$label" in
      quota128) expected=131072 ;;
      quota256) expected=262144 ;;
      quota64) expected=65536 ;;
      control) expected=32768 ;;
      *) die "no quota expectation for $label" ;;
    esac
    [[ "$hard" == "$expected" ]] || die "$label hard quota is $hard KiB, expected $expected KiB"
    printf 'OBSERVER|%s|%s|QUOTA|%s|%s|%s\n' "$capture_phase" "$observer_uid" "$label" "$project" "$hard" >>"$new_tmp"
  done

  emit_file_record "$capture_phase" quota128 payload present 134217728 yes
  emit_file_record "$capture_phase" quota256 payload absent 0 no
  emit_file_record "$capture_phase" control payload present 16777216 no
  emit_file_record "$capture_phase" noquota payload present 167772160 no
  for label in $cleanup_labels; do
    emit_file_record "$capture_phase" "$label" "$probe" absent 0 no
  done

  local uid_after
  uid_after=$(kctl -n "$namespace" get pod "$observer_pod" -o jsonpath='{.metadata.uid}')
  [[ "$uid_after" == "$observer_uid" ]] || die "observer Pod changed during capture"
}

emit_status() {
  local capture_phase=$1 capture_point=$2 label=$3 destination=${4:-$new_tmp}
  local pod pod_file raw pod_uid restarts node pod_phase host_users exit_code finished mounted_claim expected_phase gate_name lower
  pod=$(pod_name "$capture_phase" "$label")
  lower=${capture_phase,,}
  case "$label" in
    control|noquota) gate_name="ok176-c3-$lower-control-gate" ;;
    *) gate_name="ok176-c3-$lower-gate" ;;
  esac
  pod_file=$(mktemp "$output_dir/.capture-persist.writer.XXXXXX")
  tmp_files+=("$pod_file")
  kctl -n "$namespace" get pod "$pod" -o json >"$pod_file"
  raw=$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
expected_pod, expected_claim, expected_gate = sys.argv[2:5]
m, spec, status = d.get("metadata", {}), d.get("spec", {}), d.get("status", {})
if m.get("name") != expected_pod or m.get("namespace") != "ok176-c3":
    raise SystemExit("writer Pod identity mismatch")
if spec.get("hostUsers") is not False:
    raise SystemExit("writer spec.hostUsers must be false")
containers = [item for item in spec.get("containers", []) if item.get("name") == "writer"]
statuses = [item for item in status.get("containerStatuses", []) if item.get("name") == "writer"]
if len(containers) != 1 or len(statuses) != 1:
    raise SystemExit("writer container or status is missing or duplicated")
mounts = containers[0].get("volumeMounts", [])
data_mounts = [item for item in mounts if item.get("name") == "data" and item.get("mountPath") == "/data"]
if len(data_mounts) != 1 or "subPath" in data_mounts[0] or "subPathExpr" in data_mounts[0]:
    raise SystemExit("writer data mount must be /data without subPath")
gate_mounts = [item for item in mounts if item.get("name") == "gate" and item.get("mountPath") == "/gate"]
if (len(gate_mounts) != 1 or gate_mounts[0].get("readOnly") is not True
        or "subPath" in gate_mounts[0] or "subPathExpr" in gate_mounts[0]):
    raise SystemExit("writer gate mount must be read-only /gate without subPath")
for item in mounts:
    path = item.get("mountPath", "")
    if item is not data_mounts[0] and (path == "/data" or path.startswith("/data/")):
        raise SystemExit("writer has another mount at or below /data")
volumes = [item for item in spec.get("volumes", []) if item.get("name") == "data"]
if len(volumes) != 1 or volumes[0].get("persistentVolumeClaim", {}).get("claimName") != expected_claim:
    raise SystemExit("writer data volume does not use the expected PVC")
gates = [item for item in spec.get("volumes", []) if item.get("name") == "gate"]
if len(gates) != 1 or gates[0].get("configMap", {}).get("name") != expected_gate:
    raise SystemExit("writer gate volume does not use the held phase gate")
state = statuses[0].get("state", {}).get("terminated", {})
values = [m.get("uid", ""), statuses[0].get("restartCount", ""), spec.get("nodeName", ""),
          status.get("phase", ""), "false", state.get("exitCode", ""),
          state.get("finishedAt", ""), expected_claim]
print("|".join(str(value) for value in values))
' "$pod_file" "$pod" "$(pvc_name "$label")" "$gate_name") || die "$label writer Pod status or mount spec is invalid"
  IFS='|' read -r pod_uid restarts node pod_phase host_users exit_code finished mounted_claim <<<"$raw"
  safe_field "$label Pod UID" "$pod_uid"
  numeric "$label restart count" "$restarts"
  [[ "$restarts" == 0 ]] || die "$label writer restarted"
  [[ "$node" == "$observer_node" ]] || die "$label writer node $node differs from observer node $observer_node"
  [[ "$host_users" == false ]] || die "$label spec.hostUsers is not false"
  [[ "$mounted_claim" == "$(pvc_name "$label")" ]] || die "$label Pod data volume does not mount its expected PVC"
  if [[ "$capture_point" == before ]]; then
    expected_phase=Running
    [[ "$pod_phase" == "$expected_phase" && -z "$exit_code" && -z "$finished" ]] || die "$label is not held Running before its gate"
    exit_code=- finished=-
  else
    expected_phase=Succeeded
    [[ "$pod_phase" == "$expected_phase" && "$exit_code" == 0 && "$finished" == *Z ]] || die "$label did not finish successfully with a UTC timestamp"
  fi
  printf 'STATUS|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\n' \
    "$capture_phase" "$capture_point" "$label" "$pod_uid" "$restarts" "$node" "$(pvc_name "$label")" \
    "${pvc_uid[$label]}" "${pv_name[$label]}" "${pv_uid[$label]}" "${pv_path[$label]}" \
    "${storage_class[$label]}" "$exit_code" "$finished" "$pod_phase" "$host_users" >>"$destination"
  printf 'MOUNT|%s|%s|%s|%s|data|/data|%s|no-subpath\n' \
    "$capture_phase" "$capture_point" "$label" "$pod_uid" "$mounted_claim" >>"$destination"
}

emit_writer_log() {
  local capture_phase=$1 label=$2 pod uid_before uid_after log_file line kind embedded
  pod=$(pod_name "$capture_phase" "$label")
  uid_before=$(kctl -n "$namespace" get pod "$pod" -o jsonpath='{.metadata.uid}')
  safe_field "$label log source Pod UID" "$uid_before"
  log_file=$(mktemp "$output_dir/.capture-persist.log.XXXXXX")
  tmp_files+=("$log_file")
  kctl -n "$namespace" logs "$pod" -c writer >"$log_file"
  [[ -s "$log_file" ]] || die "$label writer log is empty"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" && "$line" != *$'\r'* ]] || die "$label writer log contains an empty or CR record"
    IFS='|' read -r kind embedded _ <<<"$line"
    case "$kind" in
      FS|TARGET|IDENTITY|USERNS|REASSIGN_TARGET|REASSIGN|RESULT|CLEANUP)
        [[ "$embedded" == "$label" ]] || die "$label writer emitted a record for ${embedded:-<empty>}"
        ;;
      HEADROOM)
        [[ "$embedded" == after && "$line" == "HEADROOM|after|$label|"* ]] || die "$label emitted malformed HEADROOM"
        ;;
      *) die "$label writer emitted unexpected record kind ${kind:-<empty>}" ;;
    esac
    printf 'WRITER|%s|%s|%s|%s\n' "$capture_phase" "$label" "$uid_before" "$line" >>"$new_tmp"
  done <"$log_file"
  uid_after=$(kctl -n "$namespace" get pod "$pod" -o jsonpath='{.metadata.uid}')
  [[ "$uid_after" == "$uid_before" ]] || die "$label Pod changed while its log was captured"
}

original_labels='quota128 quota256 control noquota'
if [[ "$phase" == baseline ]]; then
  [[ ! -e "$output" && ! -L "$output" ]] || die "refusing existing baseline output $output"
  quota64_json=$(kctl -n "$namespace" get pvc ok176-c3-quota-64 --ignore-not-found -o json)
  [[ -z "$quota64_json" ]] || die "quota64 PVC already exists before the baseline"
  read_observer_status
  initial_node=$(awk -F'|' '$1 == "STATUS" {if (!nodes[$6]++) count++} END {if (count == 1) for (node in nodes) print node; else exit 1}' "$initial") ||
    die "initial capture is not bound to one worker node"
  [[ "$observer_node" == "$initial_node" ]] || die "observer is not on the initial writers' sole worker"
  for label in $original_labels; do
    load_volume "$label"
    initial_matches_volume "$label"
    emit_pvc "$label"
  done
  capture_provisioner baseline
  emit_observer_evidence baseline 'quota128 quota256 control'
  require_baseline_shape "$new_tmp"
  ln -- "$new_tmp" "$output" 2>/dev/null || die "refusing output created concurrently: $output"
  echo "PASS: captured atomic persistence baseline"
  exit 0
fi

[[ -f "$output" && ! -L "$output" ]] || die "persistence capture is missing or not a regular file: $output"

if [[ "$phase:$point" == P1:restarted ]]; then
  require_baseline_shape
  capture_provisioner P1
  quota64_json=$(kctl -n "$namespace" get pvc ok176-c3-quota-64 --ignore-not-found -o json)
  [[ -z "$quota64_json" ]] || die "quota64 PVC exists before the P1 restart checkpoint"
  printf 'CHECKPOINT|P1|restarted|quota64|absent\n' >>"$new_tmp"
  append_atomic require_restart_shape
  echo "PASS: captured atomic P1 provisioner-restart checkpoint"
  exit 0
fi

labels=$original_labels
[[ "$phase" != P1 ]] || labels='quota128 quota256 quota64 control noquota'

if [[ "$point" == before ]]; then
  if [[ "$phase" == P1 ]]; then
    require_restart_shape
  else
    require_p1_complete_before_p2
    [[ $(awk -F'|' '$2 == "P2" {n++} END {print n+0}' "$output") == 0 ]] || die "P2 capture already exists"
  fi
  read_observer_status
  emit_gate_records "$phase"
  preboot_tmp=$(mktemp "$output_dir/.capture-persist.preboot.XXXXXX")
  postboot_tmp=$(mktemp "$output_dir/.capture-persist.postboot.XXXXXX")
  tmp_files+=("$preboot_tmp" "$postboot_tmp")
  for label in $labels; do
    load_volume "$label"
    [[ "$label" == quota64 ]] || matches_baseline "$label"
    emit_status "$phase" before "$label" "$preboot_tmp"
  done
  boot_before=$(read_boot_id before "$phase")
  if [[ "$phase" == P1 ]]; then
    [[ "$boot_before" == "$(baseline_boot_id)" ]] || die "P1 boot ID changed before its writers ran"
  else
    [[ "$boot_before" != "$(phase_boot_id after P1)" ]] || die "P2 boot ID did not change after the worker reboot"
  fi
  [[ "$phase" != P1 ]] || emit_quota64_request
  for label in $labels; do
    emit_status "$phase" before "$label" "$postboot_tmp"
  done
  cmp -s -- "$preboot_tmp" "$postboot_tmp" || die "$phase writer status or mount changed across the before boot capture"
  cat -- "$postboot_tmp" >>"$new_tmp"
  append_atomic true
  echo "PASS: captured atomic $phase before status"
  exit 0
fi

if [[ "$phase" == P1 ]]; then
  require_before_shape P1 5
else
  require_before_shape P2 4
  require_p1_complete_before_p2
fi
read_observer_status
capture_observer_uid=$observer_uid
for label in $labels; do
  load_volume "$label"
  [[ "$label" == quota64 ]] || matches_baseline "$label"
  emit_status "$phase" after "$label"
  emit_writer_log "$phase" "$label"
done
boot_after=$(read_boot_id after "$phase")
[[ "$boot_after" == "$(phase_boot_id before "$phase")" ]] || die "$phase boot ID changed while its writers ran"
quota_labels='quota128 quota256 control'
[[ "$phase" != P1 ]] || quota_labels='quota128 quota256 quota64 control'
probe=${phase,,}-probe
emit_observer_evidence "$phase" "$quota_labels" "$labels" "$probe" "$boot_after" "$capture_observer_uid"
append_atomic check_complete_phase "$phase"
echo "PASS: captured and validated atomic $phase after evidence"
