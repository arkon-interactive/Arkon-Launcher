"""Integration with Arkon Essentials, when it is installed.

Three things are read, all optional. If the mod is absent, or older than the
features below, everything here returns empty and the UI simply does not show
those sections - the launcher must stay useful with a plain Fabric server and no
first-party mod at all.

**The permission manifest** - ``assets/arkonessentials/permissions.json`` inside
the jar, declaring every node the mod gates, with a label, a category, a type,
and what applies when nothing grants or denies it. Read from the jar, so it
works with the server stopped and needs no protocol.

**Per-player latency** - ``/arkon ping``, which returns one line of JSON. Pulled
on demand rather than pushed to the log: Minecraft exposes latency nowhere a
tool can reach, but a mod printing it continuously would be noise in a console
someone is trying to read.

**Resolved permissions** - ``/arkon perms <player|uuid>``, which reports every
gate as ``name = true (granted)``. Better than reading LuckPerms directly: it
works with any permission provider, it answers for players who have never
connected, and it distinguishes what the provider said from what the mod's own
fallback decided.

Two naming details matter when correlating these:

* The manifest's ``node`` is the dotted form a permission mod wants
  (``arkonessentials.home.named``); its ``id`` is the namespaced identifier the
  mod checks (``arkonessentials:home.named``). ``/arkon perms`` reports the
  *path* only - ``home.named`` - so mapping is by :func:`Ability.path`.
* Dotted children inherit from their parent. Granting ``arkonessentials.home``
  also grants ``home.named`` unless that child is denied explicitly, so a node
  can be in force without appearing anywhere as an explicit grant.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
import zipfile

MOD_ID = "arkonessentials"

# Candidate locations for the manifest, most specific first. Several are
# accepted because the mod is written in parallel with this.
MANIFEST_PATHS = (
    f"assets/{MOD_ID}/permissions.json",
    f"assets/{MOD_ID}/abilities.json",
    f"{MOD_ID}.permissions.json",
)

# What applies when nothing grants or denies a node.
DEFAULT_MEANING = {
    "public": "everyone",
    "operator": "operators",
    "config": "from a server setting",
    "denied": "nobody",
}

PING_COMMAND = "arkon ping"

# `  home.named = true (granted)` - two leading spaces from the mod, and the
# server's own `[HH:MM:SS] [Server thread/INFO]:` prefix ahead of that.
PERMS_LINE = re.compile(r"^\s*([a-z0-9_.]+)\s*=\s*(true|false)\s*\((granted|denied|default)\)\s*$")


def perms_command(target: str) -> str:
    return f"arkon perms {target}"


@dataclass(frozen=True)
class Ability:
    node: str
    label: str
    category: str = "General"
    description: str = ""
    id: str = ""
    type: str = "boolean"
    default_kind: str = "operator"
    config_key: str = ""

    @property
    def path(self) -> str:
        """The form ``/arkon perms`` reports: no namespace, dots kept."""
        if self.id and ":" in self.id:
            return self.id.split(":", 1)[1]
        prefix = f"{MOD_ID}."
        return self.node[len(prefix):] if self.node.startswith(prefix) else self.node

    @property
    def parent(self) -> str:
        """The node one level up, or "" at the top."""
        return self.node.rsplit(".", 1)[0] if "." in self.node else ""

    @property
    def is_numeric(self) -> bool:
        return self.type != "boolean"

    @property
    def default_text(self) -> str:
        if self.default_kind == "config" and self.config_key:
            return f"default: {self.config_key}"
        return f"default: {DEFAULT_MEANING.get(self.default_kind, self.default_kind)}"

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.category.lower(), self.label.lower())


@dataclass
class PlayerTelemetry:
    """Per-player facts only the mod can know."""

    name: str
    uuid: str = ""
    ping_ms: int | None = None
    hidden: bool = False


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
    "permissions" key, and per-entry either "node" or "permission". Versions
    before the manifest existed simply yield nothing.
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
                id=str(entry.get("id") or ""),
                type=str(entry.get("type") or "boolean"),
                default_kind=str(entry.get("default") or "operator"),
                config_key=str(entry.get("configKey") or entry.get("config_key") or ""),
            )
        )

    abilities.sort(key=lambda a: a.sort_key)
    return abilities


def categories(abilities: list[Ability]) -> dict[str, list[Ability]]:
    grouped: dict[str, list[Ability]] = {}
    for ability in abilities:
        grouped.setdefault(ability.category, []).append(ability)
    return grouped


def by_path(abilities: list[Ability]) -> dict[str, Ability]:
    return {ability.path: ability for ability in abilities}


def granting_parent(node: str, granted: set[str]) -> str:
    """The nearest ancestor of ``node`` that is granted, or "".

    A grant on ``arkonessentials.home`` carries to ``home.named`` and
    ``home.limit``, so a node can be in force with nothing naming it.
    """
    parts = node.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        ancestor = ".".join(parts[:cut])
        if ancestor in granted:
            return ancestor
    return ""


# --- /arkon ping --------------------------------------------------------------


def _lines(reply) -> list[str]:
    """Accept either a block of text or the list of lines ``query`` returns."""
    if isinstance(reply, str):
        return reply.splitlines()
    return [str(line) for line in reply or []]


def _json_objects(reply):
    """Every line of the reply that ends in a JSON object, decoded.

    The mod's replies arrive with the server's own log prefix in front, and
    there is no marker to key off, so candidate lines are found by shape.
    """
    for line in _lines(reply):
        start = line.find("{")
        if start < 0 or not line.rstrip().endswith("}"):
            continue
        try:
            payload = json.loads(line[start:].strip())
        except ValueError:
            continue
        if isinstance(payload, dict):
            yield payload


def parse_ping_report(reply) -> list[PlayerTelemetry]:
    """Decode the reply to ``/arkon ping``. Empty if it is not in there."""
    for payload in _json_objects(reply):
        rows = payload.get("players")
        if not isinstance(rows, list):
            continue

        found: list[PlayerTelemetry] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            ping = row.get("ping")
            found.append(
                PlayerTelemetry(
                    name=name,
                    uuid=str(row.get("uuid") or ""),
                    ping_ms=int(ping) if isinstance(ping, (int, float)) else None,
                    hidden=bool(row.get("hidden")),
                )
            )
        return found
    return []


# --- /arkon perms -------------------------------------------------------------


def parse_perms_report(reply) -> dict[str, tuple[bool, str]]:
    """Decode ``/arkon perms``: path -> (effective, "granted"/"denied"/"default").

    The origin is the interesting half. A node reading ``default`` means nothing
    granted or denied it and the mod's fallback decided - and for an operator
    that fallback is always yes, which is why a permission tier looks like it
    "works" until it is tested on an unopped account.
    """
    resolved: dict[str, tuple[bool, str]] = {}
    for line in _lines(reply):
        # Strip the server's log prefix, if any, then match the mod's own shape.
        body = line.split("]: ", 1)[-1] if "]: " in line else line
        match = PERMS_LINE.match(body)
        if match:
            path, value, origin = match.groups()
            resolved[path] = (value == "true", origin)
    return resolved
