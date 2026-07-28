"""Enumerating the worlds in an instance's ``saves`` folder.

Two things here matter more than they look:

* **The player-data layout.** Modern saves keep per-player state in
  ``players/{data,stats,advancements}/<uuid>.*``, which is exactly what a
  dedicated server expects - so a singleplayer world runs as a server world with
  no migration and the host keeps their inventory. Older saves keep the host's
  character in ``level.dat``'s ``Player`` tag with data under ``playerdata/``;
  those need a migration or the host spawns empty-handed. We detect which,
  rather than assuming.
* **The session lock.** Minecraft holds an exclusive lock on ``session.lock``
  while a world is open. Probing it is how we stop the client and the server
  from writing the same chunks at once.
"""

from __future__ import annotations

import gzip
import os
import struct
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# --- Minimal NBT reader -------------------------------------------------------
#
# Only enough to read level.dat's header fields. A full NBT library would be a
# dependency for a handful of scalars.

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12

_FIXED = {
    TAG_BYTE: (1, ">b"),
    TAG_SHORT: (2, ">h"),
    TAG_INT: (4, ">i"),
    TAG_LONG: (8, ">q"),
    TAG_FLOAT: (4, ">f"),
    TAG_DOUBLE: (8, ">d"),
}


class NbtError(Exception):
    pass


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, count: int) -> bytes:
        end = self.pos + count
        if end > len(self.data):
            raise NbtError("truncated NBT data")
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    def string(self) -> str:
        (length,) = struct.unpack(">H", self.take(2))
        return self.take(length).decode("utf-8", errors="replace")

    def value(self, tag: int):
        if tag in _FIXED:
            size, fmt = _FIXED[tag]
            return struct.unpack(fmt, self.take(size))[0]
        if tag == TAG_STRING:
            return self.string()
        if tag == TAG_BYTE_ARRAY:
            (length,) = struct.unpack(">i", self.take(4))
            return self.take(max(0, length))
        if tag == TAG_INT_ARRAY:
            (length,) = struct.unpack(">i", self.take(4))
            return list(struct.unpack(f">{max(0, length)}i", self.take(4 * max(0, length))))
        if tag == TAG_LONG_ARRAY:
            (length,) = struct.unpack(">i", self.take(4))
            return list(struct.unpack(f">{max(0, length)}q", self.take(8 * max(0, length))))
        if tag == TAG_LIST:
            (item_tag,) = struct.unpack(">b", self.take(1))
            (length,) = struct.unpack(">i", self.take(4))
            return [self.value(item_tag) for _ in range(max(0, length))]
        if tag == TAG_COMPOUND:
            result: dict = {}
            while True:
                (child_tag,) = struct.unpack(">b", self.take(1))
                if child_tag == TAG_END:
                    return result
                # Name must be read before the value: in `d[k] = v` Python
                # evaluates v first, which would read the two out of order.
                name = self.string()
                result[name] = self.value(child_tag)
        raise NbtError(f"unsupported NBT tag {tag}")


def read_nbt_file(path: Path) -> dict:
    """Parse a gzipped NBT file into nested dicts."""
    try:
        with gzip.open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise NbtError(f"could not read {path}: {exc}") from exc

    reader = _Reader(raw)
    (tag,) = struct.unpack(">b", reader.take(1))
    if tag != TAG_COMPOUND:
        raise NbtError(f"{path} does not start with a compound tag")
    reader.string()  # Root name, conventionally empty.
    return reader.value(TAG_COMPOUND)


# --- Worlds -------------------------------------------------------------------


class PlayerLayout(str, Enum):
    """Where a save keeps per-player state."""

    MODERN = "modern"  # players/{data,stats,advancements} - server-ready as-is.
    LEGACY = "legacy"  # playerdata/ + host character inside level.dat's Player tag.
    UNKNOWN = "unknown"  # Never joined, so nothing has been written yet.


def _uuid_from_int_array(values: object) -> str | None:
    """Decode NBT's 4x int32 UUID encoding into canonical string form."""
    if not isinstance(values, list) or len(values) != 4:
        return None
    try:
        raw = b"".join(struct.pack(">i", int(v)) for v in values)
    except (struct.error, TypeError, ValueError):
        return None
    return str(uuid.UUID(bytes=raw))


@dataclass
class World:
    folder: Path
    display_name: str
    level_name: str
    last_played_ms: int | None
    version_name: str | None
    hardcore: bool
    difficulty: str | None
    owner_uuid: str | None
    player_layout: PlayerLayout
    has_legacy_player_tag: bool

    @property
    def folder_name(self) -> str:
        return self.folder.name

    @property
    def session_lock(self) -> Path:
        return self.folder / "session.lock"

    def needs_player_migration(self) -> bool:
        """True when the host's character would be lost on first server start."""
        return self.player_layout is PlayerLayout.LEGACY and self.has_legacy_player_tag


def _detect_player_layout(folder: Path) -> PlayerLayout:
    if (folder / "players").is_dir():
        return PlayerLayout.MODERN
    if (folder / "playerdata").is_dir():
        return PlayerLayout.LEGACY
    return PlayerLayout.UNKNOWN


def read_world(folder: Path) -> World:
    """Read one save folder. Falls back to the folder name if level.dat is unreadable."""
    folder = Path(folder)
    level_dat = folder / "level.dat"

    data: dict = {}
    try:
        data = read_nbt_file(level_dat).get("Data", {}) or {}
    except NbtError:
        pass  # A corrupt or in-flight level.dat shouldn't hide the world.

    level_name = str(data.get("LevelName") or folder.name)
    version = data.get("Version")
    version_name = None
    if isinstance(version, dict):
        raw_name = version.get("Name")
        version_name = str(raw_name) if raw_name is not None else None

    last_played = data.get("LastPlayed")

    # Difficulty and hardcore moved into a nested compound; fall back to the flat
    # keys so older saves still read correctly.
    settings = data.get("difficulty_settings")
    if not isinstance(settings, dict):
        settings = {}
    difficulty = settings.get("difficulty") or data.get("Difficulty")

    return World(
        folder=folder,
        display_name=level_name if level_name != folder.name else folder.name,
        level_name=level_name,
        last_played_ms=int(last_played) if isinstance(last_played, int) else None,
        version_name=version_name,
        hardcore=bool(settings.get("hardcore", data.get("hardcore"))),
        difficulty=str(difficulty) if difficulty is not None else None,
        # The world's creator, recorded by the client. More reliable than
        # guessing the host from usercache.json when prefilling ops.
        owner_uuid=_uuid_from_int_array(data.get("singleplayer_uuid")),
        player_layout=_detect_player_layout(folder),
        has_legacy_player_tag="Player" in data,
    )


def find_worlds(saves_dir: Path) -> list[World]:
    """List every save in an instance, newest first."""
    saves_dir = Path(saves_dir)
    if not saves_dir.is_dir():
        return []

    worlds = [
        read_world(child)
        for child in sorted(saves_dir.iterdir())
        if child.is_dir() and (child / "level.dat").is_file()
    ]
    worlds.sort(key=lambda w: w.last_played_ms or 0, reverse=True)
    return worlds


def folder_size(path: Path) -> int:
    """Total bytes under a folder. Walks the tree, so call it off the UI thread."""
    total = 0
    stack = [Path(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def is_world_busy(world_folder: Path) -> bool:
    """True when something already has the world open.

    Minecraft holds an exclusive lock on ``session.lock`` for as long as a world
    is loaded. We try to take that lock ourselves and immediately release it: if
    the attempt fails, someone else owns it. Nothing is modified either way.
    """
    lock_path = Path(world_folder) / "session.lock"
    if not lock_path.is_file():
        return False

    try:
        import msvcrt
    except ImportError:  # Non-Windows; the app is Windows-only but stay honest.
        return False

    try:
        with open(lock_path, "r+b") as handle:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return False
    except PermissionError:
        return True
    except OSError:
        # Unreadable for some other reason; assume free rather than block the user.
        return False
