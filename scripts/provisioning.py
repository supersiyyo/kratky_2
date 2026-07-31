from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any


class ProvisioningError(ValueError):
    """Raised when provisioning input is incomplete or unsafe."""


def write_remote_output(stream: Any, payload: bytes) -> None:
    """Write UTF-8 remote output without failing on a legacy local console."""
    text = payload.decode("utf-8", errors="replace")
    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(
        encoding, errors="replace"
    )
    stream.write(safe_text)
    stream.flush()


_USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_HOSTNAME = re.compile(
    r"^(?=.{1,63}$)(?!-)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_DISPLAY = re.compile(r"^:[0-9]{1,3}$")
_GEOMETRY = re.compile(r"^[1-9][0-9]{2,4}x[1-9][0-9]{2,4}$")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProvisioningError(f"{label} must be a JSON object")
    return value


def _string(
    mapping: dict[str, Any],
    key: str,
    label: str,
    *,
    secret: bool = False,
) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProvisioningError(f"{label}.{key} must be a non-empty string")
    value = value.strip()
    if value.upper().startswith("REPLACE"):
        raise ProvisioningError(f"{label}.{key} still contains a placeholder")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ProvisioningError(f"{label}.{key} contains an invalid control character")
    if secret and len(value) < 8:
        raise ProvisioningError(f"{label}.{key} must contain at least 8 characters")
    return value


def _boolean(mapping: dict[str, Any], key: str, label: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise ProvisioningError(f"{label}.{key} must be true or false")
    return value


def _camera(mapping: dict[str, Any], label: str) -> dict[str, Any]:
    camera = _mapping(mapping, label)
    enabled = _boolean(camera, "enabled", label)
    required = _boolean(camera, "required", label)
    device = camera.get("device")
    if enabled:
        if not isinstance(device, str) or not device.startswith("/dev/v4l/by-id/"):
            raise ProvisioningError(
                f"{label}.device must use a stable /dev/v4l/by-id path"
            )
        if not device.endswith("-video-index0"):
            raise ProvisioningError(f"{label}.device must select video-index0")
    elif device is not None and not isinstance(device, str):
        raise ProvisioningError(f"{label}.device must be a string or null")
    return {"enabled": enabled, "required": required, "device": device}


def validate_secrets(
    raw: dict[str, Any], *, require_ssh_password: bool = True
) -> dict[str, Any]:
    if raw.get("schema_version") != 1:
        raise ProvisioningError("schema_version must be 1")

    target = _mapping(raw.get("target"), "target")
    system = _mapping(raw.get("system"), "system")
    tailscale = _mapping(raw.get("tailscale"), "tailscale")
    vnc = _mapping(raw.get("vnc"), "vnc")
    kratky = _mapping(raw.get("kratky"), "kratky")

    username = _string(target, "username", "target")
    if not _USERNAME.fullmatch(username):
        raise ProvisioningError("target.username is not a safe Linux username")
    address = _string(target, "address", "target")
    if any(char.isspace() for char in address):
        raise ProvisioningError("target.address cannot contain whitespace")
    port = target.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ProvisioningError("target.port must be between 1 and 65535")
    ssh_password = None
    if require_ssh_password:
        ssh_password = _string(
            target, "ssh_password", "target", secret=True
        )
    elif "ssh_password" in target:
        ssh_password = _string(
            target, "ssh_password", "target", secret=True
        )
    allow_unknown_host_key = _boolean(
        target, "allow_unknown_host_key", "target"
    )
    repo_url = _string(target, "repo_url", "target")
    if not repo_url.startswith("https://github.com/") or not repo_url.endswith(".git"):
        raise ProvisioningError("target.repo_url must be an HTTPS GitHub clone URL")
    repo_dir = PurePosixPath(_string(target, "repo_dir", "target"))
    if not repo_dir.is_absolute() or ".." in repo_dir.parts:
        raise ProvisioningError("target.repo_dir must be an absolute normalized path")

    hostname = _string(system, "hostname", "system")
    if not _HOSTNAME.fullmatch(hostname):
        raise ProvisioningError("system.hostname is not a valid hostname")
    timezone = _string(system, "timezone", "system")
    display_backend = _string(system, "display_backend", "system").lower()
    if display_backend != "x11":
        raise ProvisioningError("system.display_backend must be x11")
    desktop_autologin = _boolean(system, "desktop_autologin", "system")
    if not desktop_autologin:
        raise ProvisioningError(
            "system.desktop_autologin must be true for desktop-sharing TigerVNC"
        )

    tailscale_key = _string(
        tailscale, "auth_key", "tailscale", secret=True
    )
    if not tailscale_key.startswith("tskey-"):
        raise ProvisioningError("tailscale.auth_key must be a Tailscale auth key")
    tailscale_hostname = _string(tailscale, "hostname", "tailscale")
    if not _HOSTNAME.fullmatch(tailscale_hostname):
        raise ProvisioningError("tailscale.hostname is not a valid hostname")
    accept_dns = _boolean(tailscale, "accept_dns", "tailscale")

    vnc_password = _string(vnc, "password", "vnc")
    if len(vnc_password) != 8:
        raise ProvisioningError(
            "vnc.password must contain exactly 8 characters; "
            "TigerVNC ignores characters after the eighth"
        )
    display = _string(vnc, "display", "vnc")
    if not _DISPLAY.fullmatch(display):
        raise ProvisioningError("vnc.display must look like :0")
    geometry = _string(vnc, "geometry", "vnc")
    if not _GEOMETRY.fullmatch(geometry):
        raise ProvisioningError("vnc.geometry must look like 1920x1080")
    vnc_port = vnc.get("port")
    if not isinstance(vnc_port, int) or not 1024 <= vnc_port <= 65535:
        raise ProvisioningError("vnc.port must be between 1024 and 65535")
    bind_to_tailscale = _boolean(vnc, "bind_to_tailscale", "vnc")
    if not bind_to_tailscale:
        raise ProvisioningError(
            "vnc.bind_to_tailscale must be true for the supported secure profile"
        )

    deployment_mode = _string(kratky, "deployment_mode", "kratky").lower()
    if deployment_mode not in {"development", "production"}:
        raise ProvisioningError(
            "kratky.deployment_mode must be development or production"
        )
    minimum_free_gib = kratky.get("minimum_free_gib")
    if not isinstance(minimum_free_gib, (int, float)) or minimum_free_gib < 0:
        raise ProvisioningError("kratky.minimum_free_gib must be non-negative")
    if deployment_mode == "production" and minimum_free_gib < 10:
        raise ProvisioningError(
            "production requires kratky.minimum_free_gib of at least 10"
        )
    water = _camera(kratky.get("water_camera"), "kratky.water_camera")
    environment = _camera(
        kratky.get("environment_camera"), "kratky.environment_camera"
    )
    if (
        water["enabled"]
        and environment["enabled"]
        and water["device"] == environment["device"]
    ):
        raise ProvisioningError("water and environment cameras must be distinct")

    validated = {
        "schema_version": 1,
        "target": {
            "address": address,
            "port": port,
            "username": username,
            "allow_unknown_host_key": allow_unknown_host_key,
            "repo_url": repo_url,
            "repo_dir": str(repo_dir),
        },
        "system": {
            "hostname": hostname,
            "timezone": timezone,
            "desktop_autologin": desktop_autologin,
            "display_backend": display_backend,
        },
        "tailscale": {
            "auth_key": tailscale_key,
            "hostname": tailscale_hostname,
            "accept_dns": accept_dns,
        },
        "vnc": {
            "password": vnc_password,
            "display": display,
            "geometry": geometry,
            "port": vnc_port,
            "bind_to_tailscale": bind_to_tailscale,
        },
        "kratky": {
            "deployment_mode": deployment_mode,
            "minimum_free_gib": float(minimum_free_gib),
            "water_camera": water,
            "environment_camera": environment,
        },
    }
    if ssh_password is not None:
        validated["target"]["ssh_password"] = ssh_password
    return validated


def load_secrets(
    path: str | Path, *, require_ssh_password: bool = True
) -> dict[str, Any]:
    secrets_path = Path(path)
    try:
        raw = json.loads(secrets_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvisioningError(f"cannot read {secrets_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProvisioningError("provisioning secrets root must be a JSON object")
    return validate_secrets(raw, require_ssh_password=require_ssh_password)


def remote_payload(secrets: dict[str, Any]) -> dict[str, Any]:
    """Return the subset permitted to exist briefly on the Raspberry Pi."""
    payload = json.loads(json.dumps(secrets))
    payload["target"].pop("ssh_password", None)
    return payload
