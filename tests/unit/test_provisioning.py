import importlib.util
import io
import json
from pathlib import Path

import pytest

from scripts.provisioning import (
    ProvisioningError,
    remote_payload,
    validate_secrets,
    write_remote_output,
)


ROOT = Path(__file__).parents[2]


def test_remote_output_survives_a_legacy_windows_console() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", newline="")

    write_remote_output(stream, "route → ready\n".encode())
    stream.flush()

    assert raw.getvalue().decode("cp1252") == "route ? ready\n"


def valid_secrets() -> dict:
    return {
        "schema_version": 1,
        "target": {
            "address": "arcs.local",
            "port": 22,
            "username": "kratky",
            "ssh_password": "local-password",
            "allow_unknown_host_key": True,
            "repo_url": "https://github.com/supersiyyo/kratky_2.git",
            "repo_dir": "/home/kratky/kratky-monitor",
        },
        "system": {
            "hostname": "arcs",
            "timezone": "America/Los_Angeles",
            "desktop_autologin": True,
            "display_backend": "x11",
        },
        "tailscale": {
            "auth_key": "tskey-auth-test-only",
            "hostname": "arcs",
            "accept_dns": True,
        },
        "vnc": {
            "password": "vncpass1",
            "display": ":0",
            "geometry": "1920x1080",
            "port": 5900,
            "bind_to_tailscale": True,
        },
        "kratky": {
            "deployment_mode": "development",
            "minimum_free_gib": 12,
            "water_camera": {
                "enabled": True,
                "required": True,
                "device": (
                    "/dev/v4l/by-id/"
                    "usb-UltraSemi_Guermok_USB2_Video_20210621-video-index0"
                ),
            },
            "environment_camera": {
                "enabled": True,
                "required": True,
                "device": (
                    "/dev/v4l/by-id/"
                    "usb-MACROSILICON_Guermok_USB3_Video_20210623-video-index0"
                ),
            },
        },
    }


def test_validates_complete_secrets() -> None:
    secrets = validate_secrets(valid_secrets())

    assert secrets["target"]["username"] == "kratky"
    assert secrets["kratky"]["environment_camera"]["enabled"] is True


def test_rejects_placeholder_secrets() -> None:
    raw = valid_secrets()
    raw["vnc"]["password"] = "REPLACE_WITH_PASSWORD"

    with pytest.raises(ProvisioningError, match="placeholder"):
        validate_secrets(raw)


def test_rejects_a_duplicated_tailscale_auth_key_prefix() -> None:
    raw = valid_secrets()
    raw["tailscale"]["auth_key"] = "tskey-auth-tskey-auth-test-only"

    with pytest.raises(ProvisioningError, match="prefix twice"):
        validate_secrets(raw)


def test_rejects_duplicate_camera_paths() -> None:
    raw = valid_secrets()
    raw["kratky"]["environment_camera"]["device"] = raw["kratky"][
        "water_camera"
    ]["device"]

    with pytest.raises(ProvisioningError, match="distinct"):
        validate_secrets(raw)


def test_remote_payload_excludes_ssh_password() -> None:
    secrets = validate_secrets(valid_secrets())

    payload = remote_payload(secrets)

    assert "ssh_password" not in payload["target"]
    assert payload["tailscale"]["auth_key"].startswith("tskey-")
    assert secrets["target"]["ssh_password"] == "local-password"
    validate_secrets(payload, require_ssh_password=False)


def test_rejects_vnc_password_longer_than_protocol_supports() -> None:
    raw = valid_secrets()
    raw["vnc"]["password"] = "longer-than-eight"

    with pytest.raises(ProvisioningError, match="exactly 8"):
        validate_secrets(raw)


def test_real_secret_filenames_are_ignored() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "KEY.json" in ignore
    assert "provisioning-secrets.json" in ignore
    assert "*.secrets.json" in ignore


def test_example_json_is_well_formed_and_uses_placeholders() -> None:
    example = json.loads(
        (ROOT / "config/provisioning-secrets.example.json").read_text(
            encoding="utf-8"
        )
    )

    assert example["schema_version"] == 1
    assert example["target"]["ssh_password"].startswith("REPLACE")
    assert example["vnc"]["bind_to_tailscale"] is True


def test_unit_renderer_supports_a_non_default_user(tmp_path: Path) -> None:
    script = ROOT / "scripts/render-systemd-unit.py"
    spec = importlib.util.spec_from_file_location("render_systemd_unit", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    source = (
        "User=kratky\nGroup=kratky\n"
        "ExecStart=/home/kratky/kratky-monitor/.venv/bin/python\n"
    )
    rendered = module.render_unit(
        source,
        user="grower",
        group="growers",
        repo_dir="/home/grower/kratky-monitor",
    )

    assert "User=grower" in rendered
    assert "Group=growers" in rendered
    assert "/home/grower/kratky-monitor/.venv/bin/python" in rendered


def test_vnc_launcher_waits_for_tailscale_and_binds_its_address() -> None:
    launcher = (ROOT / "scripts/start-vnc-desktop.sh").read_text(
        encoding="utf-8"
    )

    assert "tailscale ip -4" in launcher
    assert 'bind_arguments=(-interface "${bind_address}")' in launcher
    assert "X0tigervnc" in launcher
    assert "-localhost=0" in launcher
    assert "-SecurityTypes VncAuth" in launcher


def test_host_provisioner_uses_the_tigervnc_password_binary() -> None:
    provisioner = (ROOT / "scripts/provision-host.py").read_text(
        encoding="utf-8"
    )

    assert '["tigervncpasswd", "-f"]' in provisioner
    assert '["vncpasswd", "-f"]' not in provisioner
