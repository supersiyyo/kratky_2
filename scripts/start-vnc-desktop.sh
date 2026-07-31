#!/usr/bin/env bash
set -u

DISPLAY_TARGET="${KRATKY_VNC_DISPLAY:-:0}"
GEOMETRY_MODE="${KRATKY_VNC_GEOMETRY:-1920x1080}"
RFB_PORT="${KRATKY_VNC_PORT:-5900}"
BIND_TO_TAILSCALE="${KRATKY_VNC_BIND_TO_TAILSCALE:-1}"
RESTART_DELAY="${KRATKY_VNC_RESTART_DELAY:-5}"
PASSWORD_FILE="${HOME}/.vnc/passwd"

export DISPLAY="${DISPLAY_TARGET}"

log() {
  logger -t kratky-vnc -- "$*"
}

if [[ ! -r "${PASSWORD_FILE}" ]]; then
  log "password file is missing or unreadable; refusing to start"
  exit 1
fi

if command -v xrandr >/dev/null 2>&1; then
  xrandr --output HDMI-1 --mode "${GEOMETRY_MODE}" --rate 60 >/dev/null 2>&1 || true
fi

while true; do
  bind_arguments=()
  if [[ "${BIND_TO_TAILSCALE}" == "1" ]]; then
    bind_address="$(tailscale ip -4 2>/dev/null | head -n 1)"
    if [[ -z "${bind_address}" ]]; then
      log "waiting for a Tailscale IPv4 address"
      sleep "${RESTART_DELAY}"
      continue
    fi
    bind_arguments=(-interface "${bind_address}")
  fi

  log "starting TigerVNC desktop on display ${DISPLAY_TARGET}, port ${RFB_PORT}"
  X0tigervnc \
    -display "${DISPLAY_TARGET}" \
    -PasswordFile "${PASSWORD_FILE}" \
    -rfbport "${RFB_PORT}" \
    -localhost=0 \
    -NeverShared=0 \
    -AlwaysShared=1 \
    -AcceptSetDesktopSize=0 \
    -SecurityTypes VncAuth \
    "${bind_arguments[@]}" 2>&1 | logger -t kratky-vnc
  status="${PIPESTATUS[0]}"
  log "TigerVNC exited with status ${status}; restarting in ${RESTART_DELAY}s"
  sleep "${RESTART_DELAY}"
done
