from __future__ import annotations

import json
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple

LOSSLESS_EXTS = {".flac", ".wav", ".aiff", ".aif"}

LogFunc = Callable[[str, str], None]
ProgressFunc = Callable[[int, int, str], None]


class CodecPreset(NamedTuple):
    name: str
    codec: str
    bitrate: str
    ext: str
    description: str


CODEC_PRESETS = [
    CodecPreset("FLAC (Lossless)",     "flac",        "",     ".flac", "Highest quality, larger file size"),
    CodecPreset("MP3 320kbps (High)",  "libmp3lame",  "320k", ".mp3",  "High quality, widely compatible"),
    CodecPreset("AAC 256kbps (High)",  "aac",         "256k", ".m4a",  "High quality, works on iOS"),
    CodecPreset("MP3 192kbps (Medium)","libmp3lame",  "192k", ".mp3",  "Balanced quality and size"),
    CodecPreset("AAC 128kbps (Low)",   "aac",         "128k", ".m4a",  "Lower quality, saves space"),
]

PRESET_MAP = {p.name: p for p in CODEC_PRESETS}

_FFMPEG_TO_FFPROBE_CODEC = {
    "libmp3lame": "mp3",
    "flac": "flac",
    "aac": "aac",
}


def get_audio_codec_info(path: Path) -> tuple[str, int] | None:
    """Return (codec_name, bitrate_kbps) for the first audio stream, or None on failure."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", "-select_streams", "a:0", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            return None
        stream = streams[0]
        codec = stream.get("codec_name", "")
        bitrate_str = stream.get("bit_rate") or data.get("format", {}).get("bit_rate", "0")
        bitrate_kbps = int(bitrate_str) // 1000
        return codec, bitrate_kbps
    except Exception:
        return None


def _info_matches_preset(info: tuple[str, int], preset: CodecPreset) -> bool:
    codec, bitrate_kbps = info
    expected_codec = _FFMPEG_TO_FFPROBE_CODEC.get(preset.codec, preset.codec)
    if codec != expected_codec:
        return False
    if preset.bitrate:
        target_kbps = int(preset.bitrate.rstrip("k"))
        return abs(bitrate_kbps - target_kbps) <= target_kbps * 0.15
    return True


def matches_preset(dst: Path, preset: CodecPreset) -> bool:
    """Return True if dst's codec and bitrate match the given preset."""
    info = get_audio_codec_info(dst)
    if info is None:
        return False
    return _info_matches_preset(info, preset)


@dataclass
class SyncItem:
    """A single pending change between source and destination."""
    # action: "add", "update", "delete", "present", "reformat"
    action: str
    src: Path       # source file (add/update) or file to remove (delete)
    dst: Path       # destination file path
    rel: Path       # relative path used for tree display
    checked: bool = True
    old_dst: Path | None = None  # for reformat: old file to delete before transcoding
    codec_info: tuple[str, int] | None = None  # (codec_name, bitrate_kbps) of dst when it exists


@dataclass
class SyncPlan:
    add: list[Path] = field(default_factory=list)
    update: list[Path] = field(default_factory=list)
    delete: list[Path] = field(default_factory=list)


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


def dest_path(src: Path, src_root: Path, dst_root: Path, output_ext: str) -> Path:
    rel = src.relative_to(src_root)
    if needs_transcode(src):
        rel = rel.with_suffix(output_ext)
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


def transcode(src: Path, dst: Path, codec: str, bitrate: str, log: LogFunc | None = None) -> bool:
    tmp = dst.with_name(dst.stem + ".tmp" + dst.suffix)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        cmd = ["ffmpeg", "-y", "-i", str(src), "-c:a", codec, "-vn"]
        if bitrate:
            cmd.extend(["-b:a", bitrate])
        cmd.append(str(tmp))
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=300,
        )
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


def build_sync_plan(src_root: Path, dst_root: Path, output_ext: str) -> SyncPlan:
    plan = SyncPlan()

    try:
        src_files = {f.relative_to(src_root): f for f in src_root.rglob("*") if f.is_file()}
    except PermissionError as e:
        raise PermissionError(f"Cannot read source library: {e}") from e

    for rel, src in src_files.items():
        dst = dst_root / rel
        if needs_transcode(src):
            dst = dst.with_suffix(output_ext)
        try:
            if not dst.exists():
                plan.add.append(src)
            elif needs_update(src, dst):
                plan.update.append(src)
        except OSError:
            plan.add.append(src)

    src_dest_names = set()
    for rel, src in src_files.items():
        d = dst_root / rel
        if needs_transcode(src):
            d = d.with_suffix(output_ext)
        src_dest_names.add(d)

    try:
        for dst_file in dst_root.rglob("*"):
            if dst_file.is_file() and dst_file not in src_dest_names:
                plan.delete.append(dst_file)
    except PermissionError:
        pass

    return plan


def process_file(
    src: Path,
    src_root: Path,
    dst_root: Path,
    prefix: str,
    codec: str,
    bitrate: str,
    output_ext: str,
    log: LogFunc | None = None,
) -> bool:
    _check_prefix(dst_root, prefix)
    _check_not_source(dst_root, src_root)

    dst = dest_path(src, src_root, dst_root, output_ext)

    if not needs_update(src, dst):
        return True

    if needs_transcode(src):
        if log:
            log("XCODE", str(src.relative_to(src_root)))
        return transcode(src, dst, codec, bitrate, log)
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
    output_ext: str,
    recompress_existing: bool = False,
    current_preset: CodecPreset | None = None,
    prev_preset: CodecPreset | None = None,
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
        rel_dst = rel.with_suffix(output_ext) if needs_transcode(src) else rel
        dst = dst_root / rel_dst
        expected_dsts.add(dst)
        try:
            if not dst.exists():
                items.append(SyncItem("add", src, dst, rel))
            elif needs_transcode(src) and current_preset is not None:
                info = get_audio_codec_info(dst)
                if info is not None and _info_matches_preset(info, current_preset):
                    items.append(SyncItem("present", src, dst, rel, checked=False, codec_info=info))
                else:
                    items.append(SyncItem("update", src, dst, rel, codec_info=info))
            elif needs_update(src, dst):
                items.append(SyncItem("update", src, dst, rel))
            else:
                items.append(SyncItem("present", src, dst, rel, checked=False))
        except OSError:
            items.append(SyncItem("add", src, dst, rel))

    try:
        for dst_file in dst_root.rglob("*"):
            if dst_file.is_file() and dst_file not in expected_dsts:
                items.append(SyncItem("delete", dst_file, dst_file, dst_file.relative_to(dst_root)))
    except (PermissionError, FileNotFoundError):
        pass

    if not recompress_existing or current_preset is None:
        return items

    recompress_old_dsts: set[Path] = set()
    recompress_by_rel: dict[Path, tuple[str, Path | None]] = {}  # rel → (action, old_dst)

    for rel, src in src_files.items():
        if not needs_transcode(src):
            continue
        new_dst = dst_root / rel.with_suffix(output_ext)
        parent = new_dst.parent
        stem = new_dst.stem

        # Case 1: old file with a DIFFERENT extension exists (codec changed).
        # Use iterdir + string compare to avoid glob misinterpreting special chars
        # in filenames (e.g. "[Interlude]", "[Bonus Track]").
        old_file: Path | None = None
        try:
            for existing_file in parent.iterdir():
                if existing_file.is_file() and existing_file.stem == stem and existing_file != new_dst:
                    old_file = existing_file
                    recompress_old_dsts.add(existing_file)
                    break
        except (PermissionError, OSError):
            pass

        # Case 2: same extension but different preset (bitrate change within same codec).
        if old_file is None and prev_preset is not None and prev_preset != current_preset:
            if new_dst.exists() and prev_preset.ext == output_ext:
                old_file = new_dst  # same path — overwrite in place

        if old_file is not None:
            recompress_by_rel[rel] = ("reformat", old_file if old_file != new_dst else None)

    # Convert items to reformat.
    # Handles both "present" (file exists at new path) and "add" (codec change,
    # new-extension file doesn't exist yet but an old-extension file does).
    new_items: list[SyncItem] = []
    for item in items:
        if item.action in ("present", "add", "update") and item.rel in recompress_by_rel:
            action, old_dst = recompress_by_rel[item.rel]
            new_items.append(SyncItem(action, item.src, item.dst, item.rel, checked=True, old_dst=old_dst))
        elif item.action == "delete" and item.dst in recompress_old_dsts:
            pass  # Dropped — the recompress item handles deletion of the old file
        else:
            new_items.append(item)

    return new_items


def apply_sync_items(
    items: list[SyncItem],
    src_root: Path,
    dst_root: Path,
    prefix: str,
    codec: str,
    bitrate: str,
    output_ext: str,
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
                ok = process_file(item.src, src_root, dst_root, prefix, codec, bitrate, output_ext, log)
            elif item.action == "delete":
                ok = delete_orphan(item.dst, src_root, dst_root, prefix, log)
            elif item.action == "reformat":
                # Delete old file first (if different path), then transcode
                if item.old_dst and item.old_dst.exists():
                    _check_prefix(dst_root, prefix)
                    _check_not_source(item.old_dst, src_root)
                    try:
                        item.old_dst.unlink()
                    except Exception as e:
                        if log:
                            log("ERROR", f"could not delete old file {item.old_dst.name}: {e}")
                ok = transcode(item.src, item.dst, codec, bitrate, log)
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
