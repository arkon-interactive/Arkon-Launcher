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
    # --- Schema 2 ---
    kind: str = "ability"  # ability | mode | grant | value | immunity
    parent: str = ""  # Where this belongs in a UI.
    inherits_from: str = ""  # Where a permission grant flows from.
    exclusive_group: str = ""  # Modes turn each other off.
    command: str = ""  # What a player types to use it.
    grant_command: str = ""  # What an operator types to grant it.
    revoke_command: str = ""

    @property
    def is_value(self) -> bool:
        """Config-backed: a number or flag read from a setting, not a grant.

        ``kind`` answers this offline, which the old type-based guess could not:
        one of these is ``type: boolean`` and so looked like an ordinary toggle.
        """
        return self.kind == "value"

    @property
    def tooltip(self) -> str:
        """Description plus whatever commands genuinely exist.

        Only modes have a grant command - everything else is granted through the
        permission provider. Inventing ``/admin grant <node>`` for the other
        forty would be syntax the server does not accept, so absence is reported
        as what it means rather than papered over.
        """
        parts = [self.description or self.label, "", self.node]
        if self.command:
            parts.append(f"Used with: {self.command}")
        if self.grant_command:
            parts.append(f"Granted with: {self.grant_command}")
        else:
            parts.append("Granted through the permission provider.")
        if self.config_key:
            parts.append(f"Set by: {self.config_key}")
        return "\n".join(parts)

    @property
    def path(self) -> str:
        """The form ``/arkon perms`` reports: no namespace, dots kept."""
        if self.id and ":" in self.id:
            return self.id.split(":", 1)[1]
        prefix = f"{MOD_ID}."
        return self.node[len(prefix):] if self.node.startswith(prefix) else self.node

    @property
    def lexical_parent(self) -> str:
        """The node one level up by name.

        Kept only as a fallback for manifests older than schema 2. Schema 2 says
        outright which node this nests under (``parent``) and which one a grant
        flows from (``inheritsFrom``), and those two genuinely differ - so a name
        is no longer the thing to reason from.
        """
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


def _read_manifest(mods_dir: Path, candidates) -> dict | None:
    """First readable manifest from the jar, or None."""
    jar = find_jar(mods_dir)
    if jar is None:
        return None
    try:
        with zipfile.ZipFile(jar) as archive:
            names = set(archive.namelist())
            for candidate in candidates:
                if candidate in names:
                    payload = json.loads(archive.read(candidate).decode("utf-8"))
                    return payload if isinstance(payload, (dict, list)) else None
    except (OSError, zipfile.BadZipFile, ValueError):
        return None
    return None


def read_abilities(mods_dir: Path) -> list[Ability]:
    """Abilities declared by the mod. Empty when it is absent or older.

    Tolerant about shape: a bare list, or an object with an "abilities" or
    "permissions" key, and per-entry either "node" or "permission". Versions
    before the manifest existed simply yield nothing.
    """
    payload = _read_manifest(mods_dir, MANIFEST_PATHS)
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
                kind=str(entry.get("kind") or "ability"),
                parent=str(entry.get("parent") or ""),
                inherits_from=str(entry.get("inheritsFrom") or entry.get("inherits_from") or ""),
                exclusive_group=str(
                    entry.get("exclusiveGroup") or entry.get("exclusive_group") or ""
                ),
                command=str(entry.get("command") or ""),
                grant_command=str(entry.get("grantCommand") or entry.get("grant_command") or ""),
                revoke_command=str(
                    entry.get("revokeCommand") or entry.get("revoke_command") or ""
                ),
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


@dataclass(frozen=True)
class Setting:
    """One entry from the mod's settings manifest."""

    key: str
    label: str
    description: str = ""
    category: str = "General"
    type: str = "boolean"
    default: object = None
    command: str = ""
    minimum: float | None = None
    maximum: float | None = None

    @property
    def tooltip(self) -> str:
        parts = [self.description or self.label, "", self.key]
        if self.minimum is not None and self.maximum is not None:
            parts.append(f"Range: {self.minimum} to {self.maximum}")
        if self.default is not None:
            parts.append(f"Default: {self.default}")
        if self.command:
            parts.append(f"Command: {self.command}")
        return "\n".join(parts)


SETTINGS_PATHS = (
    f"assets/{MOD_ID}/settings.json",
    f"assets/{MOD_ID}/config.json",
)


def read_settings(mods_dir: Path) -> list[Setting]:
    """Setting metadata from the jar. Empty on versions that predate it.

    Read from the mod rather than inferred from the config file, so the labels
    and descriptions are the mod author's current wording - the alternative was
    showing raw keys like ``afkTimeoutSeconds``, and hand-written copies go
    stale the moment a setting is reworded.
    """
    payload = _read_manifest(mods_dir, SETTINGS_PATHS)
    if payload is None:
        return []

    entries = payload.get("settings") or payload.get("options") or []
    settings: list[Setting] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or entry.get("name") or "").strip()
        if not key:
            continue
        settings.append(
            Setting(
                key=key,
                label=str(entry.get("label") or key),
                description=str(entry.get("description") or ""),
                category=str(entry.get("category") or "General"),
                type=str(entry.get("type") or "boolean"),
                default=entry.get("default"),
                command=str(entry.get("command") or ""),
                minimum=entry.get("min"),
                maximum=entry.get("max"),
            )
        )
    return settings


def settings_by_key(settings: list[Setting]) -> dict[str, Setting]:
    return {setting.key: setting for setting in settings}


# --- Shaping abilities for a UI ----------------------------------------------


def children_of(abilities: list[Ability]) -> dict[str, list[Ability]]:
    """node -> the abilities that nest under it in a UI.

    Keyed on ``parent``, never on dots. The manifest keeps ``parent`` and
    ``inheritsFrom`` apart because they genuinely disagree: ``admin.mode``
    inherits from ``admin`` but is not shown inside it, while ``home.limit``
    both inherits from and nests under ``home``. Deriving either from the dotted
    name gets the other one wrong.
    """
    nested: dict[str, list[Ability]] = {}
    for ability in abilities:
        if ability.parent:
            nested.setdefault(ability.parent, []).append(ability)
    return nested


def top_level(abilities: list[Ability]) -> list[Ability]:
    return [ability for ability in abilities if not ability.parent]


POWER_CATEGORIES = {"Movement"}


def live_state(abilities: list[Ability]) -> list[Ability]:
    """The subset that describes what a player *is* right now, not what they may do.

    The panel this feeds swaps a player's active mode and flips their powers on
    the fly; it is not a permission editor, which is what the Permissions tab is
    for. So the permission-shaped kinds - ``grant`` and ``immunity`` - and the
    command gates (``home``, ``tps``, ``tp``…) are left out, and what remains is
    the modes, the powers, and the settings hanging off them.
    """
    modes = {a.node for a in abilities if a.kind == "mode"}

    def belongs(ability: Ability) -> bool:
        if ability.kind in ("grant", "immunity"):
            return False
        if ability.kind == "mode" or ability.category in POWER_CATEGORIES:
            return True
        # A setting attached to a mode - Build's reach, Vanish's noclip.
        seen, parent = set(), ability.parent
        while parent and parent not in seen:
            if parent in modes:
                return True
            seen.add(parent)
            parent = next((a.parent for a in abilities if a.node == parent), "")
        return False

    return [a for a in abilities if belongs(a)]


def exclusive_groups(abilities: list[Ability]) -> dict[str, list[Ability]]:
    """Group name -> members that turn each other off."""
    groups: dict[str, list[Ability]] = {}
    for ability in abilities:
        if ability.exclusive_group:
            groups.setdefault(ability.exclusive_group, []).append(ability)
    return groups


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
