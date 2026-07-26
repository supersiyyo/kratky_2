#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run after git pull: sudo ./scripts/apply-update.sh" >&2
  exit 2
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"
CONFIG_PATH="${KRATKY_CONFIG:-/etc/kratky/config.yaml}"
PYTHON="${REPO_DIR}/.venv/bin/python"

if [[ ! -x "${PYTHON}" ]]; then
  echo "Missing virtual environment; run scripts/bootstrap.sh first." >&2
  exit 1
fi

"${PYTHON}" -m pip install -r "${REPO_DIR}/requirements.lock"
"${PYTHON}" -c "from app.common.config import load_config; load_config('${CONFIG_PATH}')"
"${PYTHON}" -m pytest -q "${REPO_DIR}/tests/unit"

changed=0
for unit in kratky-capture.service kratky-dashboard.service kratky-sensors.service; do
  if ! cmp -s "${REPO_DIR}/systemd/${unit}" "/etc/systemd/system/${unit}"; then
    install -o root -g root -m 0644 "${REPO_DIR}/systemd/${unit}" "/etc/systemd/system/${unit}"
    changed=1
  fi
done
if [[ "${changed}" -eq 1 ]]; then
  systemctl daemon-reload
fi

systemctl restart kratky-capture.service kratky-dashboard.service kratky-sensors.service
"${REPO_DIR}/scripts/verify-installation.sh"
