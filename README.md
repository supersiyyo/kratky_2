# Kratky Monitor

Kratky Monitor turns a Raspberry Pi into a self-contained observatory for a
long-running hydroponics experiment. It continuously records two cameras,
collects water and environment readings once per second, and presents live and
historical data through a browser dashboard.

The supported setup is intentionally small: image a Pi, describe the hardware
in one private JSON file, and run one provisioning command. The provisioner
installs the application, FFmpeg, Tailscale, TigerVNC, and the required systemd
services, then reboots and verifies the complete system.

This repository contains application code and safe example configuration. It
does not contain recordings, sensor history, credentials, or machine-specific
configuration.

## What you get

- persistent 1920x1080 capture from water and environment cameras;
- one archived frame per second in hourly H.265/MKV files;
- live browser previews that remain available while recording is paused;
- one-second environment and water sensor history;
- completed daily videos with synchronized timestamps and sensor readings;
- 1080p individual and combined daily timelapses with sensor overlays;
- optional checksum-verified Google Drive offload;
- protected pause, resume, and restart controls;
- automatic service startup and recovery through systemd;
- private browser and VNC access over Tailscale; and
- storage-reserve and verified-cleanup safeguards.

The browser dashboard runs on port `8080`. TigerVNC shares the Pi's physical
desktop on port `5900`, bound to its Tailscale address by default.

## Supported reference system

The automated provisioner is built for a Raspberry Pi running Raspberry Pi OS
Desktop and has been exercised on a Raspberry Pi 5. The reference hardware is:

- Raspberry Pi 5 with a high-endurance microSD card or other suitable storage;
- two distinct USB video-capture adapters or USB cameras;
- TSL2561 light sensor over I2C;
- SCD41 CO2, temperature, and humidity sensor over I2C; and
- a Modbus water probe on `/dev/ttyUSB0`, slave `1`, at 9600 baud.

Each camera must have its own stable
`/dev/v4l/by-id/...-video-index0` path. An adapter can expose both `index0` and
`index1`; those are commonly two interfaces on the same physical device, not
two cameras.

The sensor collector is isolated from video capture. A disconnected or failing
sensor is reported as unavailable and does not stop either camera.

## Reproduce the complete system

This is the recommended path for a new installation.

### 1. Image the Pi

Use Raspberry Pi Imager to install Raspberry Pi OS Desktop. In the Imager's OS
customization settings:

1. choose a username and password;
2. configure Wi-Fi or plan to use Ethernet;
3. enable SSH with password authentication; and
4. set the intended hostname, such as `arcs`.

Boot the Pi, wait for it to join the local network, and confirm that you can SSH
to it. The provisioning computer and Pi only need to share the local network
during initial setup.

### 2. Connect and identify the hardware

Connect both cameras and the sensors. From an SSH session, list the stable
camera paths:

```bash
ls -l /dev/v4l/by-id/
```

Record the two distinct paths ending in `video-index0`. These go into the
provisioning file in the next step.

### 3. Create the private provisioning file

Clone this repository on the computer that will configure the Pi, then copy the
committed template:

```bash
git clone https://github.com/supersiyyo/kratky_2.git
cd kratky_2
cp config/provisioning-secrets.example.json provisioning-secrets.json
```

On Windows PowerShell, the copy command is:

```powershell
Copy-Item config/provisioning-secrets.example.json provisioning-secrets.json
```

Edit `provisioning-secrets.json` and provide:

- the Pi's local address, username, and login password;
- the desired hostname and timezone;
- a one-off, pre-authorized, non-ephemeral Tailscale auth key;
- a dedicated VNC password containing exactly eight characters; and
- the two stable camera paths found above.

The file is ignored by Git. Keep it only on the provisioning computer and never
copy it to the Pi's FAT `bootfs` partition or commit it.

The template enables both cameras. To intentionally build a one-camera
development system, disable the absent camera and set its `required` value to
`false`.

### 4. Run the provisioner

Python 3.11 or newer is recommended on the provisioning computer.

On Linux or macOS:

```bash
python3 -m venv .provision-venv
. .provision-venv/bin/activate
python -m pip install -r requirements-provision.txt
python scripts/provision.py provisioning-secrets.json
```

On Windows PowerShell:

```powershell
python -m venv .provision-venv
.\.provision-venv\Scripts\Activate.ps1
python -m pip install -r requirements-provision.txt
python scripts/provision.py provisioning-secrets.json
```

The command will:

1. connect to the fresh Pi over SSH;
2. install Git and clone or fast-forward this repository;
3. enable I2C and configure X11 desktop autologin;
4. install and enroll Tailscale;
5. install TigerVNC and bind it to the Tailscale IPv4 address;
6. install Kratky Monitor and its systemd services;
7. create `/etc/kratky/config.yaml` from the supplied hardware profile;
8. reboot the Pi; and
9. verify Tailscale, VNC, both cameras, the sensor service, and the dashboard.

Provisioning is idempotent. Re-running it preserves the Pi's existing Tailscale
identity, runtime configuration, and data directories.

### 5. Open and verify the system

After the final verification succeeds, find the Pi in Tailscale and open:

```text
http://<tailscale-ip>:8080/
```

If Tailscale MagicDNS is enabled, the hostname may also work:

```text
http://arcs:8080/
```

Connect a VNC viewer to `<tailscale-ip>:5900` when you need the Pi's physical
desktop. On the dashboard, verify that both cameras report `RECORDING`, both
previews update, sensor values appear, and storage health is acceptable.

## How the system works

Each enabled camera is held open by one persistent FFmpeg capture process:

```text
V4L2 MJPEG camera
  -> select 1 frame per second
  -> scale to 1920x1080
  -> split
      -> detachable H.265/MKV recorder
      -> atomic JPEG dashboard preview
```

The recorder is detached and replaced at each hourly boundary. The camera
capture process stays open, avoiding a multi-second hardware reconnect between
files. Pausing recording also detaches only the recorder, so the live preview
continues to update.

The independent sensor service writes one timestamped row per second. Each
finalized recording receives a `.timing.json` sidecar containing its actual
first- and last-frame timestamps. During review, the dashboard combines video
playback time with that timestamp so the displayed sensor reading follows the
moment visible in the recording.

The dashboard communicates with the capture manager through a narrow Unix
socket. It can request status, pause, resume, or restart, but it has no sudo or
general systemd access. Its control lock prevents accidental changes; it is not
a user-authentication boundary. Keep the dashboard on a trusted private network
such as Tailscale.

## Data and configuration

Runtime data lives outside the Git checkout:

```text
/etc/kratky/config.yaml                 active device configuration
/var/lib/kratky/recordings/YYYY-MM-DD/ hourly camera recordings
/var/lib/kratky/sensors/                daily sensor history
/var/lib/kratky/timelapses/YYYY-MM-DD/ daily timelapse outputs
/var/lib/kratky/state/                  persistent application state
/run/kratky/                            previews, status, and control socket
```

Start custom configurations from
[`config/kratky.example.yaml`](config/kratky.example.yaml). Important settings
include the timezone, stable device paths, retention period, minimum free space,
and optional offload policy. Production validation requires all required
cameras to be enabled and a free-space reserve of at least 10 GiB; 10-12 GiB is
recommended.

## Daily timelapses

The dashboard's Recordings view is a viewer-facing library of completed
combined timelapses. Users can preview or download the side-by-side daily MP4;
raw hourly recordings, synchronized raw review, sensor CSV files, and full-day
archives are intentionally not exposed through the browser. They remain
internal inputs for rendering and verified Drive offload.

The timelapse renderer uses recording timing metadata to sample a fixed
midnight-to-midnight timeline. Each day produces two clean, full-resolution
1920x1080 camera videos and one labeled 1920x1080 side-by-side preview. All
three are 30-second, 30 fps H.264 MP4 files with no audio. The combined preview
also displays the nearest timestamped environment and water readings. Missing
footage and unavailable readings remain explicit rather than being concealed.

Render one or more finalized dates with:

```bash
cd /home/kratky/kratky-monitor
.venv/bin/python -m app.timelapse.render 2026-07-31 2026-08-01
```

Outputs are written under `/var/lib/kratky/timelapses/YYYY-MM-DD/` with a
`daily-summary.json` containing coverage, dimensions, duration, frame count,
file size, MD5, and sensor-overlay statistics. Existing outputs are preserved
unless `--force` is supplied. Use `--combined-only` to rebuild the presentation
layout from existing individual timelapses.

## Google Drive offload

The optional offload service transfers finalized raw camera recordings, timing
sidecars, daily sensor history, and a manifest to a user-selected Google Drive
account. Uploads are resumable, and each file must match the remote byte size
and Google-reported MD5 checksum before the ledger marks it verified.

One-time Google setup:

1. Create or select a Google Cloud project and enable the Google Drive API.
2. Configure the OAuth consent screen. For an external app in testing, add each
   connecting Google account as a test user.
3. Create an OAuth client for **TVs and Limited Input devices** and download its
   JSON credential file.
4. Open **Storage & Offload** in the dashboard and upload the JSON once.
5. Connect the desired Google account and create the project folder through the
   dashboard.
6. Set `offload.enabled: true` only after automatic transfer and cleanup are
   approved for that deployment.

The application requests only the `drive.file` scope and creates its own
project folder with `raw`, `timelapse-daily`, and `final` subfolders. It does
not receive general access to the rest of the user's Drive.

Automatic processing is finalized-day and oldest-first. For one day at a time,
the service renders the water, environment, and sensor-overlaid combined
timelapses; validates their H.264 format, 1920x1080 dimensions, frame count,
duration, checksums, and complete sensor matching; and then registers the raw
recordings, timing sidecars, sensor history, all three timelapses, and daily
summary in the ledger. Raw data is uploaded under `raw/YYYY-MM-DD`; retained
outputs are uploaded under `timelapse-daily/YYYY-MM-DD`.

Uploads are resumable. Every source and the generated manifest must match the
remote byte size and Google-reported MD5 before a durable daily verification
receipt is written. Immediately before cleanup, the service validates the local
timelapses again, checks every local raw recording against the ledger, and
re-queries every uploaded Drive file. Any active, missing, changed, failed, or
mismatched file stops cleanup and preserves the remaining recordings.

If an interrupted service shutdown leaves a finalized prior-day MKV without its
timing sidecar, the offload scanner can reconstruct that metadata from the
video's verified packet count and the filename timestamp. This recovery assumes
the enforced 1 fps archive format, marks the sidecar as recovered, and never
touches the current date or any active recording. Camera shutdowns run in
parallel and systemd allows up to 120 seconds for both encoders to finalize,
reducing the chance that recovery is needed after future updates.

Only verified `.mkv` source recordings are deleted. Each deletion is recorded
individually so an interrupted cleanup can resume safely. Timing files, sensor
history, water/environment/combined timelapses, daily summaries, manifests,
receipts, and the ledger stay on the Pi. The current calendar day is never
eligible. Keep `offload.enabled: false` until OAuth, the Drive destination, and
the cleanup policy have been reviewed for a deployment; enabling it starts this
pipeline for finalized days automatically.

## Retention safety

Without Google Drive offload, finalized footage older than the configured
retention period is eligible for deletion. With offload enabled, age-based
deletion is disabled and only remotely verified dates are cleaned up. Footage
is never silently deleted just to recover the free-space reserve. If usable
space reaches that reserve, recorders finalize their current files and pause
safely; they require an explicit resume after space is restored.

## Manual installation

Use this path when the operating system, Tailscale, and remote desktop are
already configured, or when you want to manage them yourself.

```bash
git clone https://github.com/supersiyyo/kratky_2.git /home/kratky/kratky-monitor
cd /home/kratky/kratky-monitor
sudo ./scripts/bootstrap.sh
```

`bootstrap.sh` installs the OS and Python dependencies, creates the virtual
environment and data directories, installs and enables the systemd units, and
creates `/etc/kratky/config.yaml` from the example if it does not exist. Review
that configuration carefully before allowing real capture.

The script derives the service user from the checkout owner. For a different
user or group, set `KRATKY_USER` and `KRATKY_GROUP` when invoking it.

## Updating an installed Pi

Normal application updates are:

```bash
cd /home/kratky/kratky-monitor
git pull --ff-only
sudo ./scripts/apply-update.sh
```

The updater installs locked dependencies, validates configuration, runs the
unit tests, refreshes changed systemd units, restarts services gracefully, and
verifies health.

## Local development

The unit suite does not require a Raspberry Pi or physical sensors. The FFmpeg
integration test creates synthetic media and skips when compatible local FFmpeg
support is unavailable.

On Linux or macOS:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
pytest
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
pytest
```

For a disposable local dashboard, copy
[`config/kratky.example.yaml`](config/kratky.example.yaml) to
`config/local.yaml`. Change `storage.root`, `runtime.run_dir`,
`runtime.state_dir`, and `runtime.sensor_dir` to writable local paths, then
disable both cameras and sensors. Start the services in separate terminals:

```bash
export KRATKY_CONFIG="$PWD/config/local.yaml"
python -m app.capture.service
python -m app.sensors.service
python -m app.dashboard.server
```

In PowerShell, set the configuration path with:

```powershell
$env:KRATKY_CONFIG = (Resolve-Path config/local.yaml)
```

## Diagnostics and recovery

Run these read-only checks on the Pi from the repository directory:

```bash
./scripts/verify-installation.sh
./scripts/diagnose.sh
```

Useful service checks are:

```bash
systemctl status kratky-capture kratky-sensors kratky-dashboard kratky-offload
journalctl -u kratky-capture -u kratky-sensors -u kratky-dashboard -u kratky-offload --since=-30min
```

For host-local recovery, securely transfer a reduced provisioning file to a
root-readable path and run:

```bash
sudo python3 scripts/provision-host.py /path/to/secrets.json --delete-secrets
```

The complete provisioner deliberately selects X11/Openbox because
`x0vncserver` shares the active physical desktop. Changing the Pi to Wayland
requires a different remote-desktop arrangement.

## Production acceptance

Before relying on a new installation for an unattended experiment, exercise
hour and midnight rollover, USB disconnect and reconnect, persisted pause after
reboot, sensor failure isolation, low-disk behavior, daily archive download,
timelapse rendering, and Drive verification using representative data. Confirm
with `ffprobe` that both archives are 1920x1080 HEVC at one frame per second
with no audio and that the previews remain fresh.

When tuning storage, stop other software that owns the test camera and run:

```bash
sudo -u kratky ./scripts/benchmark-camera.sh
```

The benchmark compares the supported CRF and encoder presets using real camera
input. CRF is quality-based, so measured footage—not a theoretical bitrate—is
the reliable basis for storage planning.
