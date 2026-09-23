"""One-time import of a verified local add-on snapshot from addon_config."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

STATE_FILES = {
    "settings.json",
    "studio_apps.json",
    "app_order.json",
    "device_controls.json",
    "sonos_settings.json",
}
ICON_NAME = re.compile(r"icons/[A-Za-z0-9_-]+\.(?:gif|png|jpg|jpeg)")
MAX_FILE_SIZE = 8 * 1024 * 1024
MARKER = ".tc002-migration-complete"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migrate(source: Path, target: Path) -> int:
    manifest_path = source / "manifest.json"
    if not manifest_path.exists():
        return 0  # Normal fresh installation.
    if (target / MARKER).exists():
        return 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise TypeError("Invalid migration manifest")
    files = manifest["files"]
    if not files or len(files) > 256:
        raise ValueError("Invalid migration file count")
    for name, expected in files.items():
        if not isinstance(name, str) or not (
            name in STATE_FILES or ICON_NAME.fullmatch(name)
        ):
            raise ValueError("Unexpected migration file")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Invalid migration checksum")
        src = source / name
        if src.is_symlink() or not src.is_file() or src.stat().st_size > MAX_FILE_SIZE:
            raise ValueError("Invalid migration source")
        if _digest(src) != expected:
            raise ValueError("Migration checksum mismatch")
        if name in STATE_FILES:
            json.loads(src.read_text(encoding="utf-8"))
        dest = target / name
        if dest.exists() and _digest(dest) != expected:
            raise ValueError("Migration would overwrite existing data")

    target.mkdir(parents=True, exist_ok=True)
    for name, expected in files.items():
        src = source / name
        dest = target / name
        if dest.exists() and _digest(dest) == expected:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as stream:
            temporary = Path(stream.name)
            with src.open("rb") as source_stream:
                shutil.copyfileobj(source_stream, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, dest)
    (target / MARKER).write_text("Imported from local add-on snapshot\n", encoding="utf-8")
    return len(files)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    count = migrate(args.source, args.target)
    if count:
        print(f"Imported {count} verified bridge data files")


if __name__ == "__main__":
    main()
