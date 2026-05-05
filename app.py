from __future__ import annotations

import threading
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Label, Tree
from textual.widgets.tree import TreeNode

from browser import FileBrowserScreen
from config import ConfigManager
from mirror import SyncItem, apply_sync_items, build_sync_items, paths_overlap


_SPINNER = r"|\-/"
_ACTION_ICON = {"add": "+", "update": "↑", "delete": "×", "present": "✓"}
_ACTION_COLOR = {"add": "green", "update": "yellow", "delete": "red", "present": ""}


# ---------------------------------------------------------------------------
# Tree label helpers
# ---------------------------------------------------------------------------

def _branch_summary(items: list[SyncItem]) -> tuple[str, str]:
    """Return (icon, color) representing the worst-pending-action in a group."""
    changes = [i for i in items if i.action != "present"]
    if not changes:
        return "✓", ""
    if any(i.action == "add" for i in changes):
        return "+", "green"
    if any(i.action == "update" for i in changes):
        return "↑", "yellow"
    return "×", "red"


def _leaf_label(item: SyncItem) -> Text:
    icon = _ACTION_ICON.get(item.action, "?")
    color = _ACTION_COLOR.get(item.action, "")
    name = item.rel.name
    if color:
        return Text.assemble((f"  {icon}  ", f"bold {color}"), (name, color))
    return Text.assemble(("  ✓  ", "bold dim"), (name, "dim"))


def _branch_label(name: str, items: list[SyncItem]) -> Text:
    icon, color = _branch_summary(items)
    changes = [i for i in items if i.action != "present"]
    present = len(items) - len(changes)
    counts: dict[str, int] = {}
    for i in changes:
        counts[i.action] = counts.get(i.action, 0) + 1

    if not color:
        t = Text.assemble(("✓  ", "bold dim"), (name, "dim"))
    else:
        t = Text.assemble((f"{icon}  ", f"bold {color}"), (name, color))

    badges: list[tuple[str, str]] = []
    for action, badge_icon in (("add", "+"), ("update", "↑"), ("delete", "×")):
        if counts.get(action):
            badges.append((f"{badge_icon}{counts[action]}", _ACTION_COLOR[action]))
    if present:
        badges.append((f"·{present}", "dim"))

    if badges:
        t.append("   ")
        for j, (badge, bcol) in enumerate(badges):
            if j:
                t.append(" ")
            t.append(badge, f"bold {bcol}" if bcol != "dim" else "dim")

    return t


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class MusicMirrorApp(App):
    TITLE = "MusicMirror"
    CSS = """
    Screen { layout: vertical; }

    #header-bar {
        height: 6;
        border-bottom: solid $accent;
        background: $panel;
    }
    #src-row, #dest-row {
        height: 3;
        padding: 0 1;
        align: left middle;
    }
    #src-row {
        border-bottom: solid $panel;
    }
    .row-caption {
        width: 6;
        color: $text-muted;
    }
    #source-label, #dest-label {
        width: 1fr;
        color: $text;
    }
    .hdr-btn {
        min-width: 10;
        padding: 0 1;
        margin-left: 1;
    }

    #library-tree {
        height: 1fr;
        padding: 0 1;
    }

    #status-bar {
        height: 1;
        padding: 0 1;
        background: $panel;
        align: left middle;
    }
    #spinner-label {
        width: 2;
        color: $accent;
    }
    #sync-status {
        width: 1fr;
        color: $text-muted;
    }
    #watcher-label {
        color: $text-muted;
    }

    #confirm-bar {
        height: 3;
        padding: 0 1;
        border-top: solid $warning;
        background: $panel;
        align: left middle;
        display: none;
    }
    #confirm-msg { width: 1fr; }
    .confirm-btn {
        min-width: 14;
        padding: 0 1;
        margin-left: 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("s", "sync", "Sync"),
        Binding("x", "stop_sync", "Stop"),
        Binding("w", "toggle_watcher", "Watcher"),
        Binding("r", "rescan", "Rescan"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, config: ConfigManager) -> None:
        super().__init__()
        self.config = config
        self._watcher = None
        self._syncing = False
        self._cancel_event: threading.Event | None = None
        self._spinner_frame = 0
        self._pending_items: list[SyncItem] = []
        # Live lookup tables populated by _populate_tree
        self._leaf_map: dict[Path, TreeNode] = {}
        self._artist_nodes: dict[str, tuple[TreeNode, list[SyncItem]]] = {}
        self._album_nodes: dict[tuple[str, str], tuple[TreeNode, list[SyncItem]]] = {}

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        dest = self.config.get_active_destination()
        dest_path_str = dest["path"] if dest else "—"
        with Vertical(id="header-bar"):
            with Horizontal(id="src-row"):
                yield Label("Src:  ", classes="row-caption")
                yield Label(self.config.source or "—", id="source-label")
                yield Button("Change", id="change-source-btn", classes="hdr-btn")
            with Horizontal(id="dest-row"):
                yield Label("Dest: ", classes="row-caption")
                yield Label(dest_path_str, id="dest-label")
                yield Button("Change", id="change-dest-btn", classes="hdr-btn")
        yield Tree("Library", id="library-tree")
        with Horizontal(id="status-bar"):
            yield Label(" ", id="spinner-label")
            yield Label("Press r to scan", id="sync-status")
            yield Label("  Watcher: –", id="watcher-label")
        with Horizontal(id="confirm-bar"):
            yield Label("", id="confirm-msg")
            yield Button("Confirm ▶", variant="primary", id="confirm-btn", classes="confirm-btn")
            yield Button("Cancel", id="cancel-confirm-btn", classes="confirm-btn")
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one("#library-tree", Tree)
        tree.show_root = False
        self.set_interval(0.12, self._tick_spinner)
        self._update_watcher_label()
        if self.config.source and Path(self.config.source).exists():
            self._start_watcher()
            self._do_scan()
        else:
            self._set_status("No library — click Change to select one")

    # ------------------------------------------------------------------
    # Tree building
    # ------------------------------------------------------------------

    @work(thread=True)
    def _do_scan(self) -> None:
        self.call_from_thread(self._set_status, "Scanning…")
        dest = self.config.get_active_destination()
        if not dest or dest["type"] != "local":
            self.call_from_thread(self._set_status, "No local destination selected")
            return
        src_root = Path(self.config.source)
        dst_root = Path(dest["path"])
        try:
            items = build_sync_items(src_root, dst_root, self.config.output_ext)
        except Exception as e:
            self.call_from_thread(self._set_status, f"Scan error: {e}")
            return
        self.call_from_thread(self._on_scan_done, items)

    def _on_scan_done(self, items: list[SyncItem]) -> None:
        self._pending_items = items
        self._populate_tree(items)
        n = sum(1 for i in items if i.action != "present")
        self._set_status(f"{n} change(s) pending" if n else "Library up to date  ✓")

    def _populate_tree(self, items: list[SyncItem]) -> None:
        tree = self.query_one("#library-tree", Tree)
        tree.clear()
        self._leaf_map = {}
        self._artist_nodes = {}
        self._album_nodes = {}

        by_artist: dict[str, list[SyncItem]] = {}
        root_files: list[SyncItem] = []

        for item in items:
            if len(item.rel.parts) >= 2:
                by_artist.setdefault(item.rel.parts[0], []).append(item)
            else:
                root_files.append(item)

        for item in sorted(root_files, key=lambda i: i.rel.name.lower()):
            node = tree.root.add_leaf(_leaf_label(item), data=item)
            self._leaf_map[item.rel] = node

        for artist in sorted(by_artist.keys(), key=str.lower):
            a_items = by_artist[artist]
            has_changes = any(i.action != "present" for i in a_items)
            a_node = tree.root.add(
                _branch_label(artist, a_items),
                data=("artist", artist),
                expand=has_changes,
            )
            self._artist_nodes[artist] = (a_node, a_items)
            self._add_album_subnodes(a_node, artist, a_items)

    def _add_album_subnodes(self, parent: TreeNode, artist: str, items: list[SyncItem]) -> None:
        by_album: dict[str, list[SyncItem]] = {}
        direct: list[SyncItem] = []
        for item in items:
            if len(item.rel.parts) >= 3:
                by_album.setdefault(item.rel.parts[1], []).append(item)
            else:
                direct.append(item)

        for item in sorted(direct, key=lambda i: i.rel.name.lower()):
            node = parent.add_leaf(_leaf_label(item), data=item)
            self._leaf_map[item.rel] = node

        for album in sorted(by_album.keys(), key=str.lower):
            al_items = by_album[album]
            has_changes = any(i.action != "present" for i in al_items)
            al_node = parent.add(
                _branch_label(album, al_items),
                data=("album", artist, album),
                expand=has_changes,
            )
            self._album_nodes[(artist, album)] = (al_node, al_items)
            for item in sorted(al_items, key=lambda i: i.rel.name.lower()):
                leaf = al_node.add_leaf(_leaf_label(item), data=item)
                self._leaf_map[item.rel] = leaf

    def _mark_item_done(self, item: SyncItem) -> None:
        """Update tree node to ✓ after a file finishes syncing."""
        item.action = "present"
        leaf = self._leaf_map.get(item.rel)
        if leaf:
            leaf.set_label(_leaf_label(item))
        parts = item.rel.parts
        if len(parts) >= 2:
            artist = parts[0]
            if artist in self._artist_nodes:
                a_node, a_items = self._artist_nodes[artist]
                a_node.set_label(_branch_label(artist, a_items))
            if len(parts) >= 3:
                album = parts[1]
                key = (artist, album)
                if key in self._album_nodes:
                    al_node, al_items = self._album_nodes[key]
                    al_node.set_label(_branch_label(album, al_items))

    # ------------------------------------------------------------------
    # Status bar / spinner
    # ------------------------------------------------------------------

    def _set_status(self, msg: str) -> None:
        self.query_one("#sync-status", Label).update(msg)

    def _update_watcher_label(self) -> None:
        running = self._watcher and self._watcher.running
        self.query_one("#watcher-label", Label).update(
            f"  Watcher: {'On' if running else 'Off'}"
        )

    def _tick_spinner(self) -> None:
        if not self._syncing:
            self.query_one("#spinner-label", Label).update(" ")
            return
        frame = _SPINNER[self._spinner_frame % len(_SPINNER)]
        self.query_one("#spinner-label", Label).update(frame)
        self._spinner_frame += 1

    def _on_progress(self, done: int, total: int, track: str) -> None:
        if done >= total:
            self._set_status(f"Sync complete  ✓  —  {total} processed")
            return
        name = Path(track).name if track else ""
        self._set_status(f"({done}/{total})  {name}")

    # ------------------------------------------------------------------
    # Sync flow
    # ------------------------------------------------------------------

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

        changes = [i for i in self._pending_items if i.action != "present"]
        if not changes:
            if not self._pending_items:
                self.notify("Scan the library first (r).", severity="information")
            else:
                self.notify("Already up to date.", severity="information")
            return

        n_add = sum(1 for i in changes if i.action == "add")
        n_upd = sum(1 for i in changes if i.action == "update")
        n_del = sum(1 for i in changes if i.action == "delete")
        parts = []
        if n_add:
            parts.append(f"[green]+{n_add} to add[/]")
        if n_upd:
            parts.append(f"[yellow]↑{n_upd} to update[/]")
        if n_del:
            parts.append(f"[red]×{n_del} to delete[/]")
        self.query_one("#confirm-msg", Label).update("  ".join(parts))
        self.query_one("#confirm-bar").display = True

    def _run_sync(self, items: list[SyncItem], src_root: Path, dst_root: Path) -> None:
        self._syncing = True
        self._cancel_event = threading.Event()
        try:
            finished = apply_sync_items(
                items, src_root, dst_root,
                self.config.destination_prefix,
                self.config.ffmpeg_codec,
                self.config.ffmpeg_bitrate,
                self.config.output_ext,
                log=lambda tag, msg: (
                    self.call_from_thread(self.notify, msg, severity="error")
                    if tag == "ERROR" else None
                ),
                progress=lambda done, total, track: self.call_from_thread(
                    self._on_progress, done, total, track
                ),
                on_complete=lambda item: self.call_from_thread(self._mark_item_done, item),
                cancel=self._cancel_event,
            )
            if not finished:
                self.call_from_thread(self._set_status, "Sync stopped.")
        except Exception as e:
            self.call_from_thread(self._set_status, f"Sync error: {e}")
        finally:
            self._syncing = False
            self._cancel_event = None

    async def action_stop_sync(self) -> None:
        if not self._syncing or self._cancel_event is None:
            self.notify("No sync in progress.", severity="warning")
            return
        self._cancel_event.set()
        self._set_status("Stopping after current file…")

    async def action_rescan(self) -> None:
        if self._syncing:
            self.notify("Cannot rescan while syncing.", severity="warning")
            return
        if not self.config.source:
            self.notify("No library selected.", severity="error")
            return
        self._do_scan()

    # ------------------------------------------------------------------
    # Watcher
    # ------------------------------------------------------------------

    def _start_watcher(self) -> None:
        from watcher import FileWatcher
        src = Path(self.config.source)
        if not src.exists():
            return
        if self._watcher:
            self._watcher.stop()
        try:
            self._watcher = FileWatcher(src, self._on_file_change)
            self._watcher.start()
            self._update_watcher_label()
        except Exception as e:
            self.notify(f"Could not start watcher: {e}", severity="error")

    def _on_file_change(self, event_type: str, path: Path) -> None:
        self.call_from_thread(
            self._set_status, f"Change detected ({path.name}) — press r to rescan"
        )

    async def action_toggle_watcher(self) -> None:
        if self._watcher is None:
            self._start_watcher()
            return
        running = self._watcher.toggle()
        self._update_watcher_label()

    # ------------------------------------------------------------------
    # Destination management
    # ------------------------------------------------------------------

    async def action_quit_app(self) -> None:
        if self._syncing:
            self.notify("Sync in progress — stop it first (x).", severity="warning")
            return
        if self._watcher:
            self._watcher.stop()
        self.exit()

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    @on(Button.Pressed, "#change-source-btn")
    @work
    async def _change_source(self) -> None:
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
            self._start_watcher()
            self._pending_items = []
            self._do_scan()
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    @on(Button.Pressed, "#change-dest-btn")
    @work
    async def _change_dest(self) -> None:
        try:
            dest = self.config.get_active_destination()
            start = Path(dest["path"]) if dest else None
            path_str = await self.push_screen_wait(
                FileBrowserScreen(start_path=start, title="Select Destination Folder")
            )
            if not path_str:
                return
            dst = Path(path_str)
            prefix = self.config.destination_prefix
            if not dst.name.startswith(prefix):
                self.notify(
                    f"Folder must start with '{prefix}' (got '{dst.name}').",
                    severity="error",
                    timeout=8,
                )
                return
            if self.config.source and paths_overlap(Path(self.config.source), dst):
                self.notify("Destination overlaps with source.", severity="error")
                return
            if dest:
                self.config.update_destination_path(dest["name"], path_str)
            else:
                self.config.add_destination(dst.name, path_str, "local")
            self.query_one("#dest-label", Label).update(path_str)
            self._pending_items = []
            self._do_scan()
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    @on(Button.Pressed, "#confirm-btn")
    def _confirm_sync(self) -> None:
        self.query_one("#confirm-bar").display = False
        dest = self.config.get_active_destination()
        if not dest:
            return
        items = [i for i in self._pending_items if i.action != "present"]
        if not items:
            return
        src_root = Path(self.config.source)
        dst_root = Path(dest["path"])
        threading.Thread(
            target=self._run_sync, args=(items, src_root, dst_root), daemon=True
        ).start()

    @on(Button.Pressed, "#cancel-confirm-btn")
    def _cancel_confirm(self) -> None:
        self.query_one("#confirm-bar").display = False
        self._set_status("Sync cancelled.")

    def on_worker_state_changed(self, event) -> None:
        from textual.worker import WorkerState
        if event.state == WorkerState.ERROR:
            self.notify(f"Worker error: {event.worker.error}", severity="error")
