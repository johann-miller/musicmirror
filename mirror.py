from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

LOSSLESS_EXTS = {".flac", ".wav", ".aiff", ".aif"}

TRANSCODE_CODEC = "aac"
TRANSCODE_BITRATE = "256k"
TRANSCODE_EXT = ".m4a"

LogFunc = Callable[[str, str], None]
ProgressFunc = Callable[[int, int, str], None]


@dataclass
class SyncItem:
    """A single pending change between source and destination."""
    # action: "add", "update", "delete", "present"
    action: str
    src: Path       # source file (add/update) or file to remove (delete)
    dst: Path       # destination file path
    rel: Path       # relative path used for tree display
    checked: bool = True


def is_alac(path: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", str(path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        return '"alac"' in result.stdout.lower()
    except Exception:
        return False


def needs_transcode(path: Path) -> bool:
    ext = path.suffix.lower()
    if ext in LOSSLESS_EXTS:
        return True
    if ext == ".m4a":
        return is_alac(path)
    return False


def dest_path(src: Path, src_root: Path, dst_root: Path) -> Path:
    rel = src.relative_to(src_root)
    if needs_transcode(src):
        rel = rel.with_suffix(TRANSCODE_EXT)
    return dst_root / rel


def needs_update(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return True
    return src.stat().st_mtime > dst.stat().st_mtime


def _check_prefix(dst_root: Path, prefix: str) -> None:
    if not dst_root.name.startswith(prefix):
        raise ValueError(
            f"Destination folder '{dst_root.name}' does not start with required prefix '{prefix}'. "
            "Operation aborted."
        )


def _check_not_source(path: Path, src_root: Path) -> None:
    try:
        path.relative_to(src_root)
        raise ValueError(f"Write/delete target '{path}' is inside the source directory. Operation aborted.")
    except ValueError as e:
        if "inside the source directory" in str(e):
            raise


def transcode(src: Path, dst: Path, log: LogFunc | None = None) -> bool:
    tmp = dst.with_name(dst.stem + ".tmp" + dst.suffix)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        cmd = [
            "ffmpeg", "-y", "-i", str(src),
            "-c:a", TRANSCODE_CODEC, "-b:a", TRANSCODE_BITRATE, "-vn",
            str(tmp),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            if tmp.exists():
                tmp.unlink()
            if log:
                log("ERROR", f"ffmpeg failed for {src.name}:\n{result.stderr}")
            return False
        try:
            tmp.rename(dst)
        except OSError:
            import shutil as _shutil
            try:
                _shutil.move(str(tmp), str(dst))
            except Exception as e:
                if tmp.exists():
                    tmp.unlink()
                if log:
                    log("ERROR", f"could not move output for {src.name}: {e}")
                return False
        return True
    except Exception as e:
        if tmp.exists():
            tmp.unlink()
        if log:
            log("ERROR", f"transcode exception for {src.name}: {e}")
        return False


def copy_file(src: Path, dst: Path, src_root: Path, dst_root: Path, prefix: str, log: LogFunc | None = None) -> bool:
    _check_prefix(dst_root, prefix)
    _check_not_source(dst, src_root)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
        if log:
            log("COPY", str(src.relative_to(src_root)))
        return True
    except Exception as e:
        if log:
            log("ERROR", f"copy failed for {src.name}: {e}")
        return False


def process_file(
    src: Path,
    src_root: Path,
    dst_root: Path,
    prefix: str,
    log: LogFunc | None = None,
) -> bool:
    _check_prefix(dst_root, prefix)
    _check_not_source(dst_root, src_root)

    dst = dest_path(src, src_root, dst_root)

    if not needs_update(src, dst):
        return True

    if needs_transcode(src):
        if log:
            log("XCODE", str(src.relative_to(src_root)))
        return transcode(src, dst, log)
    else:
        return copy_file(src, dst, src_root, dst_root, prefix, log)


def delete_orphan(dst_file: Path, src_root: Path, dst_root: Path, prefix: str, log: LogFunc | None = None) -> bool:
    _check_prefix(dst_root, prefix)
    _check_not_source(dst_file, src_root)
    try:
        dst_file.unlink()
        if log:
            log("DEL", str(dst_file.relative_to(dst_root)))
        return True
    except Exception as e:
        if log:
            log("ERROR", f"delete failed for {dst_file.name}: {e}")
        return False


def check_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_ffprobe() -> bool:
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def build_sync_items(
    src_root: Path,
    dst_root: Path,
    delete_orphans: bool = False,
) -> list[SyncItem]:
    """Compute the full diff between source and destination as a flat list of SyncItems."""
    items: list[SyncItem] = []

    try:
        src_files = {
            f.relative_to(src_root): f
            for f in src_root.rglob("*")
            if f.is_file()
        }
    except (PermissionError, FileNotFoundError) as e:
        raise PermissionError(f"Cannot read source library: {e}") from e

    expected_dsts: set[Path] = set()
    for rel, src in src_files.items():
        rel_dst = rel.with_suffix(TRANSCODE_EXT) if needs_transcode(src) else rel
        dst = dst_root / rel_dst
        expected_dsts.add(dst)
        try:
            if not dst.exists():
                items.append(SyncItem("add", src, dst, rel))
            elif needs_update(src, dst):
                items.append(SyncItem("update", src, dst, rel))
            else:
                items.append(SyncItem("present", src, dst, rel, checked=False))
        except OSError:
            items.append(SyncItem("add", src, dst, rel))

    if delete_orphans:
        try:
            for dst_file in dst_root.rglob("*"):
                if dst_file.is_file() and dst_file not in expected_dsts:
                    items.append(SyncItem("delete", dst_file, dst_file, dst_file.relative_to(dst_root)))
        except (PermissionError, FileNotFoundError):
            pass

    return items


def apply_sync_items(
    items: list[SyncItem],
    src_root: Path,
    dst_root: Path,
    prefix: str,
    log: LogFunc | None = None,
    progress: ProgressFunc | None = None,
    on_complete: Callable[[SyncItem], None] | None = None,
    cancel: threading.Event | None = None,
) -> bool:
    """Apply a list of SyncItems (only those with checked=True).

    Returns True if all items were processed, False if cancelled early.
    """
    checked = [item for item in items if item.checked]
    total = len(checked)
    for i, item in enumerate(checked):
        if cancel and cancel.is_set():
            if log:
                log("INFO", f"Sync stopped after {i}/{total} item(s).")
            return False
        if progress:
            progress(i, total, str(item.rel))
        try:
            ok = False
            if item.action in ("add", "update"):
                ok = process_file(item.src, src_root, dst_root, prefix, log)
            elif item.action == "delete":
                ok = delete_orphan(item.dst, src_root, dst_root, prefix, log)
            if ok and on_complete:
                on_complete(item)
        except Exception as e:
            if log:
                log("ERROR", str(e))
    if progress:
        progress(total, total, "")
    return True


def paths_overlap(p1: Path, p2: Path) -> bool:
    try:
        p1.relative_to(p2)
        return True
    except ValueError:
        pass
    try:
        p2.relative_to(p1)
        return True
    except ValueError:
        pass
    return p1 == p2
