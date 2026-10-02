#!/usr/bin/env python3
"""Render, but never apply, the pinned test-only C3 fixtures."""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import urllib.error
import urllib.request

CHART_URL = "https://openebs.github.io/dynamic-localpv-provisioner/localpv-provisioner-4.6.0.tgz"
CHART_SHA256 = "3e6e0661462abbecf944399e2624b0b22249ac386e5d43473f6c1cc8a4337133"
PROVISIONER_COMMIT = "7426127d566ac249dde04cad9e22a66f80049e0f"
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
IMAGE_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


def run(command: list[str]) -> str:
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, check=False)
    if completed.returncode:
        raise RuntimeError(f"command failed: {' '.join(command)}\n{completed.stderr}")
    return completed.stdout


def require_node_name(node_name: str | None) -> str:
    if not node_name or not NAME_RE.fullmatch(node_name):
        raise ValueError("--node-name must be a DNS label")
    return node_name


def require_builder_image(image: str | None) -> str:
    if not image or not IMAGE_RE.fullmatch(image):
        raise ValueError("--writer-builder-image must be an image with a 64-hex sha256 digest")
    return image


def quota64_pvc() -> str:
    return textwrap.dedent("""
        ---
        apiVersion: v1
        kind: PersistentVolumeClaim
        metadata:
          name: ok176-c3-quota-64
          namespace: ok176-c3
          labels: {openkubes.io/test-only: "ok-176-c3"}
        spec:
          accessModes: [ReadWriteOnce]
          storageClassName: ok176-c3-xfs
          resources:
            requests:
              storage: 64Mi
        """).rstrip()


def quota64_pod() -> str:
    return textwrap.dedent("""
        ---
        apiVersion: v1
        kind: Pod
        metadata:
          name: ok176-c3-p1-quota64
          namespace: ok176-c3
          labels: {openkubes.io/test-only: "ok-176-c3", openkubes.io/phase: "P1"}
        spec:
          hostUsers: false
          restartPolicy: Never
          automountServiceAccountToken: false
          nodeSelector: {kubernetes.io/hostname: "__NODE_NAME__"}
          initContainers:
          - name: build-writer
            image: __WRITER_BUILDER_IMAGE__
            command: [gcc, -static, -O2, -Wall, -Wextra, -Werror, /source/quota_writer.c, -o, /writer/quota-writer]
            volumeMounts: [{name: source, mountPath: /source, readOnly: true}, {name: writer, mountPath: /writer}]
          - name: prepare-volume
            image: openebs/linux-utils:4.6.0
            command: [sh, -ec, "mkdir -p /data/nested && chown -R 1000:1000 /data/nested"]
            volumeMounts: [{name: data, mountPath: /data}]
          containers:
          - name: writer
            image: openebs/linux-utils:4.6.0
            securityContext: {runAsUser: 1000, runAsGroup: 1000, allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}
            command:
            - sh
            - -ec
            - |
              fs=$(stat -f -c '%i %T' /data)
              printf 'FS|quota64|%s\\n' "$fs"
              while [ "$(cat /gate/start)" != go ]; do sleep 1; done
              /writer/quota-writer quota64 /data/nested/p1-probe 83886080 18446744073709551615 cleanup
              set -- $(stat -f -c '%a %S' /data)
              printf 'HEADROOM|after|quota64|%s\\n' "$(( $1 * $2 / 1024 ))"
            volumeMounts: [{name: writer, mountPath: /writer, readOnly: true}, {name: gate, mountPath: /gate, readOnly: true}, {name: data, mountPath: /data}]
          volumes:
          - {name: source, configMap: {name: ok176-c3-p1-writer-source}}
          - {name: writer, emptyDir: {}}
          - {name: gate, configMap: {name: ok176-c3-p1-gate}}
          - {name: data, persistentVolumeClaim: {claimName: ok176-c3-quota-64}}
        """).rstrip()


def render_phase(root: pathlib.Path, phase: str, node_name: str,
                 builder_image: str | None) -> str:
    if phase == "observer":
        rendered = (root / "phase-observer.yaml.in").read_text()
        rendered = rendered.replace("__NODE_NAME__", node_name)
    else:
        image = require_builder_image(builder_image)
        rendered = (root / "phase-workload.yaml.in").read_text()
        source = textwrap.indent((root / "quota_writer.c").read_text().rstrip(), "    ")
        rendered = rendered.replace("__WRITER_SOURCE__", source)
        rendered = rendered.replace("__QUOTA64_PVC__", quota64_pvc() if phase == "p1" else "")
        rendered = rendered.replace("__QUOTA64_POD__", quota64_pod() if phase == "p1" else "")
        rendered = rendered.replace("__PHASE_UPPER__", phase.upper())
        rendered = rendered.replace("__PHASE__", phase)
        rendered = rendered.replace("__NODE_NAME__", node_name)
        rendered = rendered.replace("__WRITER_BUILDER_IMAGE__", image)
    if "__" in rendered:
        raise RuntimeError("unsubstituted phase workload placeholder")
    return rendered.rstrip() + "\n"


def write_exclusive(output: pathlib.Path, content: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        stream.write(content)


def render_full(root: pathlib.Path, node_name: str, builder_image: str) -> str:
    with tempfile.TemporaryDirectory(prefix="ok176-c3-render-") as temp:
        temp_path = pathlib.Path(temp)
        archive = temp_path / "localpv-provisioner-4.6.0.tgz"
        with urllib.request.urlopen(CHART_URL) as response:
            archive.write_bytes(response.read())
        if hashlib.sha256(archive.read_bytes()).hexdigest() != CHART_SHA256:
            raise RuntimeError("pinned OpenEBS chart SHA256 mismatch")
        with tarfile.open(archive) as tar:
            tar.extractall(temp_path)
        node_values = temp_path / "node-values.yaml"
        node_values.write_text(textwrap.dedent(f"""\
            hostpathClass:
              allowedTopologies:
                - matchLabelExpressions:
                    - key: kubernetes.io/hostname
                      values: ["{node_name}"]
            """))
        chart = temp_path / "localpv-provisioner"
        chart_render = run(["helm", "template", "ok176-c3", str(chart),
                            "--namespace", "ok176-c3", "-f", str(root / "chart-values.yaml"),
                            "-f", str(node_values)])
    workload = (root / "workload.yaml.in").read_text()
    source = textwrap.indent((root / "quota_writer.c").read_text().rstrip(), "    ")
    workload = workload.replace("__WRITER_SOURCE__", source)
    workload = workload.replace("__NODE_NAME__", node_name)
    workload = workload.replace("__WRITER_BUILDER_IMAGE__", builder_image)
    if "__" in workload:
        raise RuntimeError("unsubstituted workload placeholder")
    observer = (root / "observer.yaml.in").read_text().replace("__NODE_NAME__", node_name)
    namespace, workload_rest = workload.split("\n---\n", 1)
    return (f"# TEST ONLY: OK-176 C3; provisioner source commit {PROVISIONER_COMMIT}\n"
            f"# Chart: {CHART_URL}\n# SHA256: {CHART_SHA256}\n" + namespace + "\n---\n"
            + chart_render + "\n---\n" + workload_rest + "\n---\n" + observer)


def require_observer_spec(document: dict) -> None:
    spec = document["spec"]
    expected_paths = {
        "base": {"path": "/var/openebs/ok176-c3", "type": "DirectoryOrCreate"},
        "host-dev": {"path": "/dev", "type": "Directory"},
        "host-var": {"path": "/var", "type": "Directory"},
        "host-boot-id": {"path": "/proc/sys/kernel/random/boot_id", "type": "File"},
    }
    volumes = {volume["name"]: volume for volume in spec["volumes"]}
    if len(volumes) != len(spec["volumes"]) or set(volumes) != set(expected_paths):
        raise RuntimeError("observer must declare exactly the four hostPath volumes")
    for name, source in expected_paths.items():
        if volumes[name] != {"name": name, "hostPath": source}:
            raise RuntimeError(f"observer {name}: unexpected hostPath source or type")
    observer = spec["containers"][0]
    expected_mounts = [
        {"name": "base", "mountPath": "/data", "readOnly": True},
        {"name": "host-dev", "mountPath": "/dev", "readOnly": True},
        {"name": "host-var", "mountPath": "/host-var", "readOnly": True},
        {"name": "host-boot-id", "mountPath": "/host-boot-id", "readOnly": True},
    ]
    if observer["volumeMounts"] != expected_mounts:
        raise RuntimeError("observer mounts must bind each exact host source read-only")
    if observer["image"] != "openebs/linux-utils:4.6.0" or observer["securityContext"] != {
            "privileged": True, "allowPrivilegeEscalation": True}:
        raise RuntimeError("observer must use the pinned helper image with privilege")


def self_test(root: pathlib.Path) -> None:
    import copy
    import yaml

    image = "gcc@sha256:" + "a" * 64
    for invalid in (None, "Worker", "worker.example"):
        try:
            require_node_name(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid node name {invalid!r}")
    for invalid in (None, "gcc:latest", "gcc@sha256:abc", "gcc@sha256:" + "g" * 64):
        try:
            require_builder_image(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid builder image {invalid!r}")
    with tempfile.TemporaryDirectory(prefix="ok176-c3-render-test-") as temp:
        temp_path = pathlib.Path(temp)
        existing = temp_path / "existing.yaml"
        existing.write_text("sentinel\n")
        dangling = temp_path / "dangling.yaml"
        dangling.symlink_to(temp_path / "missing.yaml")
        for refused in (existing, dangling):
            try:
                write_exclusive(refused, "replacement\n")
            except FileExistsError:
                pass
            else:
                raise AssertionError(f"overwrote existing output {refused}")
        assert existing.read_text() == "sentinel\n"
        assert dangling.is_symlink() and not dangling.exists()
    print("PASS: invalid names/digests and existing regular/symlink outputs are rejected")

    rendered = {
        "p1": render_phase(root, "p1", "worker-1", image),
        "p2": render_phase(root, "p2", "worker-1", image),
        "observer": render_phase(root, "observer", "worker-1", None),
    }
    documents = {phase: [doc for doc in yaml.safe_load_all(text) if doc]
                 for phase, text in rendered.items()}
    observer_document = documents["observer"][0]
    for index, volume in enumerate(observer_document["spec"]["volumes"]):
        bad = copy.deepcopy(observer_document)
        bad["spec"]["volumes"][index]["hostPath"]["path"] = "/tmp/fake-host-source"
        try:
            require_observer_spec(bad)
        except RuntimeError as exc:
            assert "unexpected hostPath source or type" in str(exc)
            print(f"PASS: negative observer {volume['name']} source: FAIL: {exc}")
        else:
            raise AssertionError("accepted fabricated observer host source")
    bad = copy.deepcopy(observer_document)
    bad["spec"]["containers"][0]["volumeMounts"][0]["readOnly"] = False
    try:
        require_observer_spec(bad)
    except RuntimeError as exc:
        assert "exact host source read-only" in str(exc)
        print(f"PASS: negative writable observer mount: FAIL: {exc}")
    else:
        raise AssertionError("accepted writable observer mount")
    expected_requests = {
        "quota128": 2097152,
        "quota256": 314572800,
        "noquota": 167772160,
        "control": 8388608,
        "quota64": 83886080,
    }
    for phase, expected_labels in (("p1", set(expected_requests)),
                                   ("p2", set(expected_requests) - {"quota64"})):
        docs = documents[phase]
        configmaps = [doc for doc in docs if doc["kind"] == "ConfigMap"]
        assert {doc["metadata"]["name"] for doc in configmaps} == {
            f"ok176-c3-{phase}-writer-source",
            f"ok176-c3-{phase}-gate",
            f"ok176-c3-{phase}-control-gate",
        }
        pods = [doc for doc in docs if doc["kind"] == "Pod"]
        assert {pod["metadata"]["name"].removeprefix(f"ok176-c3-{phase}-")
                for pod in pods} == expected_labels
        for pod in pods:
            label = pod["metadata"]["name"].removeprefix(f"ok176-c3-{phase}-")
            assert pod["spec"]["hostUsers"] is False
            preparation = pod["spec"]["initContainers"][1]["command"][2]
            expected_preparation = ("mkdir -p /data/nested && chown -R 1000:1000 /data/nested"
                                    if label == "quota64" else
                                    "test -d /data/nested && chown 1000:1000 /data/nested")
            assert preparation == expected_preparation, f"{phase}/{label}: prepare must leave retained payload metadata untouched"
            writer = pod["spec"]["containers"][0]
            security = writer["securityContext"]
            assert security == {"runAsUser": 1000, "runAsGroup": 1000,
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]}}
            command = writer["command"][2]
            subprocess.run(["sh", "-n", "-c", command], check=True)
            assert f"/data/nested/{phase}-probe" in command
            assert str(expected_requests[label]) in command
            assert "18446744073709551615 cleanup" in command
            gate = next(volume["configMap"]["name"] for volume in pod["spec"]["volumes"]
                        if volume["name"] == "gate")
            expected_gate = (f"ok176-c3-{phase}-control-gate"
                             if label in {"noquota", "control"}
                             else f"ok176-c3-{phase}-gate")
            assert gate == expected_gate
    p1_pvcs = [doc for doc in documents["p1"] if doc["kind"] == "PersistentVolumeClaim"]
    assert len(p1_pvcs) == 1
    assert p1_pvcs[0]["metadata"]["name"] == "ok176-c3-quota-64"
    assert p1_pvcs[0]["spec"]["storageClassName"] == "ok176-c3-xfs"
    assert p1_pvcs[0]["spec"]["resources"]["requests"]["storage"] == "64Mi"
    assert not [doc for doc in documents["p2"] if doc["kind"] == "PersistentVolumeClaim"]
    observer = documents["observer"]
    assert len(observer) == 1 and observer[0]["metadata"]["name"] == "ok176-c3-persist-observer"
    require_observer_spec(observer_document)
    mounts = {mount["name"]: mount for mount in observer[0]["spec"]["containers"][0]["volumeMounts"]}
    assert mounts["host-var"] == {"name": "host-var", "mountPath": "/host-var", "readOnly": True}
    assert mounts["host-boot-id"] == {"name": "host-boot-id", "mountPath": "/host-boot-id", "readOnly": True}
    with tempfile.TemporaryDirectory(prefix="ok176-c3-render-positive-") as temp:
        fresh = pathlib.Path(temp) / "fresh.yaml"
        write_exclusive(fresh, rendered["p1"])
        assert fresh.read_text() == rendered["p1"]
    print("PASS: parsed phase schemas, shell commands, gates, claims, security, and observer mounts")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node-name", help="sole disposable worker hostname")
    parser.add_argument("--writer-builder-image", help="immutable image reference containing gcc")
    parser.add_argument("--output")
    parser.add_argument("--phase", choices=("p1", "p2", "observer"),
                        help="render only a persistence phase; skips chart download and Helm")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parent
    if args.self_test:
        if args.node_name or args.writer_builder_image or args.output or args.phase:
            parser.error("--self-test does not accept render options")
        self_test(root)
        return 0
    if not args.output:
        parser.error("--output is required")
    output = pathlib.Path(args.output)
    if output.exists() or output.is_symlink():
        print(f"FAIL: refusing existing render output {args.output}", file=sys.stderr)
        return 1
    try:
        node_name = require_node_name(args.node_name)
        if args.phase:
            content = render_phase(root, args.phase, node_name, args.writer_builder_image)
        else:
            content = render_full(root, node_name, require_builder_image(args.writer_builder_image))
        write_exclusive(output, content)
    except FileExistsError:
        print(f"FAIL: refusing existing render output {args.output}", file=sys.stderr)
        return 1
    except ValueError as exc:
        parser.error(str(exc))
    except (OSError, RuntimeError, tarfile.TarError, urllib.error.URLError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    description = f"persistence phase {args.phase}" if args.phase else "pinned OpenEBS 4.6.0 test fixture"
    print(f"PASS: rendered {description} to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
