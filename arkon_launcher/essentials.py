"""Integration with Arkon Essentials, when it is installed.

Two things are read, both optional. If the mod is absent, or present but older
than the features below, everything here returns empty and the UI simply does
not show those sections - the launcher must stay useful with a plain Fabric
server and no first-party mod at all.

**Ability manifest** - a resource inside the mod jar listing the permission
nodes it gates, so the launcher can offer them as toggles without hardcoding a
list that goes stale. Read from the jar, so it works with the server stopped and
needs no protocol.

**Live telemetry** - lines the mod prints to stdout, which the launcher already
reads. Used for things Minecraft does not expose to the console at all, per
player ping being the motivating case. The console pane filters these out so
they never appear as noise.

The wire formats are documented in INTEGRATION.md.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MOD_ID = "arkonessentials"

# Candidate locations for the manifest, most specific first. Several are
# accepted because the mod is being written in parallel with this.
MANIFEST_PATHS = (
    f"assets/{MOD_ID}/permissions.json",
    f"assets/{MOD_ID}/abilities.json",
    f"{MOD_ID}.permissions.json",
)

# "[ARKON] {json}" on stdout. Prefixed so it is unambiguous in a log full of
# other mods' output, and so the console can strip it.
TELEMETRY_LINE = re.compile(r"\[ARKON\]\s*(\{.*\})\s*$")


@dataclass(frozen=True)
class Ability:
    node: str
    label: str
    category: str = "General"
    description: str = ""

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.category.lower(), self.label.lower())


@dataclass
class PlayerTelemetry:
    """Per-player facts only the mod can know."""

    name: str
    ping_ms: int | None = None
    session_seconds: int | None = None
    afk: bool = False
    extra: dict = field(default_factory=dict)


def find_jar(mods_dir: Path) -> Path | None:
    """The installed Arkon Essentials jar, if there is one."""
    mods_dir = Path(mods_dir)
    if not mods_dir.is_dir():
        return None
    for jar in sorted(mods_dir.glob("*.jar")):
        if MOD_ID in jar.name.lower():
            return jar
    return None


def is_installed(mods_dir: Path) -> bool:
    return find_jar(mods_dir) is not None


def read_abilities(mods_dir: Path) -> list[Ability]:
    """Abilities declared by the mod. Empty when it is absent or older.

    Tolerant about shape: a bare list, or an object with an "abilities" or
    "permissions" key, and per-entry either "node" or "permission". The mod is
    being written alongside this, so being strict would just mean the two have
    to land in the same commit.
    """
    jar = find_jar(mods_dir)
    if jar is None:
        return []

    payload = None
    try:
        with zipfile.ZipFile(jar) as archive:
            names = set(archive.namelist())
            for candidate in MANIFEST_PATHS:
                if candidate in names:
                    payload = json.loads(archive.read(candidate).decode("utf-8"))
                    break
    except (OSError, zipfile.BadZipFile, ValueError):
        return []

    if payload is None:
        return []

    if isinstance(payload, dict):
        entries = payload.get("abilities") or payload.get("permissions") or []
    else:
        entries = payload

    abilities: list[Ability] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        node = str(entry.get("node") or entry.get("permission") or "").strip()
        if not node:
            continue
        abilities.append(
            Ability(
                node=node,
                label=str(entry.get("label") or entry.get("name") or node),
                category=str(entry.get("category") or entry.get("group") or "General"),
                description=str(entry.get("description") or ""),
            )
        )

    abilities.sort(key=lambda a: a.sort_key)
    return abilities


def categories(abilities: list[Ability]) -> dict[str, list[Ability]]:
    grouped: dict[str, list[Ability]] = {}
    for ability in abilities:
        grouped.setdefault(ability.category, []).append(ability)
    return grouped


def is_telemetry(line: str) -> bool:
    return "[ARKON]" in line and TELEMETRY_LINE.search(line) is not None


def parse_telemetry(line: str) -> dict | None:
    """Decode one telemetry line, or None if it is not one."""
    match = TELEMETRY_LINE.search(line)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def players_from_telemetry(payload: dict) -> list[PlayerTelemetry]:
    """Pull per-player rows out of a telemetry payload.

    Expected shape:
        {"type": "players", "players": [{"name": "X", "ping": 42, "session": 900}]}
    """
    if payload.get("type") not in (None, "players", "player"):
        return []

    rows = payload.get("players")
    if rows is None and payload.get("name"):
        rows = [payload]
    if not isinstance(rows, list):
        return []

    found: list[PlayerTelemetry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue

        def number(*keys):
            for key in keys:
                value = row.get(key)
                if isinstance(value, (int, float)):
                    return int(value)
            return None

        found.append(
            PlayerTelemetry(
                name=name,
                ping_ms=number("ping", "ping_ms", "latency"),
                session_seconds=number("session", "session_seconds", "online_for"),
                afk=bool(row.get("afk")),
                extra={
                    k: v
                    for k, v in row.items()
                    if k not in {"name", "ping", "ping_ms", "latency", "session",
                                 "session_seconds", "online_for", "afk"}
                },
            )
        )
    return found
