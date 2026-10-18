# OK-176 C3 restart/reboot persistence evidence — 2026-10-02

Historical disposable-cluster run using the [C3 harness](../../README.md).
The 2026-10-02 run used the harness before it was committed. The published PASS
is an offline re-validation of the captures using the harness in this tree
and this tree's checker. No live run was repeated for publication.
This is the standalone fixture proof; it does not extend another evidence file's
claims or change a StorageClass contract.

`initial.capture` is the initial user-namespace quota run required by the checker.
`persistence.capture` contains its baseline, P1 provisioner-restart checkpoint,
and completed P1/P2 writer and observer records. The rendered P1/P2 manifests are
not included: the offline checker reads only these two captures, including the
independently captured mount, Pod security, and observer-spec records. Phase
manifests can be rendered using the harness but are not offline validation inputs.
The two run logs preserve the intermediate failure and subsequent completion.

| Phase | Recorded result |
|---|---|
| Baseline | Existing 128/256/32 MiB quota projects, retained payloads, PVC/PV identities and XFS `prjquota` read back. |
| P1, after provisioner restart | PASS. Controller Pod UID changed; worker boot ID stayed `7da313dd-598b-4202-a1fd-095786e0a1fd`. Full quota128 refused a new file with ENOSPC; quota256 stopped at 256 MiB; a newly provisioned quota64 received a distinct project and stopped at 64 MiB. Control wrote 8 MiB and noquota wrote 160 MiB. |
| P2, after worker reboot | PASS. Boot ID changed to `133fd613-1f0a-427a-b2a9-d996553cda96`. Existing projects, hard limits, volume identities and retained payload checks survived; quota128 again refused a new file and quota256 stopped at 256 MiB. Control/noquota completed. No new quota64 test in P2. |

## Intermediate failure and re-capture

[Part 1](run.part1.log) ended with exit 1 at the P1 after-capture:

```text
FAIL: P1/quota128: writer FS record must be exactly 'device xfs'
STEP FAIL: capture --phase P1 --point after
```

The comparison expected the sample filesystem string `device xfs`; the actual
initial and phase writer records report `fd0600000000 xfs`. This was a checker
comparison failure, not a successful first pass. The published harness compares
phase filesystem identity with the actual initial capture (`state.fs`) instead
of the sample literal. Its check still requires exact equality and an XFS type.

[Part 2](run.part2.log) begins with re-capture/validation of the completed P1
after evidence, then P1 PASS. The final capture retains the P1 writer UIDs and
completion times from before that re-capture. The re-capture collected the
completed P1 status, writer logs and observer snapshot under the corrected
comparison; it did not represent a second P1 writer run. Part 2 then records
the worker reboot, P2 PASS, `ALL PHASES PASS`, and exit 0. The failed capture
step was not accepted as evidence. The checker fix is included in the harness
in this tree; publication required no checker changes or relaxed fields.

## Offline re-validation

From the repository root (Python 3; no cluster access):

```sh
make c3-quota-preflight-persist-check \
  C3_INITIAL_RESULTS=tests/c3-quota-preflight/evidence/2026-10-02-persistence/initial.capture \
  C3_RESULTS=tests/c3-quota-preflight/evidence/2026-10-02-persistence/persistence.capture
```

```text
PASS: C3 quota persistence evidence through P2
```

This checks both P1 and P2. `--through P1` expects only the completed P1 prefix,
ending immediately before the first `GATE|P2|` record; it rejects a full capture
with extra P2 records. The published prefix was also checked successfully:

```text
PASS: C3 quota persistence evidence through P1
```

Negative control, changing one P2 hard-limit value in a temporary copy, followed
by the unmodified positive. The temporary directory is removed on exit:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
from pathlib import Path
import subprocess
import tempfile

base = Path('tests/c3-quota-preflight/evidence/2026-10-02-persistence')
lines = (base / 'persistence.capture').read_text().splitlines(keepends=True)
matches = [i for i, line in enumerate(lines)
           if line.startswith('OBSERVER|P2|') and '|QUOTA|quota128|' in line]
assert len(matches) == 1
index = matches[0]
assert lines[index].endswith('|1|131072\n')
lines[index] = lines[index].replace('|1|131072\n', '|1|131073\n')
command = ['python3', 'tests/c3-quota-preflight/c3_persist.py',
           '--initial', str(base / 'initial.capture'), '--capture']
with tempfile.TemporaryDirectory(prefix='.c3-negative-', dir='.') as scratch:
    mutated = Path(scratch) / 'mutated.capture'
    assert not mutated.exists()
    mutated.write_text(''.join(lines))
    result = subprocess.run(command + [str(mutated)], capture_output=True, text=True)
    print(result.stderr.strip())
    print('exit=' + str(result.returncode))
    assert result.returncode == 1
    assert 'changed from initial capture' in result.stderr
subprocess.run(command + [str(base / 'persistence.capture')], check=True)
PY
```

```text
FAIL: P2/quota128: project ID or hard limit changed from initial capture
exit=1
PASS: C3 quota persistence evidence through P2
```

Publication-time `make verify` also passed (separate from the historical run logs): offline manifest/image guards, GPU and local-quota
self-tests, initial C3 checker tests, 193 negative persistence rules before
positives, and structural phase-renderer tests. Self-tests alone are not live proof.

## Scope of this evidence

One disposable cluster (`ok-176-c3`); all Pods ran on one node
(`ok-176-c3-workers-jvmn2-vvcvr`). The captures do not record Talos/Kubernetes
versions or establish the cluster's worker count, and the checker enforces neither.
The run used OpenEBS Hostpath 4.6.0 on XFS with `prjquota`, with one provisioner
restart and one worker reboot. Writers use `hostUsers: false`,
UID/GID 1000, no effective capabilities, and no privilege escalation.

The checker enforces captured record completeness/order and source binding;
PVC/PV identities, classes and local paths; distinct writer/observer identities,
zero writer restarts and successful exits; Pod mount linkage without subPath;
observer image/privilege/read-only host mounts; changed provisioner Pod identity;
unchanged P1 boot ID and changed P2 boot ID; unchanged project IDs/hard limits
and distinct new quota64 project; UID/GID/capabilities and user-namespace maps;
rejected project reassignment (EINVAL, unchanged project); quota-bound writes
and reported errors; control/noquota completion after the bounded writers;
same XFS filesystem and sufficient noquota headroom; retained quota128 payload
size/SHA-256, retained control/noquota sizes, absent quota256 payload, and phase
probe cleanup. Control/noquota retained contents are not hashed. P2 does not
retest the new quota64 volume. See `c3_persist.py` and its initial checker for
exact tolerances and required records.

The retained quota128 payload SHA-256 is checked only for consistency within
`persistence.capture` (baseline = P1 = P2), not against the initial run. Boot IDs
are checked relative to each other: P1 equals the baseline, and P2 differs from
P1; they are not independently pinned to the IDs quoted above. The quota256
writer stopped at 256 MiB in this capture; the checker permits up to
capacity + 1 MiB rather than requiring that exact stopping point.

These are offline consistency checks on captured observations, not independent
attestation that the historical commands ran. This single run does not establish
production durability, replication, recovery from node/disk loss, power-loss
safety, repeated-reboot reliability, arbitrary workload safety, or online quota
expansion. Workloads without the tested user-namespace restriction can bypass
plain project quotas; this fixture does not prove admission enforces that restriction.

## Integrity and sanitization

Both captures are byte-identical to their original private artifacts; no capture
redaction was needed, so checked fields remain intact and the checker is unchanged.

| Capture | Original and published SHA-256 |
|---|---|
| `initial.capture` | `b740dc238ed285299529a6cb002f7a159a71e5e37567f6ae9931564dd25bcb74` |
| `persistence.capture` | `95de6a2187f7b36985d6bfd349d279607d1d1b20313e1d42e60f134b8dc08b0c` |

Redaction was applied to the two run logs only: local workstation manifest-output
paths were replaced with `[redacted-phase-manifest]`. No result, error, timestamp,
boot ID or cluster/node identity was changed. Original private log hashes:

| Log | Original SHA-256 | Published SHA-256 |
|---|---|---|
| `run.part1.log` | `86378d7e53590df901cc0a7363be9cb1ec09a1ebcb9834493adc819dfa162e30` | `e3423e3e268185c61ee016e6217aa038361d1dffca211f584bc06e3894058b12` |
| `run.part2.log` | `f9e74798422970e2f3018d3571355e94f322861df76d45f3d8f451cf450e66c9` | `c0e00ad073a14bbb46bf9a3fe7d861083dca95dd4161396e6766ae634a962fcf` |

`SHA256SUMS` records published artifact hashes. Verify them from this directory
with `sha256sum -c SHA256SUMS`. Cluster filesystem paths, object UIDs, filesystem
identity, image digests and boot IDs are retained as evidence; no workstation
paths, access-file names, credential contents or private network addresses are
included.
