#!/usr/bin/env bash
set -u

echo "Kratky diagnostic report"
echo "Generated: $(date --iso-8601=seconds)"
echo "Version: $(git -C "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
echo
echo "System"
uname -a
df -h / /var/lib/kratky/recordings 2>/dev/null
vcgencmd measure_temp 2>/dev/null || true
echo
echo "Video devices"
ls -l /dev/v4l/by-id/ 2>/dev/null || true
v4l2-ctl --device=/dev/video0 --all 2>/dev/null | sed -E 's/(serial|password|token|key)[^:]*:.*/\1: [REDACTED]/Ig' || true
echo
echo "Services"
systemctl show kratky-capture.service kratky-dashboard.service kratky-sensors.service \
  -p Id -p ActiveState -p SubState -p NRestarts --no-pager 2>/dev/null
echo
echo "Recent service logs"
journalctl -u kratky-capture.service -u kratky-dashboard.service -u kratky-sensors.service \
  --since=-30min --no-pager -n 300 2>/dev/null |
  sed -E 's/(password|token|secret|stream[_ -]?key)([=: ]+)[^ ]+/\1\2[REDACTED]/Ig'
