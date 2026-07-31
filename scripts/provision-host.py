#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from provisioning import ProvisioningError, load_secrets


REPO_DIR = Path(__file__).resolve().parents[1]


def run(
    label: str,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    print(f"==> {label}", flush=True)
    try:
        return subprocess.run(
            command,
            check=True,
            env=env,
            input=input_bytes,
            capture_output=capture_output,
        )
    except subprocess.CalledProcessError as exc:
        raise ProvisioningError(
            f"{label} failed with exit status {exc.returncode}"
        ) from None


def ensure_root() -> None:
    if os.geteuid() != 0:
        raise ProvisioningError(
            "run through sudo: sudo python3 scripts/provision-host.py KEY.json"
        )


def ensure_private_file(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ProvisioningError(
            f"{path} must not be accessible by group or other users; use chmod 600"
        )


def read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def install_base_packages() -> None:
    run("refreshing Debian package metadata", ["apt-get", "update"])
    run(
        "installing X11, TigerVNC, and provisioning dependencies",
        [
            "apt-get",
            "install",
            "-y",
            "ca-certificates",
            "curl",
            "python3-yaml",
            "raspi-config",
            "tigervnc-scraping-server",
            "tigervnc-tools",
            "x11-xserver-utils",
        ],
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
    )


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "kratky-provisioner/1"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read()
    if not content:
        raise ProvisioningError(f"download was empty: {url}")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(content)
    os.chmod(temporary, 0o644)
    os.replace(temporary, destination)


def install_tailscale() -> None:
    if shutil.which("tailscale"):
        print("==> Tailscale is already installed", flush=True)
        return
    os_release = read_os_release()
    codename = os_release.get("VERSION_CODENAME", "")
    if not codename or not codename.replace("-", "").isalnum():
        raise ProvisioningError("cannot determine a safe Debian release codename")
    base = f"https://pkgs.tailscale.com/stable/debian/{codename}"
    download(
        f"{base}.noarmor.gpg",
        Path("/usr/share/keyrings/tailscale-archive-keyring.gpg"),
    )
    download(
        f"{base}.tailscale-keyring.list",
        Path("/etc/apt/sources.list.d/tailscale.list"),
    )
    run("refreshing package metadata with Tailscale", ["apt-get", "update"])
    run(
        "installing Tailscale",
        ["apt-get", "install", "-y", "tailscale"],
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
    )


def configure_desktop(secrets: dict[str, Any], username: str) -> None:
    run(
        "setting the Raspberry Pi hostname",
        ["raspi-config", "nonint", "do_hostname", secrets["system"]["hostname"]],
    )
    run(
        "selecting the X11/Openbox display backend",
        ["raspi-config", "nonint", "do_wayland", "W1"],
    )
    boot_env = {**os.environ, "SUDO_USER": username}
    run(
        "enabling desktop autologin for the VNC display",
        ["raspi-config", "nonint", "do_boot_behaviour", "B4"],
        env=boot_env,
    )
    run(
        "setting the application timezone",
        [
            "raspi-config",
            "nonint",
            "do_change_timezone",
            secrets["system"]["timezone"],
        ],
    )


def write_owned_text(path: Path, content: str, uid: int, gid: int, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def configure_vnc(secrets: dict[str, Any], user: pwd.struct_passwd) -> None:
    vnc = secrets["vnc"]
    vnc_dir = Path(user.pw_dir) / ".vnc"
    vnc_dir.mkdir(parents=True, exist_ok=True)
    os.chown(vnc_dir, user.pw_uid, user.pw_gid)
    os.chmod(vnc_dir, 0o700)

    result = run(
        "creating the TigerVNC password file",
        ["tigervncpasswd", "-f"],
        input_bytes=(vnc["password"] + "\n").encode("utf-8"),
        capture_output=True,
    )
    password_path = vnc_dir / "passwd"
    password_path.write_bytes(result.stdout)
    os.chown(password_path, user.pw_uid, user.pw_gid)
    os.chmod(password_path, 0o600)

    launcher_dir = Path("/usr/local/libexec/kratky")
    launcher_dir.mkdir(parents=True, exist_ok=True)
    launcher = launcher_dir / "start-vnc-desktop.sh"
    shutil.copyfile(REPO_DIR / "scripts/start-vnc-desktop.sh", launcher)
    os.chown(launcher, 0, 0)
    os.chmod(launcher, 0o755)

    bind_value = "1" if vnc["bind_to_tailscale"] else "0"
    desktop = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Kratky TigerVNC Desktop\n"
        "Comment=Share the active X11 desktop over the private Tailscale address\n"
        "Exec=env "
        f"KRATKY_VNC_DISPLAY={vnc['display']} "
        f"KRATKY_VNC_GEOMETRY={vnc['geometry']} "
        f"KRATKY_VNC_PORT={vnc['port']} "
        f"KRATKY_VNC_BIND_TO_TAILSCALE={bind_value} "
        f"{launcher}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    autostart_dir = Path(user.pw_dir) / ".config/autostart"
    autostart_dir.mkdir(parents=True, exist_ok=True)
    os.chown(autostart_dir, user.pw_uid, user.pw_gid)
    os.chmod(autostart_dir, 0o755)
    write_owned_text(
        autostart_dir / "kratky-vnc.desktop",
        desktop,
        user.pw_uid,
        user.pw_gid,
        0o644,
    )


def configure_kratky_yaml(
    secrets: dict[str, Any], user: pwd.struct_passwd
) -> None:
    import yaml

    example = yaml.safe_load(
        (REPO_DIR / "config/kratky.example.yaml").read_text(encoding="utf-8")
    )
    kratky = secrets["kratky"]
    example["deployment"]["mode"] = kratky["deployment_mode"]
    example["deployment"]["timezone"] = secrets["system"]["timezone"]
    example["storage"]["minimum_free_gib"] = kratky["minimum_free_gib"]
    for name, key in (
        ("water", "water_camera"),
        ("environment", "environment_camera"),
    ):
        camera = kratky[key]
        example["cameras"][name]["enabled"] = camera["enabled"]
        example["cameras"][name]["required"] = camera["required"]
        example["cameras"][name]["device"] = camera["device"]

    config_dir = Path("/etc/kratky")
    config_dir.mkdir(parents=True, exist_ok=True)
    os.chown(config_dir, 0, user.pw_gid)
    os.chmod(config_dir, 0o750)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=config_dir,
        prefix="config.yaml.",
        delete=False,
    ) as handle:
        yaml.safe_dump(example, handle, sort_keys=False)
        temporary = Path(handle.name)
    os.chown(temporary, 0, user.pw_gid)
    os.chmod(temporary, 0o640)
    os.replace(temporary, config_dir / "config.yaml")


def configure_tailscale(secrets: dict[str, Any]) -> None:
    run(
        "enabling the Tailscale service",
        ["systemctl", "enable", "--now", "tailscaled.service"],
    )
    already_running = False
    status = subprocess.run(
        ["tailscale", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if status.returncode == 0:
        try:
            already_running = json.loads(status.stdout).get("BackendState") == "Running"
        except json.JSONDecodeError:
            already_running = False
    if already_running:
        print("==> Tailscale is already enrolled; preserving its node identity")
        return
    tailscale = secrets["tailscale"]
    run(
        "enrolling this Pi in Tailscale",
        [
            "tailscale",
            "up",
            f"--auth-key={tailscale['auth_key']}",
            f"--hostname={tailscale['hostname']}",
            f"--accept-dns={str(tailscale['accept_dns']).lower()}",
        ],
    )


def run_kratky_bootstrap(username: str) -> None:
    env = {**os.environ, "KRATKY_USER": username}
    run(
        "installing and starting Kratky Monitor",
        [str(REPO_DIR / "scripts/bootstrap.sh")],
        env=env,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision X11, TigerVNC, Tailscale, and Kratky on a fresh Pi"
    )
    parser.add_argument("secrets", type=Path)
    parser.add_argument(
        "--delete-secrets",
        action="store_true",
        help="delete the transient Pi-side secrets file on exit",
    )
    args = parser.parse_args()
    secrets_path = args.secrets.resolve()

    try:
        ensure_root()
        ensure_private_file(secrets_path)
        secrets = load_secrets(
            secrets_path, require_ssh_password=False
        )
        username = secrets["target"]["username"]
        user = pwd.getpwnam(username)
        configured_repo = Path(secrets["target"]["repo_dir"]).resolve()
        if configured_repo != REPO_DIR:
            raise ProvisioningError(
                f"repository is at {REPO_DIR}, but target.repo_dir is {configured_repo}"
            )

        install_base_packages()
        install_tailscale()
        configure_desktop(secrets, username)
        configure_vnc(secrets, user)
        configure_kratky_yaml(secrets, user)
        run_kratky_bootstrap(username)
        configure_tailscale(secrets)
        print("==> Provisioning completed; reboot is required to enter X11")
        return 0
    except (ProvisioningError, KeyError, OSError) as exc:
        print(f"PROVISIONING FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.delete_secrets:
            try:
                secrets_path.unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
