#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./scripts/bootstrap.sh" >&2
  exit 2
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"
APP_USER="${KRATKY_USER:-kratky}"
CONFIG_DIR="/etc/kratky"
CONFIG_PATH="${CONFIG_DIR}/config.yaml"
DATA_DIR="/var/lib/kratky"
RUN_DIR="/run/kratky"

if ! id "${APP_USER}" >/dev/null 2>&1; then
  echo "Required user does not exist: ${APP_USER}" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  curl ffmpeg git i2c-tools python3 python3-dev python3-lgpio python3-pip \
  python3-venv time v4l-utils

usermod -a -G video,dialout,i2c,gpio "${APP_USER}"
install -d -o root -g "${APP_USER}" -m 0750 "${CONFIG_DIR}"
install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 \
  "${DATA_DIR}/recordings" "${DATA_DIR}/state" \
  "${DATA_DIR}/sensors" "${RUN_DIR}"

if [[ ! -f "${CONFIG_PATH}" ]]; then
  install -o root -g "${APP_USER}" -m 0640 \
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
chown -R "${APP_USER}:${APP_USER}" "${REPO_DIR}/.venv"

for unit in kratky-capture.service kratky-dashboard.service kratky-sensors.service; do
  install -o root -g root -m 0644 "${REPO_DIR}/systemd/${unit}" "/etc/systemd/system/${unit}"
done

systemctl daemon-reload
systemctl enable kratky-capture.service kratky-dashboard.service kratky-sensors.service

"${REPO_DIR}/.venv/bin/python" -c \
  "from app.common.config import load_config; load_config('${CONFIG_PATH}')" \
  || { echo "Configuration is invalid; services were not started." >&2; exit 1; }

systemctl restart kratky-capture.service kratky-dashboard.service kratky-sensors.service
"${REPO_DIR}/scripts/verify-installation.sh"
