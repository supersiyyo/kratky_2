#!/usr/bin/env bash
set -euo pipefail

DEVICE="${1:-/dev/v4l/by-id/usb-UltraSemi_Guermok_USB2_Video_20210621-video-index0}"
DURATION="${KRATKY_BENCHMARK_SECONDS:-600}"
OUTPUT_DIR="${KRATKY_BENCHMARK_DIR:-/var/lib/kratky/benchmark}"
mkdir -p "${OUTPUT_DIR}"

if pgrep -f 'app.capture.service|obs' >/dev/null; then
  echo "A capture manager or OBS process is running. Stop the camera owner before benchmarking." >&2
  exit 1
fi

for spec in "28 ultrafast" "28 veryfast" "30 veryfast"; do
  read -r crf preset <<<"${spec}"
  output="${OUTPUT_DIR}/crf-${crf}-${preset}.mkv"
  metrics="${OUTPUT_DIR}/crf-${crf}-${preset}.log"
  echo "Benchmarking CRF ${crf} ${preset} for ${DURATION}s"
  /usr/bin/time -v -o "${metrics}" timeout --signal=INT "${DURATION}" \
    ffmpeg -hide_banner -f v4l2 -input_format mjpeg -framerate 10 \
      -video_size 1920x1080 -i "${DEVICE}" -vf "fps=1" -an \
      -c:v libx265 -crf "${crf}" -preset "${preset}" -pix_fmt yuv420p \
      -f matroska -y "${output}" 2>>"${metrics}" || status=$?
  if [[ "${status:-0}" -ne 0 && "${status}" -ne 124 ]]; then
    exit "${status}"
  fi
  ffprobe -v error -count_frames -select_streams v:0 \
    -show_entries stream=codec_name,width,height,avg_frame_rate,nb_read_frames \
    -of default=noprint_wrappers=1 "${output}" >>"${metrics}"
  stat --printf='size_bytes=%s\n' "${output}" >>"${metrics}"
  vcgencmd measure_temp >>"${metrics}" 2>/dev/null || true
done
echo "Results written to ${OUTPUT_DIR}. Do not select a preset until they are reviewed."
