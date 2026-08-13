from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from app.capture.recordings import Recording, recording_day, timing_path
from app.capture.state import atomic_write_json
from app.common.config import AppConfig, load_config
from app.sensors.history import load_history


OUTPUT_SECONDS = 30
OUTPUT_FPS = 30
OUTPUT_FRAMES = OUTPUT_SECONDS * OUTPUT_FPS
MAX_NEAREST_GAP_SECONDS = 15
MAX_SENSOR_AGE_SECONDS = 3
INDIVIDUAL_WIDTH = 1920
INDIVIDUAL_HEIGHT = 1080
COMBINED_WIDTH = 1920
COMBINED_HEIGHT = 1080
FONT_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
CAMERAS = ("water", "environment")


class TimelapseError(RuntimeError):
    """Raised when a timelapse cannot be rendered safely."""


@dataclass(frozen=True, slots=True)
class FrameSelection:
    output_index: int
    source_index: int


@dataclass(frozen=True, slots=True)
class CameraPlan:
    camera: str
    selections: dict[Path, tuple[FrameSelection, ...]]
    missing_indices: tuple[int, ...]
    first_recorded_at: datetime | None
    last_recorded_at: datetime | None

    @property
    def recorded_samples(self) -> int:
        return OUTPUT_FRAMES - len(self.missing_indices)


def timelapse_root(config: AppConfig) -> Path:
    return config.runtime.state_dir.parent / "timelapses"


def combined_timelapse_path(config: AppConfig, day: str) -> Path:
    return timelapse_root(config) / day / f"combined-timelapse-{day}.mp4"


def plan_camera(
    camera: str,
    recordings: Sequence[Recording],
    day_start: datetime,
    frame_counts: dict[Path, int],
) -> CameraPlan:
    ordered = sorted(recordings, key=lambda item: item.start)
    selections: dict[Path, list[FrameSelection]] = {}
    missing: list[int] = []
    sample_step = timedelta(days=1) / OUTPUT_FRAMES

    for output_index in range(OUTPUT_FRAMES):
        sampled_at = day_start + sample_step * output_index
        candidates = [
            item for item in ordered if item.start <= sampled_at <= item.end
        ]
        if not candidates:
            nearest = min(
                ordered,
                key=lambda item: min(
                    abs((sampled_at - item.start).total_seconds()),
                    abs((sampled_at - item.end).total_seconds()),
                ),
                default=None,
            )
            nearest_gap = (
                min(
                    abs((sampled_at - nearest.start).total_seconds()),
                    abs((sampled_at - nearest.end).total_seconds()),
                )
                if nearest
                else float("inf")
            )
            if nearest is None or nearest_gap > MAX_NEAREST_GAP_SECONDS:
                missing.append(output_index)
                continue
            candidates = [nearest]
        recording = max(candidates, key=lambda item: item.start)
        frame_count = frame_counts[recording.path]
        duration = (recording.end - recording.start).total_seconds()
        if frame_count < 1 or duration <= 0:
            missing.append(output_index)
            continue
        fraction = (sampled_at - recording.start).total_seconds() / duration
        source_index = round(fraction * (frame_count - 1))
        source_index = max(0, min(frame_count - 1, source_index))
        selections.setdefault(recording.path, []).append(
            FrameSelection(output_index, source_index)
        )

    return CameraPlan(
        camera=camera,
        selections={path: tuple(values) for path, values in selections.items()},
        missing_indices=tuple(missing),
        first_recorded_at=min((item.start for item in ordered), default=None),
        last_recorded_at=max((item.end for item in ordered), default=None),
    )


def render_day(
    config: AppConfig,
    day: str,
    *,
    output_root: Path | None = None,
    force: bool = False,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> dict[str, Any]:
    timezone = ZoneInfo(config.deployment.timezone)
    value = recording_day(
        config.storage.root, config.runtime.sensor_dir, timezone, day
    )
    if value is None or not value.downloadable:
        raise TimelapseError(f"{day} has no finalized recordings")
    if any(not value.cameras[name].recordings for name in CAMERAS):
        raise TimelapseError(f"{day} does not contain both camera recordings")

    destination = (output_root or timelapse_root(config)) / day
    destination.mkdir(parents=True, exist_ok=True)
    outputs = {
        "water": destination / f"water-timelapse-{day}.mp4",
        "environment": destination / f"environment-timelapse-{day}.mp4",
        "combined": destination / f"combined-timelapse-{day}.mp4",
        "summary": destination / "daily-summary.json",
    }
    existing = [path for path in outputs.values() if path.exists()]
    if existing and not force:
        names = ", ".join(path.name for path in existing)
        raise TimelapseError(f"output already exists: {names}; use --force to replace")

    day_start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone)
    sensor_samples = _sensor_timeline(config.runtime.sensor_dir, day_start)
    plans: dict[str, CameraPlan] = {}
    for camera in CAMERAS:
        recordings = value.cameras[camera].recordings
        counts = {
            item.path: _recording_frame_count(item.path, ffprobe)
            for item in recordings
        }
        plans[camera] = plan_camera(camera, recordings, day_start, counts)

    with tempfile.TemporaryDirectory(prefix=".render-", dir=destination) as raw_temp:
        temporary = Path(raw_temp)
        for camera in CAMERAS:
            part = destination / f".{outputs[camera].name}.part"
            part.unlink(missing_ok=True)
            _render_camera(plans[camera], temporary / camera, part, ffmpeg)
            part.replace(outputs[camera])

        subtitles = temporary / "timestamps.ass"
        _write_timestamp_subtitles(subtitles, day_start, sensor_samples)
        combined_part = destination / f".{outputs['combined'].name}.part"
        combined_part.unlink(missing_ok=True)
        _render_combined(
            outputs["water"],
            outputs["environment"],
            subtitles,
            day,
            combined_part,
            ffmpeg,
        )
        combined_part.replace(outputs["combined"])

    summary = {
        "schema_version": 1,
        "date": day,
        "timezone": config.deployment.timezone,
        "created_at": datetime.now(timezone).isoformat(),
        "timeline": {
            "start": day_start.isoformat(),
            "end": (day_start + timedelta(days=1)).isoformat(),
            "output_seconds": OUTPUT_SECONDS,
            "output_fps": OUTPUT_FPS,
            "output_frames": OUTPUT_FRAMES,
            "source_seconds_per_output_frame": 86400 / OUTPUT_FRAMES,
        },
        "cameras": {
            name: {
                "recording_count": len(value.cameras[name].recordings),
                "first_recorded_at": _iso(plans[name].first_recorded_at),
                "last_recorded_at": _iso(plans[name].last_recorded_at),
                "recorded_samples": plans[name].recorded_samples,
                "missing_samples": len(plans[name].missing_indices),
            }
            for name in CAMERAS
        },
        "sensor_overlay": _sensor_coverage(sensor_samples),
        "outputs": {
            name: _output_metadata(path, ffprobe)
            for name, path in outputs.items()
            if name != "summary"
        },
    }
    atomic_write_json(outputs["summary"], summary, mode=0o644)
    return summary


def render_combined_only(
    config: AppConfig,
    day: str,
    *,
    output_root: Path | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> dict[str, Any]:
    timezone = ZoneInfo(config.deployment.timezone)
    day_start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone)
    sensor_samples = _sensor_timeline(config.runtime.sensor_dir, day_start)
    destination = (output_root or timelapse_root(config)) / day
    water = destination / f"water-timelapse-{day}.mp4"
    environment = destination / f"environment-timelapse-{day}.mp4"
    combined = destination / f"combined-timelapse-{day}.mp4"
    missing = [path.name for path in (water, environment) if not path.is_file()]
    if missing:
        raise TimelapseError(
            "individual timelapse is missing: " + ", ".join(missing)
        )

    with tempfile.TemporaryDirectory(prefix=".combined-", dir=destination) as raw_temp:
        temporary = Path(raw_temp)
        subtitles = temporary / "timestamps.ass"
        _write_timestamp_subtitles(subtitles, day_start, sensor_samples)
        part = destination / f".{combined.name}.part"
        part.unlink(missing_ok=True)
        _render_combined(water, environment, subtitles, day, part, ffmpeg)
        part.replace(combined)

    metadata = _output_metadata(combined, ffprobe)
    summary_path = destination / "daily-summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        summary = {"schema_version": 1, "date": day}
    summary.setdefault("outputs", {})["combined"] = metadata
    summary["sensor_overlay"] = _sensor_coverage(sensor_samples)
    summary["updated_at"] = datetime.now(timezone).isoformat()
    atomic_write_json(summary_path, summary, mode=0o644)
    return metadata


def _recording_frame_count(path: Path, ffprobe: str) -> int:
    try:
        timing = json.loads(timing_path(path).read_text(encoding="utf-8"))
        count = int(timing["frame_count"])
        if count > 0:
            return count
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass
    result = _run(
        [
            ffprobe,
            "-v", "error",
            "-count_packets",
            "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_packets",
            "-of", "json",
            str(path),
        ]
    )
    try:
        count = int(json.loads(result.stdout)["streams"][0]["nb_read_packets"])
    except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise TimelapseError(f"could not count video frames in {path}") from exc
    if count < 1:
        raise TimelapseError(f"recording contains no video frames: {path}")
    return count


def _render_camera(
    plan: CameraPlan,
    temporary: Path,
    output: Path,
    ffmpeg: str,
) -> None:
    temporary.mkdir(parents=True, exist_ok=True)
    missing_frame = temporary / "missing.jpg"
    _run(
        [
            ffmpeg,
            "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"color=c=black:s={INDIVIDUAL_WIDTH}x{INDIVIDUAL_HEIGHT}",
            "-frames:v", "1",
            "-update", "1",
            "-vf",
            _drawtext(
                "No recording available",
                "(w-text_w)/2",
                "(h-text_h)/2",
                48,
                "white",
            ),
            "-q:v", "3",
            str(missing_frame),
        ]
    )
    for output_index in plan.missing_indices:
        _link_or_copy(missing_frame, temporary / f"frame-{output_index:04d}.jpg")

    for group_index, (source, selections) in enumerate(plan.selections.items()):
        group = temporary / f"source-{group_index:03d}"
        group.mkdir()
        expression = "+".join(
            f"eq(n\\,{selection.source_index})" for selection in selections
        )
        _run(
            [
                ffmpeg,
                "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-vf",
                (
                    f"select={expression},"
                    f"scale={INDIVIDUAL_WIDTH}:{INDIVIDUAL_HEIGHT}:"
                    "force_original_aspect_ratio=decrease:flags=lanczos,"
                    f"pad={INDIVIDUAL_WIDTH}:{INDIVIDUAL_HEIGHT}:"
                    "(ow-iw)/2:(oh-ih)/2:black"
                ),
                "-fps_mode:v", "passthrough",
                "-q:v", "3",
                str(group / "selected-%04d.jpg"),
            ]
        )
        rendered = sorted(group.glob("selected-*.jpg"))
        if len(rendered) != len(selections):
            raise TimelapseError(
                f"selected {len(rendered)} frames from {source}; "
                f"expected {len(selections)}"
            )
        for image, selection in zip(rendered, selections, strict=True):
            image.replace(temporary / f"frame-{selection.output_index:04d}.jpg")

    frames = list(temporary.glob("frame-*.jpg"))
    if len(frames) != OUTPUT_FRAMES:
        raise TimelapseError(
            f"{plan.camera} assembled {len(frames)} frames; expected {OUTPUT_FRAMES}"
        )
    _run(
        [
            ffmpeg,
            "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", str(OUTPUT_FPS),
            "-start_number", "0",
            "-i", str(temporary / "frame-%04d.jpg"),
            "-frames:v", str(OUTPUT_FRAMES),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-an",
            "-f", "mp4",
            str(output),
        ]
    )


def _render_combined(
    water: Path,
    environment: Path,
    subtitles: Path,
    day: str,
    output: Path,
    ffmpeg: str,
) -> None:
    title = _drawtext(
        f"Kratky Monitor - {day}", "(w-text_w)/2", "70", 44, "white"
    )
    water_label = _drawtext("WATER", "40", "210", 30, "white")
    environment_label = _drawtext(
        "ENVIRONMENT", "1000", "210", 30, "white"
    )
    subtitle_path = str(subtitles).replace("\\", "/").replace(":", "\\:")
    filters = (
        "[0:v]scale=960:540:flags=lanczos,setpts=PTS-STARTPTS[water];"
        "[1:v]scale=960:540:flags=lanczos,setpts=PTS-STARTPTS[environment];"
        f"color=c=0x101820:s={COMBINED_WIDTH}x{COMBINED_HEIGHT}:"
        f"r={OUTPUT_FPS}:d={OUTPUT_SECONDS}[base];"
        "[base][water]overlay=0:270:shortest=1[first];"
        "[first][environment]overlay=960:270:shortest=1[second];"
        f"[second]{title},{water_label},{environment_label},"
        f"ass='{subtitle_path}'[out]"
    )
    _run(
        [
            ffmpeg,
            "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(water),
            "-i", str(environment),
            "-filter_complex", filters,
            "-map", "[out]",
            "-frames:v", str(OUTPUT_FRAMES),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-an",
            "-f", "mp4",
            str(output),
        ]
    )


def _sensor_timeline(
    sensor_directory: Path,
    day_start: datetime,
) -> list[dict[str, Any] | None]:
    sample_step = timedelta(days=1) / OUTPUT_FRAMES
    targets = [day_start + sample_step * index for index in range(OUTPUT_FRAMES)]
    history = load_history(
        sensor_directory,
        day_start - timedelta(seconds=MAX_SENSOR_AGE_SECONDS),
        day_start + timedelta(days=1, seconds=MAX_SENSOR_AGE_SECONDS),
    )
    parsed = [
        (datetime.fromisoformat(str(sample["timestamp"])), sample)
        for sample in history
    ]
    result: list[dict[str, Any] | None] = []
    cursor = 0
    for target in targets:
        while cursor + 1 < len(parsed) and parsed[cursor + 1][0] <= target:
            cursor += 1
        candidates = parsed[max(0, cursor - 1):cursor + 2]
        nearest = min(
            candidates,
            key=lambda item: abs((item[0] - target).total_seconds()),
            default=None,
        )
        if (
            nearest is None
            or abs((nearest[0] - target).total_seconds()) > MAX_SENSOR_AGE_SECONDS
        ):
            result.append(None)
        else:
            result.append(nearest[1])
    return result


def _sensor_coverage(
    samples: Sequence[dict[str, Any] | None],
) -> dict[str, int]:
    return {
        "total_frames": len(samples),
        "matched_frames": sum(sample is not None for sample in samples),
        "environment_frames": sum(
            _section_available(sample, "environment") for sample in samples
        ),
        "water_frames": sum(
            _section_available(sample, "water") for sample in samples
        ),
        "maximum_sample_age_seconds": MAX_SENSOR_AGE_SECONDS,
    }


def _write_timestamp_subtitles(
    path: Path,
    day_start: datetime,
    sensor_samples: Sequence[dict[str, Any] | None] | None = None,
) -> None:
    sample_step = timedelta(days=1) / OUTPUT_FRAMES
    samples = list(sensor_samples or [None] * OUTPUT_FRAMES)
    if len(samples) != OUTPUT_FRAMES:
        raise TimelapseError(
            f"sensor timeline has {len(samples)} frames; expected {OUTPUT_FRAMES}"
        )
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {COMBINED_WIDTH}",
        f"PlayResY: {COMBINED_HEIGHT}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            "Style: Timestamp,DejaVu Sans,28,&H00FFFFFF,&H00FFFFFF,"
            "&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,40,40,0,1"
        ),
        (
            "Style: Sensor,DejaVu Sans,23,&H00E8F0EC,&H00E8F0EC,"
            "&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,4,40,40,0,1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for index, sample in enumerate(samples):
        timestamp = (day_start + sample_step * index).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        start = _ass_time(index / OUTPUT_FPS)
        end = _ass_time((index + 1) / OUTPUT_FPS)
        lines.append(
            f"Dialogue: 0,{start},{end},Timestamp,,0,0,0,,"
            f"{{\\an5\\pos(960,850)}}{timestamp}"
        )
        lines.append(
            f"Dialogue: 0,{start},{end},Sensor,,0,0,0,,"
            f"{{\\an4\\pos(60,925)}}{_sensor_line(sample, 'environment')}"
        )
        lines.append(
            f"Dialogue: 0,{start},{end},Sensor,,0,0,0,,"
            f"{{\\an4\\pos(60,990)}}{_sensor_line(sample, 'water')}"
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def _sensor_line(sample: dict[str, Any] | None, section: str) -> str:
    label = section.upper()
    if not _section_available(sample, section):
        return f"{{\\b1}}{label}{{\\b0}}  Unavailable"
    values = sample[section]["values"]
    if section == "environment":
        readings = (
            f"Air {_shown(values.get('air_temperature_f'), 1)} °F  |  "
            f"Humidity {_shown(values.get('relative_humidity_percent'), 1)}%  |  "
            f"CO₂ {_shown(values.get('co2_ppm'), 0)} ppm  |  "
            f"Light {_shown(values.get('light_lux'), 1)} lx"
        )
    else:
        readings = (
            f"Temp {_shown(values.get('temperature_c'), 1)} °C  |  "
            f"pH {_shown(values.get('ph'), 2)}  |  "
            f"EC {_shown(values.get('electrical_conductivity_us_cm'), 0)} µS/cm  |  "
            f"Moisture {_shown(values.get('moisture_percent'), 1)}%  |  "
            "N/P/K "
            f"{_shown(values.get('nitrogen_mg_kg'), 0)}/"
            f"{_shown(values.get('phosphorus_mg_kg'), 0)}/"
            f"{_shown(values.get('potassium_mg_kg'), 0)} mg/kg"
        )
    return f"{{\\b1}}{label}{{\\b0}}  {readings}"


def _section_available(sample: dict[str, Any] | None, section: str) -> bool:
    if not sample:
        return False
    values = sample.get(section, {}).get("values", {})
    return any(value is not None for value in values.values())


def _shown(value: object, decimals: int) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    if decimals == 0:
        return str(round(value))
    return f"{value:.{decimals}f}"


def _ass_time(seconds: float) -> str:
    centiseconds = round(seconds * 100)
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def _drawtext(
    text: str, x: str, y: str, size: int, color: str
) -> str:
    escaped = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    font = str(FONT_PATH).replace("\\", "/").replace(":", "\\:")
    return (
        f"drawtext=fontfile='{font}':text='{escaped}':x={x}:y={y}:"
        f"fontsize={size}:fontcolor={color}:borderw=2:bordercolor=black"
    )


def _output_metadata(path: Path, ffprobe: str) -> dict[str, Any]:
    result = _run(
        [
            ffprobe,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate,nb_frames:format=duration",
            "-of", "json",
            str(path),
        ]
    )
    try:
        probe = json.loads(result.stdout)
        stream = probe["streams"][0]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise TimelapseError(f"could not validate rendered video: {path}") from exc
    return {
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "md5": _md5(path),
        "codec": stream.get("codec_name"),
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "frame_rate": stream.get("avg_frame_rate"),
        "frame_count": int(stream.get("nb_frames", 0)),
        "duration_seconds": float(probe["format"]["duration"]),
    }


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _link_or_copy(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise TimelapseError(
            f"required program is not installed: {command[0]}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown error").strip()
        raise TimelapseError(f"{command[0]} failed: {detail}") from exc


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render timestamp-aligned daily Kratky timelapses"
    )
    parser.add_argument("days", nargs="+", help="calendar dates in YYYY-MM-DD form")
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--combined-only",
        action="store_true",
        help="rebuild only the combined preview from existing individual videos",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    for day in args.days:
        if args.combined_only:
            print(f"Rendering combined preview for {day}...", flush=True)
            metadata = render_combined_only(
                config,
                day,
                output_root=args.output_root,
            )
            print(
                f"Completed combined preview for {day}: "
                f"{metadata['size_bytes'] / (1024 * 1024):.1f} MiB",
                flush=True,
            )
            continue
        print(f"Rendering {day}...", flush=True)
        summary = render_day(
            config,
            day,
            output_root=args.output_root,
            force=args.force,
        )
        sizes = ", ".join(
            f"{name}={details['size_bytes'] / (1024 * 1024):.1f} MiB"
            for name, details in summary["outputs"].items()
        )
        print(f"Completed {day}: {sizes}", flush=True)


if __name__ == "__main__":
    main()
