from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


class AtomicJsonStore:
    """Small crash-resistant JSON store with corrupt-file quarantine."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError("unsafe state name")
        return self.directory / f"{name}.json"

    def load(self, name: str, default: Any) -> Any:
        path = self._path(name)
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            quarantine = path.with_name(f"{path.name}.corrupt-{stamp}-{uuid.uuid4().hex}")
            try:
                path.replace(quarantine)
            except OSError:
                pass
            return default

    def save(self, name: str, value: Any) -> None:
        path = self._path(name)
        temporary = path.with_suffix(".json.tmp")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some container/overlay filesystems do not support directory fsync.
            pass
