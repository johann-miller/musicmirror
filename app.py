from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from textual import on, work
from textual.worker import Worker
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    ListView,
    ListItem,
    Log,
    ProgressBar,
)

from browser import FileBrowserScreen
from config import ConfigManager
from mirror import SyncItem, apply_sync_items, build_sync_items, paths_overlap
from sync_preview import SyncPreviewScreen
from watcher import FileWatcher


# ---------------------------------------------------------------------------
# Name-destination modal
# ---------------------------------------------------------------------------

class NameDestScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    NameDestScreen { align: center middle; }
    #name-box {
        width: 60; height: auto;
        border: thick $accent; padding: 1 2; background: $surface;
    }
    #name-box Label { margin-bottom: 1; }
    """

    def __init__(self, suggested: str) -> None:
        super().__init__()
        self._suggested = suggested

    def compose(self) -> ComposeResult:
        from textual.widgets import Input
        with Container(id="name-box"):
            yield Label("Name this destination:")
            yield Input(self._suggested, id="name-input")
            with Horizontal():
                yield Button("Cancel", id="cancel-btn")
                yield Button("Add", variant="primary", id="add-btn")

    @on(Button.Pressed, "#add-btn")
    def add(self) -> None:
        from textual.widgets import Input
        name = self.query_one("#name-input", Input).value.strip()
        if name:
            self.dismiss(name)

    @on(Button.Pressed, "#cancel-btn")
    def cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class MusicMirrorApp(App):
    TITLE = "MusicMirror"
    CSS = """
    Screen { layers: base overlay; }

    #source-panel {
        width: 35%;
        border: solid $accent;
        padding: 1;
    }
    #dest-panel {
        width: 65%;
        border: solid $accent;
        padding: 1;
    }
    #top-row { height: auto; }
    #status-panel {
        height: 3;
        border: solid $accent;
        padding: 0 1;
        align: left middle;
    }
    #progress-panel {
        height: 5;
        border: solid $accent;
        padding: 0 1;
    }
    #sync-status {
        color: $text-muted;
    }
    #log-panel {
        border: solid $accent;
        padding: 0 1;
    }
    .panel-title {
        text-style: bold;
        color: $accent;
        margin-right: 2;
    }
    #dest-list { height: auto; }
    #dest-buttons { height: auto; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit_app", "Quit"),
        Binding("s", "sync", "Sync"),
        Binding("d", "switch_dest", "Switch Dest"),
        Binding("w", "toggle_watcher", "Watcher"),
    ]

    def __init__(self, config: ConfigManager) -> None:
        super().__init__()
        self.config = config
        self._watcher: FileWatcher | None = None
        self._syncing = False
        self._last_sync = "Never"

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="top-row"):
            with Vertical(id="source-panel"):
                yield Label("SOURCE", classes="panel-title")
                yield Label(self.config.source or "No library selected", id="source-label")
                yield Button("Select Library…", id="change-source-btn", variant="primary")
            with Vertical(id="dest-panel"):
                yield Label("DESTINATIONS", classes="panel-title")
                yield ListView(id="dest-list")
                with Horizontal(id="dest-buttons"):
                    yield Button("Add", id="add-dest-btn", variant="primary")
                    yield Button("Remove", id="remove-dest-btn", variant="default")
                    yield Button("Set Active", id="set-active-btn", variant="default")
        with Horizontal(id="status-panel"):
            yield Label("STATUS", classes="panel-title")
            yield Label("Watcher: Stopped  |  Last sync: Never", id="status-label")
        with Container(id="progress-panel"):
            yield Label("PROGRESS", classes="panel-title")
            yield ProgressBar(id="progress-bar", show_eta=False)
            yield Label("Idle", id="sync-status")
        with Container(id="log-panel"):
            yield Label("LOG", classes="panel-title")
            yield Log(id="log", auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_dest_list()
        if self.config.source and Path(self.config.source).exists():
            self.query_one("#change-source-btn", Button).label = "Change Library…"
            self._start_watcher()
        else:
            self._log("INFO", "No library selected — click 'Select Library…' to get started.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log(self, tag: str, msg: str) -> None:
        now = datetime.now().strftime("%H:%M")
        self.query_one("#log", Log).write_line(f"[{now}] {tag:<6} {msg}")

    def _refresh_dest_list(self) -> None:
        lv = self.query_one("#dest-list", ListView)
        lv.clear()
        active = self.config.active_destination
        for d in self.config.destinations:
            marker = "> " if d["name"] == active else "  "
            lv.append(ListItem(Label(f"{marker}{d['name']} ({d['type']})")))

    def _update_status(self) -> None:
        watcher = "Active" if (self._watcher and self._watcher.running) else "Stopped"
        self.query_one("#status-label", Label).update(
            f"Watcher: {watcher}  |  Last sync: {self._last_sync}"
        )

    def _set_sync_status(self, msg: str) -> None:
        self.query_one("#sync-status", Label).update(msg)

    def _update_progress(self, done: int, total: int, current: str) -> None:
        bar = self.query_one("#progress-bar", ProgressBar)
        bar.update(total=total, progress=done)
        if done >= total:
            self._set_sync_status(f"Complete — {total} item(s) processed")
        else:
            self._set_sync_status(f"({done}/{total})  {current}")

    def _start_watcher(self) -> None:
        src = Path(self.config.source)
        if not src.exists():
            return
        if self._watcher:
            self._watcher.stop()
        try:
            self._watcher = FileWatcher(src, self._on_file_change)
            self._watcher.start()
            self._update_status()
            self._log("INFO", "Watching source for changes…")
        except Exception as e:
            self._log("ERROR", f"Could not start file watcher: {e}")
            self._watcher = None

    def _on_file_change(self, event_type: str, path: Path) -> None:
        msgs = {
            "created": f"New in source: {path.name}",
            "modified": f"Modified in source: {path.name}",
            "moved": f"Moved in source: {path.name}",
            "deleted": f"Removed from source: {path.name}",
        }
        self.call_from_thread(self._log, "WATCH", msgs.get(event_type, f"{event_type}: {path.name}"))

    def _run_sync(self, items: list[SyncItem], src_root: Path, dst_root: Path) -> None:
        self._syncing = True
        try:
            apply_sync_items(
                items, src_root, dst_root,
                self.config.destination_prefix,
                self.config.ffmpeg_codec,
                self.config.ffmpeg_bitrate,
                self.config.output_ext,
                log=lambda tag, msg: self.call_from_thread(self._log, tag, msg),
                progress=lambda done, total, track: self.call_from_thread(
                    self._update_progress, done, total, track
                ),
            )
            self._last_sync = datetime.now().strftime("%H:%M")
            self.call_from_thread(self._update_status)
            self.call_from_thread(self._log, "INFO", "Sync complete.")
        except Exception as e:
            self.call_from_thread(self._log, "ERROR", f"Sync failed: {e}")
            self.call_from_thread(self._set_sync_status, "Sync failed")
        finally:
            self._syncing = False

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def action_quit_app(self) -> None:
        if self._syncing:
            self.notify("Sync in progress — wait for it to finish or press Ctrl+C.", severity="warning")
            return
        if self._watcher:
            self._watcher.stop()
        self.exit()

    async def action_sync(self) -> None:
        if self._syncing:
            self.notify("Sync already in progress.", severity="warning")
            return
        if not self.config.source:
            self.notify("No library selected.", severity="error")
            return
        dest = self.config.get_active_destination()
        if not dest:
            self.notify("No active destination configured.", severity="error")
            return
        if dest["type"] != "local":
            self.notify("MTP sync not yet supported.", severity="warning")
            return
        self._open_sync_preview(dest)

    @work
    async def _open_sync_preview(self, dest: dict) -> None:
        try:
            src_root = Path(self.config.source)
            dst_root = Path(dest["path"])
            self._set_sync_status("Scanning for changes…")
            self._log("INFO", "Scanning for changes…")
            items = build_sync_items(src_root, dst_root, self.config.output_ext)
            if not items:
                self.notify("Already up to date.", severity="information")
                self._log("INFO", "Already up to date.")
                self._set_sync_status("Already up to date")
                return
            selected = await self.push_screen_wait(SyncPreviewScreen(dest["name"], items))
            if not selected:
                self._log("INFO", "Sync cancelled.")
                self._set_sync_status("Idle")
                return
            self._log("INFO", f"Mirroring {len(selected)} change(s) to {dest['name']}…")
            self.query_one("#progress-bar", ProgressBar).update(total=len(selected), progress=0)
            threading.Thread(
                target=self._run_sync,
                args=(selected, src_root, dst_root),
                daemon=True,
            ).start()
        except Exception as e:
            self._set_sync_status("Idle")
            self.notify(f"Sync error: {e}", severity="error")

    async def action_switch_dest(self) -> None:
        dests = self.config.destinations
        if len(dests) < 2:
            self.notify("Only one destination configured.", severity="warning")
            return
        current = self.config.active_destination
        names = [d["name"] for d in dests]
        idx = names.index(current) if current in names else 0
        next_name = names[(idx + 1) % len(names)]
        self.config.set_active_destination(next_name)
        self._refresh_dest_list()
        self._log("INFO", f"Active destination: {next_name}")

    async def action_toggle_watcher(self) -> None:
        if self._watcher is None:
            self._start_watcher()
            return
        running = self._watcher.toggle()
        self._update_status()
        self._log("INFO", f"Watcher {'started' if running else 'stopped'}.")

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    @on(Button.Pressed, "#change-source-btn")
    @work
    async def change_source(self) -> None:
        try:
            start = Path(self.config.source) if self.config.source else None
            result = await self.push_screen_wait(
                FileBrowserScreen(start_path=start, title="Select Music Library")
            )
            if not result:
                return
            p = Path(result)
            if any(paths_overlap(p, Path(d["path"])) for d in self.config.destinations):
                self.notify("Source overlaps with a destination.", severity="error")
                return
            self.config.set("source", result)
            self.config.save()
            self.query_one("#source-label", Label).update(result)
            self.query_one("#change-source-btn", Button).label = "Change Library…"
            self._start_watcher()
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    @on(Button.Pressed, "#add-dest-btn")
    @work
    async def add_destination(self) -> None:
        try:
            prefix = self.config.destination_prefix
            path_str = await self.push_screen_wait(
                FileBrowserScreen(title="Select Destination Folder")
            )
            if not path_str:
                return
            dst = Path(path_str)
            if not dst.name.startswith(prefix):
                self.notify(
                    f"Destination folder must be named '{prefix}…' (got '{dst.name}').\n"
                    f"Rename or create a folder starting with '{prefix}'.",
                    severity="error",
                    timeout=8,
                )
                return
            if self.config.source and paths_overlap(Path(self.config.source), dst):
                self.notify("Destination overlaps with source.", severity="error")
                return
            for d in self.config.destinations:
                if paths_overlap(dst, Path(d["path"])):
                    self.notify("Destination overlaps with an existing destination.", severity="error")
                    return
            name = await self.push_screen_wait(NameDestScreen(dst.name))
            if not name:
                return
            self.config.add_destination(name, path_str, "local")
            self._refresh_dest_list()
            self._log("INFO", f"Added destination: {name}")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    @on(Button.Pressed, "#remove-dest-btn")
    async def remove_destination(self) -> None:
        lv = self.query_one("#dest-list", ListView)
        if lv.index is None:
            self.notify("Select a destination first.", severity="warning")
            return
        dests = self.config.destinations
        if lv.index >= len(dests):
            return
        name = dests[lv.index]["name"]
        try:
            self.config.remove_destination(name)
            self._refresh_dest_list()
            self._log("INFO", f"Removed destination: {name}")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    @on(Button.Pressed, "#set-active-btn")
    async def set_active_destination(self) -> None:
        lv = self.query_one("#dest-list", ListView)
        if lv.index is None:
            self.notify("Select a destination first.", severity="warning")
            return
        dests = self.config.destinations
        if lv.index >= len(dests):
            return
        name = dests[lv.index]["name"]
        try:
            self.config.set_active_destination(name)
            self._refresh_dest_list()
            self._log("INFO", f"Active destination: {name}")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        from textual.worker import WorkerState
        if event.state == WorkerState.ERROR:
            self._log("ERROR", f"Unexpected worker error: {event.worker.error}")
