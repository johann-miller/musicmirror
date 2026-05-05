from __future__ import annotations

from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer


class _Handler(FileSystemEventHandler):
    def __init__(self, on_change: Callable[[str, Path], None]) -> None:
        super().__init__()
        self._on_change = on_change

    def on_created(self, event: FileSystemEvent) -> None:
        # Fire for both files and folders — a new folder is a meaningful change
        self._on_change("created", Path(str(event.src_path)))

    def on_modified(self, event: FileSystemEvent) -> None:
        # Skip directory-modified events: they fire constantly as files inside change
        if not event.is_directory:
            self._on_change("modified", Path(str(event.src_path)))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._on_change("moved", Path(str(event.dest_path)))

    def on_deleted(self, event: FileSystemEvent) -> None:
        # Fire for both files and folders
        self._on_change("deleted", Path(str(event.src_path)))


class FileWatcher:
    def __init__(self, src_root: Path, on_change: Callable[[str, Path], None]) -> None:
        self.src_root = src_root
        self._on_change = on_change
        self._observer: Observer | None = None
        self.running = False

    def start(self) -> None:
        if self.running:
            return
        self._observer = Observer()
        self._observer.schedule(_Handler(self._on_change), str(self.src_root), recursive=True)
        self._observer.start()
        self.running = True

    def stop(self) -> None:
        if not self.running or self._observer is None:
            return
        self._observer.stop()
        self._observer.join()
        self._observer = None
        self.running = False

    def toggle(self) -> bool:
        if self.running:
            self.stop()
        else:
            self.start()
        return self.running
