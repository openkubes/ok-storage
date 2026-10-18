#!/usr/bin/env bash
# Read-only capture for a operator-run OK-176 C3 fixture. It never applies
# or deletes an object. Run before the writers start and after all four finish.
set -Eeuo pipefail
tmp_files=()
cleanup() { rm -f "${tmp_files[@]:-}"; }
trap cleanup EXIT

usage() { echo "usage: $0 --kubeconfig FILE --phase before|after --output FILE" >&2; exit 2; }
kubeconfig= phase= output=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kubeconfig|--phase|--output)
      [[ $# -ge 2 ]] || usage
      case "$1" in
        --kubeconfig) kubeconfig=$2 ;;
        --phase) phase=$2 ;;
        --output) output=$2 ;;
      esac
      shift 2 ;;
    *) usage ;;
  esac
done
[[ -n "$kubeconfig" && -r "$kubeconfig" && ( "$phase" == before || "$phase" == after ) && -n "$output" ]] || usage
if [[ "$phase" == before ]]; then
  [[ ! -e "$output" ]] || { echo "FAIL: refusing existing capture $output" >&2; exit 1; }
  : >"$output"
else
  [[ -f "$output" ]] || { echo "FAIL: after capture requires prior before capture" >&2; exit 1; }
fi

for pair in quota128:ok176-c3-quota-128 quota256:ok176-c3-quota-256 control:ok176-c3-control noquota:ok176-c3-noquota; do
  label=${pair%%:*}; pod=${pair#*:}; pvc=$pod
  uid=$(kubectl --kubeconfig "$kubeconfig" -n ok176-c3 get pod "$pod" -o jsonpath='{.metadata.uid}')
  restarts=$(kubectl --kubeconfig "$kubeconfig" -n ok176-c3 get pod "$pod" -o jsonpath='{.status.containerStatuses[?(@.name=="writer")].restartCount}')
  node=$(kubectl --kubeconfig "$kubeconfig" -n ok176-c3 get pod "$pod" -o jsonpath='{.spec.nodeName}')
  pv=$(kubectl --kubeconfig "$kubeconfig" -n ok176-c3 get pvc "$pvc" -o jsonpath='{.spec.volumeName}')
  sc=$(kubectl --kubeconfig "$kubeconfig" -n ok176-c3 get pvc "$pvc" -o jsonpath='{.spec.storageClassName}')
  local_path=$(kubectl --kubeconfig "$kubeconfig" get pv "$pv" -o jsonpath='{.spec.local.path}')
  hostpath_path=$(kubectl --kubeconfig "$kubeconfig" get pv "$pv" -o jsonpath='{.spec.hostPath.path}')
  expected_sc=ok176-c3-xfs
  [[ "$label" != noquota ]] || expected_sc=ok176-c3-xfs-noquota
  [[ "$sc" == "$expected_sc" ]] || { echo "FAIL: unexpected StorageClass $sc for $pod, expected $expected_sc" >&2; exit 1; }
  [[ -n "$local_path" && -z "$hostpath_path" ]] || { echo "FAIL: expected local PV path for $pod" >&2; exit 1; }
  pv_path=$local_path
  if [[ "$label" == noquota ]]; then
    [[ "$pv_path" == /var/openebs/ok176-c3/* && "$pv_path" != *..* ]] || { echo "FAIL: noquota PV path must be under /var/openebs/ok176-c3" >&2; exit 1; }
  fi
  pod_phase=$(kubectl --kubeconfig "$kubeconfig" -n ok176-c3 get pod "$pod" -o jsonpath='{.status.phase}')
  host_users=$(kubectl --kubeconfig "$kubeconfig" -n ok176-c3 get pod "$pod" -o jsonpath='{.spec.hostUsers}')
  [[ -n "$host_users" ]] || host_users=absent
  exit_code=- finished=-
  if [[ "$phase" == after ]]; then
    exit_code=$(kubectl --kubeconfig "$kubeconfig" -n ok176-c3 get pod "$pod" -o jsonpath='{.status.containerStatuses[?(@.name=="writer")].state.terminated.exitCode}')
    finished=$(kubectl --kubeconfig "$kubeconfig" -n ok176-c3 get pod "$pod" -o jsonpath='{.status.containerStatuses[?(@.name=="writer")].state.terminated.finishedAt}')
  fi
  [[ -n "$uid" && -n "$restarts" && -n "$node" && -n "$pv" && -n "$pv_path" && -n "$sc" ]] || { echo "FAIL: incomplete status for $pod" >&2; exit 1; }
  [[ "$phase" == before || ( "$exit_code" == 0 && -n "$finished" ) ]] || { echo "FAIL: writer did not terminate successfully for $pod" >&2; exit 1; }
  [[ "$phase" == before && "$pod_phase" == Running || "$phase" == after && "$pod_phase" == Succeeded ]] || { echo "FAIL: unexpected pod phase $pod_phase for $phase capture of $pod" >&2; exit 1; }
  printf 'STATUS|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\n' "$phase" "$label" "$uid" "$restarts" "$node" "$pv" "$pv_path" "$sc" "$exit_code" "$finished" "$pod_phase" "$host_users" >>"$output"
  [[ "$host_users" == false ]] || { echo "FAIL: hostUsers must be explicitly false for $pod, got $host_users" >&2; exit 1; }
  if [[ "$phase" == before ]]; then
    read -r _ _ free block_size < <(kubectl --kubeconfig "$kubeconfig" -n ok176-c3 exec "$pod" -c writer -- stat -f -c '%i %T %a %S' /data)
    [[ "$free" =~ ^[0-9]+$ && "$block_size" =~ ^[0-9]+$ ]] || { echo "FAIL: invalid headroom for $pod" >&2; exit 1; }
    printf 'HEADROOM|before|%s|%s\n' "$label" "$((free * block_size / 1024))" >>"$output"
    if [[ "$label" == quota128 || "$label" == quota256 ]]; then
      relative=${pv_path#'/var/openebs/ok176-c3'}
      [[ "$relative" != "$pv_path" && "$relative" != *..* ]] || { echo "FAIL: PV path escapes observer base for $label" >&2; exit 1; }
      observer_path="/data${relative}"
      stat_output=$(kubectl --kubeconfig "$kubeconfig" -n ok176-c3 exec ok176-c3-quota-observer -c observer -- xfs_io -c stat "$observer_path")
      project_id=$(awk '/projid/ {print $3; exit}' <<<"$stat_output")
      [[ "$project_id" =~ ^[1-9][0-9]*$ ]] || { echo "FAIL: observer did not read project ID for $label" >&2; exit 1; }
      report=$(kubectl --kubeconfig "$kubeconfig" -n ok176-c3 exec ok176-c3-quota-observer -c observer -- xfs_quota -x -c 'report -p -n -N' /data)
      [[ -n "$report" ]] || { echo "FAIL: observer received empty XFS quota report for $label" >&2; exit 1; }
      printf 'EVIDENCE|observer|before|%s|%s\n' "$label" "$observer_path" >>"$output"
      printf 'READBACK|%s|%s\n' "$label" "$project_id" >>"$output"
      while IFS= read -r report_line; do printf 'XFS_REPORT|%s|%s\n' "$label" "$report_line" >>"$output"; done <<<"$report"
    fi
  fi
done
if [[ "$phase" == after ]]; then
  for pair in quota128:ok176-c3-quota-128 quota256:ok176-c3-quota-256 control:ok176-c3-control noquota:ok176-c3-noquota; do
    label=${pair%%:*}; pod=${pair#*:}
    pod_uid=$(kubectl --kubeconfig "$kubeconfig" -n ok176-c3 get pod "$pod" -o jsonpath='{.metadata.uid}')
    [[ -n "$pod_uid" ]] || { echo "FAIL: missing Pod UID while capturing writer log for $pod" >&2; exit 1; }
    log_file=$(mktemp)
    tmp_files+=("$log_file")
    kubectl --kubeconfig "$kubeconfig" -n ok176-c3 logs "$pod" -c writer >"$log_file"
    # Consumer legacy reports are not observer evidence and must not create
    # duplicate READBACK/XFS_REPORT records in the validated capture.
    awk -v label="$label" -v pod_uid="$pod_uid" \
      '!/^(READBACK|XFS_REPORT)\|/ { printf "WRITER|%s|%s|%s\n", label, pod_uid, $0 }' \
      "$log_file" >>"$output"
    rm -f "$log_file"
  done
fi
printf 'PASS: captured independent %s status%s\n' "$phase" "$([[ "$phase" == after ]] && echo ' and writer logs')"
