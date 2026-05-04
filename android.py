from __future__ import annotations

from pathlib import Path

try:
    import pymtp
    PYMTP_AVAILABLE = True
except ImportError:
    PYMTP_AVAILABLE = False


class AndroidDevice:
    """Wraps pymtp for MTP device operations."""

    def __init__(self) -> None:
        self._mtp: "pymtp.MTP | None" = None
        self.connected = False
        self.device_name = ""
        self.storage_info: dict = {}

    def connect(self) -> bool:
        if not PYMTP_AVAILABLE:
            return False
        try:
            mtp = pymtp.MTP()
            mtp.connect()
            self._mtp = mtp
            self.connected = True
            self.device_name = str(mtp.get_devicename() or "Android Device")
            return True
        except Exception:
            self._mtp = None
            self.connected = False
            return False

    def disconnect(self) -> None:
        if self._mtp and self.connected:
            try:
                self._mtp.disconnect()
            except Exception:
                pass
        self._mtp = None
        self.connected = False
        self.device_name = ""

    def list_files(self, remote_root: str) -> list[str]:
        if not self.connected or self._mtp is None:
            return []
        try:
            result = []
            files = self._mtp.get_filelisting()
            for f in files:
                if f.filename.startswith(remote_root):
                    result.append(f.filename)
            return result
        except Exception:
            self.connected = False
            return []

    def push_file(self, local_path: Path, remote_path: str) -> bool:
        if not self.connected or self._mtp is None:
            return False
        try:
            self._mtp.send_file_from_file(str(local_path), remote_path)
            return True
        except Exception as e:
            self.connected = False
            raise RuntimeError(f"MTP push failed: {e}") from e

    def delete_file(self, remote_path: str) -> bool:
        if not self.connected or self._mtp is None:
            return False
        try:
            files = self._mtp.get_filelisting()
            for f in files:
                if f.filename == remote_path:
                    self._mtp.delete_object(f.item_id)
                    return True
            return False
        except Exception as e:
            self.connected = False
            raise RuntimeError(f"MTP delete failed: {e}") from e

    def get_storage_info(self) -> dict:
        if not self.connected or self._mtp is None:
            return {}
        try:
            storage = self._mtp.get_storage_info()
            return {
                "total": getattr(storage, "max_capacity", 0),
                "free": getattr(storage, "free_space_in_bytes", 0),
            }
        except Exception:
            return {}
