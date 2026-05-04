from __future__ import annotations

from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView

from devices import detect_external_locations


class FileBrowserScreen(ModalScreen[str | None]):
    """Modal directory browser. Dismisses with the selected path string, or None."""

    DEFAULT_CSS = """
    FileBrowserScreen { align: center middle; }

    #browser-box {
        width: 85%;
        height: 80%;
        border: thick $accent;
        background: $surface;
    }
    #path-row {
        height: 3;
        padding: 0 1;
        border-bottom: solid $panel;
        align: left middle;
    }
    #path-input { width: 1fr; margin: 0 1; }
    #go-btn { min-width: 6; }

    #panels { height: 1fr; }

    #sidebar {
        width: 26;
        border-right: solid $panel;
    }
    #sidebar-list { height: 1fr; }
    #refresh-btn { width: 100%; }
    .section-title {
        text-style: bold;
        color: $accent;
        padding: 0 1;
    }

    #dir-list { width: 1fr; }

    #btn-row {
        height: 3;
        padding: 0 1;
        border-top: solid $panel;
        align: right middle;
    }
    #select-btn { margin-left: 1; }
    """

    def __init__(self, start_path: Path | None = None, title: str = "Select Folder") -> None:
        super().__init__()
        self._cwd: Path = (start_path or Path.home()).resolve()
        self._title = title
        self._entries: list[Path | None] = []   # None = parent (..) entry
        self._sidebar_paths: list[Path] = []

    def compose(self) -> ComposeResult:
        with Container(id="browser-box"):
            with Horizontal(id="path-row"):
                yield Label(f"{self._title}:  ")
                yield Input(str(self._cwd), id="path-input")
                yield Button("Go", id="go-btn")
            with Horizontal(id="panels"):
                with Vertical(id="sidebar"):
                    yield Label("QUICK ACCESS", classes="section-title")
                    yield ListView(id="sidebar-list")
                    yield Button("↻  Refresh", id="refresh-btn", variant="default")
                yield ListView(id="dir-list")
            with Horizontal(id="btn-row"):
                yield Button("Cancel", id="cancel-btn")
                yield Button("Select This Folder", variant="primary", id="select-btn")

    def on_mount(self) -> None:
        self._populate_sidebar()
        self._navigate(self._cwd)

    # ------------------------------------------------------------------
    # Sidebar

    def _populate_sidebar(self) -> None:
        lv = self.query_one("#sidebar-list", ListView)
        lv.clear()
        self._sidebar_paths = []

        lv.append(ListItem(Label("🏠  Home")))
        self._sidebar_paths.append(Path.home())

        try:
            devices = detect_external_locations()
        except Exception:
            devices = []

        if devices:
            for dev in devices:
                icon = "📱" if dev["type"] == "android" else "💾"
                lv.append(ListItem(Label(f"{icon}  {dev['name']}")))
                self._sidebar_paths.append(Path(dev["path"]))
        else:
            lv.append(ListItem(Label("  (no drives detected)")))
            # no path for the placeholder entry — sidebar_paths stays shorter

    # ------------------------------------------------------------------
    # Directory listing

    def _navigate(self, path: Path) -> None:
        try:
            path = path.resolve()
            dirs = sorted(
                [e for e in path.iterdir() if e.is_dir() and not e.name.startswith(".")],
                key=lambda e: e.name.lower(),
            )
        except PermissionError:
            self.notify(f"Permission denied: {path}", severity="error")
            return
        except (FileNotFoundError, OSError) as e:
            self.notify(str(e), severity="error")
            return

        self._cwd = path
        self.query_one("#path-input", Input).value = str(path)

        lv = self.query_one("#dir-list", ListView)
        lv.clear()
        self._entries = []

        if path.parent != path:
            lv.append(ListItem(Label("📁  ..")))
            self._entries.append(None)          # sentinel → go to parent

        for d in dirs:
            lv.append(ListItem(Label(f"📁  {d.name}")))
            self._entries.append(d)

    # ------------------------------------------------------------------
    # Event handlers

    @on(ListView.Selected, "#dir-list")
    def dir_chosen(self, event: ListView.Selected) -> None:
        idx = self.query_one("#dir-list", ListView).index
        if idx is None or idx >= len(self._entries):
            return
        target = self._entries[idx]
        self._navigate(self._cwd.parent if target is None else target)

    @on(ListView.Selected, "#sidebar-list")
    def sidebar_chosen(self, event: ListView.Selected) -> None:
        idx = self.query_one("#sidebar-list", ListView).index
        if idx is None or idx >= len(self._sidebar_paths):
            return                              # placeholder row ("no drives")
        self._navigate(self._sidebar_paths[idx])

    @on(Input.Submitted, "#path-input")
    def path_submitted(self, event: Input.Submitted) -> None:
        val = event.value.strip()
        if val:
            self._navigate(Path(val))

    @on(Button.Pressed, "#go-btn")
    def go_pressed(self) -> None:
        val = self.query_one("#path-input", Input).value.strip()
        if val:
            self._navigate(Path(val))

    @on(Button.Pressed, "#refresh-btn")
    def refresh_pressed(self) -> None:
        self._populate_sidebar()
        self._navigate(self._cwd)

    @on(Button.Pressed, "#select-btn")
    def select_pressed(self) -> None:
        self.dismiss(str(self._cwd))

    @on(Button.Pressed, "#cancel-btn")
    def cancel_pressed(self) -> None:
        self.dismiss(None)
