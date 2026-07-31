#!/usr/bin/env bash
set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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

KRATKY_USER="${APP_USER}" "${REPO_DIR}/scripts/verify-installation.sh" \
  || failures=$((failures + 1))

check "Tailscale service active" systemctl is-active --quiet tailscaled.service
check "Tailscale IPv4 assigned" tailscale ip -4
check "graphical display manager active" systemctl is-active --quiet display-manager.service
check_retry "X11 server running" 24 5 pgrep -x Xorg
check_retry "TigerVNC desktop running" 24 5 pgrep -x X0tigervnc

check_retry "both cameras recording" 24 5 \
  "${REPO_DIR}/.venv/bin/python" -c \
  "import json,urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/status', timeout=5)); states=data['capture']['cameras']; assert states['water']['status']=='RECORDING'; assert states['environment']['status']=='RECORDING'"

TAILSCALE_IP="$(tailscale ip -4 2>/dev/null | head -n 1)"
if [[ -n "${TAILSCALE_IP}" ]] && ss -H -ltn | grep -Fq "${TAILSCALE_IP}:5900"; then
  printf 'OK    TigerVNC bound to Tailscale only\n'
else
  printf 'FAIL  TigerVNC bound to Tailscale only\n'
  failures=$((failures + 1))
fi

if [[ "${failures}" -gt 0 ]]; then
  echo "${failures} provisioning verification check(s) failed." >&2
  exit 1
fi
echo "Fresh-Pi provisioning verified."
