"""Driving LuckPerms through console commands, and reading its replies.

LuckPerms keeps its data in an H2 database by default, which is not something
worth reaching into from here. Everything therefore goes through ``/lp``
commands and the output is parsed back - which is also what makes the GUI honest
about state, since it only ever shows what the server just told us.

All parsing is written against real output captured from a live server:

    [LP] Showing group entries:    (page 1 of 1 - 1 entries)
    [LP] Groups: (name, weight, tracks)
    [LP] -  default - 0

    [LP] > User Info: fenixrysing
    [LP] - UUID: 77086af9-eeee-4bf5-90af-d223670841f8
    [LP] - Parent Groups:
    [LP]     > default
    [LP]     Primary Group: default

Lines arrive with the server's log prefix attached and may carry colour codes,
so both are stripped before matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# "[16:04:52] [Server thread/INFO]: [LP] ..." -> "[LP] ..."
LOG_PREFIX = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s*\[[^\]]+\]:\s?")
COLOUR_CODES = re.compile(r"[§&][0-9a-fk-orA-FK-OR]")
LP_PREFIX = re.compile(r"^\[LP\]\s?")

GROUP_ENTRY = re.compile(r"^-\s+(\S+)\s+-\s+(-?\d+)")
PARENT_GROUP = re.compile(r"^>\s+(\S+)")
PRIMARY_GROUP = re.compile(r"^Primary Group:\s*(\S+)")
UUID_LINE = re.compile(r"^-\s*UUID:\s*([0-9a-fA-F-]{32,36})")
NO_PERMISSIONS = re.compile(r"does not have any permissions set", re.I)

# "moderator's Permissions:  (page 1 of 1 - 2 entries)" - note the entries that
# follow are continuation lines printed WITHOUT the [LP] tag, so the node list
# has to be read positionally from this header rather than by filtering on [LP].
PERMISSIONS_HEADER = re.compile(r"'s Permissions:", re.I)
PERMISSION_NODE = re.compile(r"^>\s*(\S+)\s*$")

# "- moderator has minecraft.command.kick set to true in context global."
# Emitted by `permission check`, which conveniently enumerates every permission
# the holder has set - not just the one being checked.
PERMISSION_VALUE = re.compile(
    r"\bhas\s+(\S+)\s+set to\s+(true|false)\b", re.I
)


def clean(line: str) -> str:
    """Strip the log prefix, colour codes and the [LP] tag."""
    text = LOG_PREFIX.sub("", line)
    text = COLOUR_CODES.sub("", text)
    text = LP_PREFIX.sub("", text)
    return text.strip()


def is_luckperms_line(line: str) -> bool:
    return "[LP]" in line


@dataclass
class Group:
    name: str
    weight: int = 0

    @property
    def label(self) -> str:
        return f"{self.name}  (weight {self.weight})" if self.weight else self.name


@dataclass
class UserInfo:
    name: str
    uuid: str | None = None
    primary_group: str | None = None
    groups: list[str] = field(default_factory=list)


@dataclass
class Permission:
    node: str
    value: bool


def parse_groups(lines: list[str]) -> list[Group]:
    groups: list[Group] = []
    seen: set[str] = set()
    for line in lines:
        if not is_luckperms_line(line):
            continue
        match = GROUP_ENTRY.match(clean(line))
        if match:
            name = match.group(1)
            # Console output is a shared stream, so a stray repeat is possible;
            # a group listed twice is never meaningful.
            if name in seen:
                continue
            seen.add(name)
            try:
                weight = int(match.group(2))
            except ValueError:
                weight = 0
            groups.append(Group(name=name, weight=weight))
    return groups


def parse_user_info(lines: list[str], fallback_name: str) -> UserInfo:
    info = UserInfo(name=fallback_name)
    in_parents = False

    for line in lines:
        if not is_luckperms_line(line):
            continue
        text = clean(line)

        uuid_match = UUID_LINE.match(text)
        if uuid_match:
            info.uuid = uuid_match.group(1)

        primary = PRIMARY_GROUP.search(text)
        if primary:
            info.primary_group = primary.group(1)

        if text.startswith("- Parent Groups"):
            in_parents = True
            continue

        if in_parents:
            parent = PARENT_GROUP.match(text)
            if parent:
                info.groups.append(parent.group(1))
                continue
            # The parent list ends at the next unindented section.
            if text.startswith("-"):
                in_parents = False

    return info


def parse_permission_nodes(lines: list[str]) -> list[str]:
    """Node names from ``permission info``.

    The nodes are printed as bare ``> node`` continuation lines with no [LP] tag,
    so collection only starts after the "'s Permissions:" header - otherwise any
    other console output shaped like ``> something`` would be swallowed.
    """
    nodes: list[str] = []
    collecting = False

    for line in lines:
        text = clean(line)
        if NO_PERMISSIONS.search(text):
            return []
        if PERMISSIONS_HEADER.search(text):
            collecting = True
            continue
        if not collecting:
            continue

        match = PERMISSION_NODE.match(text)
        if match:
            node = match.group(1)
            if node not in nodes:
                nodes.append(node)
        elif text and is_luckperms_line(line):
            # A new [LP] section means the listing has ended.
            collecting = False

    return nodes


def parse_permission_values(lines: list[str]) -> dict[str, bool]:
    """Node -> allow/deny, harvested from a ``permission check`` reply."""
    values: dict[str, bool] = {}
    for line in lines:
        if not is_luckperms_line(line):
            continue
        for node, value in PERMISSION_VALUE.findall(clean(line)):
            values[node] = value.lower() == "true"
    return values


def combine_permissions(nodes: list[str], values: dict[str, bool]) -> list[Permission]:
    """Merge the node list with whatever values were discovered.

    Anything the check did not mention is shown as allowed, which is what a bare
    ``permission set <node>`` defaults to.
    """
    return [Permission(node=node, value=values.get(node, True)) for node in nodes]


def check_group_permission(group: str, node: str) -> str:
    return f"lp group {group} permission check {node}"


def check_user_permission(player: str, node: str) -> str:
    return f"lp user {player} permission check {node}"


def response_text(lines: list[str]) -> str:
    """LuckPerms' own reply, for showing errors back to the user."""
    return "\n".join(clean(line) for line in lines if is_luckperms_line(line))


# --- Command builders ---------------------------------------------------------
#
# Kept in one place so the UI never assembles command strings itself, and so the
# quoting of player names stays consistent.


def list_groups() -> str:
    return "lp listgroups"


def group_permissions(group: str) -> str:
    return f"lp group {group} permission info"


def user_info(player: str) -> str:
    return f"lp user {player} info"


def user_permissions(player: str) -> str:
    return f"lp user {player} permission info"


def create_group(group: str) -> str:
    return f"lp creategroup {group}"


def delete_group(group: str) -> str:
    return f"lp deletegroup {group}"


def set_group_permission(group: str, node: str, value: bool) -> str:
    return f"lp group {group} permission set {node} {'true' if value else 'false'}"


def unset_group_permission(group: str, node: str) -> str:
    return f"lp group {group} permission unset {node}"


def set_user_permission(player: str, node: str, value: bool) -> str:
    return f"lp user {player} permission set {node} {'true' if value else 'false'}"


def unset_user_permission(player: str, node: str) -> str:
    return f"lp user {player} permission unset {node}"


def add_user_to_group(player: str, group: str) -> str:
    return f"lp user {player} parent add {group}"


def remove_user_from_group(player: str, group: str) -> str:
    return f"lp user {player} parent remove {group}"


def set_primary_group(player: str, group: str) -> str:
    return f"lp user {player} parent switchprimarygroup {group}"


def set_group_weight(group: str, weight: int) -> str:
    return f"lp group {group} setweight {weight}"


# --- Inheritance between groups ----------------------------------------------


def add_group_parent(group: str, parent: str) -> str:
    return f"lp group {group} parent add {parent}"


def remove_group_parent(group: str, parent: str) -> str:
    return f"lp group {group} parent remove {parent}"


def group_info(group: str) -> str:
    return f"lp group {group} info"


# "moderator inherits minecraft.command.give set to true from builder in context global."
INHERITED_VALUE = re.compile(
    r"\binherits\s+(\S+)\s+set to\s+(true|false)\s+from\s+(\S+)", re.I
)


def parse_inherited_permissions(lines: list[str]) -> dict[str, tuple[bool, str]]:
    """node -> (value, which group it came from), from a ``permission check`` reply.

    LuckPerms reports not just the result but its origin, which is what lets the
    UI show an inherited permission greyed out and labelled with the group it
    actually comes from instead of pretending the group owns it.
    """
    inherited: dict[str, tuple[bool, str]] = {}
    for line in lines:
        if not is_luckperms_line(line):
            continue
        for node, value, parent in INHERITED_VALUE.findall(clean(line)):
            inherited[node] = (value.lower() == "true", parent)
    return inherited


# "- Parent Groups:" then indented "> builder" lines. Same shape as user info.
def parse_group_parents(lines: list[str]) -> list[str]:
    parents: list[str] = []
    in_parents = False
    for line in lines:
        if not is_luckperms_line(line):
            continue
        text = clean(line)
        if text.startswith("- Parent Groups"):
            in_parents = True
            continue
        if in_parents:
            match = PARENT_GROUP.match(text)
            if match:
                if match.group(1) not in parents:
                    parents.append(match.group(1))
                continue
            if text.startswith("-"):
                in_parents = False
    return parents


GROUP_WEIGHT = re.compile(r"^-\s*Weight:\s*(-?\d+)", re.I)


def parse_group_weight(lines: list[str]) -> int | None:
    for line in lines:
        if not is_luckperms_line(line):
            continue
        match = GROUP_WEIGHT.match(clean(line))
        if match:
            return int(match.group(1))
    return None


# --- Tracks -------------------------------------------------------------------
#
# A track is an ordered ladder of groups - default ---> member ---> mod - that
# promote and demote step along.


def list_tracks() -> str:
    return "lp listtracks"


def track_info(track: str) -> str:
    return f"lp track {track} info"


def create_track(track: str) -> str:
    return f"lp createtrack {track}"


def delete_track(track: str) -> str:
    return f"lp deletetrack {track}"


def track_append(track: str, group: str) -> str:
    return f"lp track {track} append {group}"


def track_remove(track: str, group: str) -> str:
    return f"lp track {track} remove {group}"


def promote(player: str, track: str) -> str:
    return f"lp user {player} promote {track}"


def demote(player: str, track: str) -> str:
    return f"lp user {player} demote {track}"


# "Tracks: staff" / "Tracks: None"
TRACKS_LINE = re.compile(r"^Tracks:\s*(.+)$", re.I)
# "- Path: default ---> member ---> mod"
TRACK_PATH = re.compile(r"^-?\s*Path:\s*(.+)$", re.I)
PATH_SEPARATOR = re.compile(r"\s*--+>\s*")


def parse_tracks(lines: list[str]) -> list[str]:
    for line in lines:
        if not is_luckperms_line(line):
            continue
        match = TRACKS_LINE.match(clean(line))
        if match:
            raw = match.group(1).strip()
            if raw.lower() in ("none", "-"):
                return []
            return [t.strip() for t in raw.split(",") if t.strip()]
    return []


def parse_track_path(lines: list[str]) -> list[str]:
    """The ordered groups in a track."""
    for line in lines:
        if not is_luckperms_line(line):
            continue
        match = TRACK_PATH.match(clean(line))
        if match:
            return [g.strip() for g in PATH_SEPARATOR.split(match.group(1)) if g.strip()]
    return []


# --- Verbose recording --------------------------------------------------------


def verbose_on(filter_expression: str = "") -> str:
    return f"lp verbose on {filter_expression}".strip()


def verbose_off() -> str:
    return "lp verbose off"


# Nodes worth offering as a starting point. Deliberately short and vanilla-ish:
# a modpack's own nodes are impossible to guess, so the UI also takes free text.
COMMON_NODES: tuple[tuple[str, str], ...] = (
    ("minecraft.command.tp", "Use /tp"),
    ("minecraft.command.give", "Use /give"),
    ("minecraft.command.gamemode", "Use /gamemode"),
    ("minecraft.command.time", "Use /time"),
    ("minecraft.command.weather", "Use /weather"),
    ("minecraft.command.kick", "Use /kick"),
    ("minecraft.command.ban", "Use /ban"),
    ("minecraft.command.whitelist", "Use /whitelist"),
    ("luckperms.*", "Full LuckPerms access"),
    ("*", "Everything (full operator equivalent)"),
)
