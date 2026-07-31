#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from provisioning import ProvisioningError, load_secrets, remote_payload


def connect(paramiko: Any, secrets: dict[str, Any]) -> Any:
    target = secrets["target"]
    client = paramiko.SSHClient()
    if target["allow_unknown_host_key"]:
        print(
            "NOTICE: trusting the first SSH host key because this is a freshly imaged Pi"
        )
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        hostname=target["address"],
        port=target["port"],
        username=target["username"],
        password=target["ssh_password"],
        timeout=15,
        auth_timeout=15,
        banner_timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def run_remote(
    client: Any,
    command: str,
    *,
    sudo_password: str | None = None,
    tolerate_disconnect: bool = False,
) -> int:
    stdin, stdout, _stderr = client.exec_command(command, get_pty=False)
    if sudo_password is not None:
        stdin.write(sudo_password + "\n")
        stdin.flush()
    channel = stdout.channel
    try:
        while True:
            emitted = False
            if channel.recv_ready():
                sys.stdout.write(channel.recv(65536).decode("utf-8", errors="replace"))
                sys.stdout.flush()
                emitted = True
            if channel.recv_stderr_ready():
                sys.stderr.write(
                    channel.recv_stderr(65536).decode("utf-8", errors="replace")
                )
                sys.stderr.flush()
                emitted = True
            if (
                channel.exit_status_ready()
                and not channel.recv_ready()
                and not channel.recv_stderr_ready()
            ):
                return channel.recv_exit_status()
            if not emitted:
                time.sleep(0.1)
    except (EOFError, OSError):
        if tolerate_disconnect:
            return 0
        raise


def checked_remote(
    client: Any,
    label: str,
    command: str,
    *,
    sudo_password: str | None = None,
) -> None:
    print(f"\n==> {label}", flush=True)
    status = run_remote(client, command, sudo_password=sudo_password)
    if status != 0:
        raise ProvisioningError(f"{label} failed with exit status {status}")


def upload_remote_payload(client: Any, secrets: dict[str, Any]) -> str:
    remote_path = f"/dev/shm/kratky-provision-{uuid.uuid4().hex}.json"
    content = json.dumps(remote_payload(secrets), indent=2) + "\n"
    sftp = client.open_sftp()
    try:
        with sftp.file(remote_path, "w") as handle:
            handle.write(content)
        sftp.chmod(remote_path, 0o600)
    finally:
        sftp.close()
    return remote_path


def wait_for_reboot(paramiko: Any, secrets: dict[str, Any]) -> Any:
    print("\n==> waiting for the Pi to reboot into X11", flush=True)
    time.sleep(12)
    deadline = time.monotonic() + 240
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client = connect(paramiko, secrets)
            print("==> SSH is available after reboot", flush=True)
            return client
        except Exception as exc:  # connection libraries expose several subclasses
            last_error = exc
            time.sleep(5)
    raise ProvisioningError(f"Pi did not return after reboot: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision a fresh Kratky Raspberry Pi over SSH"
    )
    parser.add_argument("secrets", type=Path)
    parser.add_argument(
        "--no-reboot",
        action="store_true",
        help="finish after installation without rebooting into X11",
    )
    args = parser.parse_args()

    try:
        secrets = load_secrets(args.secrets)
        try:
            import paramiko
        except ImportError as exc:
            raise ProvisioningError(
                "Paramiko is required; run: "
                "python -m pip install -r requirements-provision.txt"
            ) from exc

        target = secrets["target"]
        password = target["ssh_password"]
        repo_dir = target["repo_dir"]
        quoted_repo = shlex.quote(repo_dir)
        quoted_url = shlex.quote(target["repo_url"])

        print(f"==> connecting to {target['address']} as {target['username']}")
        client = connect(paramiko, secrets)
        try:
            checked_remote(
                client,
                "installing Git on the base OS",
                "sudo -S -p '' sh -c "
                + shlex.quote(
                    "apt-get update && "
                    "DEBIAN_FRONTEND=noninteractive apt-get install -y git"
                ),
                sudo_password=password,
            )
            checked_remote(
                client,
                "cloning or updating the Kratky repository",
                f"if test -d {quoted_repo}/.git; then "
                f"git -C {quoted_repo} pull --ff-only; "
                f"else git clone {quoted_url} {quoted_repo}; fi",
            )
            remote_secrets = upload_remote_payload(client, secrets)
            checked_remote(
                client,
                "provisioning X11, TigerVNC, Tailscale, and Kratky",
                f"cd {quoted_repo} && "
                "sudo -S -p '' python3 scripts/provision-host.py "
                f"{shlex.quote(remote_secrets)} --delete-secrets",
                sudo_password=password,
            )

            if args.no_reboot:
                print(
                    "\nProvisioning completed. Reboot the Pi before verifying VNC."
                )
                return 0

            print("\n==> rebooting the Pi", flush=True)
            run_remote(
                client,
                "sudo -S -p '' systemctl reboot",
                sudo_password=password,
                tolerate_disconnect=True,
            )
        finally:
            client.close()

        client = wait_for_reboot(paramiko, secrets)
        try:
            checked_remote(
                client,
                "running post-reboot verification",
                f"cd {quoted_repo} && sudo -S -p '' env "
                f"KRATKY_USER={shlex.quote(target['username'])} "
                "scripts/verify-provisioning.sh",
                sudo_password=password,
            )
        finally:
            client.close()

        print("\nFresh-Pi provisioning and verification completed successfully.")
        return 0
    except (ProvisioningError, OSError) as exc:
        print(f"PROVISIONING FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
