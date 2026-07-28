"""Operators, whitelist, and the people who play on the server.

Two ways to change things, and which one is correct depends on the server:

* **Stopped** - edit ``ops.json`` / ``whitelist.json`` directly.
* **Running** - send ``/op``, ``/whitelist add`` and so on, because the running
  server owns those files and would overwrite anything written underneath it.

Names are seeded from two places the instance already knows: the world's own
``singleplayer_uuid`` (the person who created it - the obvious first operator)
and ``usercache.json`` (everyone the client has seen).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

LUCKPERMS_PREFIX = "luckperms"


@dataclass
class KnownPlayer:
    name: str
    uuid: str | None = None
    is_op: bool = False
    is_whitelisted: bool = False
    is_online: bool = False
    is_world_owner: bool = False

    @property
    def role(self) -> str:
        if self.is_world_owner:
            return "World owner"
        return "Operator" if self.is_op else "Player"


def _read_json_list(path: Path) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_json_list(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(entries, handle, indent=2)
    temporary.replace(path)


def read_ops(server_dir: Path) -> list[dict]:
    return _read_json_list(Path(server_dir) / "ops.json")


def read_whitelist(server_dir: Path) -> list[dict]:
    return _read_json_list(Path(server_dir) / "whitelist.json")


def read_usercache(instance_dir: Path) -> list[dict]:
    """Everyone the Minecraft client has resolved - a ready-made friend list."""
    return _read_json_list(Path(instance_dir) / "usercache.json")


def gather_players(
    instance_dir: Path,
    server_dir: Path,
    world_owner_uuid: str | None = None,
    online_names: set[str] | None = None,
) -> list[KnownPlayer]:
    """Merge every source into one list, world owner first, then operators."""
    online_names = online_names or set()
    players: dict[str, KnownPlayer] = {}

    def slot(name: str, uuid: str | None) -> KnownPlayer:
        key = name.lower()
        if key not in players:
            players[key] = KnownPlayer(name=name, uuid=uuid)
        elif uuid and not players[key].uuid:
            players[key].uuid = uuid
        return players[key]

    for entry in read_usercache(instance_dir):
        name = entry.get("name")
        if isinstance(name, str):
            slot(name, entry.get("uuid"))

    for entry in read_ops(server_dir):
        name = entry.get("name")
        if isinstance(name, str):
            slot(name, entry.get("uuid")).is_op = True

    for entry in read_whitelist(server_dir):
        name = entry.get("name")
        if isinstance(name, str):
            slot(name, entry.get("uuid")).is_whitelisted = True

    for name in online_names:
        slot(name, None).is_online = True

    if world_owner_uuid:
        for player in players.values():
            if player.uuid and player.uuid.lower() == world_owner_uuid.lower():
                player.is_world_owner = True

    return sorted(
        players.values(),
        key=lambda p: (not p.is_world_owner, not p.is_op, p.name.lower()),
    )


def set_op_offline(server_dir: Path, player: KnownPlayer, op: bool) -> None:
    """Edit ops.json directly. Only valid while the server is stopped."""
    path = Path(server_dir) / "ops.json"
    entries = [
        entry
        for entry in read_ops(server_dir)
        if str(entry.get("name", "")).lower() != player.name.lower()
    ]
    if op:
        entries.append(
            {
                "uuid": player.uuid or "",
                "name": player.name,
                "level": 4,
                "bypassesPlayerLimit": False,
            }
        )
    _write_json_list(path, entries)


def set_whitelisted_offline(server_dir: Path, player: KnownPlayer, allowed: bool) -> None:
    """Edit whitelist.json directly. Only valid while the server is stopped."""
    path = Path(server_dir) / "whitelist.json"
    entries = [
        entry
        for entry in read_whitelist(server_dir)
        if str(entry.get("name", "")).lower() != player.name.lower()
    ]
    if allowed:
        entries.append({"uuid": player.uuid or "", "name": player.name})
    _write_json_list(path, entries)


def name_for_uuid(instance_dir: Path, uuid: str) -> str | None:
    """Look a UUID up in the client's usercache. None if it has never been seen."""
    target = uuid.lower()
    for entry in read_usercache(instance_dir):
        if str(entry.get("uuid", "")).lower() == target:
            name = entry.get("name")
            return str(name) if name else None
    return None


def has_luckperms(server_mods_dir: Path) -> bool:
    """True when LuckPerms is among the mods actually mirrored to the server.

    Checked against the server's own mods folder rather than the instance's, so
    the panel only appears when the mod will really be loaded.
    """
    directory = Path(server_mods_dir)
    if not directory.is_dir():
        return False
    return any(
        jar.name.lower().startswith(LUCKPERMS_PREFIX) for jar in directory.glob("*.jar")
    )
