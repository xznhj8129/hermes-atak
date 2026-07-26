#!/usr/bin/env python3
"""Install the ATAK plugin, skill, and versioned Python dependencies."""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
from pathlib import Path


PLUGIN_FILES = ("adapter.py", "ots_snapshot.py", "plugin.yaml")
FROGCOT_REMOTE = (
    "frogcot @ git+https://github.com/xznhj8129/frogcot.git@v1.2.0"
)
FROGGEOLIB_REMOTE = (
    "froggeolib @ git+https://github.com/xznhj8129/froggeolib.git@v1.1.0"
)


def copy_file(source: Path, destination: Path, *, dry_run: bool) -> None:
    print(f"{source} -> {destination}")
    if dry_run:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def link_directory(source: Path, destination: Path, *, dry_run: bool) -> None:
    print(f"{destination} -> {source}")
    if destination.is_symlink():
        if destination.resolve() == source.resolve():
            return
        raise SystemExit(f"refusing to replace existing symlink: {destination}")
    if destination.exists():
        raise SystemExit(
            f"refusing to replace existing path: {destination}\n"
            "Move it aside first, then run the installer again."
        )
    if dry_run:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(source.resolve(), target_is_directory=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the Hermes ATAK plugin and operational skill."
    )
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path.home() / ".hermes",
        help="Hermes data/configuration directory (default: ~/.hermes)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print destination paths without writing files.",
    )
    parser.add_argument(
        "--link",
        action="store_true",
        help="Symlink Hermes directly to this repository instead of copying files.",
    )
    parser.add_argument(
        "--gateway-python",
        type=Path,
        help="Install frogcot and froggeolib into this interpreter.",
    )
    parser.add_argument(
        "--frogcot",
        type=Path,
        help="Use this live frogcot checkout with an editable install.",
    )
    parser.add_argument(
        "--froggeolib",
        type=Path,
        help="Use this live froggeolib checkout with an editable install.",
    )
    parser.add_argument(
        "--uv",
        default=shutil.which("uv"),
        help="Path to uv when --gateway-python is supplied.",
    )
    args = parser.parse_args()

    repository = Path(__file__).resolve().parents[1]
    hermes_home = args.hermes_home.expanduser().resolve()
    plugin_destination = hermes_home / "plugins" / "atak"
    skill_destination = hermes_home / "skills" / "productivity" / "atak"

    if args.link:
        link_directory(
            repository / "plugin",
            plugin_destination,
            dry_run=args.dry_run,
        )
        link_directory(repository, skill_destination, dry_run=args.dry_run)
    else:
        for filename in PLUGIN_FILES:
            copy_file(
                repository / "plugin" / filename,
                plugin_destination / filename,
                dry_run=args.dry_run,
            )
        copy_file(
            repository / "SKILL.md",
            skill_destination / "SKILL.md",
            dry_run=args.dry_run,
        )
        for reference in sorted((repository / "references").glob("*.md")):
            copy_file(
                reference,
                skill_destination / "references" / reference.name,
                dry_run=args.dry_run,
            )

    if args.gateway_python:
        if not args.uv:
            parser.error("--gateway-python requires uv on PATH or an explicit --uv")
        dependencies = []
        for name, checkout, remote in (
            ("frogcot", args.frogcot, FROGCOT_REMOTE),
            ("froggeolib", args.froggeolib, FROGGEOLIB_REMOTE),
        ):
            if checkout is None:
                dependencies.append(remote)
                continue
            checkout = checkout.expanduser().resolve()
            if not checkout.is_dir():
                parser.error(f"{name} checkout does not exist: {checkout}")
            dependencies.extend(("--editable", str(checkout)))
        command = [
            str(args.uv),
            "pip",
            "install",
            "--python",
            str(args.gateway_python.expanduser().resolve()),
            *dependencies,
        ]
        print(shlex.join(command))
        if not args.dry_run:
            subprocess.run(command, check=True)

    print()
    print("Files prepared. Apply references/configuration.md, then restart Hermes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
