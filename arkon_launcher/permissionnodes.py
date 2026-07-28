"""Working out which permission nodes exist on a given server.

There is no registry to ask. On Bukkit, plugins declare their permissions in
``plugin.yml``, so a server-wide list exists; **Fabric has no equivalent**. A
node is just a string a mod passes to a check at the moment it needs one, so
nothing declares them up front and ``/lp`` has no "list all permissions" command
because there is nothing for it to list.

Four sources between them cover most of the ground:

1. **The command tree.** ``help`` enumerates every registered command, modded
   ones included - 290 of them on the reference pack. LuckPerms on Fabric gates
   commands behind ``minecraft.command.<name>``, so this is derived per-pack with
   nothing hardcoded.
2. **LuckPerms' own nodes**, which are a fixed documented set.
3. **Verbose recording.** ``lp verbose on`` logs every permission check as it
   happens, so playing for a few minutes reveals the real node strings a pack
   uses - including ones no static list could predict.
4. **Nodes already assigned** to any group or user.

None of this is guaranteed complete, and the UI says so rather than implying the
list is exhaustive. Free-text entry always remains available.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import paths

# "/gamemode <mode>" or "/lp ..." - the leading token is the command name.
HELP_COMMAND = re.compile(r"^/([A-Za-z0-9_\-]{2,})")

# Commands worth turning into nodes. WorldEdit and friends register symbol
# aliases (`//`, `!`, `.s`, `;`) that are real commands but useless as
# checkboxes, so anything that isn't word-shaped is dropped.
VALID_COMMAND = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{1,}$")


class Source(str, Enum):
    COMMAND = "command"
    LUCKPERMS = "luckperms"
    RECORDED = "recorded"
    ASSIGNED = "assigned"
    MANUAL = "manual"


@dataclass(frozen=True)
class Node:
    node: str
    source: Source
    description: str = ""

    @property
    def label(self) -> str:
        return f"{self.node}   -   {self.description}" if self.description else self.node


# LuckPerms' own permissions. Fixed set, worth offering because managing
# permissions is itself a permission.
LUCKPERMS_NODES: tuple[tuple[str, str], ...] = (
    ("luckperms.*", "Full LuckPerms access"),
    ("luckperms.user.info", "View a player's permissions"),
    ("luckperms.user.permission.set", "Change a player's permissions"),
    ("luckperms.user.parent.add", "Add a player to a group"),
    ("luckperms.group.info", "View a group"),
    ("luckperms.group.permission.set", "Change a group's permissions"),
    ("luckperms.creategroup", "Create groups"),
    ("luckperms.deletegroup", "Delete groups"),
    ("luckperms.listgroups", "List groups"),
    ("luckperms.track.info", "View promotion tracks"),
    ("luckperms.promote", "Promote players along a track"),
    ("luckperms.demote", "Demote players along a track"),
    ("luckperms.verbose", "Record permission checks"),
)

# Handy nodes that are not commands.
EXTRA_NODES: tuple[tuple[str, str], ...] = (
    ("*", "Everything - full operator equivalent"),
    ("minecraft.command.*", "Every vanilla command"),
    ("minecraft.autocraft", "Use the crafting recipe book"),
)


def nodes_from_help(help_lines: list[str]) -> list[Node]:
    """Turn a ``help`` reply into ``minecraft.command.<name>`` nodes."""
    commands: set[str] = set()
    for line in help_lines:
        text = line.split("]: ", 1)[-1] if "]: " in line else line
        match = HELP_COMMAND.match(text.strip())
        if match and VALID_COMMAND.match(match.group(1)):
            commands.add(match.group(1))

    return [
        Node(f"minecraft.command.{name}", Source.COMMAND, f"Use /{name}")
        for name in sorted(commands)
    ]


def builtin_nodes() -> list[Node]:
    return [
        Node(node, Source.LUCKPERMS, description) for node, description in LUCKPERMS_NODES
    ] + [Node(node, Source.MANUAL, description) for node, description in EXTRA_NODES]


# --- Verbose recording --------------------------------------------------------

# LuckPerms verbose lines name the checked node. The exact shape is matched
# loosely on purpose: it carries player, node and result in one line, and the
# node is the part shaped like a permission.
VERBOSE_LINE = re.compile(r"\bVB\b.*?([a-z0-9_\-]+(?:\.[a-z0-9_\-*]+)+)", re.I)
NODE_SHAPE = re.compile(r"^[a-z0-9_\-]+(?:\.[a-z0-9_\-*]+)+$", re.I)


# Used when scanning continuously in the background. Command checks are both the
# noisiest and the least informative - they are already enumerated from the
# command tree - so they are excluded at the source rather than being logged and
# then discarded. LuckPerms accepts this negated-glob form directly.
#
# Measured on a real 89-mod server: verbose produces *zero* lines on an idle
# server and about two per second while commands are being run, and a
# command-tree rebuild costs nothing. Leaving it on is cheap.
PASSIVE_FILTER = "!minecraft.command.*"


def is_verbose_line(line: str) -> bool:
    return "[LP] VB" in line


def nodes_from_verbose(lines: list[str]) -> list[str]:
    """Pull node names out of verbose log lines."""
    found: list[str] = []
    for line in lines:
        if "[LP]" not in line:
            continue
        match = VERBOSE_LINE.search(line)
        if match:
            node = match.group(1)
            if NODE_SHAPE.match(node) and node not in found:
                found.append(node)
    return found


# --- Persistence --------------------------------------------------------------
#
# Discovered nodes are pack-specific, so they are stored with the instance rather
# than with the app.


def recorded_path(instance_dir: Path) -> Path:
    return paths.instance_data_dir(instance_dir) / "permission_nodes.json"


def load_recorded(instance_dir: Path) -> list[str]:
    path = recorded_path(instance_dir)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    nodes = data.get("nodes") if isinstance(data, dict) else None
    return [n for n in (nodes or []) if isinstance(n, str)]


def save_recorded(instance_dir: Path, nodes: list[str]) -> None:
    path = recorded_path(instance_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = sorted(set(load_recorded(instance_dir)) | set(nodes))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "_comment": "Permission nodes seen on this pack, discovered with "
                "LuckPerms verbose recording.",
                "nodes": merged,
            },
            handle,
            indent=2,
        )


# --- Attributing nodes to the mod that owns them ------------------------------
#
# Best effort, and it says so in the UI. Two rules, both easy to explain:
#
#   * a node whose first segment is a mod id belongs to that mod
#     (worldedit.brush.sphere -> worldedit)
#   * minecraft.command.<name> belongs to a mod when <name> is that mod's id
#     (minecraft.command.waystones -> waystones), otherwise it is filed under
#     Minecraft
#
# A mod that adds a command under an unrelated name - arkonessentials providing
# /home, say - cannot be attributed from the command tree alone, so it shows up
# under Minecraft until recording finds its real nodes.

MINECRAFT_OWNER = "__minecraft__"
OTHER_OWNER = "__other__"

MINECRAFT_LABEL = "Minecraft commands"
OTHER_LABEL = "Unattributed"


@dataclass
class ModOwner:
    key: str
    label: str
    count: int = 0


def attribute_node(node: str, mod_ids: dict[str, str]) -> str:
    """Return the owning mod id, or a MINECRAFT_OWNER / OTHER_OWNER sentinel."""
    segments = node.split(".")
    head = segments[0].lower()

    if head in mod_ids:
        return head

    if node.startswith("minecraft.command.") and len(segments) >= 3:
        command = segments[2].lower()
        if command in mod_ids:
            return command
        return MINECRAFT_OWNER

    if node.startswith("minecraft.") or node == "*":
        return MINECRAFT_OWNER

    return OTHER_OWNER


def owners_for(nodes: list[Node], mod_ids: dict[str, str]) -> list[ModOwner]:
    """Count nodes per owner, for building the filter dropdown."""
    counts: dict[str, int] = {}
    for node in nodes:
        key = attribute_node(node.node, mod_ids)
        counts[key] = counts.get(key, 0) + 1

    owners: list[ModOwner] = []
    if counts.get(MINECRAFT_OWNER):
        owners.append(ModOwner(MINECRAFT_OWNER, MINECRAFT_LABEL, counts[MINECRAFT_OWNER]))

    for mod_id, label in sorted(mod_ids.items(), key=lambda kv: kv[1].lower()):
        if counts.get(mod_id):
            owners.append(ModOwner(mod_id, label, counts[mod_id]))

    if counts.get(OTHER_OWNER):
        owners.append(ModOwner(OTHER_OWNER, OTHER_LABEL, counts[OTHER_OWNER]))

    return owners


@dataclass
class NodeCatalogue:
    """Everything known about which nodes exist, and where each came from."""

    nodes: list[Node] = field(default_factory=list)

    def merge(self, more: list[Node]) -> None:
        known = {n.node for n in self.nodes}
        for node in more:
            if node.node not in known:
                self.nodes.append(node)
                known.add(node.node)

    def excluding(self, assigned: set[str]) -> list[Node]:
        return [n for n in self.nodes if n.node not in assigned]

    def search(self, text: str, owner: str = "", mod_ids: dict[str, str] | None = None) -> list[Node]:
        text = text.strip().lower()
        results = self.nodes

        if owner and mod_ids is not None:
            results = [n for n in results if attribute_node(n.node, mod_ids) == owner]

        if text:
            results = [
                n
                for n in results
                if text in n.node.lower() or text in n.description.lower()
            ]
        return list(results)

    def sorted_nodes(self) -> list[Node]:
        # Recorded nodes first: they are the ones this pack demonstrably uses.
        order = {
            Source.RECORDED: 0,
            Source.ASSIGNED: 1,
            Source.LUCKPERMS: 2,
            Source.MANUAL: 3,
            Source.COMMAND: 4,
        }
        return sorted(self.nodes, key=lambda n: (order.get(n.source, 9), n.node))


def build_catalogue(
    help_lines: list[str],
    recorded: list[str],
    assigned: set[str] | None = None,
) -> NodeCatalogue:
    catalogue = NodeCatalogue()
    catalogue.merge([Node(n, Source.RECORDED, "seen in use on this server") for n in recorded])
    if assigned:
        catalogue.merge([Node(n, Source.ASSIGNED) for n in sorted(assigned)])
    catalogue.merge(builtin_nodes())
    catalogue.merge(nodes_from_help(help_lines))
    return catalogue
