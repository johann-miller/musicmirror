from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
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
    Static,
)

from config import ConfigManager
from mirror import (
    SyncPlan,
    build_sync_plan,
    delete_orphan,
    paths_overlap,
    process_file,
)
from watcher import FileWatcher


# ---------------------------------------------------------------------------
# Confirm-sync modal
# ---------------------------------------------------------------------------

class ConfirmSyncScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmSyncScreen {
        align: center middle;
    }
    #confirm-box {
        width: 60;
        height: auto;
        border: thick $warning;
        padding: 1 2;
        background: $surface;
    }
    #confirm-box Label { margin-bottom: 1; }
    """

    def __init__(self, dest_name: str, plan: SyncPlan) -> None:
        super().__init__()
        self._dest_name = dest_name
        self._plan = plan

    def compose(self) -> ComposeResult:
        with Container(id="confirm-box"):
            yield Label(f"Confirm Sync to {self._dest_name}")
            yield Static("")
            yield Label(f"ADD     {len(self._plan.add)} files")
            yield Label(f"UPDATE  {len(self._plan.update)} files")
            yield Label(f"DELETE  {len(self._plan.delete)} files")
            for f in self._plan.delete[:10]:
                yield Label(f"  - {f.name}")
            if len(self._plan.delete) > 10:
                yield Label(f"  ... and {len(self._plan.delete) - 10} more")
            yield Static("")
            with Horizontal():
                yield Button("Cancel", variant="default", id="cancel-btn")
                yield Button("Confirm", variant="error", id="confirm-btn")

    @on(Button.Pressed, "#confirm-btn")
    def confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-btn")
    def cancel(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Add destination modal
# ---------------------------------------------------------------------------

class AddDestScreen(ModalScreen[dict | None]):
    DEFAULT_CSS = """
    AddDestScreen { align: center middle; }
    #add-box {
        width: 60; height: auto;
        border: thick $accent; padding: 1 2; background: $surface;
    }
    """

    def __init__(self, prefix: str) -> None:
        super().__init__()
        self._prefix = prefix

    def compose(self) -> ComposeResult:
        from textual.widgets import Input
        with Container(id="add-box"):
            yield Label(f"Add Destination (name must start with '{self._prefix}')")
            yield Label("Name:")
            yield Input(placeholder=f"{self._prefix}new", id="name-input")
            yield Label("Path:")
            yield Input(placeholder="/path/to/dest", id="path-input")
            with Horizontal():
                yield Button("Cancel", variant="default", id="cancel-btn")
                yield Button("Add", variant="primary", id="add-btn")

    @on(Button.Pressed, "#add-btn")
    def add(self) -> None:
        from textual.widgets import Input
        name = self.query_one("#name-input", Input).value.strip()
        path = self.query_one("#path-input", Input).value.strip()
        if not name.startswith(self._prefix):
            self.notify(f"Name must start with '{self._prefix}'", severity="error")
            return
        if not path:
            self.notify("Path is required.", severity="error")
            return
        self.dismiss({"name": name, "path": path, "type": "local"})

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
    #queue-panel {
        height: 5;
        border: solid $accent;
        padding: 0 1;
    }
    #log-panel {
        border: solid $accent;
        padding: 0 1;
    }
    .panel-title {
        text-style: bold;
        color: $accent;
    }
    #dest-list { height: auto; }
    #dest-buttons { height: auto; }
    #queue-label { height: 1; }
    #progress { margin: 0 1; }
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
        self._queue: Queue[Path] = Queue()
        self._watcher: FileWatcher | None = None
        self._worker_thread: threading.Thread | None = None
        self._syncing = False
        self._current_file = ""
        self._pending_count = 0

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
        with Container(id="queue-panel"):
            yield Label("QUEUE", classes="panel-title")
            yield Label("Idle", id="queue-label")
            yield ProgressBar(total=100, show_eta=False, id="progress")
        with Container(id="log-panel"):
            yield Label("LOG", classes="panel-title")
            yield Log(id="log", auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_dest_list()
        if self.config.source and Path(self.config.source).exists():
            self.query_one("#change-source-btn", Button).label = "Change Library…"
            self._start_watcher()
            self._log("INFO", "Watching source for changes...")
        else:
            self._log("INFO", "No library selected — click 'Select Library…' to get started.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log(self, tag: str, msg: str) -> None:
        now = datetime.now().strftime("%H:%M")
        log = self.query_one("#log", Log)
        log.write_line(f"[{now}] {tag:<6} {msg}")

    def _refresh_dest_list(self) -> None:
        lv = self.query_one("#dest-list", ListView)
        lv.clear()
        active = self.config.active_destination
        for d in self.config.destinations:
            marker = "> " if d["name"] == active else "  "
            label = f"{marker}{d['name']} ({d['type']})"
            lv.append(ListItem(Label(label)))

    def _start_watcher(self) -> None:
        src = Path(self.config.source)
        if not src.exists():
            return
        if self._watcher:
            self._watcher.stop()
        try:
            self._watcher = FileWatcher(src, self._on_file_change)
            self._watcher.start()
        except Exception as e:
            self._log("ERROR", f"Could not start file watcher: {e}")
            self._watcher = None
            return
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()

    def _on_file_change(self, event_type: str, path: Path) -> None:
        if event_type == "deleted":
            self.call_from_thread(self._log, "WATCH", f"deleted: {path.name} (manual sync needed)")
            return
        self._queue.put(path)
        self._pending_count = self._queue.qsize()
        self.call_from_thread(self._update_queue_label)

    def _update_queue_label(self) -> None:
        label = self.query_one("#queue-label", Label)
        if self._current_file:
            label.update(f"Transcoding: {self._current_file}  |  Pending: {self._pending_count}")
        elif self._pending_count:
            label.update(f"Pending: {self._pending_count} files")
        else:
            label.update("Idle")

    def _worker_loop(self) -> None:
        while True:
            try:
                src = self._queue.get(timeout=1)
            except Empty:
                continue

            dest = self.config.get_active_destination()
            if not dest or dest["type"] != "local":
                self._queue.task_done()
                continue

            src_root = Path(self.config.source)
            dst_root = Path(dest["path"])

            self._current_file = src.name
            self._pending_count = self._queue.qsize()
            self.call_from_thread(self._update_queue_label)

            try:
                process_file(
                    src, src_root, dst_root,
                    self.config.destination_prefix,
                    self.config.ffmpeg_codec,
                    self.config.ffmpeg_bitrate,
                    self.config.output_ext,
                    log=lambda tag, msg: self.call_from_thread(self._log, tag, msg),
                )
            except Exception as e:
                self.call_from_thread(self._log, "ERROR", str(e))

            self._current_file = ""
            self._pending_count = self._queue.qsize()
            self.call_from_thread(self._update_queue_label)
            self._queue.task_done()

    def _run_sync(self, src_root: Path, dst_root: Path) -> None:
        self._syncing = True
        try:
            plan = build_sync_plan(src_root, dst_root, self.config.output_ext)

            for src in plan.add + plan.update:
                try:
                    process_file(
                        src, src_root, dst_root,
                        self.config.destination_prefix,
                        self.config.ffmpeg_codec,
                        self.config.ffmpeg_bitrate,
                        self.config.output_ext,
                        log=lambda tag, msg: self.call_from_thread(self._log, tag, msg),
                    )
                except Exception as e:
                    self.call_from_thread(self._log, "ERROR", str(e))

            for dst_file in plan.delete:
                try:
                    delete_orphan(
                        dst_file, src_root, dst_root,
                        self.config.destination_prefix,
                        log=lambda tag, msg: self.call_from_thread(self._log, tag, msg),
                    )
                except Exception as e:
                    self.call_from_thread(self._log, "ERROR", str(e))
        finally:
            self._syncing = False
            self.call_from_thread(self._log, "INFO", "Sync complete.")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def action_quit_app(self) -> None:
        if not self._queue.empty():
            self.notify("Queue is not empty. Press Q again to force quit.", severity="warning")
            return
        if self._watcher:
            self._watcher.stop()
        self.exit()

    async def action_sync(self) -> None:
        if self._syncing:
            self.notify("Sync already in progress.", severity="warning")
            return
        dest = self.config.get_active_destination()
        if not dest:
            self.notify("No active destination configured.", severity="error")
            return
        if dest["type"] != "local":
            self.notify("MTP sync via 's' not supported yet; use the UI.", severity="warning")
            return
        self._confirm_and_sync(dest)

    @work
    async def _confirm_and_sync(self, dest: dict) -> None:
        try:
            src_root = Path(self.config.source)
            dst_root = Path(dest["path"])
            plan = build_sync_plan(src_root, dst_root, self.config.output_ext)
            if plan.delete:
                confirmed = await self.push_screen_wait(ConfirmSyncScreen(dest["name"], plan))
                if not confirmed:
                    return
            self._log("INFO", f"Starting sync to {dest['name']}...")
            thread = threading.Thread(target=self._run_sync, args=(src_root, dst_root), daemon=True)
            thread.start()
        except Exception as e:
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
            self._log("INFO", "Watching source for changes...")
            return
        running = self._watcher.toggle()
        self._log("INFO", f"Watcher {'started' if running else 'stopped'}.")

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    @on(Button.Pressed, "#change-source-btn")
    @work
    async def change_source(self) -> None:
        from textual.widgets import Input

        class ChangeSourceScreen(ModalScreen[str | None]):
            DEFAULT_CSS = "ChangeSourceScreen { align: center middle; } #box { width: 60; height: auto; border: thick $accent; padding: 1 2; background: $surface; }"

            def compose(self) -> ComposeResult:
                with Container(id="box"):
                    yield Label("New source path:")
                    yield Input(placeholder="/path/to/lossless", id="src-input")
                    with Horizontal():
                        yield Button("Cancel", variant="default", id="cancel")
                        yield Button("Set", variant="primary", id="set")

            @on(Button.Pressed, "#set")
            def do_set(self) -> None:
                val = self.query_one("#src-input", Input).value.strip()
                self.dismiss(val or None)

            @on(Button.Pressed, "#cancel")
            def do_cancel(self) -> None:
                self.dismiss(None)

        try:
            result = await self.push_screen_wait(ChangeSourceScreen())
            if result:
                p = Path(result)
                if not p.exists():
                    self.notify(f"Path does not exist: {result}", severity="error")
                    return
                all_paths = [Path(d["path"]) for d in self.config.destinations]
                if any(paths_overlap(p, dp) for dp in all_paths):
                    self.notify("Source overlaps with a destination.", severity="error")
                    return
                self.config.set("source", result)
                self.config.save()
                self.query_one("#source-label", Label).update(result)
                self.query_one("#change-source-btn", Button).label = "Change Library…"
                self._start_watcher()
                self._log("INFO", f"Library set to {result}")
                self._log("INFO", "Watching source for changes...")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    @on(Button.Pressed, "#add-dest-btn")
    @work
    async def add_destination(self) -> None:
        try:
            result = await self.push_screen_wait(AddDestScreen(self.config.destination_prefix))
            if not result:
                return

            src = Path(self.config.source)
            dst = Path(result["path"])
            if paths_overlap(src, dst):
                self.notify("Destination overlaps with source.", severity="error")
                return
            for d in self.config.destinations:
                if paths_overlap(dst, Path(d["path"])):
                    self.notify("Destination overlaps with existing destination.", severity="error")
                    return

            self.config.add_destination(result["name"], result["path"], result["type"])
            self._refresh_dest_list()
            self._log("INFO", f"Added destination: {result['name']}")
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
            self._log("ERROR", f"Unexpected error in worker: {event.worker.error}")
