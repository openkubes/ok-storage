#!/usr/bin/env bash
# ok-storage prerequisites: open-iscsi + Longhorn preflight
#
# Run on every RKE2 host node (ok-infra, ok-gpu) before `make install`.
# Longhorn's engine requires the iSCSI initiator to be present on the host.

set -euo pipefail

echo "==> Installing open-iscsi"
apt-get update -qq
apt-get install -y open-iscsi nfs-common
systemctl enable --now iscsid

echo "==> Verifying /var/lib/longhorn is writable and on root filesystem"
mkdir -p /var/lib/longhorn
df -h /var/lib/longhorn

echo "==> Prereqs complete. Longhorn environment check (optional, requires kubectl):"
echo "    curl -sSfL https://raw.githubusercontent.com/longhorn/longhorn/v1.7.0/scripts/environment_check.sh | bash"
