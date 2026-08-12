#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./scripts/bootstrap.sh" >&2
  exit 2
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"
APP_USER="${KRATKY_USER:-$(stat -c '%U' "${REPO_DIR}")}"
CONFIG_DIR="/etc/kratky"
CONFIG_PATH="${CONFIG_DIR}/config.yaml"
DATA_DIR="/var/lib/kratky"
RUN_DIR="/run/kratky"

if ! id "${APP_USER}" >/dev/null 2>&1; then
  echo "Required user does not exist: ${APP_USER}" >&2
  exit 1
fi
APP_GROUP="${KRATKY_GROUP:-$(id -gn "${APP_USER}")}"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  curl ffmpeg git i2c-tools python3 python3-dev python3-lgpio python3-pip \
  python3-venv time v4l-utils

usermod -a -G video,dialout,i2c,gpio "${APP_USER}"
install -d -o root -g "${APP_GROUP}" -m 0750 "${CONFIG_DIR}"
install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 \
  "${DATA_DIR}/recordings" "${DATA_DIR}/state" "${DATA_DIR}/timelapses" \
  "${DATA_DIR}/sensors" "${RUN_DIR}"

if [[ ! -f "${CONFIG_PATH}" ]]; then
  install -o root -g "${APP_GROUP}" -m 0640 \
    "${REPO_DIR}/config/kratky.example.yaml" "${CONFIG_PATH}"
  echo "Created ${CONFIG_PATH}; review it before recording."
else
  echo "Preserved existing ${CONFIG_PATH}."
fi

if [[ ! -x "${REPO_DIR}/.venv/bin/python" ]]; then
  python3 -m venv --system-site-packages "${REPO_DIR}/.venv"
fi
"${REPO_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${REPO_DIR}/.venv/bin/python" -m pip install -r "${REPO_DIR}/requirements.lock"
chown -R "${APP_USER}:${APP_GROUP}" "${REPO_DIR}/.venv"

UNIT_RENDER_DIR="$(mktemp -d)"
cleanup() {
  rm -rf -- "${UNIT_RENDER_DIR}"
}
trap cleanup EXIT
for unit in kratky-capture.service kratky-dashboard.service kratky-sensors.service kratky-offload.service; do
  python3 "${REPO_DIR}/scripts/render-systemd-unit.py" \
    "${REPO_DIR}/systemd/${unit}" "${UNIT_RENDER_DIR}/${unit}" \
    --user "${APP_USER}" --group "${APP_GROUP}" --repo-dir "${REPO_DIR}"
  install -o root -g root -m 0644 \
    "${UNIT_RENDER_DIR}/${unit}" "/etc/systemd/system/${unit}"
done

systemctl daemon-reload
systemctl enable kratky-capture.service kratky-dashboard.service kratky-sensors.service kratky-offload.service

"${REPO_DIR}/.venv/bin/python" -c \
  "from app.common.config import load_config; load_config('${CONFIG_PATH}')" \
  || { echo "Configuration is invalid; services were not started." >&2; exit 1; }

systemctl restart kratky-capture.service kratky-dashboard.service kratky-sensors.service kratky-offload.service
KRATKY_USER="${APP_USER}" "${REPO_DIR}/scripts/verify-installation.sh"
