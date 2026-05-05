from __future__ import annotations

import threading
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Label, Static, Tree
from textual.widgets.tree import TreeNode

from textual.theme import Theme

from browser import FileBrowserScreen
from config import ConfigManager
from mirror import SyncItem, apply_sync_items, build_sync_items, paths_overlap


# ---------------------------------------------------------------------------
# Custom themes  (built-ins are unregistered at startup)
# ---------------------------------------------------------------------------

_HACKER_THEME = Theme(
    name="hacker",
    dark=True,
    background="#000000",
    surface="#0d0d0d",
    panel="#141414",
    primary="#00ff88",
    secondary="#00ccff",
    accent="#00ff88",
    warning="#ffdd00",
    error="#ff2244",
    success="#00ff88",
    foreground="#e0ffe0",
)

_LIGHT_THEME = Theme(
    name="light",
    dark=False,
    background="#ffffff",
    surface="#f5f5f5",
    panel="#e8e8e8",
    primary="#1a6bc5",
    secondary="#2e86ab",
    accent="#1a6bc5",
    warning="#e67e00",
    error="#cc2200",
    success="#1a8000",
    foreground="#1a1a1a",
)

_GREYSCALE_THEME = Theme(
    name="greyscale",
    dark=True,
    background="#141414",
    surface="#1e1e1e",
    panel="#282828",
    primary="#c8c8c8",
    secondary="#909090",
    accent="#c8c8c8",
    warning="#a0a0a0",
    error="#787878",
    success="#c8c8c8",
    foreground="#d8d8d8",
)

_CUSTOM_THEMES = [_HACKER_THEME, _LIGHT_THEME, _GREYSCALE_THEME]
_THEME_CYCLE = ["hacker", "light", "greyscale"]

_SPINNER = r"|\-/"
_NODE_SPINNER = "◐◓◑◒"
_ACTION_COLOR = {"add": "green", "update": "yellow", "delete": "red"}
_BADGE = {"add": "+", "update": "↑", "delete": "×"}

_LYRIC_EXTS = {".lrc"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}


def _item_kind(item: SyncItem) -> str:
    ext = item.rel.suffix.lower()
    if ext in _LYRIC_EXTS:
        return "lyric"
    if ext in _IMAGE_EXTS:
        return "cover"
    return "audio"


# ---------------------------------------------------------------------------
# Tree label helpers
# ---------------------------------------------------------------------------
#
# Selection state is communicated through BOTH shape AND color so the UI
# remains legible for users with color vision deficiency:
#
#   ✓  already in destination  (dim — no action needed)
#   ●  pending + selected       (filled circle, action color)
#   ○  pending + deselected     (empty circle, dim)
#   ~  branch: mixed selection  (tilde, yellow)

def _lyric_frag(lyric_item: SyncItem | None) -> list[tuple[str, str]]:
    if lyric_item is None:
        return []
    if lyric_item.action == "present":
        return [(" ♫", "dim")]
    return [(" ♫", f"bold {_ACTION_COLOR.get(lyric_item.action, 'white')}")]


def _leaf_label(item: SyncItem, lyric_item: SyncItem | None = None) -> Text:
    lf = _lyric_frag(lyric_item)
    if item.action == "present":
        return Text.assemble(("  ✓  ", "bold dim"), (item.rel.name, "dim"), *lf)
    badge = _BADGE.get(item.action, "?")
    color = _ACTION_COLOR.get(item.action, "white")
    if item.checked:
        return Text.assemble(
            ("  ●  ", f"bold {color}"),
            (item.rel.name, color),
            *lf,
            (f"  {badge}", f"bold {color}"),
        )
    return Text.assemble(
        ("  ○  ", "dim"),
        (item.rel.name, "dim"),
        *lf,
        (f"  {badge}", "dim"),
    )


def _active_leaf_label(item: SyncItem, frame: str, lyric_item: SyncItem | None = None) -> Text:
    color = _ACTION_COLOR.get(item.action, "white")
    badge = _BADGE.get(item.action, "?")
    return Text.assemble(
        (f"  {frame}  ", f"bold {color}"),
        (item.rel.name, f"bold {color}"),
        *_lyric_frag(lyric_item),
        (f"  {badge}", f"bold {color}"),
    )


def _cover_frag(cover_item: SyncItem | None) -> list[tuple[str, str]]:
    if cover_item is None:
        return []
    if cover_item.action == "present":
        return [("  ◈", "dim")]
    return [("  ◈", f"bold {_ACTION_COLOR.get(cover_item.action, 'white')}")]


def _branch_label(
    name: str,
    items: list[SyncItem],
    cover_item: SyncItem | None = None,
) -> Text:
    changes = [i for i in items if i.action != "present"]
    present = len(items) - len(changes)

    if not changes:
        t = Text.assemble(("  ✓  ", "bold dim"), (name, "dim"))
        if present:
            t.append(f"  ·{present}", "dim")
        for frag in _cover_frag(cover_item):
            t.append(*frag)
        return t

    n_sel = sum(1 for i in changes if i.checked)
    if n_sel == 0:
        sym, sym_style, name_style = "○", "dim", "dim"
    elif n_sel == len(changes):
        if any(i.action == "add" for i in changes):
            c = "green"
        elif any(i.action == "update" for i in changes):
            c = "yellow"
        else:
            c = "red"
        sym, sym_style, name_style = "●", f"bold {c}", c
    else:
        sym, sym_style, name_style = "~", "bold yellow", "yellow"

    t = Text.assemble((f"  {sym}  ", sym_style), (name, name_style))

    counts: dict[str, int] = {}
    for i in changes:
        counts[i.action] = counts.get(i.action, 0) + 1
    badges: list[tuple[str, str]] = []
    for action, badge in (("add", "+"), ("update", "↑"), ("delete", "×")):
        if counts.get(action):
            badges.append((f"{badge}{counts[action]}", _ACTION_COLOR[action]))
    if present:
        badges.append((f"·{present}", "dim"))
    if badges:
        t.append("   ")
        for j, (b, bcol) in enumerate(badges):
            if j:
                t.append(" ")
            t.append(b, f"bold {bcol}" if bcol != "dim" else "dim")
    for frag in _cover_frag(cover_item):
        t.append(*frag)
    return t


# ---------------------------------------------------------------------------
# Help screen
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
[bold]KEYBOARD SHORTCUTS[/bold]
[dim]────────────────────────────────[/dim]
 [bold cyan]r[/bold cyan]      Rescan — recomputes which files need syncing
 [bold cyan]s[/bold cyan]      Sync — shows a count of selected changes, then confirms before starting
 [bold cyan]x[/bold cyan]      Stop — halts an in-progress sync after the current file finishes
 [bold cyan]Space[/bold cyan]  Select / deselect the highlighted item for sync
 [bold cyan]w[/bold cyan]      Toggle the file watcher on or off
 [bold cyan]h[/bold cyan]      Open this help screen
 [bold cyan]q[/bold cyan]      Quit (blocked while a sync is running)


[bold]NAVIGATING THE LIBRARY TREE[/bold]
[dim]────────────────────────────────[/dim]
 [bold]↑ / ↓[/bold]    Move the cursor up and down through artists, albums, and tracks
 [bold]Enter[/bold]     Expand or collapse the highlighted artist or album node
 [bold]Space[/bold]     Toggle selection on the highlighted track, album, or artist
           Toggling an artist or album toggles all tracks inside it at once


[bold]SYNC STATUS INDICATORS[/bold]
[dim]────────────────────────────────[/dim]
 [bold dim]✓[/bold dim]   Already present in the destination — nothing to do
 [bold green]●[/bold green]   Selected for sync  ([green]green[/green] = add  [yellow]yellow[/yellow] = update  [red]red[/red] = delete)
 [dim]○[/dim]   In source but not destination, and [bold]not[/bold] selected — will be skipped this sync
 [bold yellow]~[/bold yellow]   Artist or album that has a mix of selected and deselected tracks


[bold]FILE WATCHER[/bold]
[dim]────────────────────────────────[/dim]
 The file watcher monitors your source library folder in the background.
 When files are created, modified, moved, or deleted in the source, a notice
 appears in the status bar at the bottom. The library tree is [bold]not[/bold] updated
 automatically — press [bold cyan]r[/bold cyan] to rescan and see the updated diff.
 Toggle the watcher on or off with [bold cyan]w[/bold cyan]. It starts automatically on launch
 if a source folder is configured.


[bold]HEADER CONTROLS[/bold]
[dim]────────────────────────────────[/dim]
 [bold]Src  [Change][/bold]   Open a directory browser to select your lossless source library
 [bold]Dest [Change][/bold]   Open a directory browser to select the compressed destination folder
                The destination folder name must begin with the configured prefix
                (default: [dim]compressed_[/dim]) — this is a safety guard that prevents
                accidentally syncing into an unintended folder

 [bold]↕ Collapse All[/bold]  Fold all artist and album nodes for a compact overview
 [bold]↕ Expand All[/bold]    Unfold all nodes so every track is visible
"""


class HelpScreen(ModalScreen):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help-box {
        width: 72;
        height: 85%;
        border: thick $accent;
        background: $surface;
    }
    #help-title {
        height: 3;
        padding: 0 2;
        background: $accent;
        color: $background;
        text-style: bold;
        align: left middle;
    }
    #help-scroll { padding: 1 2; }
    #help-footer {
        height: 3;
        padding: 0 2;
        border-top: solid $panel;
        align: right middle;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("h", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Label("MusicMirror — Help", id="help-title")
            with VerticalScroll(id="help-scroll"):
                yield Static(_HELP_TEXT)
            with Horizontal(id="help-footer"):
                yield Button("Close", variant="primary", id="close-help-btn")

    @on(Button.Pressed, "#close-help-btn")
    def _close(self) -> None:
        self.dismiss()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class MusicMirrorApp(App):
    TITLE = "MusicMirror"
    ENABLE_COMMAND_PALETTE = False
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
    #source-label, #dest-label {
        width: 1fr;
        color: $text;
    }
    .hdr-btn {
        min-width: 10;
        padding: 0 1;
        margin-left: 1;
    }

    #toolbar {
        height: 3;
        padding: 0 1;
        border-bottom: solid $panel;
        background: $panel;
        align: left middle;
    }
    #toolbar Button {
        min-width: 16;
        margin-right: 1;
    }

    #notification-bar {
        height: 1;
        padding: 0 1;
        display: none;
    }
    #notification-bar.-notify-warn {
        display: block;
        background: $warning 15%;
        color: $warning;
    }
    #notification-bar.-notify-error {
        display: block;
        background: $error 15%;
        color: $error;
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
        Binding("space", "toggle_selection", "Select/Deselect", priority=True),
        Binding("w", "toggle_watcher", "Watcher"),
        Binding("r", "rescan", "Rescan"),
        Binding("h", "open_help", "Help"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, config: ConfigManager) -> None:
        super().__init__()
        self.config = config
        self._watcher = None
        self._syncing = False
        self._cancel_event: threading.Event | None = None
        self._spinner_frame = 0
        self._active_item: SyncItem | None = None
        self._pending_items: list[SyncItem] = []
        # Live lookup tables populated by _populate_tree
        self._leaf_map: dict[Path, TreeNode] = {}
        self._artist_nodes: dict[str, tuple[TreeNode, list[SyncItem]]] = {}
        self._album_nodes: dict[tuple[str, str], tuple[TreeNode, list[SyncItem]]] = {}
        self._track_lyrics: dict[Path, SyncItem] = {}   # audio rel → its .lrc SyncItem
        self._album_cover: dict[tuple[str, str], SyncItem] = {}  # (artist, album) → cover SyncItem

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        dest = self.config.get_active_destination()
        dest_path_str = dest["path"] if dest else "—"
        with Vertical(id="header-bar"):
            with Horizontal(id="src-row"):
                yield Button("Select SRC", id="change-source-btn", classes="hdr-btn")
                yield Label(self.config.source or "—", id="source-label")
            with Horizontal(id="dest-row"):
                yield Button("Select DEST", id="change-dest-btn", classes="hdr-btn")
                yield Label(dest_path_str, id="dest-label")
        with Horizontal(id="toolbar"):
            yield Button("↕ Collapse All", id="collapse-all-btn")
            yield Button("↕ Expand All", id="expand-all-btn")
            yield Button("✓ Collapse Synced", id="collapse-synced-btn")
            yield Button("◐ Theme", id="theme-btn")
        yield Label("", id="notification-bar")
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
        # Replace all built-in themes with our three custom ones
        for name in list(self.available_themes.keys()):
            try:
                self.unregister_theme(name)
            except Exception:
                pass
        for t in _CUSTOM_THEMES:
            self.register_theme(t)
        saved = self.config.get("theme", "hacker")
        self.theme = saved if saved in _THEME_CYCLE else "hacker"

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
        self.call_from_thread(self._clear_notification)
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
        self._update_tree_status()

    def _update_tree_status(self) -> None:
        changes = [i for i in self._pending_items if i.action != "present"]
        if not changes:
            self._set_status("Library up to date  ✓")
            return
        n_sel = sum(1 for i in changes if i.checked)
        total = len(changes)
        if n_sel == total:
            self._set_status(f"{total} change(s) pending — all selected")
        elif n_sel == 0:
            self._set_status(f"0/{total} selected  (Space to select)")
        else:
            self._set_status(f"{n_sel}/{total} selected for sync")

    def _populate_tree(self, items: list[SyncItem]) -> None:
        tree = self.query_one("#library-tree", Tree)
        tree.clear()
        self._leaf_map = {}
        self._artist_nodes = {}
        self._album_nodes = {}
        self._track_lyrics = {}
        self._album_cover = {}

        # Classify items into audio tracks vs companion files
        lyric_by_key: dict[tuple[Path, str], SyncItem] = {}   # (parent, stem) → item
        cover_by_dir: dict[Path, SyncItem] = {}                # dir → item
        audio_items: list[SyncItem] = []

        for item in items:
            kind = _item_kind(item)
            if kind == "lyric":
                lyric_by_key[(item.rel.parent, item.rel.stem)] = item
            elif kind == "cover":
                cover_by_dir[item.rel.parent] = item
            else:
                audio_items.append(item)

        # Build audio-rel → lyric item lookup
        for item in audio_items:
            lk = (item.rel.parent, item.rel.stem)
            if lk in lyric_by_key:
                self._track_lyrics[item.rel] = lyric_by_key[lk]

        # Build (artist, album) → cover item lookup
        for dir_path, cover_item in cover_by_dir.items():
            parts = dir_path.parts
            if len(parts) >= 2:
                self._album_cover[(parts[0], parts[1])] = cover_item

        # Build tree from audio items only
        by_artist: dict[str, list[SyncItem]] = {}
        root_files: list[SyncItem] = []

        for item in audio_items:
            if len(item.rel.parts) >= 2:
                by_artist.setdefault(item.rel.parts[0], []).append(item)
            else:
                root_files.append(item)

        for item in sorted(root_files, key=lambda i: i.rel.name.lower()):
            node = tree.root.add_leaf(
                _leaf_label(item, self._track_lyrics.get(item.rel)), data=item
            )
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
            node = parent.add_leaf(
                _leaf_label(item, self._track_lyrics.get(item.rel)), data=item
            )
            self._leaf_map[item.rel] = node

        for album in sorted(by_album.keys(), key=str.lower):
            al_items = by_album[album]
            has_changes = any(i.action != "present" for i in al_items)
            cover_item = self._album_cover.get((artist, album))
            al_node = parent.add(
                _branch_label(album, al_items, cover_item),
                data=("album", artist, album),
                expand=has_changes,
            )
            self._album_nodes[(artist, album)] = (al_node, al_items)
            for item in sorted(al_items, key=lambda i: i.rel.name.lower()):
                leaf = al_node.add_leaf(
                    _leaf_label(item, self._track_lyrics.get(item.rel)), data=item
                )
                self._leaf_map[item.rel] = leaf

    def _mark_item_done(self, item: SyncItem) -> None:
        if self._active_item is item:
            self._active_item = None
        item.action = "present"
        leaf = self._leaf_map.get(item.rel)
        if leaf:
            leaf.set_label(_leaf_label(item))
        self._refresh_ancestor_labels(item)

    def _refresh_ancestor_labels(self, item: SyncItem) -> None:
        parts = item.rel.parts
        if len(parts) >= 2:
            artist = parts[0]
            if len(parts) >= 3:
                album = parts[1]
                key = (artist, album)
                if key in self._album_nodes:
                    al_node, al_items = self._album_nodes[key]
                    al_node.set_label(_branch_label(album, al_items))
            if artist in self._artist_nodes:
                a_node, a_items = self._artist_nodes[artist]
                a_node.set_label(_branch_label(artist, a_items))

    # ------------------------------------------------------------------
    # Selection toggling
    # ------------------------------------------------------------------

    def action_toggle_selection(self) -> None:
        node = self.query_one("#library-tree", Tree).cursor_node
        if node is None or node.data is None:
            return
        data = node.data

        if isinstance(data, SyncItem):
            if data.action == "present":
                return
            data.checked = not data.checked
            node.set_label(_leaf_label(data))
            self._refresh_ancestor_labels(data)

        elif isinstance(data, tuple):
            if data[0] == "artist":
                artist = data[1]
                if artist not in self._artist_nodes:
                    return
                _, items = self._artist_nodes[artist]
                actionable = [i for i in items if i.action != "present"]
                if not actionable:
                    return
                new_val = not all(i.checked for i in actionable)
                for item in actionable:
                    item.checked = new_val
                    leaf = self._leaf_map.get(item.rel)
                    if leaf:
                        leaf.set_label(_leaf_label(item))
                # Refresh album branch labels under this artist
                for (art, alb), (al_node, al_items) in self._album_nodes.items():
                    if art == artist:
                        al_node.set_label(_branch_label(alb, al_items))
                a_node, a_items = self._artist_nodes[artist]
                a_node.set_label(_branch_label(artist, a_items))

            elif data[0] == "album":
                _, artist, album = data
                key = (artist, album)
                if key not in self._album_nodes:
                    return
                al_node, al_items = self._album_nodes[key]
                actionable = [i for i in al_items if i.action != "present"]
                if not actionable:
                    return
                new_val = not all(i.checked for i in actionable)
                for item in actionable:
                    item.checked = new_val
                    leaf = self._leaf_map.get(item.rel)
                    if leaf:
                        leaf.set_label(_leaf_label(item))
                al_node.set_label(_branch_label(album, al_items))
                if artist in self._artist_nodes:
                    a_node, a_items = self._artist_nodes[artist]
                    a_node.set_label(_branch_label(artist, a_items))

        self._update_tree_status()

    # ------------------------------------------------------------------
    # Collapse / expand all
    # ------------------------------------------------------------------

    @on(Button.Pressed, "#collapse-all-btn")
    def _collapse_all(self) -> None:
        for _, (node, _) in self._album_nodes.items():
            node.collapse()
        for _, (node, _) in self._artist_nodes.items():
            node.collapse()

    @on(Button.Pressed, "#expand-all-btn")
    def _expand_all(self) -> None:
        for _, (node, _) in self._artist_nodes.items():
            node.expand()
        for _, (node, _) in self._album_nodes.items():
            node.expand()

    @on(Button.Pressed, "#collapse-synced-btn")
    def _collapse_synced(self) -> None:
        for (artist, album), (node, items) in self._album_nodes.items():
            if all(i.action == "present" for i in items):
                node.collapse()
        for artist, (node, items) in self._artist_nodes.items():
            if all(i.action == "present" for i in items):
                node.collapse()

    @on(Button.Pressed, "#theme-btn")
    def _cycle_theme(self) -> None:
        current = self.theme
        try:
            idx = _THEME_CYCLE.index(current)
        except ValueError:
            idx = -1
        next_name = _THEME_CYCLE[(idx + 1) % len(_THEME_CYCLE)]
        self.theme = next_name
        self.config.set("theme", next_name)
        self.config.save()

    # ------------------------------------------------------------------
    # Notification bar
    # ------------------------------------------------------------------

    def _show_notification(self, msg: str, kind: str = "warn") -> None:
        bar = self.query_one("#notification-bar", Label)
        bar.update(f"  ⚠  {msg}")
        bar.remove_class("-notify-warn", "-notify-error")
        bar.add_class(f"-notify-{kind}")

    def _clear_notification(self) -> None:
        bar = self.query_one("#notification-bar", Label)
        bar.remove_class("-notify-warn", "-notify-error")
        bar.display = False

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
        node_frame = _NODE_SPINNER[self._spinner_frame % len(_NODE_SPINNER)]
        self.query_one("#spinner-label", Label).update(frame)
        self._spinner_frame += 1
        if self._active_item is not None:
            leaf = self._leaf_map.get(self._active_item.rel)
            if leaf:
                leaf.set_label(_active_leaf_label(self._active_item, node_frame))

    def _on_progress(self, done: int, total: int, track: str) -> None:
        if done >= total:
            self._active_item = None
            self._set_status(f"Sync complete  ✓  —  {total} processed")
            return
        rel = Path(track) if track else None
        self._active_item = next(
            (i for i in self._pending_items if i.rel == rel), None
        ) if rel else None
        name = rel.name if rel else ""
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

        all_changes = [i for i in self._pending_items if i.action != "present"]
        selected = [i for i in all_changes if i.checked]

        if not all_changes:
            if not self._pending_items:
                self.notify("Scan the library first (r).", severity="information")
            else:
                self.notify("Already up to date.", severity="information")
            return
        if not selected:
            self.notify("Nothing selected — use Space to select items.", severity="warning")
            return

        n_add = sum(1 for i in selected if i.action == "add")
        n_upd = sum(1 for i in selected if i.action == "update")
        n_del = sum(1 for i in selected if i.action == "delete")
        n_skip = len(all_changes) - len(selected)
        parts = []
        if n_add:
            parts.append(f"[green]+{n_add} to add[/]")
        if n_upd:
            parts.append(f"[yellow]↑{n_upd} to update[/]")
        if n_del:
            parts.append(f"[red]×{n_del} to delete[/]")
        if n_skip:
            parts.append(f"[dim]({n_skip} skipped)[/]")
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

    async def action_open_help(self) -> None:
        await self.push_screen(HelpScreen())

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
            self._show_notification,
            f"Source changed: {path.name}  —  press r to rescan",
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
            active_name = dest["name"] if dest else None
            for d in self.config.destinations:
                if d["name"] == active_name:
                    continue
                if paths_overlap(dst, Path(d["path"])):
                    self.notify(
                        f"Path overlaps with existing destination '{d['name']}'. "
                        "Remove it first or choose a different folder.",
                        severity="error",
                        timeout=8,
                    )
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
