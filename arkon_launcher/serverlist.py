"""The Minecraft client's multiplayer list, so the host can join in one click.

``servers.dat`` is plain NBT - not gzipped, unlike ``level.dat`` - holding one
compound per saved server. Adding the running world to it means the host opens
Minecraft and the entry is already there, named after the world.

**Auto-joining is deliberately not attempted.** Launching Minecraft straight
into a server needs the account's session token, which CurseForge holds and
which this app has no business touching. So the launcher does the two parts it
legitimately can - put the entry in the list, and open CurseForge - and leaves
the sign-in and the Play button to the user.

Writing someone's server list is a destructive operation if it goes wrong, so
existing entries are preserved exactly and the file is replaced atomically.
"""

from __future__ import annotations

import struct
from pathlib import Path

TAG_END = 0
TAG_BYTE = 1
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10

SERVERS_FILE = "servers.dat"


class ServerListError(RuntimeError):
    pass


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, count: int) -> bytes:
        chunk = self.data[self.pos : self.pos + count]
        if len(chunk) != count:
            raise ServerListError("servers.dat ended unexpectedly")
        self.pos += count
        return chunk

    def string(self) -> str:
        (length,) = struct.unpack(">H", self.take(2))
        return self.take(length).decode("utf-8", errors="replace")

    def value(self, tag: int):
        if tag == TAG_BYTE:
            return struct.unpack(">b", self.take(1))[0]
        if tag == TAG_STRING:
            return self.string()
        if tag == TAG_COMPOUND:
            return self.compound()
        if tag == TAG_LIST:
            (child,) = struct.unpack(">b", self.take(1))
            (count,) = struct.unpack(">i", self.take(4))
            return [self.value(child) for _ in range(max(count, 0))]
        raise ServerListError(f"unsupported tag {tag} in servers.dat")

    def compound(self) -> dict:
        result: dict = {}
        while True:
            (tag,) = struct.unpack(">b", self.take(1))
            if tag == TAG_END:
                return result
            # Name before value: in ``d[k] = v`` Python evaluates v first, which
            # would read the two out of order.
            name = self.string()
            result[name] = self.value(tag)


def _write_string(text: str) -> bytes:
    encoded = text.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded


def _write_entry(entry: dict) -> bytes:
    out = b""
    for key, value in entry.items():
        if isinstance(value, str):
            out += struct.pack(">b", TAG_STRING) + _write_string(key) + _write_string(value)
        elif isinstance(value, int):
            out += struct.pack(">b", TAG_BYTE) + _write_string(key) + struct.pack(">b", value)
        # Anything else is dropped rather than guessed at; the only fields
        # Minecraft writes here are strings and bytes.
    return out + struct.pack(">b", TAG_END)


def read_servers(path: Path) -> list[dict]:
    """Every saved server, or [] when the file is absent."""
    path = Path(path)
    if not path.is_file():
        return []
    try:
        reader = _Reader(path.read_bytes())
    except OSError as exc:
        raise ServerListError(f"could not read {path}: {exc}") from exc

    (tag,) = struct.unpack(">b", reader.take(1))
    if tag != TAG_COMPOUND:
        raise ServerListError("servers.dat does not start with a compound tag")
    reader.string()  # Root name, conventionally empty.
    root = reader.compound()

    servers = root.get("servers")
    return [s for s in servers if isinstance(s, dict)] if isinstance(servers, list) else []


def write_servers(path: Path, servers: list[dict]) -> None:
    """Replace the file, atomically."""
    # List elements are bare payloads: the element type is declared once in the
    # list header, so writing a type byte per entry corrupts the file.
    body = b""
    for entry in servers:
        body += _write_entry(entry)

    payload = (
        struct.pack(">b", TAG_COMPOUND)
        + _write_string("")
        + struct.pack(">b", TAG_LIST)
        + _write_string("servers")
        + struct.pack(">b", TAG_COMPOUND)
        + struct.pack(">i", len(servers))
        + body
        + struct.pack(">b", TAG_END)
    )

    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def upsert(instance_dir: Path, name: str, address: str) -> str:
    """Add or update the entry for ``address``. Returns what happened.

    Matched on address rather than name, since that is what makes two entries
    duplicates from the client's point of view - and the world name is exactly
    the part expected to change between sessions.
    """
    path = Path(instance_dir) / SERVERS_FILE
    servers = read_servers(path)

    for entry in servers:
        if str(entry.get("ip", "")).strip().lower() == address.lower():
            if entry.get("name") == name:
                return "unchanged"
            entry["name"] = name
            write_servers(path, servers)
            return "updated"

    servers.append({"name": name, "ip": address})
    write_servers(path, servers)
    return "added"
