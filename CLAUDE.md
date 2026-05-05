# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with this repository.

**Keep this file updated after every major feature addition or architectural change.**

---

## Running the app

```bash
python main.py
```

**System requirements:** `ffmpeg` and `ffprobe` must be on `PATH`. Python 3.10+.

**Python dependencies:**
```bash
pip install textual watchdog pymtp
```

For MTP/Android support, `pymtp` requires `libmtp`:
```bash
sudo apt install libmtp-dev   # Debian/Ubuntu
sudo pacman -S libmtp          # Arch
```

There is no test suite and no linter configuration in this project.

---

## Architecture

The app is a [Textual](https://textual.textualize.io/) TUI. The entry point (`main.py`) validates `ffmpeg`/`ffprobe` and path safety, then launches `MusicMirrorApp`.

### Module responsibilities

| File | Role |
|---|---|
| `main.py` | Startup checks (ffmpeg, path overlap), launches `MusicMirrorApp` |
| `app.py` | Main Textual app: layout, keybindings, library tree, sync orchestration |
| `config.py` | `ConfigManager` — loads/saves `config.json`, typed property accessors |
| `mirror.py` | All sync logic: diff computation, transcoding, file copy/delete, safety guards |
| `browser.py` | `FileBrowserScreen` — modal directory picker with sidebar |
| `devices.py` | `detect_external_locations()` — scans `/media`, `/mnt`, gvfs MTP mounts |
| `watcher.py` | `FileWatcher` — wraps watchdog to notify on source file changes |
| `android.py` | `AndroidDevice` — pymtp wrapper; implemented but **not yet wired into sync** |

### Sync flow

`s` key → `action_sync()` → inline confirm bar shows stats (counts of add/update/delete) → user presses Confirm → `threading.Thread(_run_sync)` → `apply_sync_items()`

`r` key → `_do_scan()` (`@work(thread=True)`) → `build_sync_items()` → `_populate_tree()` rebuilds the tree on the main thread.

The actual sync runs in a background thread. All UI updates from that thread go through `call_from_thread()`. A `threading.Event` (`_cancel_event`) allows the `x` key to stop sync after the current file.

After each file completes, `apply_sync_items` calls `on_complete(item)`, which fires `_mark_item_done()` via `call_from_thread`. This sets `item.action = "present"` and updates the leaf and branch labels in the tree in real time.

### Key data type: `SyncItem` (`mirror.py`)

```python
@dataclass
class SyncItem:
    action: str   # "add", "update", "delete", "present"
    src: Path
    dst: Path
    rel: Path     # relative path — used for tree display
    checked: bool # whether user has selected this item to sync
```

`build_sync_items()` produces the full diff; `apply_sync_items()` executes only items where `checked=True` and `action != "present"`.

### Safety guards (enforced in `mirror.py` before every write/delete)

- `_check_prefix(dst_root, prefix)` — aborts if destination folder name doesn't start with `config["destination_prefix"]`
- `_check_not_source(path, src_root)` — aborts if the target path is inside the source directory
- Transcoding writes to a `.tmp` file, renamed to final path only on success; `.tmp` is deleted on failure

### Config (`config.json`)

Auto-generated next to `main.py` on first run. Key fields: `source`, `destinations` (list of `{name, path, type}`), `active_destination`, `destination_prefix`, `codec_preset`, `recompress_existing`.

`ConfigManager` exposes typed properties and saves automatically on mutation methods (`add_destination`, `remove_destination`, `set_active_destination`). Call `.save()` manually after `config.set()`.

**Codec Presets** (`mirror.py`): Five presets define compression quality:
- FLAC (Lossless) — no bitrate limit, full audio fidelity
- MP3 320kbps (High) — high quality, widely compatible
- MP3 192kbps (Medium) — balanced quality/size
- AAC 256kbps (High) — high quality, iOS-friendly
- AAC 128kbps (Low) — lowest quality, smallest files

Users select a preset via dropdown in the toolbar. The preset determines ffmpeg codec, bitrate, and output file extension.

**Re-compression** (`recompress_existing` config flag): When enabled (default), changing the codec preset automatically marks existing destination files (with old codecs) for deletion and their sources for re-transcoding. This allows converting a library from one format to another while keeping only the new format in the destination. When disabled, only new/updated source files are synced, leaving existing destination files untouched.

### MTP / Android status

`android.py` (`AndroidDevice`) and `devices.py` (`detect_external_locations`) are implemented. The file browser sidebar shows detected MTP mounts. However, `action_sync()` in `app.py` currently rejects non-local destinations with "MTP sync not yet supported" — the `AndroidDevice` class is not yet called during sync.
