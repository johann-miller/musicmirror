from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "source": "",
    "destinations": [],
    "active_destination": "",
    "destination_prefix": "compressed_",
    "ffmpeg_codec": "aac",
    "ffmpeg_bitrate": "256k",
    "output_ext": ".m4a",
}

CONFIG_PATH = Path(__file__).parent / "config.json"


class ConfigManager:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path) as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}

    def save(self) -> None:
        try:
            with open(self.path, "w") as f:
                json.dump(self._data, f, indent=2)
        except OSError as e:
            raise OSError(f"Could not save config to {self.path}: {e}") from e

    def is_configured(self) -> bool:
        return bool(self._data.get("source") and self._data.get("destinations"))

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULT_CONFIG.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    @property
    def source(self) -> str:
        return self._data.get("source", "")

    @property
    def destinations(self) -> list[dict]:
        return self._data.get("destinations", [])

    @property
    def active_destination(self) -> str:
        return self._data.get("active_destination", "")

    @property
    def destination_prefix(self) -> str:
        return self._data.get("destination_prefix", DEFAULT_CONFIG["destination_prefix"])

    @property
    def ffmpeg_codec(self) -> str:
        return self._data.get("ffmpeg_codec", DEFAULT_CONFIG["ffmpeg_codec"])

    @property
    def ffmpeg_bitrate(self) -> str:
        return self._data.get("ffmpeg_bitrate", DEFAULT_CONFIG["ffmpeg_bitrate"])

    @property
    def output_ext(self) -> str:
        return self._data.get("output_ext", DEFAULT_CONFIG["output_ext"])

    def get_active_destination(self) -> dict | None:
        name = self.active_destination
        for d in self.destinations:
            if d["name"] == name:
                return d
        return None

    def add_destination(self, name: str, path: str, dtype: str) -> None:
        self._data.setdefault("destinations", [])
        self._data["destinations"].append({"name": name, "path": path, "type": dtype})
        if not self._data.get("active_destination"):
            self._data["active_destination"] = name
        self.save()

    def remove_destination(self, name: str) -> None:
        self._data["destinations"] = [d for d in self.destinations if d["name"] != name]
        if self._data.get("active_destination") == name:
            remaining = self._data["destinations"]
            self._data["active_destination"] = remaining[0]["name"] if remaining else ""
        self.save()

    def set_active_destination(self, name: str) -> None:
        self._data["active_destination"] = name
        self.save()

    def initialize(self, source: str, dest_name: str, dest_path: str) -> None:
        self._data = dict(DEFAULT_CONFIG)
        self._data["source"] = source
        self._data["destinations"] = [{"name": dest_name, "path": dest_path, "type": "local"}]
        self._data["active_destination"] = dest_name
        self.save()
