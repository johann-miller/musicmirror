from __future__ import annotations

import sys
from pathlib import Path

from app import MusicMirrorApp
from config import ConfigManager
from mirror import check_ffmpeg, check_ffprobe, paths_overlap


def main() -> None:
    if not check_ffmpeg():
        print("ERROR: ffmpeg not found on PATH. Please install ffmpeg and try again.", file=sys.stderr)
        sys.exit(1)
    if not check_ffprobe():
        print("ERROR: ffprobe not found on PATH. Please install ffprobe and try again.", file=sys.stderr)
        sys.exit(1)

    config = ConfigManager()

    src = Path(config.source) if config.source else None
    dest_paths = [Path(d["path"]) for d in config.destinations]

    if src and dest_paths:
        for dp in dest_paths:
            if paths_overlap(src, dp):
                print(f"ERROR: Source and destination overlap: {src} / {dp}", file=sys.stderr)
                sys.exit(1)
        for i, dp1 in enumerate(dest_paths):
            for dp2 in dest_paths[i + 1:]:
                if paths_overlap(dp1, dp2):
                    print(f"ERROR: Destinations overlap each other: {dp1} / {dp2}", file=sys.stderr)
                    sys.exit(1)

    MusicMirrorApp(config).run()


if __name__ == "__main__":
    main()
