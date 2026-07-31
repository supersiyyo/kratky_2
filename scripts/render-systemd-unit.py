#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path, PurePosixPath


_USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def render_unit(text: str, *, user: str, group: str, repo_dir: str) -> str:
    if not _USERNAME.fullmatch(user):
        raise ValueError("invalid Linux username")
    if not _USERNAME.fullmatch(group):
        raise ValueError("invalid Linux group name")
    path = PurePosixPath(repo_dir)
    if not path.is_absolute() or ".." in path.parts or "\n" in repo_dir:
        raise ValueError("repo_dir must be an absolute normalized path")
    return (
        text.replace("User=kratky", f"User={user}")
        .replace("Group=kratky", f"Group={group}")
        .replace("/home/kratky/kratky-monitor", repo_dir)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--user", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--repo-dir", required=True)
    args = parser.parse_args()

    rendered = render_unit(
        args.source.read_text(encoding="utf-8"),
        user=args.user,
        group=args.group,
        repo_dir=args.repo_dir,
    )
    args.destination.write_text(rendered, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
