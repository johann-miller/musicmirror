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

_COLOR = {"add": "green", "update": "yellow", "delete": "red", "present": "dim"}
_BADGE = {"add": "+", "update": "↑", "delete": "×"}


def _action_items(items: list[SyncItem]) -> list[SyncItem]:
    return [i for i in items if i.action != "present"]


def _sym(items: list[SyncItem]) -> str:
    """Checkbox symbol based only on the selectable (non-present) items."""
    acts = _action_items(items)
    if not acts:
        return " "
    n = sum(1 for i in acts if i.checked)
    if n == len(acts):
        return "✓"
    if n == 0:
        return " "
    return "~"


def _branch_text(items: list[SyncItem], name: str) -> Text:
    sym = _sym(items)
    acts = _action_items(items)
    present_count = len(items) - len(acts)

    if not acts:
        base_color = "dim"
        sym_style = "bold dim"
        name_style = "dim"
    else:
        if any(i.action == "add" for i in acts):
            base_color = "green"
        elif any(i.action == "update" for i in acts):
            base_color = "yellow"
        else:
            base_color = "red"
        sym_style = (f"bold {base_color}" if sym == "✓"
                     else ("bold yellow" if sym == "~" else "bold dim"))
        name_style = base_color if sym != " " else "dim"

    t = Text.assemble(
        ("[", "dim"), (sym, sym_style), ("]  ", "dim"),
        (name, name_style),
    )

    counts: dict[str, int] = {}
    for item in acts:
        counts[item.action] = counts.get(item.action, 0) + 1

    badges: list[tuple[str, str]] = []
    for action in ("add", "update", "delete"):
        if counts.get(action):
            badges.append((f"{_BADGE[action]}{counts[action]}", _COLOR[action]))
    if present_count:
        badges.append((f"·{present_count}", "dim"))

    if badges:
        t.append("  ")
        for i, (badge, style) in enumerate(badges):
            if i:
                t.append(" ")
            t.append(badge, style)

    return t


def _leaf_text(item: SyncItem) -> Text:
    if item.action == "present":
        return Text.assemble(
            ("[", "dim"), (" ", "bold dim"), ("]  ", "dim"),
            (item.rel.name, "dim"),
            ("  ·", "dim"),
        )
    sym = "✓" if item.checked else " "
    color = _COLOR[item.action]
    sym_style = f"bold {color}" if item.checked else "bold dim"
    badge = _BADGE.get(item.action, "")
    return Text.assemble(
        ("[", "dim"), (sym, sym_style), ("]  ", "dim"),
        (item.rel.name, color if item.checked else "dim"),
        (f"  {badge}", f"bold {color}" if item.checked else "dim"),
    )


def _present_summary_text(count: int) -> Text:
    return Text.assemble(
        ("  ·  ", "dim"),
        (f"{count} track{'s' if count != 1 else ''} already present", "dim"),
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
        Binding("space", "toggle", "Check/Uncheck", show=True),
        Binding("enter", "toggle", "Check/Uncheck", show=False),
        Binding("a", "select_all", "Select All", show=True),
        Binding("n", "deselect_all", "Deselect All", show=True),
    ]

    def __init__(self, dest_name: str, items: list[SyncItem]) -> None:
        super().__init__()
        self._dest_name = dest_name
        self._items = items
        self._safe_sync = False
        self._show_all = False
        self._highlighted: TreeNode | None = None
        self._branch_nodes: list[tuple[TreeNode, list[SyncItem], str]] = []
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
                yield Button("👁  Show All: OFF", id="show-all-btn")
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

        visible = [i for i in self._items
                   if not (self._safe_sync and i.action == "delete")]

        # Partition into artist-keyed groups and bare root files
        by_artist: dict[str, list[SyncItem]] = {}
        root_files: list[SyncItem] = []
        for item in visible:
            if len(item.rel.parts) > 1:
                by_artist.setdefault(item.rel.parts[0], []).append(item)
            else:
                root_files.append(item)

        for item in sorted(root_files, key=lambda i: i.rel.name.lower()):
            leaf = tree.root.add_leaf(_leaf_text(item), data={"item": item})
            self._leaf_nodes.append((leaf, item))

        for artist, a_items in sorted(by_artist.items(), key=lambda x: x[0].lower()):
            has_actions = any(i.action != "present" for i in a_items)
            if not has_actions and not self._show_all:
                continue
            node = tree.root.add(
                _branch_text(a_items, artist),
                data={"items": a_items},
                expand=has_actions,  # fully-synced artists start collapsed
            )
            self._branch_nodes.append((node, a_items, artist))
            self._add_album_nodes(node, a_items)

    def _add_album_nodes(self, parent: TreeNode, items: list[SyncItem]) -> None:
        by_album: dict[str, list[SyncItem]] = {}
        direct: list[SyncItem] = []
        for item in items:
            if len(item.rel.parts) > 2:
                by_album.setdefault(item.rel.parts[1], []).append(item)
            else:
                direct.append(item)

        for item in self._sorted_leaves(direct):
            leaf = parent.add_leaf(_leaf_text(item), data={"item": item})
            self._leaf_nodes.append((leaf, item))

        for album, al_items in sorted(by_album.items(), key=lambda x: x[0].lower()):
            has_actions = any(i.action != "present" for i in al_items)
            node = parent.add(
                _branch_text(al_items, album),
                data={"items": al_items},
                expand=has_actions,
            )
            self._branch_nodes.append((node, al_items, album))

            if has_actions:
                # Mixed album: show every track individually (action + present)
                for item in self._sorted_leaves(al_items):
                    leaf = node.add_leaf(_leaf_text(item), data={"item": item})
                    self._leaf_nodes.append((leaf, item))
            else:
                # Fully-present album: one summary leaf avoids flooding the tree
                node.add_leaf(_present_summary_text(len(al_items)), data={})

    @staticmethod
    def _sorted_leaves(items: list[SyncItem]) -> list[SyncItem]:
        """Action items first (alpha), then present items (alpha)."""
        return sorted(items, key=lambda i: (i.action == "present", i.rel.name.lower()))

    # ------------------------------------------------------------------
    # Label & summary refresh

    def _refresh_labels(self) -> None:
        for node, items, name in self._branch_nodes:
            node.set_label(_branch_text(items, name))
        for node, item in self._leaf_nodes:
            node.set_label(_leaf_text(item))
        self._update_summary()

    def _update_summary(self) -> None:
        all_action = [i for i in self._items if i.action != "present"]
        visible = [i for i in all_action
                   if not (self._safe_sync and i.action == "delete")]
        present_count = sum(1 for i in self._items if i.action == "present")
        total = len(visible)
        selected = sum(1 for i in visible if i.checked)
        counts: dict[str, int] = {"add": 0, "update": 0, "delete": 0}
        for i in visible:
            if i.checked:
                counts[i.action] += 1
        parts = []
        if counts["add"]:
            parts.append(f"[green]+{counts['add']}[/]")
        if counts["update"]:
            parts.append(f"[yellow]↑{counts['update']}[/]")
        if counts["delete"]:
            parts.append(f"[red]×{counts['delete']}[/]")
        suffix = ("  " + "  ".join(parts)) if parts else "  nothing selected"
        present_str = f"  [dim]·{present_count} already present[/]" if present_count else ""
        self.query_one("#summary", Label).update(
            f"{selected}/{total} selected{suffix}{present_str}"
        )

    # ------------------------------------------------------------------
    # Events

    @on(Tree.NodeHighlighted, "#sync-tree")
    def node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        self._highlighted = event.node

    @on(Tree.NodeSelected, "#sync-tree")
    def node_selected(self, event: Tree.NodeSelected) -> None:
        self._highlighted = event.node
        self.action_toggle()

    def action_toggle(self) -> None:
        node = self._highlighted
        if node is None or node.data is None:
            return
        data = node.data
        if "item" in data:
            item: SyncItem = data["item"]
            if item.action == "present":
                return
            item.checked = not item.checked
        elif "items" in data:
            group: list[SyncItem] = data["items"]
            acts = _action_items(group)
            if not acts:
                return
            all_on = all(i.checked for i in acts)
            for i in acts:
                i.checked = not all_on
        else:
            return
        self._refresh_labels()

    def _visible_items(self) -> list[SyncItem]:
        return [i for i in self._items
                if i.action != "present" and not (self._safe_sync and i.action == "delete")]

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

    @on(Button.Pressed, "#show-all-btn")
    def toggle_show_all(self) -> None:
        self._show_all = not self._show_all
        btn = self.query_one("#show-all-btn", Button)
        btn.label = f"👁  Show All: {'ON ' if self._show_all else 'OFF'}"
        self._build_tree()
        self._update_summary()

    @on(Button.Pressed, "#mirror-btn")
    def mirror(self) -> None:
        to_apply = [
            i for i in self._items
            if i.checked and i.action != "present"
            and not (self._safe_sync and i.action == "delete")
        ]
        if not to_apply:
            self.notify("Nothing selected to sync.", severity="warning")
            return
        self.dismiss(to_apply)

    @on(Button.Pressed, "#cancel-btn")
    def cancel(self) -> None:
        self.dismiss(None)
