from __future__ import annotations

import os
from pathlib import Path


def detect_external_locations() -> list[dict]:
    """Scan for mounted external drives and Android/MTP devices."""
    seen: set[str] = set()
    locations: list[dict] = []

    def add(name: str, path: Path, dtype: str) -> None:
        key = str(path)
        if key not in seen and path.exists():
            seen.add(key)
            locations.append({"name": name, "path": str(path), "type": dtype})

    # udisks automounts: /media/<user>/* and /run/media/<user>/*
    username = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    for base in [Path("/media"), Path("/run/media")]:
        if not base.exists():
            continue
        candidate = base / username
        user_dirs = [candidate] if (username and candidate.exists()) else []
        if not user_dirs:
            try:
                user_dirs = [d for d in base.iterdir() if d.is_dir()]
            except PermissionError:
                pass
        for user_dir in user_dirs:
            try:
                for entry in sorted(user_dir.iterdir()):
                    if entry.is_dir():
                        add(entry.name, entry, "drive")
            except PermissionError:
                pass

    # /mnt entries: WSL Windows drives, manually mounted volumes
    _WSL_SKIP = {"wslg", "wsl", "wslhyper", "tmp", "wsl2"}
    mnt = Path("/mnt")
    if mnt.exists():
        try:
            for entry in sorted(mnt.iterdir()):
                if not entry.is_dir() or entry.name in _WSL_SKIP:
                    continue
                name = entry.name
                # Single-letter = WSL Windows drive letter
                if len(name) == 1 and name.isalpha():
                    name = f"{name.upper()}: (Windows)"
                add(name, entry, "drive")
        except PermissionError:
            pass

    # gvfs MTP mounts (GNOME/KDE desktop Linux)
    try:
        for gvfs_base in Path("/run/user").glob("*/gvfs"):
            for entry in sorted(gvfs_base.iterdir()):
                if not entry.is_dir() or "mtp" not in entry.name.lower():
                    continue
                display = "Android Device"
                for part in entry.name.split(","):
                    if part.startswith("host="):
                        display = part[5:].replace("%20", " ")[:30]
                add(display, entry, "android")
    except (PermissionError, FileNotFoundError):
        pass

    return locations
