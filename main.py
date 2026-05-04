from __future__ import annotations

import sys
from pathlib import Path

from app import MusicMirrorApp, SetupScreen
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

    if not config.is_configured():
        _run_setup(config)
        return

    src = Path(config.source)
    if not src.exists():
        print(f"ERROR: Source path does not exist: {src}", file=sys.stderr)
        sys.exit(1)

    dest_paths = [Path(d["path"]) for d in config.destinations]
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


def _run_setup(config: ConfigManager) -> None:
    class SetupApp(MusicMirrorApp):
        async def on_mount(self) -> None:
            result = await self.push_screen_wait(SetupScreen())
            if result is None:
                self.exit()
                return
            config.initialize(result["source"], result["dest_name"], result["dest_path"])
            self.exit()

    SetupApp(config).run()
    if config.is_configured():
        MusicMirrorApp(config).run()


if __name__ == "__main__":
    main()
