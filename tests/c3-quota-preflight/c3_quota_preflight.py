#!/usr/bin/env python3
"""Offline evidence checker for the OK-176 C3 OpenEBS quota preflight.

This program never creates a cluster, container, namespace, PVC, Pod, loop
mapping, or filesystem.  The coordinator supplies output captured from the
separately-approved disposable Talos/OpenEBS run.  `quota_writer.c` is a small
unprivileged writer for the test workload image; this checker validates its
RESULT records rather than treating a nonzero command as proof of a bound.
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import sys
from dataclasses import dataclass

MIB = 1024 * 1024
TOLERANCE = 2 * MIB  # writer writes/fsyncs in 1MiB chunks; allows metadata/chunk edge
QUOTA_CAPACITY_MIB = {"quota128": 128, "quota256": 256, "control": 32}


@dataclass(frozen=True)
class Result:
    label: str
    written: int
    allocated: int
    write_errno: int
    fsync_errno: int
    fstat_errno: int
    close_errno: int
    uid: int
    cap_eff: int
    unlinked: bool


def fail(message: str) -> None:
    raise RuntimeError(message)


def parse_result(line: str) -> Result:
    fields = line.strip().split("|")
    if len(fields) != 11 or fields[0] != "RESULT":
        fail(f"malformed writer result: {line!r}")
    try:
        values = [int(value) for value in fields[2:]]
        if values[-1] not in (0, 1):
            fail(f"{fields[1]}: unlink flag must be 0 or 1")
        return Result(fields[1], *values)
    except ValueError as exc:
        fail(f"non-numeric writer result: {line!r}: {exc}")


def require_unprivileged(result: Result) -> None:
    if result.uid != 1000 or result.cap_eff != 0:
        fail(f"{result.label}: writer must be uid 1000 with no effective capabilities, "
             f"got uid={result.uid} cap_eff={result.cap_eff}")


def require_quota_bound(result: Result, capacity: int) -> None:
    require_unprivileged(result)
    quota_errors = [error for error in (result.write_errno, result.fsync_errno, result.close_errno) if error]
    if result.fstat_errno or len(quota_errors) != 1 or quota_errors[0] not in (28, 122):
        fail(f"{result.label}: expected one ENOSPC(28)/EDQUOT(122) from write/fsync/close, "
             f"got write={result.write_errno} fsync={result.fsync_errno} "
             f"fstat={result.fstat_errno} close={result.close_errno}")
    if result.written < capacity - TOLERANCE:
        fail(f"{result.label}: wrote {result.written} below capacity tolerance")
    if result.written >= capacity + MIB:
        fail(f"{result.label}: wrote {result.written} beyond declared {capacity}")
    lower, upper = capacity - TOLERANCE, capacity + TOLERANCE
    if not lower <= result.allocated <= upper:
        fail(f"{result.label}: allocated {result.allocated} outside explicit "
             f"[{lower}, {upper}] tolerance around {capacity}")


def require_open_unlinked(result: Result) -> None:
    if not result.unlinked:
        fail(f"{result.label}: open-but-unlinked writer evidence missing")


def require_complete(result: Result, requested: int, label: str) -> None:
    require_unprivileged(result)
    if result.write_errno or result.fsync_errno or result.fstat_errno or result.close_errno:
        fail(f"{result.label}: {label} unexpectedly failed")
    if result.written != requested or result.allocated < requested:
        fail(f"{result.label}: {label} did not complete {requested} allocated bytes")


def parse_status(line: str) -> tuple[str, str, str, int, str, str, str, str, int, str, str, str]:
    fields = line.split("|")
    if len(fields) != 13 or fields[0] != "STATUS" or fields[1] not in ("before", "after"):
        fail(f"malformed independent kubectl status: {line!r}")
    phase, label, pod_uid, restarts, node, pv, pv_path, storage_class, exit_code, finished, pod_phase, host_users = fields[1:]
    if host_users != "false":
        fail(f"{label}: captured spec.hostUsers must be false, got {host_users!r}")
    if not all((label, pod_uid, node, pv, pv_path, storage_class)):
        fail(f"incomplete independent kubectl status: {line!r}")
    if phase == "before":
        if pod_phase != "Running" or exit_code != "-" or finished != "-":
            fail(f"before status must precede writer termination: {line!r}")
        exit_value = -1
    else:
        if pod_phase != "Succeeded" or not finished:
            fail(f"after status needs writer finishedAt: {line!r}")
        exit_value = exit_code
    try:
        if phase == "after":
            if not finished.endswith("Z"):
                fail(f"{label}: finishedAt must be a UTC Kubernetes timestamp")
            datetime.datetime.fromisoformat(finished.replace("Z", "+00:00"))
        exit_value = int(exit_value)
        return phase, label, pod_uid, int(restarts), node, pv, pv_path, storage_class, exit_value, finished, pod_phase, host_users
    except ValueError as exc:
        fail(f"non-numeric status field: {line!r}: {exc}")


def require_readback(lines: list[str], label: str, capacity_mib: int) -> int:
    ids = [line.split("|", 2)[2] for line in lines if line.startswith(f"READBACK|{label}|")]
    if len(ids) != 1 or not ids[0].isdigit() or int(ids[0]) <= 0:
        fail(f"{label}: require one nonzero XFS project ID readback")
    # `report -p -n -N` is emitted raw by a separately checked command. Locate
    # this project's Used/Soft/Hard table row and require the exact hard limit.
    rows = [line.split("|", 2)[2] for line in lines if line.startswith(f"XFS_REPORT|{label}|")]
    project = re.escape(ids[0])
    row = next((row for row in rows if re.search(rf"^#?{project}\s+", row)), None)
    if row is None:
        fail(f"{label}: XFS report has no row for project {ids[0]}")
    fields = re.split(r"\s+", row.lstrip("#").strip())
    if len(fields) < 4:
        fail(f"{label}: malformed XFS project-quota row: {row!r}")
    hard = fields[3]
    # xfs_quota(8) documents report blocks as 1KiB by default. `-n -N`
    # below yields numeric project IDs and no headers, so this is an exact,
    # unit-defined comparison rather than a locale/human-format string.
    expected = str(capacity_mib * 1024)
    if hard != expected:
        fail(f"{label}: XFS hard limit {hard!r} KiB, expected {expected!r} KiB")
    return int(ids[0])


def numeric_record(lines: list[str], kind: str, label: str, size: int) -> list[int]:
    records = [line.split("|") for line in lines if line.startswith(f"{kind}|{label}|")]
    if len(records) != 1 or len(records[0]) != size + 2:
        fail(f"{label}: require one complete {kind} record")
    try:
        return [int(value) for value in records[0][2:]]
    except ValueError:
        fail(f"{label}: non-numeric {kind} record")


def require_user_namespace(lines: list[str], label: str) -> None:
    uid, gid, caps = numeric_record(lines, "IDENTITY", label, 3)
    if (uid, gid, caps) != (1000, 1000, 0):
        fail(f"{label}: identity must be UID/GID 1000 with no effective capabilities")
    mapping = numeric_record(lines, "USERNS", label, 6)
    for offset, kind in ((0, "UID"), (3, "GID")):
        inside, outside, count = mapping[offset:offset + 3]
        if inside != 0 or outside <= 0 or count <= 1000 or outside + count > 2**32 - 1:
            fail(f"{label}: {kind} map disagrees with spec.hostUsers=false: {mapping}")


def require_reassignment_rejected(lines: list[str], label: str, project_id: int) -> None:
    rc, error, before, after = numeric_record(lines, "REASSIGN", label, 4)
    if rc != -1 or error != 22:
        fail(f"{label}: project-ID reassignment must be rejected with EINVAL(22), got rc={rc} errno={error}")
    if before != after:
        fail(f"{label}: project ID changed during rejected reassignment: {before} -> {after}")
    if before != project_id:
        fail(f"{label}: payload project ID {before} differs from observer-assigned {project_id}")


WRITER_KINDS = {"FS", "IDENTITY", "USERNS", "REASSIGN", "RESULT"}


def captured_writer_records(lines: list[str], statuses: list[tuple]) -> list[str]:
    normalized = []
    for line in lines:
        fields = line.split("|")
        if fields[0] == "WRITER":
            if len(fields) < 5:
                fail(f"malformed source-bound writer record: {line!r}")
            label, uid = fields[1:3]
            record = fields[3:]
            matching = [entry for entry in statuses if entry[1] == label]
            if len(matching) != 2 or any(entry[2] != uid for entry in matching):
                fail(f"{label}: writer record source Pod UID does not match captured status")
            if record[0] in WRITER_KINDS:
                if record[1] != label:
                    fail(f"{label}: writer record label disagrees with source Pod")
            elif record[0] == "HEADROOM":
                if len(record) != 4 or record[1] != "after" or record[2] != label:
                    fail(f"{label}: writer headroom disagrees with source Pod")
            else:
                fail(f"{label}: unexpected writer log record: {record[0]}")
            normalized.append("|".join(record))
        elif fields[0] in WRITER_KINDS or line.startswith("HEADROOM|after|"):
            fail(f"writer record missing independent source Pod: {line!r}")
        else:
            normalized.append(line)
    return normalized


def validate_records(lines: list[str]) -> None:
    statuses = [parse_status(line) for line in lines if line.startswith("STATUS|")]
    lines = captured_writer_records(lines, statuses)
    results_list = [parse_result(line) for line in lines if line.startswith("RESULT|")]
    results = {result.label: result for result in results_list}
    required = {"noquota", "quota128", "quota256", "control"}
    if len(results) != len(results_list) or set(results) != required:
        fail(f"require unique results exactly {sorted(required)}, got {[result.label for result in results_list]}")
    expected_class = {"quota128": "ok176-c3-xfs", "quota256": "ok176-c3-xfs", "control": "ok176-c3-xfs", "noquota": "ok176-c3-xfs-noquota"}
    for label, storage_class in expected_class.items():
        entries = [entry for entry in statuses if entry[1] == label]
        if len(entries) != 2 or {entry[0] for entry in entries} != {"before", "after"}:
            fail(f"{label}: require independent before/after kubectl status")
        before = next(entry for entry in entries if entry[0] == "before")
        after = next(entry for entry in entries if entry[0] == "after")
        if before[2:8] != after[2:8] or before[3] != 0 or after[3] != 0:
            fail(f"{label}: pod UID/restarts/node/PV/path/StorageClass changed or restarted")
        if after[8] != 0 or after[9] == "-":
            fail(f"{label}: writer did not terminate exit 0 with finishedAt")
        if before[7] != storage_class:
            fail(f"{label}: expected StorageClass {storage_class}, got {before[7]}")
        if label == "noquota":
            pv_path = pathlib.PurePosixPath(before[6])
            fixture_root = pathlib.PurePosixPath("/var/openebs/ok176-c3")
            try:
                relative_path = pv_path.relative_to(fixture_root)
            except ValueError:
                fail(f"{label}: PV local path must be under {fixture_root}, got {pv_path}")
            if not relative_path.parts or ".." in relative_path.parts:
                fail(f"{label}: PV local path must be under {fixture_root}, got {pv_path}")
        require_user_namespace(lines, label)
    nodes = {entry[4] for entry in statuses if entry[0] == "before"}
    if len(nodes) != 1:
        fail(f"fixture volumes are not tied to one backing-store node: {sorted(nodes)}")
    finished = {entry[1]: datetime.datetime.fromisoformat(entry[9].replace("Z", "+00:00")) for entry in statuses if entry[0] == "after"}
    if not (finished["control"] > finished["quota128"] and finished["control"] > finished["quota256"]):
        fail("control writer must finish after both bounded writers")
    project128 = require_readback(lines, "quota128", 128)
    project256 = require_readback(lines, "quota256", 256)
    if project128 == project256:
        fail("quota128 and quota256 share a project ID")
    require_reassignment_rejected(lines, "quota128", project128)
    require_reassignment_rejected(lines, "quota256", project256)
    for label in expected_class:
        kinds = ("IDENTITY", "USERNS", "REASSIGN", "RESULT") if label.startswith("quota") else ("IDENTITY", "USERNS", "RESULT")
        positions = [next(index for index, line in enumerate(lines) if line.startswith(f"{kind}|{label}|")) for kind in kinds]
        if positions != sorted(positions):
            fail(f"{label}: writer evidence must precede RESULT in IDENTITY/USERNS/REASSIGN order")
    fs = {}
    for label in expected_class:
        matches = [line.split("|", 2)[2] for line in lines if line.startswith(f"FS|{label}|")]
        if len(matches) != 1 or not matches[0]:
            fail(f"{label}: require one filesystem source/type readback")
        fs[label] = matches[0]
    fixture_fs = set(fs.values())
    if len(fixture_fs) != 1 or "xfs" not in fs["quota128"].lower():
        fail(f"fixture volumes are not proven on one XFS backing store: {fs}")
    for label in expected_class:
        values = [line.split("|") for line in lines if line.startswith(f"HEADROOM|before|{label}|") or line.startswith(f"HEADROOM|after|{label}|")]
        if len(values) != 2 or any(len(value) != 4 for value in values) or {value[1] for value in values} != {"before", "after"}:
            fail(f"{label}: require one complete before/after HEADROOM record")
        try:
            headroom = {value[1]: int(value[3]) for value in values}
        except ValueError as exc:
            fail(f"{label}: invalid HEADROOM value: {exc}")
        if min(headroom.values()) < 0:
            fail(f"{label}: HEADROOM must be nonnegative")
        if label == "noquota":
            # statfs here is not project-quota scoped; FS equality above ties
            # this parent-filesystem measurement to every bounded volume.
            if min(headroom.values()) < 1024 * 1024:
                fail(f"{label}: parent filesystem headroom fell below 1GiB")
        else:
            expected = QUOTA_CAPACITY_MIB[label] * 1024
            if headroom["before"] != expected:
                fail(f"{label}: before headroom {headroom['before']} KiB, expected declared capacity {expected} KiB")
            if headroom["after"] > headroom["before"]:
                fail(f"{label}: after headroom exceeds before headroom")
    # Negative control first: same-XFS noquota volume must overfill, or an
    # unrelated full disk/permission/eviction could masquerade as quota proof.
    require_complete(results["noquota"], 160 * MIB, "no-quota control")
    if results["noquota"].allocated <= 128 * MIB:
        fail("no-quota control did not exceed its declared 128Mi")
    require_quota_bound(results["quota128"], 128 * MIB)
    require_quota_bound(results["quota256"], 256 * MIB)
    require_open_unlinked(results["quota256"])
    require_complete(results["control"], 16 * MIB, "same-backing-store independent control")


def self_test() -> None:
    # Negative checker cases first. If any invalid evidence is accepted, fail.
    def sample(label: str, written: int, allocated: int, write: int = 0,
               fsync: int = 0, fstat: int = 0, close: int = 0, uid: int = 1000,
               caps: int = 0, unlinked: bool = False) -> Result:
        return Result(label, written, allocated, write, fsync, fstat, close, uid, caps, unlinked)
    bad = (
        (require_quota_bound, sample("quota", 129 * MIB, 129 * MIB, write=28), (128 * MIB,)),
        (require_quota_bound, sample("quota", 128 * MIB, 128 * MIB), (128 * MIB,)),
        (require_quota_bound, sample("quota", 128 * MIB, 0, write=28, fstat=5), (128 * MIB,)),
        (require_quota_bound, sample("quota", 128 * MIB, 128 * MIB, write=5), (128 * MIB,)),
        (require_quota_bound, sample("quota", 128 * MIB, 128 * MIB, write=28, uid=0), (128 * MIB,)),
        (require_quota_bound, sample("quota", 128 * MIB, 128 * MIB, write=28, caps=1), (128 * MIB,)),
        (require_complete, sample("control", 0, 0, write=28), (16 * MIB, "control")),
        (require_open_unlinked, sample("quota", 256 * MIB, 256 * MIB, write=122, unlinked=False), ()),
    )
    for checker, result, args in bad:
        try:
            checker(result, *args)
        except RuntimeError:
            continue
        fail(f"self-test negative case was accepted: {result}")
    # Exercise the complete parser/checker, not a parallel model of its rules.
    good = []
    for label, capacity in (("quota128", 128), ("quota256", 256), ("control", 16), ("noquota", 160)):
        sc = "ok176-c3-xfs-noquota" if label == "noquota" else "ok176-c3-xfs"
        for phase in ("before", "after"):
            finished = "-" if phase == "before" else ("2026-10-02T00:00:02Z" if label == "control" else "2026-10-02T00:00:01Z")
            good.append(f"STATUS|{phase}|{label}|uid-{label}|0|worker|pv-{label}|/var/openebs/ok176-c3/{label}|{sc}|{'-' if phase == 'before' else '0'}|{finished}|{'Running' if phase == 'before' else 'Succeeded'}|false")
            if label == "noquota":
                headroom = 24197524 if phase == "before" else 24033656
            else:
                headroom = QUOTA_CAPACITY_MIB[label] * 1024
                if phase == "after" and label != "quota256":
                    headroom -= capacity * 1024
            good.append(f"HEADROOM|{phase}|{label}|{headroom}")
        good.extend((f"IDENTITY|{label}|1000|1000|0", f"USERNS|{label}|0|65536|65536|0|65536|65536", f"FS|{label}|abcd xfs"))
        good.append(f"RESULT|{label}|{capacity * MIB}|{capacity * MIB}|{28 if label.startswith('quota') else 0}|0|0|0|1000|0|{1 if label == 'quota256' else 0}")
    for label, project, capacity in (("quota128", 2, 128), ("quota256", 3, 256)):
        good.extend((f"READBACK|{label}|{project}", f"XFS_REPORT|{label}|#{project} 0 0 {capacity * 1024} 0 0 0",
                     f"REASSIGN|{label}|-1|22|{project}|{project}"))
    for label in ("quota128", "quota256"):
        record = next(line for line in good if line.startswith(f"REASSIGN|{label}|"))
        good.remove(record)
        good.insert(next(index for index, line in enumerate(good) if line.startswith(f"RESULT|{label}|")), record)
    def captured(records: list[str]) -> list[str]:
        output = []
        for line in records:
            fields = line.split("|")
            if fields[0] in WRITER_KINDS or line.startswith("HEADROOM|after|"):
                label = fields[2] if fields[0] == "HEADROOM" else fields[1]
                output.append(f"WRITER|{label}|uid-{label}|{line}")
            else:
                output.append(line)
        return output
    original = "REASSIGN|quota128|-1|22|2|2"
    negatives = [
        ("reassignment succeeding", original, "REASSIGN|quota128|0|0|2|2", "must be rejected"),
        ("project ID changed", original, "REASSIGN|quota128|-1|22|2|0", "project ID changed"),
        ("reassignment record missing", original, None, "require one complete REASSIGN"),
        ("observer ID mismatch", original, "REASSIGN|quota128|-1|22|4|4", "differs from observer-assigned"),
        ("unexpected reassignment errno", original, "REASSIGN|quota128|-1|25|2|2", "must be rejected"),
        ("duplicate reassignment record", original, original + "\n" + original, "require one complete REASSIGN"),
        ("initial UID map", "USERNS|quota128|0|65536|65536|0|65536|65536", "USERNS|quota128|0|0|4294967295|0|65536|65536", "UID map disagrees"),
        ("initial GID map", "USERNS|quota128|0|65536|65536|0|65536|65536", "USERNS|quota128|0|65536|65536|0|0|4294967295", "GID map disagrees"),
        ("UID map record missing", "USERNS|quota128|0|65536|65536|0|65536|65536", None, "require one complete USERNS"),
        ("wrong writer GID", "IDENTITY|quota128|1000|1000|0", "IDENTITY|quota128|1000|0|0", "identity must be UID/GID"),
    ]
    for label in ("quota128", "quota256", "control", "noquota"):
        for phase in ("before", "after"):
            record = next(line for line in good if line.startswith(f"HEADROOM|{phase}|{label}|"))
            prefix = record.rsplit("|", 1)[0]
            negatives.extend((
                (f"missing {label} {phase} headroom", record, None, "require one complete before/after HEADROOM"),
                (f"duplicate {label} {phase} headroom", record, record + "\n" + record, "require one complete before/after HEADROOM"),
                (f"non-numeric {label} {phase} headroom", record, prefix + "|invalid", "invalid HEADROOM value"),
            ))
            if label == "noquota":
                negatives.append((f"noquota {phase} headroom below 1GiB", record, prefix + "|1048575", "parent filesystem headroom fell below 1GiB"))
            elif phase == "before":
                negatives.append((f"{label} before headroom differs from capacity", record, prefix + f"|{QUOTA_CAPACITY_MIB[label] * 1024 - 1}", "expected declared capacity"))
            else:
                negatives.append((f"{label} after headroom exceeds before", record, prefix + f"|{QUOTA_CAPACITY_MIB[label] * 1024 + 1}", "after headroom exceeds before headroom"))
    # Both status snapshots of every writer must explicitly opt out of host users.
    for line in good:
        if line.startswith("STATUS|"):
            for value in ("true", "absent", ""):
                negatives.append((f"hostUsers {value or 'empty'} {line.split('|')[1:3]}", line,
                                  line.rsplit("|", 1)[0] + "|" + value, "spec.hostUsers must be false"))
            negatives.append((f"hostUsers field missing {line.split('|')[1:3]}", line,
                              line.rsplit("|", 1)[0], "malformed independent kubectl status"))
    for name, old, replacement, reason in negatives:
        changed = [value for value in good if value != old] if replacement is None else [replacement if value == old else value for value in good]
        changed = "\n".join(changed).splitlines()
        try:
            validate_records(captured(changed))
        except RuntimeError as exc:
            if reason not in str(exc):
                fail(f"self-test {name} failed for unrelated reason: {exc}")
            print(f"PASS: negative {name}: FAIL: {exc}")
        else:
            fail(f"self-test negative case was accepted: {name}")
    source_good = captured(good)
    chronology = [line for line in source_good if "|REASSIGN|quota128|" not in line]
    chronology.append(next(line for line in source_good if "|REASSIGN|quota128|" in line))
    extra_bad = [
        ("reassignment after write", chronology, "must precede RESULT"),
        ("wrong source Pod label", [line.replace("WRITER|quota256|uid-quota256|REASSIGN|quota256", "WRITER|quota128|uid-quota128|REASSIGN|quota256") for line in source_good], "label disagrees with source Pod"),
        ("wrong source Pod UID", [line.replace("WRITER|quota128|uid-quota128|REASSIGN", "WRITER|quota128|wrong-uid|REASSIGN") for line in source_good], "source Pod UID does not match"),
        ("missing source Pod", good, "missing independent source Pod"),
        ("immediate ENOSPC", [line.replace(f"RESULT|quota128|{128 * MIB}|", "RESULT|quota128|0|") for line in source_good], "below capacity tolerance"),
        ("invalid unlink flag", [line[:-1] + "2" if "|RESULT|quota256|" in line else line for line in source_good], "unlink flag must be 0 or 1"),
        ("non-UTC finishedAt", [line.replace("2026-10-02T00:00:02Z", "2026-10-02T10:00:00+10:00") for line in source_good], "finishedAt must be a UTC"),
        ("noquota local-path class", [line.replace("|ok176-c3-xfs-noquota|", "|local-path|") for line in source_good], "expected StorageClass ok176-c3-xfs-noquota"),
        ("noquota PV outside fixture root", [line.replace("|/var/openebs/ok176-c3/noquota|", "|/var/lib/rancher/k3s/storage/noquota|") for line in source_good], "PV local path must be under /var/openebs/ok176-c3"),
        ("noquota different filesystem", [line.replace("FS|noquota|abcd xfs", "FS|noquota|efgh xfs") for line in source_good], "fixture volumes are not proven on one XFS backing store"),
    ]
    for name, records, reason in extra_bad:
        try:
            validate_records(records)
        except RuntimeError as exc:
            if reason not in str(exc):
                fail(f"self-test {name} failed for unrelated reason: {exc}")
            print(f"PASS: negative {name}: FAIL: {exc}")
        else:
            fail(f"self-test negative case was accepted: {name}")
    validate_records(source_good)
    print("PASS: positive complete user-namespace/reassignment sample accepted")
    require_quota_bound(sample("quota", 128 * MIB, 128 * MIB, write=28), 128 * MIB)
    unlinked = sample("quota", 256 * MIB, 256 * MIB, write=122, unlinked=True)
    require_quota_bound(unlinked, 256 * MIB)
    require_open_unlinked(unlinked)
    require_complete(sample("control", 16 * MIB, 16 * MIB), 16 * MIB, "control")
    print("PASS: C3 checker unit assertions reject wrong errno, fstat, UID/capability, bounds, controls, and missing unlink")
    print("PASS: C3 checker unit assertions accept isolated bounded ENOSPC/EDQUOT records")


def check_results(path: str) -> None:
    lines = sys.stdin.read().splitlines() if path == "-" else pathlib.Path(path).read_text().splitlines()
    validate_records(lines)
    print("PASS: captured record schema and assertions are internally consistent; this is not live-proof publication")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("self-test")
    check = sub.add_parser("check-results")
    check.add_argument("--results", required=True, help="captured workload output file, or - for stdin")
    args = parser.parse_args()
    try:
        if args.mode == "self-test":
            self_test()
        else:
            check_results(args.results)
    except (OSError, RuntimeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
