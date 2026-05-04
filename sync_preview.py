from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Tree
from textual.widgets.tree import TreeNode

from mirror import SyncItem

_COLOR = {"add": "green", "update": "yellow", "delete": "red"}
_LABEL = {"add": "ADD", "update": "UPDATE", "delete": "DELETE"}


def _sym(items: list[SyncItem]) -> str:
    n = sum(1 for i in items if i.checked)
    if n == len(items):
        return "✓"
    if n == 0:
        return " "
    return "~"


def _branch_text(items: list[SyncItem], name: str, action: str) -> Text:
    sym = _sym(items)
    color = _COLOR[action]
    name_style = color if sym != " " else "dim"
    sym_style = f"bold {color}" if sym == "✓" else ("bold yellow" if sym == "~" else "bold dim")
    return Text.assemble(
        ("[", "dim"), (sym, sym_style), ("]  ", "dim"),
        (name, name_style),
    )


def _leaf_text(item: SyncItem) -> Text:
    sym = "✓" if item.checked else " "
    color = _COLOR[item.action]
    sym_style = f"bold {color}" if item.checked else "bold dim"
    if item.action != "delete" and item.src.suffix.lower() != item.dst.suffix.lower():
        name = f"{item.src.name} → {item.dst.name}"
    else:
        name = item.rel.name
    return Text.assemble(
        ("[", "dim"), (sym, sym_style), ("]  ", "dim"),
        (name, color if item.checked else "dim"),
    )


class SyncPreviewScreen(ModalScreen[list[SyncItem] | None]):
    """Interactive diff tree — select which changes to apply before mirroring."""

    DEFAULT_CSS = """
    SyncPreviewScreen { align: center middle; }

    #preview-box {
        width: 90%;
        height: 90%;
        border: thick $accent;
        background: $surface;
    }
    #preview-header {
        height: auto;
        padding: 1 1 0 1;
        border-bottom: solid $panel;
    }
    #preview-title { text-style: bold; margin-bottom: 0; }
    #summary { color: $text-muted; }

    #toolbar {
        height: 3;
        padding: 0 1;
        border-bottom: solid $panel;
        align: left middle;
    }
    #toolbar Button { margin-right: 1; min-width: 16; }
    #safe-sync-btn.safe-active { background: $warning 50%; }

    #sync-tree { height: 1fr; }

    #preview-footer {
        height: 3;
        padding: 0 1;
        border-top: solid $panel;
        align: right middle;
    }
    #mirror-btn { margin-left: 1; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("space", "toggle", "Toggle", show=True),
        Binding("a", "select_all", "Select All", show=True),
        Binding("n", "deselect_all", "Deselect All", show=True),
    ]

    def __init__(self, dest_name: str, items: list[SyncItem]) -> None:
        super().__init__()
        self._dest_name = dest_name
        self._items = items
        self._safe_sync = False
        self._highlighted: TreeNode | None = None
        # Populated during _build_tree for in-place label updates:
        self._branch_nodes: list[tuple[TreeNode, str, list[SyncItem], str]] = []
        self._leaf_nodes: list[tuple[TreeNode, SyncItem]] = []

    # ------------------------------------------------------------------
    # Layout

    def compose(self) -> ComposeResult:
        with Container(id="preview-box"):
            with Container(id="preview-header"):
                yield Label(f"Sync Preview — {self._dest_name}", id="preview-title")
                yield Label("", id="summary")
            with Horizontal(id="toolbar"):
                yield Button("✓  Select All", id="select-all-btn")
                yield Button("✗  Deselect All", id="deselect-all-btn")
                yield Button("🔒  Safe Sync: OFF", id="safe-sync-btn")
            yield Tree("Changes", id="sync-tree")
            with Horizontal(id="preview-footer"):
                yield Button("Cancel", id="cancel-btn")
                yield Button("Mirror Now  ▶", variant="primary", id="mirror-btn")

    def on_mount(self) -> None:
        tree = self.query_one("#sync-tree", Tree)
        tree.show_root = False
        self._build_tree()
        self._update_summary()
        tree.focus()

    # ------------------------------------------------------------------
    # Tree construction

    def _build_tree(self) -> None:
        tree = self.query_one("#sync-tree", Tree)
        tree.clear()
        self._branch_nodes = []
        self._leaf_nodes = []

        by_action: dict[str, list[SyncItem]] = {"add": [], "update": [], "delete": []}
        for item in self._items:
            by_action[item.action].append(item)

        for action in ("add", "update", "delete"):
            group = by_action[action]
            if not group:
                continue
            if self._safe_sync and action == "delete":
                continue
            n = len(group)
            name = f"{_LABEL[action]} — {n} file{'s' if n != 1 else ''}"
            node = tree.root.add(
                _branch_text(group, name, action),
                data={"items": group, "action": action},
                expand=True,
            )
            self._branch_nodes.append((node, action, group, name))
            self._add_artist_nodes(node, group, action)

    def _add_artist_nodes(self, parent: TreeNode, items: list[SyncItem], action: str) -> None:
        by_artist: dict[str, list[SyncItem]] = {}
        root_files: list[SyncItem] = []
        for item in items:
            if len(item.rel.parts) > 1:
                by_artist.setdefault(item.rel.parts[0], []).append(item)
            else:
                root_files.append(item)

        for item in root_files:
            leaf = parent.add_leaf(_leaf_text(item), data={"item": item})
            self._leaf_nodes.append((leaf, item))

        for artist, a_items in sorted(by_artist.items()):
            node = parent.add(
                _branch_text(a_items, artist, action),
                data={"items": a_items, "action": action},
                expand=True,
            )
            self._branch_nodes.append((node, action, a_items, artist))
            self._add_album_nodes(node, a_items, action)

    def _add_album_nodes(self, parent: TreeNode, items: list[SyncItem], action: str) -> None:
        by_album: dict[str, list[SyncItem]] = {}
        direct: list[SyncItem] = []
        for item in items:
            if len(item.rel.parts) > 2:
                by_album.setdefault(item.rel.parts[1], []).append(item)
            else:
                direct.append(item)

        for item in direct:
            leaf = parent.add_leaf(_leaf_text(item), data={"item": item})
            self._leaf_nodes.append((leaf, item))

        for album, al_items in sorted(by_album.items()):
            label = f"{album}  ({len(al_items)})"
            node = parent.add(
                _branch_text(al_items, label, action),
                data={"items": al_items, "action": action},
                expand=False,       # albums collapsed by default — expand as needed
            )
            self._branch_nodes.append((node, action, al_items, label))
            for item in al_items:
                leaf = node.add_leaf(_leaf_text(item), data={"item": item})
                self._leaf_nodes.append((leaf, item))

    # ------------------------------------------------------------------
    # Label & summary refresh

    def _refresh_labels(self) -> None:
        for node, action, items, name in self._branch_nodes:
            node.set_label(_branch_text(items, name, action))
        for node, item in self._leaf_nodes:
            node.set_label(_leaf_text(item))
        self._update_summary()

    def _update_summary(self) -> None:
        visible = [i for i in self._items if not (self._safe_sync and i.action == "delete")]
        total = len(visible)
        selected = sum(1 for i in visible if i.checked)
        counts: dict[str, int] = {"add": 0, "update": 0, "delete": 0}
        for i in visible:
            if i.checked:
                counts[i.action] += 1
        parts = []
        if counts["add"]:
            parts.append(f"[green]ADD {counts['add']}[/]")
        if counts["update"]:
            parts.append(f"[yellow]UPDATE {counts['update']}[/]")
        if counts["delete"]:
            parts.append(f"[red]DELETE {counts['delete']}[/]")
        suffix = ("  —  " + "  ".join(parts)) if parts else "  —  nothing selected"
        self.query_one("#summary", Label).update(f"{selected}/{total} selected{suffix}")

    # ------------------------------------------------------------------
    # Events

    @on(Tree.NodeHighlighted, "#sync-tree")
    def node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        self._highlighted = event.node

    def action_toggle(self) -> None:
        node = self._highlighted
        if node is None or node.data is None:
            return
        data = node.data
        if "item" in data:
            item: SyncItem = data["item"]
            item.checked = not item.checked
        elif "items" in data:
            group: list[SyncItem] = data["items"]
            all_on = all(i.checked for i in group)
            for i in group:
                i.checked = not all_on
        self._refresh_labels()

    def _visible_items(self) -> list[SyncItem]:
        return [i for i in self._items if not (self._safe_sync and i.action == "delete")]

    def action_select_all(self) -> None:
        for i in self._visible_items():
            i.checked = True
        self._refresh_labels()

    def action_deselect_all(self) -> None:
        for i in self._visible_items():
            i.checked = False
        self._refresh_labels()

    @on(Button.Pressed, "#select-all-btn")
    def btn_select_all(self) -> None:
        self.action_select_all()

    @on(Button.Pressed, "#deselect-all-btn")
    def btn_deselect_all(self) -> None:
        self.action_deselect_all()

    @on(Button.Pressed, "#safe-sync-btn")
    def toggle_safe_sync(self) -> None:
        self._safe_sync = not self._safe_sync
        btn = self.query_one("#safe-sync-btn", Button)
        btn.label = f"🔒  Safe Sync: {'ON ' if self._safe_sync else 'OFF'}"
        if self._safe_sync:
            btn.add_class("safe-active")
        else:
            btn.remove_class("safe-active")
        self._build_tree()
        self._update_summary()

    @on(Button.Pressed, "#mirror-btn")
    def mirror(self) -> None:
        to_apply = [
            i for i in self._items
            if i.checked and not (self._safe_sync and i.action == "delete")
        ]
        if not to_apply:
            self.notify("Nothing selected to sync.", severity="warning")
            return
        self.dismiss(to_apply)

    @on(Button.Pressed, "#cancel-btn")
    def cancel(self) -> None:
        self.dismiss(None)
