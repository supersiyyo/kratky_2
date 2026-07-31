#!/usr/bin/env bash
set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"
CONFIG_PATH="${KRATKY_CONFIG:-/etc/kratky/config.yaml}"
PYTHON="${REPO_DIR}/.venv/bin/python"
APP_USER="${KRATKY_USER:-$(stat -c '%U' "${REPO_DIR}")}"
failures=0

check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf 'OK    %s\n' "${label}"
  else
    printf 'FAIL  %s\n' "${label}"
    failures=$((failures + 1))
  fi
}

check_retry() {
  local label="$1"
  local attempts="$2"
  local delay_seconds="$3"
  shift 3
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if "$@" >/dev/null 2>&1; then
      printf 'OK    %s\n' "${label}"
      return
    fi
    if [[ "${attempt}" -lt "${attempts}" ]]; then
      sleep "${delay_seconds}"
    fi
  done
  printf 'FAIL  %s\n' "${label}"
  failures=$((failures + 1))
}

check "FFmpeg installed" command -v ffmpeg
check "V4L2 tools installed" command -v v4l2-ctl
check "virtual environment" test -x "${PYTHON}"
check "configuration readable" test -r "${CONFIG_PATH}"
check "configuration valid" "${PYTHON}" -c \
  "from app.common.config import load_config; load_config('${CONFIG_PATH}')"
check "recordings writable" runuser -u "${APP_USER}" -- test -w /var/lib/kratky/recordings
check "runtime writable" runuser -u "${APP_USER}" -- test -w /run/kratky
check "capture service active" systemctl is-active --quiet kratky-capture.service
check "dashboard service active" systemctl is-active --quiet kratky-dashboard.service
check "sensor service active" systemctl is-active --quiet kratky-sensors.service
check_retry "dashboard responds" 15 1 \
  curl --fail --silent --max-time 5 http://127.0.0.1:8080/api/status

"${PYTHON}" - "${CONFIG_PATH}" <<'PY' || failures=$((failures + 1))
import sys
from pathlib import Path
from app.common.config import load_config

config = load_config(sys.argv[1])
missing = []
for name, camera in config.cameras.items():
    if camera.enabled and not Path(camera.device or "").exists():
        missing.append(name)
if missing:
    print("FAIL  enabled camera devices missing: " + ", ".join(missing))
    raise SystemExit(1)
print("OK    enabled camera devices present")
PY

if [[ "${failures}" -gt 0 ]]; then
  echo "${failures} verification check(s) failed." >&2
  exit 1
fi
echo "Installation verified."
