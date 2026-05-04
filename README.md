# MusicMirror

A terminal UI application that mirrors a lossless music library to one or more compressed destinations, transcoding on-the-fly with ffmpeg.

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` on PATH
- Dependencies: `textual`, `watchdog`, `pymtp`

## Install

```bash
pip install textual watchdog pymtp
```

For Android/MTP support, `pymtp` requires `libmtp` on the system:

```bash
# Debian/Ubuntu
sudo apt install libmtp-dev

# Arch
sudo pacman -S libmtp
```

## Run

```bash
python main.py
```

On first run a setup wizard collects the source path and first destination.

## Config (`config.json`)

Auto-generated on first run. Edit manually to add destinations or change codec settings.

| Field | Description |
|---|---|
| `source` | Path to lossless music library (read-only) |
| `destinations` | List of sync targets (`name`, `path`, `type`) |
| `active_destination` | Name of the currently selected destination |
| `destination_prefix` | Safety prefix all destination folder names must match |
| `ffmpeg_codec` | Audio codec passed to ffmpeg (default: `aac`) |
| `ffmpeg_bitrate` | Target bitrate (default: `256k`) |
| `output_ext` | Output file extension for transcoded files (default: `.m4a`) |

## Keybindings

| Key | Action |
|---|---|
| `q` | Quit (prompts if queue is not empty) |
| `s` | Trigger manual sync to active destination |
| `d` | Cycle to next destination |
| `w` | Toggle file watcher on/off |

## Supported File Types

| Extension | Treatment |
|---|---|
| `.flac`, `.wav`, `.aiff`, `.aif` | Transcode to AAC |
| `.m4a` (ALAC) | Transcode to AAC |
| `.m4a` (AAC) | Copy as-is |
| `.mp3`, `.aac`, `.ogg`, `.opus`, `.wma` | Copy as-is |
| Everything else (`.jpg`, `.png`, `.cue`, …) | Copy as-is |

## Safety Rules

- **Source is read-only.** Nothing in the source directory is ever modified or deleted.
- **Destination prefix guard.** Writes are refused if the destination folder name doesn't start with `destination_prefix`.
- **Deletion confirmation.** Orphan cleanup requiring file deletion shows a diff and requires explicit confirmation.
- **Non-overlapping paths.** Startup aborts if source and any destination share a path prefix.
- **Atomic transcoding.** ffmpeg writes to a `.tmp` file, renamed to final path only on success.
