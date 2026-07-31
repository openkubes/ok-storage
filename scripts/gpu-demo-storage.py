#!/usr/bin/env python3
"""Guarded lifecycle for the non-HA ok-gpu demo StorageClass."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "demo" / "ok-storage-block-gpu-test.yaml"
LONGHORN_NAMESPACE = "longhorn-system"
NODE = "ok-gpu"
TAG = "openkubes-gpu-demo"
STORAGE_CLASS = "ok-storage-block-gpu-test"


class DemoStorageError(RuntimeError):
    """The guarded GPU demo storage lifecycle cannot continue."""


def run(command: list[str], expected: tuple[int, ...] = (0,)) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in expected:
        detail = result.stderr.strip() or result.stdout.strip()
        raise DemoStorageError(
            f"{' '.join(command[:6])} exited {result.returncode}: {detail}"
        )
    return result.stdout


def kubectl(args: argparse.Namespace, arguments: list[str]) -> str:
    return run(["kubectl", "--kubeconfig", args.kubeconfig, *arguments])


def kubectl_json(args: argparse.Namespace, arguments: list[str]) -> dict:
    return json.loads(kubectl(args, [*arguments, "-o", "json"]))


def verify_node(args: argparse.Namespace) -> tuple[dict, list[str]]:
    node = kubectl_json(args, ["get", "node", NODE])
    ready = next(
        (
            item
            for item in node.get("status", {}).get("conditions", [])
            if item.get("type") == "Ready"
        ),
        None,
    )
    if (
        node.get("spec", {}).get("unschedulable", False)
        or ready is None
        or ready.get("status") != "True"
    ):
        raise DemoStorageError(f"Kubernetes node {NODE} is not schedulable and Ready")

    longhorn = kubectl_json(
        args,
        ["-n", LONGHORN_NAMESPACE, "get", "nodes.longhorn.io", NODE],
    )
    if longhorn.get("spec", {}).get("allowScheduling") is not True:
        raise DemoStorageError(f"Longhorn node {NODE} does not allow scheduling")
    disks = longhorn.get("spec", {}).get("disks", {})
    if not disks or not any(
        disk.get("allowScheduling") is True for disk in disks.values()
    ):
        raise DemoStorageError(f"Longhorn node {NODE} has no schedulable disk")
    tags = longhorn.get("spec", {}).get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise DemoStorageError(f"Longhorn node {NODE} tags are invalid")
    return longhorn, tags


def expected_storage_class(item: dict) -> bool:
    return (
        item.get("provisioner") == "driver.longhorn.io"
        and item.get("reclaimPolicy") == "Delete"
        and item.get("volumeBindingMode") == "Immediate"
        and item.get("allowVolumeExpansion") is True
        and item.get("parameters")
        == {
            "fromBackup": "",
            "fsType": "ext4",
            "nodeSelector": TAG,
            "numberOfReplicas": "1",
            "staleReplicaTimeout": "30",
        }
        and item.get("metadata", {}).get("labels", {}).get(
            "openkubes.io/lifecycle"
        )
        == "demo"
        and item.get("metadata", {}).get("labels", {}).get(
            "openkubes.io/high-availability"
        )
        == "false"
    )


def self_test() -> int:
    fixture = {
        "metadata": {
            "labels": {
                "openkubes.io/lifecycle": "demo",
                "openkubes.io/high-availability": "false",
            }
        },
        "provisioner": "driver.longhorn.io",
        "reclaimPolicy": "Delete",
        "volumeBindingMode": "Immediate",
        "allowVolumeExpansion": True,
        "parameters": {
            "fromBackup": "",
            "fsType": "ext4",
            "nodeSelector": TAG,
            "numberOfReplicas": "1",
            "staleReplicaTimeout": "30",
        },
    }
    if not expected_storage_class(fixture):
        raise DemoStorageError("canonical GPU demo StorageClass fixture was rejected")
    for field, wrong_value in (
        ("reclaimPolicy", "Retain"),
        ("volumeBindingMode", "WaitForFirstConsumer"),
        ("allowVolumeExpansion", False),
    ):
        changed = json.loads(json.dumps(fixture))
        changed[field] = wrong_value
        if expected_storage_class(changed):
            raise DemoStorageError(f"unsafe {field} mutation was accepted")
    changed = json.loads(json.dumps(fixture))
    changed["parameters"]["numberOfReplicas"] = "2"
    if expected_storage_class(changed):
        raise DemoStorageError("two-replica mutation was accepted")
    changed = json.loads(json.dumps(fixture))
    changed["parameters"]["nodeSelector"] = "another-node-tag"
    if expected_storage_class(changed):
        raise DemoStorageError("foreign Longhorn node tag was accepted")
    print("PASS GPU demo storage offline contract self-test")
    return 0


def verify(args: argparse.Namespace) -> int:
    _, tags = verify_node(args)
    if TAG not in tags:
        raise DemoStorageError(f"Longhorn node {NODE} is missing tag {TAG}")
    storage = kubectl_json(args, ["get", "storageclass", STORAGE_CLASS])
    if not expected_storage_class(storage):
        raise DemoStorageError(f"StorageClass {STORAGE_CLASS} differs from contract")
    print(
        f"PASS GPU demo storage: node={NODE} tag={TAG} "
        f"storageClass={STORAGE_CLASS} replicas=1"
    )
    return 0


def apply(args: argparse.Namespace) -> int:
    if os.environ.get("GPU_DEMO_APPLY") != "yes":
        raise DemoStorageError("set GPU_DEMO_APPLY=yes after explicit runtime approval")
    _, tags = verify_node(args)
    desired = sorted(set(tags) | {TAG})
    if desired != tags:
        kubectl(
            args,
            [
                "-n",
                LONGHORN_NAMESPACE,
                "patch",
                "nodes.longhorn.io",
                NODE,
                "--type=merge",
                "-p",
                json.dumps({"spec": {"tags": desired}}, separators=(",", ":")),
            ],
        )
    kubectl(args, ["apply", "-f", str(MANIFEST)])
    return verify(args)


def ensure_unused(args: argparse.Namespace) -> None:
    pvcs = kubectl_json(args, ["get", "pvc", "-A"])
    consumers = [
        f"{item['metadata']['namespace']}/{item['metadata']['name']}"
        for item in pvcs.get("items", [])
        if item.get("spec", {}).get("storageClassName") == STORAGE_CLASS
    ]
    pvs = kubectl_json(args, ["get", "pv"])
    persistent = [
        item["metadata"]["name"]
        for item in pvs.get("items", [])
        if item.get("spec", {}).get("storageClassName") == STORAGE_CLASS
    ]
    volumes = kubectl_json(
        args, ["-n", LONGHORN_NAMESPACE, "get", "volumes.longhorn.io"]
    )
    tagged = [
        item["metadata"]["name"]
        for item in volumes.get("items", [])
        if TAG in (item.get("spec", {}).get("nodeSelector") or [])
    ]
    if consumers or persistent or tagged:
        raise DemoStorageError(
            "GPU demo storage is still in use: "
            f"pvcs={consumers}, pvs={persistent}, longhornVolumes={tagged}"
        )


def remove(args: argparse.Namespace) -> int:
    if os.environ.get("GPU_DEMO_REMOVE") != "yes":
        raise DemoStorageError("set GPU_DEMO_REMOVE=yes after explicit cleanup approval")
    _, tags = verify_node(args)
    ensure_unused(args)
    kubectl(args, ["delete", "storageclass", STORAGE_CLASS, "--ignore-not-found"])
    desired = [tag for tag in tags if tag != TAG]
    if desired != tags:
        kubectl(
            args,
            [
                "-n",
                LONGHORN_NAMESPACE,
                "patch",
                "nodes.longhorn.io",
                NODE,
                "--type=merge",
                "-p",
                json.dumps({"spec": {"tags": desired}}, separators=(",", ":")),
            ],
        )
    print(f"PASS removed unused GPU demo storage profile from {NODE}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("apply", "verify", "remove", "self-test"))
    parser.add_argument("--kubeconfig")
    args = parser.parse_args()
    if args.command == "self-test":
        return self_test()
    if not args.kubeconfig:
        parser.error("--kubeconfig is required for apply, verify, and remove")
    if args.command == "apply":
        return apply(args)
    if args.command == "verify":
        return verify(args)
    return remove(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DemoStorageError, json.JSONDecodeError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
