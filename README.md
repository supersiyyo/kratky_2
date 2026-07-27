# Kratky Monitor

Kratky Monitor is a direct, reproducible Raspberry Pi recording system for a
long-running hydroponics experiment. It replaces OBS, RTMP, and NGINX with a
persistent FFmpeg capture stage plus a detachable recorder per camera, a narrow
local control socket, an independent sensor collector, and a browser dashboard.

The repository does not contain camera footage, sensor history, credentials, or
machine-specific configuration.

## Current profile and hardware findings

The development profile supports the installed water camera and reports the
future environment camera as `PLANNED`. A read-only audit on 2026-07-25 found:

- Raspberry Pi 5 on Debian 13, with FFmpeg 7.1.5.
- Guermok adapter `index0` at `/dev/video0`; `index1` is another interface on the
  same adapter, not a second physical camera.
- 1920×1080 MJPEG is available at 10, 20, 25, 30, and 50 fps. There is no native
  1 fps mode, so the FFmpeg graph selects one frame per second before splitting
  to archive and preview.
- Environment sensors are TSL2561 light and SCD41 CO₂/temperature/humidity over
  I²C. The water probe is Modbus slave 1 on `/dev/ttyUSB0`, 9600 baud, using
  registers 6, 18, 19, 21, and 30–32.
- The legacy sensor service was in a restart storm. This service keeps device
  failures inside its collection loop, and systemd also limits process restarts.

## Architecture

For each enabled camera, the capture manager keeps the physical V4L2 device open
in one persistent FFmpeg capture process:

```text
V4L2 MJPEG input
  → fps=1
  → 1920×1080 scale
  → split
      → raw frames → detachable H.265 / MKV recorder (no audio)
      → reduced JPEG overwritten atomically in /run/kratky
```

The manager owns pause state and writes status to `/run/kratky`. The Flask
dashboard can only send `pause`, `resume`, `restart`, and `status` JSON commands
over `/run/kratky/capture-control.sock`; it receives no sudo or systemd access.
Pausing or performing an hourly rollover finalizes only the recorder. The
physical camera remains open and its dashboard preview continues to refresh.
Only a genuine capture failure or a capture-service restart reopens the V4L2 device.
Sensor failure never stops video.

Hourly files use their actual process start time:

```text
/var/lib/kratky/recordings/2026-07-25/water/water-2026-07-25_14-37-18.mkv
```

A reconnect or manual restart creates a new timestamped file and never
overwrites an earlier segment. Active files are excluded from downloads,
retention deletion, and the completed-recordings list.

Each finalized recording also receives a small `.timing.json` file containing
its actual first- and last-frame timestamps. The sensor service writes one
timestamped row per second to daily files under `/var/lib/kratky/sensors/`.
The archive browser's `Review` page uses the video's playback position and
first-frame timestamp to show the corresponding water and environment readings.
Older recordings fall back to filename timing and the available one-minute
sensor history.

## Local development

Python 3.11 or newer is recommended.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
pytest
```

The unit tests do not require Linux devices. The FFmpeg integration test creates
synthetic media and skips if local FFmpeg/libx265 is unavailable.

For a disposable local dashboard, copy the example config and change the
`storage.root`, `runtime.*` directories, and camera settings to writable local
paths. Set both cameras `enabled: false`, then:

```bash
export KRATKY_CONFIG="$PWD/config/local.yaml"
python -m app.capture.service
python -m app.sensors.service
python -m app.dashboard.server
```

## Raspberry Pi installation

The expected checkout path is `/home/kratky/kratky-monitor`. Review
`/etc/kratky/config.yaml` before allowing capture. On a fresh Pi:

```bash
sudo ./scripts/bootstrap.sh
```

The idempotent bootstrap installs OS packages, creates a venv with
`--system-site-packages` (so Debian's Pi-specific `lgpio` is visible), installs
pinned Python dependencies, preserves an existing config, installs systemd
units, enables boot startup, and verifies health.

Normal manual updates are:

```bash
cd /home/kratky/kratky-monitor
git pull --ff-only
sudo ./scripts/apply-update.sh
```

`apply-update.sh` validates configuration, installs locked dependencies, runs
fast tests, updates changed units, restarts services gracefully, and verifies
health.

## Configuration

Start from [`config/kratky.example.yaml`](config/kratky.example.yaml). Runtime
configuration and data stay outside Git:

```text
/etc/kratky/config.yaml
/var/lib/kratky/recordings/
/var/lib/kratky/state/
/var/lib/kratky/sensors/
/run/kratky/
```

Production mode fails validation if a required camera is disabled or if the
free-space reserve is below 10 GiB. Configure both distinct stable device paths
only after the second adapter is physically audited. The production reserve
should be 10–12 GiB; the example uses the 2 GiB development reserve.

## Retention safety

Finalized footage older than 30 days is deleted. Footage younger than 30 days is
never silently deleted to regain reserve space. If usable free space reaches the
configured reserve, workers finalize their files and pause. The dashboard shows:

- recent measured daily write rate;
- measured one-camera retention in development;
- provisional two-camera retention (twice the measured development write rate);
- actual combined retention in production;
- a warning below 35 projected days.

CRF is quality-based, not a storage guarantee. Select the final preset only from
representative analog-video measurements. With OBS and the capture service
stopped so the camera is free:

```bash
sudo -u kratky ./scripts/benchmark-camera.sh
```

This tests CRF 28/ultrafast, CRF 28/veryfast, and CRF 30/veryfast and records
file size, FFprobe frame information, process metrics, and temperature.

## Operations and acceptance

Useful read-only diagnostics:

```bash
./scripts/verify-installation.sh
./scripts/diagnose.sh
```

Before production, complete the six-hour and 24-hour water-camera tests, test
hour and midnight rollover, USB removal/reconnect, FFmpeg failure, persisted
pause across reboot, controls, sensor isolation, and low-disk behavior. On the
fresh 128 GB card, repeat with both physical cameras and demonstrate at least 35
days of projected combined retention while preserving 10–12 GiB.

Do not call the deployment production-ready until `ffprobe` confirms both
archives are 1920×1080 HEVC, exactly 1 fps, have no audio, previews remain fresh,
and all recovery/retention tests pass.

## Deployment boundary

Building and testing this repository does not authorize modifying the current
Pi. Disabling OBS/NGINX, installing packages, installing units, changing
configuration, or starting these services must be approved as a separate
deployment step. The development camera cannot be shared with OBS during live
capture or benchmark tests.
