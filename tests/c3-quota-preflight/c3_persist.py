#!/usr/bin/env python3
"""Fail-closed checker for the OK-176 C3 restart/reboot persistence run."""
from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import sys
from dataclasses import dataclass

import c3_quota_preflight as initial


MIB = initial.MIB
PHASES = ("P1", "P2")
EXISTING = ("quota128", "quota256", "control", "noquota")
P1_LABELS = EXISTING + ("quota64",)
PVC_NAMES = {
    "quota128": "ok176-c3-quota-128",
    "quota256": "ok176-c3-quota-256",
    "control": "ok176-c3-control",
    "noquota": "ok176-c3-noquota",
    "quota64": "ok176-c3-quota-64",
}
CLASSES = {
    "quota128": "ok176-c3-xfs",
    "quota256": "ok176-c3-xfs",
    "control": "ok176-c3-xfs",
    "noquota": "ok176-c3-xfs-noquota",
    "quota64": "ok176-c3-xfs",
}
HARD_KIB = {"quota128": 128 * 1024, "quota256": 256 * 1024,
            "control": 32 * 1024, "quota64": 64 * 1024}
REQUESTED = {"quota128": 2 * MIB, "quota256": 300 * MIB,
             "control": 8 * MIB, "noquota": 160 * MIB,
             "quota64": 80 * MIB}
WRITER_KINDS = {"FS", "TARGET", "IDENTITY", "USERNS", "REASSIGN_TARGET", "REASSIGN",
                "RESULT", "CLEANUP", "HEADROOM"}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z", re.I)
UTC_SECOND = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
GIB_KIB = 1024 * 1024
OBSERVER_SPEC = ("/var/openebs/ok176-c3", "/data",
                 "/var", "/host-var",
                 "/proc/sys/kernel/random/boot_id", "/host-boot-id",
                 "/dev", "/dev", "readonly", "privileged")


def fail(message: str) -> None:
    raise RuntimeError(message)


@dataclass(frozen=True)
class Inventory:
    label: str
    pvc: str
    pvc_uid: str
    pv: str
    pv_uid: str
    path: str
    storage_class: str

    @property
    def identity(self) -> tuple[str, ...]:
        return (self.pvc, self.pvc_uid, self.pv, self.pv_uid,
                self.path, self.storage_class)


@dataclass(frozen=True)
class Status:
    phase: str
    when: str
    label: str
    pod_uid: str
    restarts: int
    node: str
    inventory: Inventory
    exit_code: int
    finished: str
    pod_phase: str
    host_users: str


@dataclass(frozen=True)
class InitialState:
    pod_uids: dict[str, str]
    node: str
    pv: dict[str, tuple[str, str, str]]
    projects: dict[str, int]
    fs: str


@dataclass
class Capture:
    observer_status: dict[str, tuple[str, str]]
    observer_spec: dict[str, tuple[str, ...]]
    boots: dict[tuple[str, str], tuple[str, str]]
    gates: dict[tuple[str, str], str]
    provisioner: dict[str, tuple[str, str, str, str]]
    checkpoints: dict[str, tuple[str, str, str]]
    pvc_requests: dict[tuple[str, str], int]
    inventory: dict[tuple[str, str], Inventory]
    observer: dict[tuple[str, str, str], tuple[str, ...]]
    statuses: dict[tuple[str, str, str], Status]
    mounts: dict[tuple[str, str, str], tuple[str, ...]]
    writers: dict[tuple[str, str, str], tuple[str, ...]]
    writer_order: dict[tuple[str, str], list[str]]
    positions: dict[tuple[str, ...], int]
    phase_positions: dict[str, list[tuple[str, int]]]


def unique_put(mapping: dict, key: object, value: object, description: str) -> None:
    if key in mapping:
        fail(f"duplicate {description}: {key}")
    mapping[key] = value


def parse_nonnegative(value: str, description: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        fail(f"non-numeric {description}: {value!r}: {exc}")
    if number < 0:
        fail(f"negative {description}: {number}")
    return number


def parse_finished_at(value: str, description: str) -> datetime.datetime:
    try:
        if not UTC_SECOND.fullmatch(value):
            raise ValueError("timestamp is not UTC second precision")
        parsed = datetime.datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        fail(f"{description}: invalid finishedAt: {exc}")
    return parsed


def parse_capture(lines: list[str]) -> Capture:
    capture = Capture({}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {})
    for number, line in enumerate(lines, 1):
        if not line or line != line.strip():
            fail(f"malformed blank or whitespace-padded record at line {number}")
        fields = line.split("|")
        kind = fields[0]
        phase_index = 2 if kind == "BOOT" else 1
        if len(fields) > phase_index and fields[phase_index] in ("baseline", *PHASES):
            capture.phase_positions.setdefault(fields[phase_index], []).append((kind, number))
        if kind == "OBSERVER_STATUS":
            if len(fields) != 4 or fields[1] not in ("baseline", *PHASES) or not all(fields[2:]):
                fail(f"malformed OBSERVER_STATUS record: {line!r}")
            unique_put(capture.observer_status, fields[1], (fields[2], fields[3]),
                       "OBSERVER_STATUS record")
            capture.positions[(kind, fields[1])] = number
        elif kind == "OBSERVER_SPEC":
            if len(fields) != 13 or fields[1] not in ("baseline", *PHASES) or not fields[2]:
                fail(f"malformed OBSERVER_SPEC record: {line!r}")
            unique_put(capture.observer_spec, fields[1], tuple(fields[2:]),
                       "OBSERVER_SPEC record")
            capture.positions[(kind, fields[1])] = number
        elif kind == "BOOT":
            if (len(fields) != 5 or fields[1] not in ("before", "after")
                    or fields[2] not in PHASES or not fields[3]):
                fail(f"malformed BOOT record: {line!r}")
            unique_put(capture.boots, (fields[1], fields[2]), (fields[3], fields[4]),
                       "BOOT record")
            capture.positions[(kind, fields[1], fields[2])] = number
        elif kind == "GATE":
            if len(fields) != 4 or fields[1] not in PHASES or not all(fields[2:]):
                fail(f"malformed GATE record: {line!r}")
            unique_put(capture.gates, (fields[1], fields[2]), fields[3],
                       "GATE record")
            capture.positions[(kind, fields[1], fields[2])] = number
        elif kind == "PROVISIONER":
            if len(fields) != 6 or fields[1] not in ("baseline", "P1") or not all(fields[2:]):
                fail(f"malformed PROVISIONER record: {line!r}")
            unique_put(capture.provisioner, fields[1], tuple(fields[2:]),
                       "PROVISIONER record")
            capture.positions[(kind, fields[1])] = number
        elif kind == "CHECKPOINT":
            if len(fields) != 5 or fields[1:] != ["P1", "restarted", "quota64", "absent"]:
                fail(f"malformed CHECKPOINT record: {line!r}")
            unique_put(capture.checkpoints, fields[1], tuple(fields[2:]),
                       "CHECKPOINT record")
            capture.positions[(kind, fields[1])] = number
        elif kind == "PVC_REQUEST":
            if len(fields) != 4 or fields[1:3] != ["P1", "quota64"]:
                fail(f"malformed PVC_REQUEST record: {line!r}")
            requested = parse_nonnegative(fields[3], "P1/quota64 PVC request")
            unique_put(capture.pvc_requests, (fields[1], fields[2]), requested,
                       "PVC_REQUEST record")
            capture.positions[(kind, fields[1], fields[2])] = number
        elif kind == "PVC":
            if len(fields) != 9 or fields[1] != "baseline" or fields[2] not in EXISTING or not all(fields[3:]):
                fail(f"malformed baseline PVC record: {line!r}")
            inventory = Inventory(fields[2], *fields[3:])
            unique_put(capture.inventory, ("baseline", fields[2]), inventory,
                       "baseline PVC record")
        elif kind == "OBSERVER":
            if len(fields) < 5 or fields[1] not in ("baseline", *PHASES):
                fail(f"malformed OBSERVER record: {line!r}")
            phase, uid, observed_kind = fields[1:4]
            if observed_kind == "HOSTFS":
                if len(fields) != 7:
                    fail(f"malformed observer HOSTFS record: {line!r}")
                key, value = (phase, observed_kind, ""), tuple(fields[4:])
            elif observed_kind == "BOOT_ID":
                if len(fields) != 5:
                    fail(f"malformed observer BOOT_ID record: {line!r}")
                key, value = (phase, observed_kind, ""), (fields[4],)
            elif observed_kind == "QUOTA":
                if len(fields) != 7 or fields[4] not in (*EXISTING, "quota64"):
                    fail(f"malformed observer QUOTA record: {line!r}")
                key, value = (phase, observed_kind, fields[4]), tuple(fields[5:])
            elif observed_kind == "FILE":
                if len(fields) != 9 or fields[4] not in (*EXISTING, "quota64"):
                    fail(f"malformed observer FILE record: {line!r}")
                key, value = (phase, observed_kind, f"{fields[4]}:{fields[5]}"), tuple(fields[6:])
            else:
                fail(f"unknown observer record kind: {observed_kind!r}")
            unique_put(capture.observer, key, (uid, *value), "observer record")
        elif kind == "STATUS":
            if len(fields) != 17 or fields[1] not in PHASES or fields[2] not in ("before", "after"):
                fail(f"malformed phase STATUS record: {line!r}")
            phase, when, label, pod_uid = fields[1:5]
            if label not in (*EXISTING, "quota64"):
                fail(f"unknown STATUS label: {label!r}")
            inventory = Inventory(label, *fields[7:13])
            if not all((pod_uid, fields[6], *inventory.identity)):
                fail(f"{phase}/{label}: STATUS has an empty Pod/PVC/PV identity field")
            restarts = parse_nonnegative(fields[5], f"{phase}/{label} restart count")
            if when == "before":
                if fields[13:16] != ["-", "-", "Running"]:
                    fail(f"{phase}/{label}: before status must be Running before termination")
                exit_code = -1
            else:
                if fields[15] != "Succeeded":
                    fail(f"{phase}/{label}: after status must be Succeeded")
                exit_code = parse_nonnegative(fields[13], f"{phase}/{label} exit code")
                parse_finished_at(fields[14], f"{phase}/{label}")
            status = Status(phase, when, label, pod_uid, restarts, fields[6],
                            inventory, exit_code, fields[14], fields[15], fields[16])
            unique_put(capture.statuses, (phase, when, label), status, "phase STATUS record")
            capture.positions[(kind, phase, when, label)] = number
        elif kind == "MOUNT":
            if (len(fields) != 9 or fields[1] not in PHASES
                    or fields[2] not in ("before", "after")
                    or fields[3] not in (*EXISTING, "quota64") or not all(fields[4:])):
                fail(f"malformed MOUNT record: {line!r}")
            unique_put(capture.mounts, (fields[1], fields[2], fields[3]), tuple(fields[4:]),
                       "MOUNT record")
            capture.positions[(kind, fields[1], fields[2], fields[3])] = number
        elif kind == "WRITER":
            if len(fields) < 7 or fields[1] not in PHASES:
                fail(f"malformed source-bound WRITER record: {line!r}")
            phase, label, pod_uid, record_kind = fields[1:5]
            record = tuple(fields[4:])
            if label not in (*EXISTING, "quota64") or record_kind not in WRITER_KINDS:
                fail(f"unknown writer record: {line!r}")
            lengths = {"FS": 3, "TARGET": 4, "IDENTITY": 5, "USERNS": 8,
                       "REASSIGN_TARGET": 3, "REASSIGN": 6, "RESULT": 11, "CLEANUP": 3, "HEADROOM": 4}
            if len(record) != lengths[record_kind]:
                fail(f"malformed {record_kind} writer record: {line!r}")
            embedded_label = record[2] if record_kind == "HEADROOM" else record[1]
            if embedded_label != label or (record_kind == "HEADROOM" and record[1] != "after"):
                fail(f"{phase}/{label}: writer record label/phase disagrees with source Pod")
            unique_put(capture.writers, (phase, label, record_kind),
                       (pod_uid, *record), "writer record")
            capture.writer_order.setdefault((phase, label), []).append(record_kind)
        else:
            fail(f"unknown or unwrapped capture record at line {number}: {kind!r}")
    return capture


def numeric_quota_rows(lines: list[str], label: str) -> dict[int, int]:
    rows: dict[int, int] = {}
    prefix = f"XFS_REPORT|{label}|"
    for line in lines:
        if not line.startswith(prefix):
            continue
        row = line.split("|", 2)[2].strip()
        fields = row.lstrip("#").split()
        if not row.startswith("#") or len(fields) < 4 or not fields[0].isdigit() or not fields[3].isdigit():
            continue
        project, hard = int(fields[0]), int(fields[3])
        if project in rows and rows[project] != hard:
            fail(f"initial {label}: contradictory hard limits for project {project}")
        rows[project] = hard
    return rows


def initial_state(lines: list[str]) -> InitialState:
    try:
        initial.validate_records(lines)
    except RuntimeError as exc:
        fail(f"initial capture rejected by original checker: {exc}")
    statuses = [initial.parse_status(line) for line in lines if line.startswith("STATUS|")]
    before = {entry[1]: entry for entry in statuses if entry[0] == "before"}
    if set(before) != set(EXISTING):
        fail("initial capture lacks the four original writer identities")
    nodes = {entry[4] for entry in before.values()}
    if len(nodes) != 1:
        fail("initial capture does not identify one backing-store node")
    project128 = initial.require_readback(lines, "quota128", 128)
    project256 = initial.require_readback(lines, "quota256", 256)
    table128 = numeric_quota_rows(lines, "quota128")
    table256 = numeric_quota_rows(lines, "quota256")
    if table128 != table256:
        fail("initial observer quota reports disagree")
    controls = [project for project, hard in table128.items()
                if hard == HARD_KIB["control"] and project not in (project128, project256)]
    if len(controls) != 1 or controls[0] <= 0:
        fail("initial observer report must uniquely identify control project at hard 32768 KiB")
    pv = {label: (entry[5], entry[6], entry[7]) for label, entry in before.items()}
    writer_lines = initial.captured_writer_records(lines, statuses)
    fs = [line.split("|", 2)[2] for line in writer_lines if line.startswith("FS|quota128|")]
    if len(fs) != 1 or not fs[0].endswith(" xfs"):
        fail("initial capture lacks one quota128 XFS filesystem record")
    return InitialState({label: entry[2] for label, entry in before.items()},
                        next(iter(nodes)), pv,
                        {"quota128": project128, "quota256": project256,
                         "control": controls[0]}, fs[0])


def require_observer(capture: Capture, state: InitialState, through: str) -> None:
    phases = ("baseline", "P1") if through == "P1" else ("baseline", *PHASES)
    workload_phases = ("P1",) if through == "P1" else PHASES
    if set(capture.observer_status) != set(phases):
        fail(f"require one observer status for each phase {list(phases)}")
    if set(capture.observer_spec) != set(phases):
        fail(f"require one OBSERVER_SPEC for each phase {list(phases)}")
    expected_boots = {(point, phase) for phase in workload_phases
                      for point in ("before", "after")}
    if set(capture.boots) != expected_boots:
        fail(f"BOOT records must be exact; expected={sorted(expected_boots)}")
    expected_gates = {(phase, f"ok176-c3-{phase.lower()}-{suffix}")
                      for phase in workload_phases
                      for suffix in ("gate", "control-gate")}
    if set(capture.gates) != expected_gates or set(capture.gates.values()) != {"hold"}:
        fail("phase gate records must identify both exact ConfigMaps with start=hold")
    if set(capture.provisioner) != {"baseline", "P1"}:
        fail("require baseline and P1 provisioner identities")
    if capture.checkpoints != {"P1": ("restarted", "quota64", "absent")}:
        fail("require exact CHECKPOINT|P1|restarted|quota64|absent record")
    if capture.pvc_requests != {("P1", "quota64"): 64 * MIB}:
        fail("require PVC_REQUEST|P1|quota64|67108864 from the live API request")
    baseline_provisioner = capture.provisioner["baseline"]
    p1_provisioner = capture.provisioner["P1"]
    if baseline_provisioner[:3] != p1_provisioner[:3]:
        fail("P1 provisioner namespace/name/Deployment UID changed")
    if baseline_provisioner[:2] != ("ok176-c3", "ok176-c3-localpv-provisioner"):
        fail("P1 provisioner identity is not the fixture Deployment")
    if baseline_provisioner[3] == p1_provisioner[3]:
        fail("P1 provisioner Pod UID must change after restart")
    first_p1_status = min((position for key, position in capture.positions.items()
                           if key[:2] == ("STATUS", "P1")), default=0)
    if not first_p1_status:
        fail("P1: require phase STATUS records before ordering checks")
    if not (capture.positions[("PROVISIONER", "P1")]
            < capture.positions[("CHECKPOINT", "P1")]
            < first_p1_status):
        fail("P1 restart checkpoint must follow the healthy provisioner and precede P1 status records")
    for phase in phases:
        uid, node = capture.observer_status[phase]
        if not uid or node != state.node:
            fail(f"{phase}: observer status must identify the original backing-store node")
        if capture.observer_spec[phase] != (uid, *OBSERVER_SPEC):
            fail(f"{phase}: OBSERVER_SPEC must bind the status UID to the exact privileged read-only mounts")
        for (record_phase, _kind, _key), value in capture.observer.items():
            if record_phase == phase and value[0] != uid:
                fail(f"{phase}: observer record source Pod UID does not match observer status")
        hostfs = capture.observer.get((phase, "HOSTFS", ""))
        if hostfs is None or hostfs[1:3] != ("/var", "xfs") or "prjquota" not in hostfs[3].split(","):
            fail(f"{phase}: require observer-only /var XFS prjquota readback")
        boot = capture.observer.get((phase, "BOOT_ID", ""))
        if boot is None or not UUID.fullmatch(boot[1]):
            fail(f"{phase}: require one source-bound UUID boot_id")
        if phase in PHASES:
            before_boot = capture.boots[("before", phase)]
            after_boot = capture.boots[("after", phase)]
            if before_boot[0] != uid or after_boot[0] != uid:
                fail(f"{phase}: BOOT observer UID must match phase observer status")
            if not UUID.fullmatch(before_boot[1]) or not UUID.fullmatch(after_boot[1]):
                fail(f"{phase}: BOOT records require UUID boot IDs")
            if boot[1] != after_boot[1]:
                fail(f"{phase}: observer BOOT_ID must equal the after BOOT record")
            first_status = min((position for key, position in capture.positions.items()
                                if key[:2] == ("STATUS", phase)), default=0)
            if not first_status:
                fail(f"{phase}: require phase STATUS records before ordering checks")
            if any(capture.positions[("GATE", phase, name)]
                   >= capture.positions[("BOOT", "before", phase)]
                   for gate_phase, name in expected_gates if gate_phase == phase):
                fail(f"{phase}: both held gate records must precede BOOT before")
            if capture.positions[("BOOT", "before", phase)] >= first_status:
                fail(f"{phase}: BOOT before must be captured while writer Pods are held")
            before_position = capture.positions[("BOOT", "before", phase)]
            after_evidence = {"STATUS", "MOUNT", "WRITER", "OBSERVER_STATUS",
                              "OBSERVER_SPEC", "OBSERVER"}
            if any(position <= before_position for kind, position in capture.phase_positions[phase]
                   if kind in after_evidence):
                fail(f"{phase}: BOOT before must precede every phase status, writer, and observer record")
            after_position = capture.positions[("BOOT", "after", phase)]
            workload_positions = [position for kind, position in capture.phase_positions[phase]
                                  if kind in {"STATUS", "MOUNT", "WRITER"}]
            if not workload_positions or after_position <= max(workload_positions):
                fail(f"{phase}: BOOT after must follow all writer status and output records")
            if after_position >= capture.positions[("OBSERVER_STATUS", phase)]:
                fail(f"{phase}: BOOT after must precede the terminal observer snapshot")
            if phase == "P1" and capture.positions[("PVC_REQUEST", "P1", "quota64")] >= first_status:
                fail("P1: quota64 PVC request must be captured before P1 status records")
        quota_labels = (*state.projects, "quota64") if phase == "P1" else tuple(state.projects)
        if phase == "baseline":
            quota_labels = tuple(state.projects)
        for label in quota_labels:
            quota = capture.observer.get((phase, "QUOTA", label))
            if quota is None:
                fail(f"{phase}/{label}: missing observer quota readback")
            project = parse_nonnegative(quota[1], f"{phase}/{label} project ID")
            hard = parse_nonnegative(quota[2], f"{phase}/{label} hard limit")
            if label in state.projects and (project, hard) != (state.projects[label], HARD_KIB[label]):
                fail(f"{phase}/{label}: project ID or hard limit changed from initial capture")
            if label == "quota64":
                if project == 0 or project in state.projects.values() or hard != HARD_KIB[label]:
                    fail("P1/quota64: require distinct nonzero project ID and hard 65536 KiB")
    expected_observer_keys = set()
    for phase in phases:
        expected_observer_keys.update({(phase, "HOSTFS", ""), (phase, "BOOT_ID", "")})
        for label in state.projects:
            expected_observer_keys.add((phase, "QUOTA", label))
        if phase == "P1":
            expected_observer_keys.add((phase, "QUOTA", "quota64"))
        for label in EXISTING:
            expected_observer_keys.add((phase, "FILE", f"{label}:payload"))
        if phase in PHASES:
            for label in (P1_LABELS if phase == "P1" else EXISTING):
                expected_observer_keys.add((phase, "FILE", f"{label}:{phase.lower()}-probe"))
    if set(capture.observer) != expected_observer_keys:
        missing = sorted(expected_observer_keys - set(capture.observer))
        extra = sorted(set(capture.observer) - expected_observer_keys)
        fail(f"observer records must be exact; missing={missing} extra={extra}")
    retained = {
        "quota128": ("present", 128 * MIB, None),
        "quota256": ("absent", 0, "-"),
        "control": ("present", 16 * MIB, "-"),
        "noquota": ("present", 160 * MIB, "-"),
    }
    retained_hash = None
    for phase in phases:
        for label, (presence, size, expected_hash) in retained.items():
            record = capture.observer[(phase, "FILE", f"{label}:payload")]
            actual_presence, actual_size, digest = record[1:]
            if actual_presence != presence or parse_nonnegative(actual_size, f"{phase}/{label} payload size") != size:
                fail(f"{phase}/{label}: retained payload existence or size changed")
            if label == "quota128":
                if not SHA256.fullmatch(digest):
                    fail(f"{phase}/quota128: retained payload needs a lowercase sha256")
                retained_hash = digest if retained_hash is None else retained_hash
                if digest != retained_hash:
                    fail(f"{phase}/quota128: retained payload sha256 changed")
            elif digest != expected_hash:
                fail(f"{phase}/{label}: unexpected retained payload hash field")
        if phase in PHASES:
            for label in (P1_LABELS if phase == "P1" else EXISTING):
                record = capture.observer[(phase, "FILE", f"{label}:{phase.lower()}-probe")]
                if record[1:] != ("absent", "0", "-"):
                    fail(f"{phase}/{label}: phase probe remains after cleanup")
    baseline_boot = capture.observer[("baseline", "BOOT_ID", "")][1]
    p1_before = capture.boots[("before", "P1")][1]
    p1_after = capture.boots[("after", "P1")][1]
    if not (baseline_boot == p1_before == p1_after):
        fail("P1 before and after boot_id must both match baseline")
    if through == "P2":
        if capture.observer_status["P1"][0] == capture.observer_status["P2"][0]:
            fail("P2 observer Pod UID must differ from P1 after worker reboot")
        p2_before = capture.boots[("before", "P2")][1]
        p2_after = capture.boots[("after", "P2")][1]
        if p2_before == p1_after or p2_before != p2_after:
            fail("P2 before boot_id must differ from P1 after and equal P2 after")


def require_statuses(capture: Capture, state: InitialState, through: str) -> None:
    phases = ("P1",) if through == "P1" else PHASES
    expected_keys = {(phase, when, label)
                     for phase in phases for when in ("before", "after")
                     for label in (P1_LABELS if phase == "P1" else EXISTING)}
    if set(capture.statuses) != expected_keys:
        fail("require exact before/after status set for P1 and requested terminal phase")
    if set(capture.mounts) != expected_keys:
        fail("require one exact source-bound MOUNT record beside every phase STATUS")
    baseline = {}
    for label in EXISTING:
        inventory = capture.inventory.get(("baseline", label))
        if inventory is None:
            fail(f"baseline/{label}: missing independent PVC/PV identity")
        if inventory.pvc != PVC_NAMES[label] or inventory.storage_class != CLASSES[label]:
            fail(f"baseline/{label}: unexpected PVC name or StorageClass")
        pv, path, storage_class = state.pv[label]
        if (inventory.pv, inventory.path, inventory.storage_class) != (pv, path, storage_class):
            fail(f"baseline/{label}: PV identity differs from initial capture")
        baseline[label] = inventory
    if set(capture.inventory) != {("baseline", label) for label in EXISTING}:
        fail("baseline PVC inventory must contain exactly the four existing claims")
    phase_uids: dict[str, set[str]] = {}
    for phase in phases:
        labels = P1_LABELS if phase == "P1" else EXISTING
        phase_uids[phase] = set()
        finished_at: dict[str, datetime.datetime] = {}
        for label in labels:
            before = capture.statuses[(phase, "before", label)]
            after = capture.statuses[(phase, "after", label)]
            if before.pod_uid != after.pod_uid or before.node != after.node or before.inventory != after.inventory:
                fail(f"{phase}/{label}: Pod or PVC/PV identity changed between status snapshots")
            if before.host_users != "false" or after.host_users != "false":
                fail(f"{phase}/{label}: spec.hostUsers must be literal false")
            if before.restarts != 0 or after.restarts != 0:
                fail(f"{phase}/{label}: writer restart count must remain zero")
            if after.exit_code != 0:
                fail(f"{phase}/{label}: writer must terminate with exit code zero")
            if before.node != state.node:
                fail(f"{phase}/{label}: writer is not on original backing-store node")
            if label in EXISTING and before.inventory != baseline[label]:
                fail(f"{phase}/{label}: existing PVC/PV identity changed")
            if label == "quota64":
                inv = before.inventory
                if inv.pvc != PVC_NAMES[label] or inv.storage_class != CLASSES[label] or not all(inv.identity):
                    fail("P1/quota64: new PVC/PV identity or StorageClass is invalid")
                if (inv.pvc_uid in {item.pvc_uid for item in baseline.values()}
                        or inv.pv in {item.pv for item in baseline.values()}
                        or inv.pv_uid in {item.pv_uid for item in baseline.values()}):
                    fail("P1/quota64: new PVC/PV identity aliases an existing claim")
            if before.pod_uid in state.pod_uids.values():
                fail(f"{phase}/{label}: writer Pod UID was reused from initial run")
            if phase == "P2" and before.pod_uid in phase_uids.get("P1", set()):
                fail(f"P2/{label}: writer Pod UID was reused from P1")
            if before.pod_uid in phase_uids[phase]:
                fail(f"{phase}: writer Pod UIDs must be distinct")
            phase_uids[phase].add(before.pod_uid)
            for when, status in (("before", before), ("after", after)):
                mount = capture.mounts[(phase, when, label)]
                expected_mount = (status.pod_uid, "data", "/data",
                                  status.inventory.pvc, "no-subpath")
                if mount != expected_mount:
                    fail(f"{phase}/{when}/{label}: MOUNT must bind the STATUS UID to the exact /data PVC mount")
                if capture.positions[("MOUNT", phase, when, label)] != capture.positions[("STATUS", phase, when, label)] + 1:
                    fail(f"{phase}/{when}/{label}: MOUNT must immediately follow its STATUS")
            if capture.positions[("STATUS", phase, "before", label)] >= capture.positions[("STATUS", phase, "after", label)]:
                fail(f"{phase}/{label}: before STATUS must precede after STATUS")
            finished_at[label] = parse_finished_at(after.finished, f"{phase}/{label}")
        quota_finished = max(finished_at[label] for label in labels if label.startswith("quota"))
        for control_label in ("control", "noquota"):
            if finished_at[control_label] < quota_finished:
                fail(f"{phase}/{control_label}: finishedAt must be at or after every quota writer at second precision")


def embedded_records(capture: Capture, phase: str, label: str, pod_uid: str) -> list[str]:
    records = []
    for kind in capture.writer_order.get((phase, label), []):
        source = capture.writers[(phase, label, kind)]
        if source[0] != pod_uid:
            fail(f"{phase}/{label}: writer record source Pod UID does not match status")
        records.append("|".join(source[1:]))
    return records


def require_quota128_bound(result: initial.Result) -> None:
    initial.require_unprivileged(result)
    errors = [value for value in (result.write_errno, result.fsync_errno, result.close_errno) if value]
    if result.fstat_errno or len(errors) != 1 or errors[0] not in (28, 122):
        fail("quota128: new phase file must hit exactly one ENOSPC(28)/EDQUOT(122)")
    if result.written > MIB or result.allocated > MIB:
        fail("quota128: new phase file exceeded 1MiB before quota rejection")


def require_nonnegative_result(result: initial.Result) -> None:
    counters = (result.written, result.allocated, result.write_errno,
                result.fsync_errno, result.fstat_errno, result.close_errno)
    if any(value < 0 for value in counters):
        fail(f"{result.label}: RESULT counters and errno values must be nonnegative")


def require_writers(capture: Capture, state: InitialState, through: str) -> None:
    phases = ("P1",) if through == "P1" else PHASES
    expected_keys = set()
    for phase in phases:
        for label in (P1_LABELS if phase == "P1" else EXISTING):
            kinds = ["FS", "TARGET", "IDENTITY", "USERNS"]
            if label == "quota128":
                kinds.append("REASSIGN_TARGET")
            if label.startswith("quota"):
                kinds.append("REASSIGN")
            kinds.extend(("RESULT", "CLEANUP", "HEADROOM"))
            expected_keys.update((phase, label, kind) for kind in kinds)
    if set(capture.writers) != expected_keys:
        missing = sorted(expected_keys - set(capture.writers))
        extra = sorted(set(capture.writers) - expected_keys)
        fail(f"writer records must be exact; missing={missing} extra={extra}")
    fs_values = set()
    for phase in phases:
        labels = P1_LABELS if phase == "P1" else EXISTING
        for label in labels:
            status = capture.statuses[(phase, "before", label)]
            lines = embedded_records(capture, phase, label, status.pod_uid)
            expected_order = ["FS", "TARGET", "IDENTITY", "USERNS"]
            if label == "quota128":
                expected_order.append("REASSIGN_TARGET")
            if label.startswith("quota"):
                expected_order.append("REASSIGN")
            expected_order.extend(("RESULT", "CLEANUP", "HEADROOM"))
            if capture.writer_order[(phase, label)] != expected_order:
                fail(f"{phase}/{label}: writer evidence order is invalid")
            fs = next(line for line in lines if line.startswith("FS|"))
            fs_value = fs.split("|", 2)[2]
            if fs_value != state.fs:
                fail(f"{phase}/{label}: writer FS record must be exactly the initial capture filesystem {state.fs!r}")
            fs_values.add(fs_value)
            target = next(line for line in lines if line.startswith("TARGET|"))
            if target.split("|")[2:] != [f"/data/nested/{phase.lower()}-probe", str(REQUESTED[label])]:
                fail(f"{phase}/{label}: TARGET path or requested byte count is wrong")
            if label == "quota128":
                reassignment_target = next(line for line in lines if line.startswith("REASSIGN_TARGET|"))
                if reassignment_target != "REASSIGN_TARGET|quota128|/data/nested/payload":
                    fail(f"{phase}/quota128: reassignment target must be the retained payload")
            initial.require_user_namespace(lines, label)
            project = (capture.observer[(phase, "QUOTA", label)][1]
                       if label.startswith("quota") else None)
            if project is not None:
                initial.require_reassignment_rejected(lines, label, int(project))
            result_lines = [line for line in lines if line.startswith(f"RESULT|{label}|")]
            if len(result_lines) != 1:
                fail(f"{phase}/{label}: require one writer RESULT")
            result = initial.parse_result(result_lines[0])
            require_nonnegative_result(result)
            if result.unlinked:
                fail(f"{phase}/{label}: RESULT open-unlinked flag is invalid for cleanup mode")
            if label == "quota128":
                require_quota128_bound(result)
            elif label == "quota256":
                initial.require_quota_bound(result, 256 * MIB)
            elif label == "quota64":
                initial.require_quota_bound(result, 64 * MIB)
            elif label == "noquota":
                initial.require_complete(result, REQUESTED[label], "additional no-quota write")
            else:
                initial.require_complete(result, REQUESTED[label], "additional control write")
            cleanup = next(line for line in lines if line.startswith("CLEANUP|"))
            if cleanup == f"CLEANUP|{label}|absent":
                if (label != "quota128" or result.written != 0 or result.allocated != 0
                        or result.write_errno not in (28, 122) or result.fsync_errno
                        or result.fstat_errno or result.close_errno):
                    fail(f"{phase}/{label}: absent cleanup requires zero-byte open-time ENOSPC/EDQUOT")
            elif cleanup != f"CLEANUP|{label}|0":
                fail(f"{phase}/{label}: explicit cleanup did not succeed")
            headroom = next(line for line in lines if line.startswith("HEADROOM|"))
            remaining = parse_nonnegative(headroom.split("|")[3],
                                          f"{phase}/{label} post-cleanup headroom")
            if label == "noquota" and remaining < GIB_KIB:
                fail(f"{phase}/noquota: post-cleanup HEADROOM must be at least 1GiB")
    if len(fs_values) != 1:
        fail("phase writers are not proven on the same XFS backing store")


def validate(initial_lines: list[str], capture_lines: list[str], through: str = "P2") -> None:
    if through not in PHASES:
        fail(f"unknown terminal phase: {through}")
    state = initial_state(initial_lines)
    capture = parse_capture(capture_lines)
    require_observer(capture, state, through)
    require_statuses(capture, state, through)
    require_writers(capture, state, through)


def initial_fixture() -> list[str]:
    plain = []
    for label, capacity in (("quota128", 128), ("quota256", 256),
                            ("control", 16), ("noquota", 160)):
        storage_class = CLASSES[label]
        for when in ("before", "after"):
            finished = "-" if when == "before" else ("2026-10-02T00:00:02Z" if label == "control" else "2026-10-02T00:00:01Z")
            plain.append(f"STATUS|{when}|{label}|initial-{label}|0|worker|pv-{label}|/var/openebs/ok176-c3/{label}|{storage_class}|{'-' if when == 'before' else '0'}|{finished}|{'Running' if when == 'before' else 'Succeeded'}|false")
            headroom = 24_000_000 if label == "noquota" else initial.QUOTA_CAPACITY_MIB[label] * 1024
            if when == "after" and label not in ("quota256", "noquota"):
                headroom -= capacity * 1024
            plain.append(f"HEADROOM|{when}|{label}|{headroom}")
        plain.extend((f"FS|{label}|device xfs", f"IDENTITY|{label}|1000|1000|0",
                      f"USERNS|{label}|0|65536|65536|0|65536|65536"))
        if label.startswith("quota"):
            project = 1 if label == "quota128" else 2
            plain.append(f"REASSIGN|{label}|-1|22|{project}|{project}")
        error = 28 if label.startswith("quota") else 0
        plain.append(f"RESULT|{label}|{capacity * MIB}|{capacity * MIB}|{error}|0|0|0|1000|0|{1 if label == 'quota256' else 0}")
    for label, project, capacity in (("quota128", 1, 128), ("quota256", 2, 256)):
        plain.extend((f"READBACK|{label}|{project}",
                      f"XFS_REPORT|{label}|#1 0 0 131072 0 0 0",
                      f"XFS_REPORT|{label}|#2 0 0 262144 0 0 0",
                      f"XFS_REPORT|{label}|#3 0 0 32768 0 0 0"))
    statuses = [initial.parse_status(line) for line in plain if line.startswith("STATUS|")]
    wrapped = []
    for line in plain:
        fields = line.split("|")
        if fields[0] in initial.WRITER_KINDS or line.startswith("HEADROOM|after|"):
            label = fields[2] if fields[0] == "HEADROOM" else fields[1]
            wrapped.append(f"WRITER|{label}|initial-{label}|{line}")
        else:
            wrapped.append(line)
    return wrapped


def persistence_fixture(through: str = "P2") -> list[str]:
    digest = "a" * 64
    lines = []
    boots = {"baseline": "11111111-1111-4111-8111-111111111111",
             "P1": "11111111-1111-4111-8111-111111111111",
             "P2": "22222222-2222-4222-8222-222222222222"}

    def observer_snapshot(phase: str) -> None:
        observer_uid = f"observer-{phase}"
        lines.extend((f"OBSERVER_STATUS|{phase}|{observer_uid}|worker",
                      f"OBSERVER_SPEC|{phase}|{observer_uid}|{'|'.join(OBSERVER_SPEC)}",
                      f"OBSERVER|{phase}|{observer_uid}|HOSTFS|/var|xfs|rw,prjquota",
                      f"OBSERVER|{phase}|{observer_uid}|BOOT_ID|{boots[phase]}"))
        for label, project in (("quota128", 1), ("quota256", 2), ("control", 3)):
            lines.append(f"OBSERVER|{phase}|{observer_uid}|QUOTA|{label}|{project}|{HARD_KIB[label]}")
        if phase == "P1":
            lines.append(f"OBSERVER|P1|{observer_uid}|QUOTA|quota64|4|65536")
        file_states = {"quota128": ("present", 128 * MIB, digest),
                       "quota256": ("absent", 0, "-"),
                       "control": ("present", 16 * MIB, "-"),
                       "noquota": ("present", 160 * MIB, "-")}
        for label, values in file_states.items():
            lines.append(f"OBSERVER|{phase}|{observer_uid}|FILE|{label}|payload|{values[0]}|{values[1]}|{values[2]}")
        if phase in PHASES:
            for label in (P1_LABELS if phase == "P1" else EXISTING):
                lines.append(f"OBSERVER|{phase}|{observer_uid}|FILE|{label}|{phase.lower()}-probe|absent|0|-")

    for label in EXISTING:
        lines.append(f"PVC|baseline|{label}|{PVC_NAMES[label]}|pvcuid-{label}|pv-{label}|pvuid-{label}|/var/openebs/ok176-c3/{label}|{CLASSES[label]}")
    lines.append("PROVISIONER|baseline|ok176-c3|ok176-c3-localpv-provisioner|deployment-uid|provisioner-old")
    observer_snapshot("baseline")
    lines.extend(("PROVISIONER|P1|ok176-c3|ok176-c3-localpv-provisioner|deployment-uid|provisioner-new",
                  "CHECKPOINT|P1|restarted|quota64|absent"))
    for phase in ("P1",) if through == "P1" else PHASES:
        labels = P1_LABELS if phase == "P1" else EXISTING
        lower = phase.lower()
        lines.extend((f"GATE|{phase}|ok176-c3-{lower}-gate|hold",
                      f"GATE|{phase}|ok176-c3-{lower}-control-gate|hold",
                      f"BOOT|before|{phase}|observer-{phase}|{boots[phase]}"))
        if phase == "P1":
            lines.append(f"PVC_REQUEST|P1|quota64|{64 * MIB}")
        for label in labels:
            pod_uid = f"pod-{phase}-{label}"
            identity = f"{PVC_NAMES[label]}|pvcuid-{label}|pv-{label}|pvuid-{label}|/var/openebs/ok176-c3/{label}|{CLASSES[label]}"
            lines.append(f"STATUS|{phase}|before|{label}|{pod_uid}|0|worker|{identity}|-|-|Running|false")
            lines.append(f"MOUNT|{phase}|before|{label}|{pod_uid}|data|/data|{PVC_NAMES[label]}|no-subpath")
        for label in labels:
            pod_uid = f"pod-{phase}-{label}"
            identity = f"{PVC_NAMES[label]}|pvcuid-{label}|pv-{label}|pvuid-{label}|/var/openebs/ok176-c3/{label}|{CLASSES[label]}"
            finished = "2026-10-02T00:00:02Z" if label == "noquota" else "2026-10-02T00:00:01Z"
            lines.append(f"STATUS|{phase}|after|{label}|{pod_uid}|0|worker|{identity}|0|{finished}|Succeeded|false")
            lines.append(f"MOUNT|{phase}|after|{label}|{pod_uid}|data|/data|{PVC_NAMES[label]}|no-subpath")
            records = [f"FS|{label}|device xfs",
                       f"TARGET|{label}|/data/nested/{phase.lower()}-probe|{REQUESTED[label]}",
                       f"IDENTITY|{label}|1000|1000|0",
                       f"USERNS|{label}|0|65536|65536|0|65536|65536"]
            if label == "quota128":
                records.append("REASSIGN_TARGET|quota128|/data/nested/payload")
            if label.startswith("quota"):
                project = {"quota128": 1, "quota256": 2, "quota64": 4}[label]
                records.append(f"REASSIGN|{label}|-1|22|{project}|{project}")
            if label == "quota128":
                result = f"RESULT|{label}|0|0|28|0|0|0|1000|0|0"
            elif label == "quota256":
                result = f"RESULT|{label}|{256 * MIB}|{256 * MIB}|28|0|0|0|1000|0|0"
            elif label == "quota64":
                result = f"RESULT|{label}|{64 * MIB}|{64 * MIB}|28|0|0|0|1000|0|0"
            else:
                result = f"RESULT|{label}|{REQUESTED[label]}|{REQUESTED[label]}|0|0|0|0|1000|0|0"
            headroom = GIB_KIB if label == "noquota" else 0
            records.extend((result, f"CLEANUP|{label}|0", f"HEADROOM|after|{label}|{headroom}"))
            lines.extend(f"WRITER|{phase}|{label}|{pod_uid}|{record}" for record in records)
        lines.append(f"BOOT|after|{phase}|observer-{phase}|{boots[phase]}")
        observer_snapshot(phase)
    return lines


def replace_once(lines: list[str], old: str, new: str | None) -> list[str]:
    result = list(lines)
    matches = [index for index, line in enumerate(result) if line == old]
    if len(matches) != 1:
        fail(f"self-test fixture replacement expected one match for {old!r}")
    if new is None:
        result.pop(matches[0])
    else:
        result[matches[0]] = new
    return result


def self_test() -> None:
    original = initial_fixture()
    good = persistence_fixture()
    cases: list[tuple[str, list[str], list[str], str, str]] = []
    def mutate(name: str, old: str, new: str | None, reason: str) -> None:
        cases.append((name, original, replace_once(good, old, new), reason, "P2"))
    def field_mutation(name: str, prefix: str, field: int, value: str, reason: str) -> None:
        matches = [line for line in good if line.startswith(prefix)]
        if len(matches) != 1:
            fail(f"self-test expected one record with prefix {prefix!r}")
        fields = matches[0].split("|")
        fields[field] = value
        mutate(name, matches[0], "|".join(fields), reason)
    def token_mutation(name: str, old: str, new: str, reason: str) -> None:
        changed = [line.replace(old, new) if old in line else line for line in good]
        if changed == good:
            fail(f"self-test token not found: {old!r}")
        cases.append((name, original, changed, reason, "P2"))
    for phase in PHASES:
        prefix = f"WRITER|{phase}|quota128|pod-{phase}-quota128|"
        open_sample = replace_once(good, prefix + "CLEANUP|quota128|0",
                                   prefix + "CLEANUP|quota128|absent")
        bad_open = replace_once(open_sample, prefix + "RESULT|quota128|0|0|28|0|0|0|1000|0|0",
                                prefix + "RESULT|quota128|0|0|13|0|0|0|1000|0|0")
        cases.append((f"{phase} quota128 open EACCES", original, bad_open,
                      "exactly one ENOSPC", "P2"))
        mutate(f"{phase} quota128 missing REASSIGN", prefix + "REASSIGN|quota128|-1|22|1|1",
               None, "writer records must be exact")
        mutate(f"{phase} quota128 wrong reassignment target",
               prefix + "REASSIGN_TARGET|quota128|/data/nested/payload",
               prefix + f"REASSIGN_TARGET|quota128|/data/nested/{phase.lower()}-probe",
               "reassignment target must be the retained payload")
        mutate(f"{phase} quota128 missing reassignment target",
               prefix + "REASSIGN_TARGET|quota128|/data/nested/payload", None,
               "writer records must be exact")
        oversized = replace_once(open_sample, prefix + "RESULT|quota128|0|0|28|0|0|0|1000|0|0",
                                 prefix + f"RESULT|quota128|{MIB + 1}|{MIB + 1}|28|0|0|0|1000|0|0")
        cases.append((f"{phase} quota128 open sample over 1MiB", original, oversized,
                      "exceeded 1MiB", "P2"))
        malformed = replace_once(open_sample, prefix + "RESULT|quota128|0|0|28|0|0|0|1000|0|0",
                                 prefix + "RESULT|quota128|1|1|28|0|0|0|1000|0|0")
        cases.append((f"{phase} quota128 absent cleanup with bytes", original, malformed,
                      "absent cleanup requires zero-byte", "P2"))
    mutate("missing observer", "OBSERVER_STATUS|baseline|observer-baseline|worker", None, "observer status")
    mutate("duplicate observer", "OBSERVER_STATUS|baseline|observer-baseline|worker", "OBSERVER_STATUS|baseline|observer-baseline|worker\nOBSERVER_STATUS|baseline|observer-baseline|worker", "duplicate OBSERVER_STATUS")
    mutate("fake observer source", "OBSERVER|P1|observer-P1|HOSTFS|/var|xfs|rw,prjquota", "OBSERVER|P1|fake|HOSTFS|/var|xfs|rw,prjquota", "source Pod UID")
    mutate("provisioner not restarted", "PROVISIONER|P1|ok176-c3|ok176-c3-localpv-provisioner|deployment-uid|provisioner-new", "PROVISIONER|P1|ok176-c3|ok176-c3-localpv-provisioner|deployment-uid|provisioner-old", "Pod UID must change")
    mutate("wrong host filesystem", "OBSERVER|P1|observer-P1|HOSTFS|/var|xfs|rw,prjquota", "OBSERVER|P1|observer-P1|HOSTFS|/var|ext4|rw", "/var XFS prjquota")
    token_mutation("writer filesystem type substring", "device xfs", "device notxfs", "FS record must be exactly")
    mutate("worker reboot before P1", "BOOT|before|P1|observer-P1|11111111-1111-4111-8111-111111111111",
           "BOOT|before|P1|observer-P1|33333333-3333-4333-8333-333333333333",
           "P1 before and after boot_id must both match baseline")
    mutate("project drift", "OBSERVER|P2|observer-P2|QUOTA|quota128|1|131072", "OBSERVER|P2|observer-P2|QUOTA|quota128|8|131072", "changed from initial")
    mutate("hard-limit drift", "OBSERVER|P2|observer-P2|QUOTA|control|3|32768", "OBSERVER|P2|observer-P2|QUOTA|control|3|32767", "changed from initial")
    mutate("quota64 reused project", "OBSERVER|P1|observer-P1|QUOTA|quota64|4|65536", "OBSERVER|P1|observer-P1|QUOTA|quota64|2|65536", "distinct nonzero")
    token_mutation("same reboot ID", "22222222-2222-4222-8222-222222222222",
                   "11111111-1111-4111-8111-111111111111", "must differ from P1 after")
    token_mutation("same observer after reboot", "observer-P2", "observer-P1",
                   "observer Pod UID must differ")
    mutate("retained payload changed", f"OBSERVER|P2|observer-P2|FILE|quota128|payload|present|{128 * MIB}|{'a' * 64}", f"OBSERVER|P2|observer-P2|FILE|quota128|payload|present|{128 * MIB}|{'b' * 64}", "sha256 changed")
    mutate("retained control missing", f"OBSERVER|P1|observer-P1|FILE|control|payload|present|{16 * MIB}|-", "OBSERVER|P1|observer-P1|FILE|control|payload|absent|0|-", "existence or size changed")
    mutate("phase file remains", "OBSERVER|P2|observer-P2|FILE|control|p2-probe|absent|0|-", "OBSERVER|P2|observer-P2|FILE|control|p2-probe|present|1|-", "remains after cleanup")
    mutate("PV changed", "STATUS|P2|before|quota128|pod-P2-quota128|0|worker|ok176-c3-quota-128|pvcuid-quota128|pv-quota128|pvuid-quota128|/var/openebs/ok176-c3/quota128|ok176-c3-xfs|-|-|Running|false", "STATUS|P2|before|quota128|pod-P2-quota128|0|worker|ok176-c3-quota-128|pvcuid-quota128|pv-other|pvuid-quota128|/var/openebs/ok176-c3/quota128|ok176-c3-xfs|-|-|Running|false", "identity changed")
    mutate("hostUsers true", "STATUS|P1|after|control|pod-P1-control|0|worker|ok176-c3-control|pvcuid-control|pv-control|pvuid-control|/var/openebs/ok176-c3/control|ok176-c3-xfs|0|2026-10-02T00:00:01Z|Succeeded|false", "STATUS|P1|after|control|pod-P1-control|0|worker|ok176-c3-control|pvcuid-control|pv-control|pvuid-control|/var/openebs/ok176-c3/control|ok176-c3-xfs|0|2026-10-02T00:00:01Z|Succeeded|true", "hostUsers")
    mutate("writer restarted", "STATUS|P1|after|control|pod-P1-control|0|worker|ok176-c3-control|pvcuid-control|pv-control|pvuid-control|/var/openebs/ok176-c3/control|ok176-c3-xfs|0|2026-10-02T00:00:01Z|Succeeded|false", "STATUS|P1|after|control|pod-P1-control|1|worker|ok176-c3-control|pvcuid-control|pv-control|pvuid-control|/var/openebs/ok176-c3/control|ok176-c3-xfs|0|2026-10-02T00:00:01Z|Succeeded|false", "restart count")
    mutate("writer exit failure", "STATUS|P1|after|control|pod-P1-control|0|worker|ok176-c3-control|pvcuid-control|pv-control|pvuid-control|/var/openebs/ok176-c3/control|ok176-c3-xfs|0|2026-10-02T00:00:01Z|Succeeded|false", "STATUS|P1|after|control|pod-P1-control|0|worker|ok176-c3-control|pvcuid-control|pv-control|pvuid-control|/var/openebs/ok176-c3/control|ok176-c3-xfs|1|2026-10-02T00:00:01Z|Succeeded|false", "exit code zero")
    mutate("initial Pod UID reused", "STATUS|P1|before|quota128|pod-P1-quota128|0|worker|ok176-c3-quota-128|pvcuid-quota128|pv-quota128|pvuid-quota128|/var/openebs/ok176-c3/quota128|ok176-c3-xfs|-|-|Running|false", "STATUS|P1|before|quota128|initial-quota128|0|worker|ok176-c3-quota-128|pvcuid-quota128|pv-quota128|pvuid-quota128|/var/openebs/ok176-c3/quota128|ok176-c3-xfs|-|-|Running|false", "changed between")
    mutate("fake writer source", "WRITER|P2|control|pod-P2-control|IDENTITY|control|1000|1000|0", "WRITER|P2|control|fake|IDENTITY|control|1000|1000|0", "source Pod UID")
    mutate("wrong identity", "WRITER|P2|control|pod-P2-control|IDENTITY|control|1000|1000|0", "WRITER|P2|control|pod-P2-control|IDENTITY|control|0|0|0", "identity must be")
    mutate("initial namespace", "WRITER|P2|control|pod-P2-control|USERNS|control|0|65536|65536|0|65536|65536", "WRITER|P2|control|pod-P2-control|USERNS|control|0|0|4294967295|0|65536|65536", "UID map disagrees")
    mutate("wrong target", "WRITER|P2|control|pod-P2-control|TARGET|control|/data/nested/p2-probe|8388608", "WRITER|P2|control|pod-P2-control|TARGET|control|/data/payload|8388608", "TARGET path")
    mutate("reassignment succeeded", "WRITER|P2|quota256|pod-P2-quota256|REASSIGN|quota256|-1|22|2|2", "WRITER|P2|quota256|pod-P2-quota256|REASSIGN|quota256|0|0|2|2", "must be rejected")
    mutate("quota128 over 1MiB", "WRITER|P2|quota128|pod-P2-quota128|RESULT|quota128|0|0|28|0|0|0|1000|0|0", f"WRITER|P2|quota128|pod-P2-quota128|RESULT|quota128|{2 * MIB}|{2 * MIB}|28|0|0|0|1000|0|0", "exceeded 1MiB")
    mutate("quota128 negative written", "WRITER|P2|quota128|pod-P2-quota128|RESULT|quota128|0|0|28|0|0|0|1000|0|0", "WRITER|P2|quota128|pod-P2-quota128|RESULT|quota128|-1|0|28|0|0|0|1000|0|0", "must be nonnegative")
    mutate("quota128 negative allocated", "WRITER|P2|quota128|pod-P2-quota128|RESULT|quota128|0|0|28|0|0|0|1000|0|0", "WRITER|P2|quota128|pod-P2-quota128|RESULT|quota128|0|-1|28|0|0|0|1000|0|0", "must be nonnegative")
    mutate("quota256 unbounded", f"WRITER|P2|quota256|pod-P2-quota256|RESULT|quota256|{256 * MIB}|{256 * MIB}|28|0|0|0|1000|0|0", f"WRITER|P2|quota256|pod-P2-quota256|RESULT|quota256|{300 * MIB}|{300 * MIB}|0|0|0|0|1000|0|0", "expected one ENOSPC")
    mutate("noquota incomplete", f"WRITER|P2|noquota|pod-P2-noquota|RESULT|noquota|{160 * MIB}|{160 * MIB}|0|0|0|0|1000|0|0", f"WRITER|P2|noquota|pod-P2-noquota|RESULT|noquota|{159 * MIB}|{159 * MIB}|0|0|0|0|1000|0|0", "did not complete")
    mutate("control incomplete", f"WRITER|P2|control|pod-P2-control|RESULT|control|{8 * MIB}|{8 * MIB}|0|0|0|0|1000|0|0", f"WRITER|P2|control|pod-P2-control|RESULT|control|{7 * MIB}|{7 * MIB}|0|0|0|0|1000|0|0", "did not complete")
    mutate("quota64 unbounded", f"WRITER|P1|quota64|pod-P1-quota64|RESULT|quota64|{64 * MIB}|{64 * MIB}|28|0|0|0|1000|0|0", f"WRITER|P1|quota64|pod-P1-quota64|RESULT|quota64|{80 * MIB}|{80 * MIB}|0|0|0|0|1000|0|0", "expected one ENOSPC")
    mutate("cleanup failed", "WRITER|P2|control|pod-P2-control|CLEANUP|control|0", "WRITER|P2|control|pod-P2-control|CLEANUP|control|-1", "cleanup did not succeed")
    mutate("cleanup before result", "WRITER|P2|control|pod-P2-control|RESULT|control|8388608|8388608|0|0|0|0|1000|0|0", "WRITER|P2|control|pod-P2-control|CLEANUP|control|0", "duplicate writer")
    reordered = list(good)
    result_index = reordered.index("WRITER|P2|control|pod-P2-control|RESULT|control|8388608|8388608|0|0|0|0|1000|0|0")
    cleanup_index = reordered.index("WRITER|P2|control|pod-P2-control|CLEANUP|control|0")
    reordered[result_index], reordered[cleanup_index] = reordered[cleanup_index], reordered[result_index]
    cases.append(("cleanup ordering", original, reordered, "evidence order", "P2"))
    cases.append(("unknown record", original, good + ["UNKNOWN|P2|x"], "unknown or unwrapped", "P2"))
    cases.append(("raw writer record", original, good + ["RESULT|control|1|1|0|0|0|0|1000|0|0"], "unknown or unwrapped", "P2"))
    cases.append(("duplicate writer record", original, good + ["WRITER|P2|control|pod-P2-control|CLEANUP|control|0"], "duplicate writer", "P2"))
    cases.append(("unknown observer kind", original, good + ["OBSERVER|P2|observer-P2|MADE_UP|x"], "unknown observer", "P2"))
    cases.append(("unknown writer kind", original, good + ["WRITER|P2|control|pod-P2-control|MADE_UP|control|0"], "unknown writer", "P2"))
    cases.append(("raw observer record", original, good + ["QUOTA|P2|quota128|1|131072"], "unknown or unwrapped", "P2"))
    missing_writer = replace_once(good, "WRITER|P2|control|pod-P2-control|CLEANUP|control|0", None)
    cases.append(("missing writer record", original, missing_writer, "writer records must be exact", "P2"))
    missing_observer = replace_once(good, "OBSERVER|P2|observer-P2|QUOTA|control|3|32768", None)
    cases.append(("missing observer record", original, missing_observer, "missing observer quota", "P2"))
    duplicate_observer = good + ["OBSERVER|P2|observer-P2|QUOTA|control|3|32768"]
    cases.append(("duplicate observer record", original, duplicate_observer, "duplicate observer record", "P2"))
    missing_status = replace_once(good, "STATUS|P2|after|control|pod-P2-control|0|worker|ok176-c3-control|pvcuid-control|pv-control|pvuid-control|/var/openebs/ok176-c3/control|ok176-c3-xfs|0|2026-10-02T00:00:01Z|Succeeded|false", None)
    cases.append(("missing status", original, missing_status, "exact before/after status", "P2"))
    duplicate_status = good + ["STATUS|P2|after|control|pod-P2-control|0|worker|ok176-c3-control|pvcuid-control|pv-control|pvuid-control|/var/openebs/ok176-c3/control|ok176-c3-xfs|0|2026-10-02T00:00:01Z|Succeeded|false"]
    cases.append(("duplicate status", original, duplicate_status, "duplicate phase STATUS", "P2"))
    cases.append(("quota64 forbidden in P2", original,
                  good + ["OBSERVER|P2|observer-P2|QUOTA|quota64|4|65536"],
                  "observer records must be exact", "P2"))
    token_mutation("empty writer Pod UID", "pod-P2-control", "", "empty Pod/PVC/PV identity")
    aliased_quota64 = [line.replace("|pv-quota64|", "|pv-quota128|")
                       if line.startswith("STATUS|P1|") and "|quota64|" in line else line
                       for line in good]
    cases.append(("quota64 aliases existing PV", original, aliased_quota64,
                  "aliases an existing claim", "P2"))
    wrong_quota64_class = [line.replace("|ok176-c3-xfs|", "|wrong-class|")
                           if line.startswith("STATUS|P1|") and "|quota64|" in line else line
                           for line in good]
    cases.append(("quota64 wrong StorageClass", original, wrong_quota64_class,
                  "StorageClass is invalid", "P2"))

    observer_spec = f"OBSERVER_SPEC|P1|observer-P1|{'|'.join(OBSERVER_SPEC)}"
    mutate("missing observer spec", observer_spec, None, "one OBSERVER_SPEC")
    mutate("duplicate observer spec", observer_spec, f"{observer_spec}\n{observer_spec}",
           "duplicate OBSERVER_SPEC")
    observer_spec_fields = observer_spec.split("|")
    for offset, field in enumerate(("source-root", "data-target", "var-source", "var-target",
                                    "boot-source", "boot-target", "dev-source", "dev-target",
                                    "read-only", "privilege"), 3):
        changed = list(observer_spec_fields)
        changed[offset] = "wrong"
        mutate(f"observer spec {field}", observer_spec, "|".join(changed),
               "exact privileged read-only mounts")
    changed = list(observer_spec_fields)
    changed[2] = "wrong-observer"
    mutate("observer spec UID", observer_spec, "|".join(changed),
           "bind the status UID")

    mutate("missing P1 BOOT before",
           "BOOT|before|P1|observer-P1|11111111-1111-4111-8111-111111111111",
           None, "BOOT records must be exact")
    mutate("BOOT before wrong observer UID",
           "BOOT|before|P2|observer-P2|22222222-2222-4222-8222-222222222222",
           "BOOT|before|P2|wrong-observer|22222222-2222-4222-8222-222222222222",
           "BOOT observer UID must match")
    mutate("BOOT before malformed UUID",
           "BOOT|before|P2|observer-P2|22222222-2222-4222-8222-222222222222",
           "BOOT|before|P2|observer-P2|not-a-uuid", "BOOT records require UUID")
    p1_after_changed = [line.replace("BOOT|after|P1|observer-P1|11111111-1111-4111-8111-111111111111",
                                     "BOOT|after|P1|observer-P1|33333333-3333-4333-8333-333333333333")
                        .replace("OBSERVER|P1|observer-P1|BOOT_ID|11111111-1111-4111-8111-111111111111",
                                 "OBSERVER|P1|observer-P1|BOOT_ID|33333333-3333-4333-8333-333333333333")
                        for line in good]
    cases.append(("P1 boot changed during writers", original, p1_after_changed,
                  "P1 before and after boot_id must both match baseline", "P2"))
    p2_after_changed = [line.replace("BOOT|after|P2|observer-P2|22222222-2222-4222-8222-222222222222",
                                     "BOOT|after|P2|observer-P2|33333333-3333-4333-8333-333333333333")
                        .replace("OBSERVER|P2|observer-P2|BOOT_ID|22222222-2222-4222-8222-222222222222",
                                 "OBSERVER|P2|observer-P2|BOOT_ID|33333333-3333-4333-8333-333333333333")
                        for line in good]
    cases.append(("P2 boot changed during writers", original, p2_after_changed,
                  "equal P2 after", "P2"))
    late_boot = list(good)
    boot_line = "BOOT|before|P2|observer-P2|22222222-2222-4222-8222-222222222222"
    late_boot.remove(boot_line)
    late_boot.insert(late_boot.index("BOOT|after|P2|observer-P2|22222222-2222-4222-8222-222222222222"),
                     boot_line)
    cases.append(("BOOT before appended after writers", original, late_boot,
                  "BOOT before must be captured while writer Pods are held", "P2"))

    mutate("missing phase gate", "GATE|P2|ok176-c3-p2-control-gate|hold", None,
           "phase gate records")
    mutate("released phase gate", "GATE|P2|ok176-c3-p2-control-gate|hold",
           "GATE|P2|ok176-c3-p2-control-gate|go", "start=hold")
    late_gate = list(good)
    gate_line = "GATE|P2|ok176-c3-p2-control-gate|hold"
    late_gate.remove(gate_line)
    late_gate.insert(late_gate.index(boot_line) + 1, gate_line)
    cases.append(("gate captured after boot", original, late_gate,
                  "held gate records must precede BOOT before", "P2"))

    mutate("missing restart checkpoint", "CHECKPOINT|P1|restarted|quota64|absent", None,
           "exact CHECKPOINT")
    mutate("quota64 existed at restart checkpoint", "CHECKPOINT|P1|restarted|quota64|absent",
           "CHECKPOINT|P1|restarted|quota64|present", "malformed CHECKPOINT")
    late_checkpoint = list(good)
    checkpoint = "CHECKPOINT|P1|restarted|quota64|absent"
    late_checkpoint.remove(checkpoint)
    first_p1_status_line = next(line for line in late_checkpoint if line.startswith("STATUS|P1|"))
    late_checkpoint.insert(late_checkpoint.index(first_p1_status_line) + 1, checkpoint)
    cases.append(("restart checkpoint after P1 status", original, late_checkpoint,
                  "checkpoint must follow the healthy provisioner and precede P1 status", "P2"))
    mutate("missing quota64 API request", f"PVC_REQUEST|P1|quota64|{64 * MIB}", None,
           "PVC_REQUEST|P1|quota64|67108864")
    mutate("wrong quota64 API request", f"PVC_REQUEST|P1|quota64|{64 * MIB}",
           "PVC_REQUEST|P1|quota64|64", "PVC_REQUEST|P1|quota64|67108864")
    late_request = list(good)
    request = f"PVC_REQUEST|P1|quota64|{64 * MIB}"
    late_request.remove(request)
    late_request.insert(late_request.index(first_p1_status_line) + 1, request)
    cases.append(("quota64 API request after P1 status", original, late_request,
                  "PVC request must be captured before P1 status", "P2"))

    mount_line = "MOUNT|P2|before|control|pod-P2-control|data|/data|ok176-c3-control|no-subpath"
    mutate("missing writer mount", mount_line, None, "one exact source-bound MOUNT")
    mutate("duplicate writer mount", mount_line, f"{mount_line}\n{mount_line}",
           "duplicate MOUNT")
    for field, index in (("Pod UID", 4), ("volume", 5), ("path", 6),
                         ("PVC", 7), ("subPath", 8)):
        changed = mount_line.split("|")
        changed[index] = "wrong"
        mutate(f"writer mount {field}", mount_line, "|".join(changed),
               "MOUNT must bind the STATUS UID to the exact /data PVC mount")
    displaced_mount = list(good)
    displaced_mount.remove(mount_line)
    next_mount = next(line for line in displaced_mount
                      if line.startswith("MOUNT|P2|before|noquota|"))
    displaced_mount.insert(displaced_mount.index(next_mount) + 1, mount_line)
    cases.append(("writer mount not beside status", original, displaced_mount,
                  "MOUNT must immediately follow", "P2"))

    mutate("fractional finishedAt",
           "STATUS|P2|after|quota128|pod-P2-quota128|0|worker|ok176-c3-quota-128|pvcuid-quota128|pv-quota128|pvuid-quota128|/var/openebs/ok176-c3/quota128|ok176-c3-xfs|0|2026-10-02T00:00:01Z|Succeeded|false",
           "STATUS|P2|after|quota128|pod-P2-quota128|0|worker|ok176-c3-quota-128|pvcuid-quota128|pv-quota128|pvuid-quota128|/var/openebs/ok176-c3/quota128|ok176-c3-xfs|0|2026-10-02T00:00:01.500Z|Succeeded|false",
           "second precision")
    mutate("control finished before quota writers",
           "STATUS|P2|after|control|pod-P2-control|0|worker|ok176-c3-control|pvcuid-control|pv-control|pvuid-control|/var/openebs/ok176-c3/control|ok176-c3-xfs|0|2026-10-02T00:00:01Z|Succeeded|false",
           "STATUS|P2|after|control|pod-P2-control|0|worker|ok176-c3-control|pvcuid-control|pv-control|pvuid-control|/var/openebs/ok176-c3/control|ok176-c3-xfs|0|2026-10-02T00:00:00Z|Succeeded|false",
           "finishedAt must be at or after every quota writer")
    mutate("noquota finished before quota writers",
           "STATUS|P2|after|noquota|pod-P2-noquota|0|worker|ok176-c3-noquota|pvcuid-noquota|pv-noquota|pvuid-noquota|/var/openebs/ok176-c3/noquota|ok176-c3-xfs-noquota|0|2026-10-02T00:00:02Z|Succeeded|false",
           "STATUS|P2|after|noquota|pod-P2-noquota|0|worker|ok176-c3-noquota|pvcuid-noquota|pv-noquota|pvuid-noquota|/var/openebs/ok176-c3/noquota|ok176-c3-xfs-noquota|0|2026-10-02T00:00:00Z|Succeeded|false",
           "finishedAt must be at or after every quota writer")
    mutate("noquota low headroom", f"WRITER|P2|noquota|pod-P2-noquota|HEADROOM|after|noquota|{GIB_KIB}",
           f"WRITER|P2|noquota|pod-P2-noquota|HEADROOM|after|noquota|{GIB_KIB - 1}",
           "HEADROOM must be at least 1GiB")

    # Cross-product rules: every phase writer must independently prove its
    # pod security context, namespace mapping, clean run, and fresh process.
    for phase in PHASES:
        labels = P1_LABELS if phase == "P1" else EXISTING
        for label in labels:
            stem = f"{phase}/{label}"
            field_mutation(f"{stem} hostUsers", f"STATUS|{phase}|after|{label}|", 16,
                           "true", "hostUsers")
            field_mutation(f"{stem} restart", f"STATUS|{phase}|after|{label}|", 5,
                           "1", "restart count")
            field_mutation(f"{stem} exit", f"STATUS|{phase}|after|{label}|", 13,
                           "1", "exit code zero")
            field_mutation(f"{stem} identity", f"WRITER|{phase}|{label}|pod-{phase}-{label}|IDENTITY|", 7,
                           "0", "identity must be")
            field_mutation(f"{stem} user namespace", f"WRITER|{phase}|{label}|pod-{phase}-{label}|USERNS|", 7,
                           "0", "UID map disagrees")
            field_mutation(f"{stem} target request", f"WRITER|{phase}|{label}|pod-{phase}-{label}|TARGET|", 7,
                           "1", "requested byte count")
            field_mutation(f"{stem} cleanup", f"WRITER|{phase}|{label}|pod-{phase}-{label}|CLEANUP|", 6,
                           "-1", "cleanup did not succeed")
            field_mutation(f"{stem} open-unlinked flag", f"WRITER|{phase}|{label}|pod-{phase}-{label}|RESULT|", 14,
                           "1", "open-unlinked flag")
            if label.startswith("quota"):
                field_mutation(f"{stem} reassignment accepted", f"WRITER|{phase}|{label}|pod-{phase}-{label}|REASSIGN|", 6,
                               "0", "must be rejected")

    # Each persisted initial project is checked independently; the P1 project
    # must be distinct from zero and from every one of them.
    for label, project in (("quota128", 1), ("quota256", 2), ("control", 3)):
        field_mutation(f"{label} project changed", f"OBSERVER|P2|observer-P2|QUOTA|{label}|", 5,
                       str(project + 10), "changed from initial")
    for project in (0, 1, 2, 3):
        field_mutation(f"quota64 rejects project {project}", "OBSERVER|P1|observer-P1|QUOTA|quota64|", 5,
                       str(project), "distinct nonzero")
    field_mutation("quota64 wrong hard limit", "OBSERVER|P1|observer-P1|QUOTA|quota64|", 6,
                   "65535", "distinct nonzero")

    token_mutation("P1 writer reused initial Pod UID", "pod-P1-quota128", "initial-quota128",
                   "reused from initial")
    token_mutation("P2 writer reused P1 Pod UID", "pod-P2-quota128", "pod-P1-quota128",
                   "reused from P1")
    bad_initial = replace_once(original, "READBACK|quota128|1", "READBACK|quota128|0")
    cases.append(("invalid initial capture", bad_initial, good, "initial capture rejected", "P2"))
    no_control_project = [line.replace("#3 0 0 32768", "#3 0 0 0") for line in original]
    cases.append(("initial control project not derivable", no_control_project, good,
                  "uniquely identify control project", "P2"))
    for phase in PHASES:
        cases.append((f"all {phase} status records missing", original,
                      [line for line in good if not line.startswith(f"STATUS|{phase}|")],
                      f"{phase}: require phase STATUS records before ordering checks", "P2"))
    for name, initial_lines, capture_lines, reason, through in cases:
        expanded = []
        for line in capture_lines:
            expanded.extend(line.splitlines())
        try:
            validate(initial_lines, expanded, through)
        except RuntimeError as exc:
            if reason not in str(exc):
                fail(f"self-test {name!r} failed for wrong reason: {exc}")
            print(f"PASS: negative persistence {name}: FAIL: {exc}")
        else:
            fail(f"self-test negative case was accepted: {name}")
    print(f"PASS: {len(cases)} negative persistence rules rejected for their stated reasons")
    validate(original, persistence_fixture("P1"), "P1")
    print("PASS: positive persistence capture through P1")
    validate(original, good, "P2")
    print("PASS: positive persistence capture through P2")
    for error in (28, 122):
        for through in PHASES:
            open_sample = persistence_fixture(through)
            for phase in (("P1",) if through == "P1" else PHASES):
                prefix = f"WRITER|{phase}|quota128|pod-{phase}-quota128|"
                open_sample = replace_once(open_sample, prefix + "CLEANUP|quota128|0",
                                           prefix + "CLEANUP|quota128|absent")
                open_sample = replace_once(open_sample, prefix + "RESULT|quota128|0|0|28|0|0|0|1000|0|0",
                                           prefix + f"RESULT|quota128|0|0|{error}|0|0|0|1000|0|0")
            validate(original, open_sample, through)
            print(f"PASS: positive quota128 open errno {error} through {through}")
    write_sample = list(good)
    for phase in PHASES:
        prefix = f"WRITER|{phase}|quota128|pod-{phase}-quota128|"
        write_sample = replace_once(write_sample, prefix + "RESULT|quota128|0|0|28|0|0|0|1000|0|0",
                                    prefix + f"RESULT|quota128|{MIB}|{MIB}|122|0|0|0|1000|0|0")
    validate(original, write_sample)
    print("PASS: positive quota128 write-time EDQUOT at 1MiB through P2")
    print(f"PASS: c3 persistence checker self-test ({len(cases)} negative rules before positives)")


def read_lines(path: pathlib.Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        fail(f"cannot read {path}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initial", type=pathlib.Path)
    parser.add_argument("--capture", type=pathlib.Path)
    parser.add_argument("--through", choices=PHASES, default="P2")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_test:
            if args.initial or args.capture:
                fail("--self-test does not accept --initial or --capture")
            self_test()
        else:
            if args.initial is None or args.capture is None:
                fail("--initial and --capture are required")
            validate(read_lines(args.initial), read_lines(args.capture), args.through)
            print(f"PASS: C3 quota persistence evidence through {args.through}")
        return 0
    except RuntimeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
