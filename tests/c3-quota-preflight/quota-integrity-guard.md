# Quota-integrity guard (future C3 conformance gate)

The disposable preflight found that a UID 1000, capabilities-dropped writer can
invoke `xfs_io -r -c 'chproj 0'` on its owned file and move it from the
provisioner-assigned project to project 0. A capacity result is therefore **not
conforming** until this negative control is denied and the original project ID
and hard-limit accounting remain intact.

The harness now requires Kubernetes user namespaces (`spec.hostUsers: false`,
<https://kubernetes.io/docs/concepts/workloads/pods/user-namespaces/>) for every
writer. Each bounded writer attempts the reassignment (`FS_IOC_FSSETXATTR`,
project ID 0) before writing; the checker requires `EINVAL`, an unchanged
project ID equal to the observer readback, and a user-namespace UID/GID map.

In the 2026-10-02 disposable Talos 1.9.6 / Kubernetes 1.34.1 run, both
reassignments were rejected with `EINVAL` and both writers stopped with
`ENOSPC` at exactly 128 and 256 MiB. Prerequisite: Talos defaults
`user.max_user_namespaces` to 0, so the worker needed it raised before such
Pods could start.

This protects only workloads that are forced to run with `hostUsers: false`.
A conforming profile must therefore enforce that field at admission, and the
node configuration must enable user namespaces. The bound also held after a
provisioner restart and a worker reboot (2026-10-02). A seccomp filter on the
same ioctl was not tested.

The privileged node observer remains evidence-only; it does not make the
workspace writer safe. Do not adopt `ok176-c3-xfs` as a production class from
this result.
