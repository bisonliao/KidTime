#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
else
  echo "Cannot detect Linux distribution because /etc/os-release is missing." >&2
  exit 1
fi

os_like=" ${ID:-} ${ID_LIKE:-} "

if [[ "$os_like" == *" debian "* || "$os_like" == *" ubuntu "* ]]; then
  exec "$SCRIPT_DIR/install_srv_deb.sh" "$@"
fi

if [[ "$os_like" == *" rhel "* || "$os_like" == *" fedora "* || "$os_like" == *" centos "* || "$os_like" == *" anolis "* || "$os_like" == *" alinux "* ]]; then
  exec "$SCRIPT_DIR/install_srv_rpm.sh" "$@"
fi

echo "Unsupported Linux distribution: ID=${ID:-unknown}, ID_LIKE=${ID_LIKE:-unknown}" >&2
echo "Run install_srv_deb.sh for Debian/Ubuntu or install_srv_rpm.sh for Alibaba/RHEL-like systems." >&2
exit 1
