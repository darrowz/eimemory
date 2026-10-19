#!/usr/bin/env bash
set -euo pipefail

USER_SYSTEMD_DIR="${1:?user systemd directory is required}"

# The declaration is authoritative; no file-pattern or health-based fallback.
# This script is also streamed over stdin by the installer, so the entrypoint
# comes from the explicit trusted checkout, not an inferred $0 directory.
CONTROL="${EIMEMORY_RUNTIME_MANAGER_CONTROL:-${REPO_DIR:-${EIMEMORY_TRUSTED_REPOSITORY_ROOT:-/dev-project/eimemory}}/deploy/runtime_manager_control.py}"
python3 -I -B "$CONTROL" systemctl list-units
exit $?
