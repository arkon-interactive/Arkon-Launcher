"""Minecraft server settings: server.properties and game rules.

Two different stores with different rules about when they can be changed:

* **server.properties** is read once at startup. A few entries have a matching
  console command (``difficulty``, ``whitelist``, ``defaultgamemode``) and can be
  applied to a live server; the rest need a restart, and the UI says which.
* **Game rules** live in the world, not the properties file. As of 26.2 they sit
  in ``data/minecraft/game_rules.dat`` under the new namespaced snake_case names
  (``minecraft:keep_inventory``) - the old camelCase ``keepInventory`` is
  rejected by the command outright.

Game rules are read straight from that file when the server is stopped, and
changed with ``/gamerule`` when it is running. Edits made while stopped are
queued and applied once the server is next ready, so the file never has to be
rewritten by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .worlds import NbtError, read_nbt_file

GAME_RULES_FILE = Path("data") / "minecraft" / "game_rules.dat"
WORLD_GEN_FILE = Path("data") / "minecraft" / "world_gen_settings.dat"


class Kind(str, Enum):
    BOOL = "bool"
    INT = "int"
    CHOICE = "choice"
    TEXT = "text"


@dataclass
class Setting:
    """One server.properties entry, described well enough to build a widget."""

    key: str
    label: str
    kind: Kind
    default: str
    group: str
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: int = 0
    maximum: int = 0
    # Command that applies the value to a running server, or None if a restart
    # is the only way.
    live_command: str | None = None
    # Show the help under the control rather than only on hover. Reserved for
    # settings that are costly to get wrong - printing every hint inline turns
    # the page into a wall of grey text nobody reads.
    inline_help: bool = False

    @property
    def needs_restart(self) -> bool:
        return self.live_command is None

    def command_for(self, value: str) -> str | None:
        if self.live_command is None:
            return None
        return self.live_command.format(value=value)


# Ordered as they should appear. Only settings worth exposing - the full
# properties file has entries nobody should be poking at from a launcher.
SETTINGS: tuple[Setting, ...] = (
    Setting(
        "difficulty", "Difficulty", Kind.CHOICE, "easy", "Gameplay",
        choices=("peaceful", "easy", "normal", "hard"),
        help="Peaceful removes hostile mobs entirely.",
        live_command="difficulty {value}",
    ),
    Setting(
        "gamemode", "Default game mode", Kind.CHOICE, "survival", "Gameplay",
        choices=("survival", "creative", "adventure", "spectator"),
        help="What new players start in.",
        live_command="defaultgamemode {value}",
    ),
    Setting(
        "force-gamemode", "Force game mode on join", Kind.BOOL, "false", "Gameplay",
        help="Resets everyone to the default mode each time they log in.",
    ),
    Setting(
        "hardcore", "Hardcore", Kind.BOOL, "false", "Gameplay",
        help="Death is permanent - players are set to spectator. Cannot be undone "
             "for players who have already died.",
        inline_help=True,
    ),
    Setting(
        "pvp", "Player versus player", Kind.BOOL, "true", "Gameplay",
        help="Whether players can damage each other.",
    ),
    Setting(
        "allow-flight", "Allow flight", Kind.BOOL, "false", "Gameplay",
        help="Turn on if a mod in your pack gives players flight, or they will be "
             "kicked for flying.",
        inline_help=True,
    ),
    Setting(
        "allow-nether", "Allow the Nether", Kind.BOOL, "true", "Gameplay",
    ),
    Setting(
        "spawn-monsters", "Spawn hostile mobs", Kind.BOOL, "true", "Gameplay",
    ),
    Setting(
        "player-idle-timeout", "Kick idle players after", Kind.INT, "0", "Gameplay",
        minimum=0, maximum=1440, help="Minutes. 0 never kicks.",
    ),

    Setting(
        "max-players", "Maximum players", Kind.INT, "10", "Players",
        minimum=1, maximum=200,
    ),
    Setting(
        "white-list", "Use whitelist", Kind.BOOL, "false", "Players",
        help="Only whitelisted players can join. The list appears below when this "
             "is on.",
        live_command="whitelist {value}",
        inline_help=True,
    ),
    Setting(
        "enforce-whitelist", "Kick non-whitelisted players immediately", Kind.BOOL,
        "false", "Players",
        help="Applies the whitelist to people already online, not just new joins.",
    ),
    Setting(
        "online-mode", "Verify accounts with Mojang", Kind.BOOL, "true", "Players",
        help="Leave this on. Turning it off lets anyone join using any username, "
             "including one of your friends' names.",
        inline_help=True,
    ),
    Setting(
        "enable-command-block", "Enable command blocks", Kind.BOOL, "true", "Players",
    ),
    Setting(
        "spawn-protection", "Spawn protection radius", Kind.INT, "0", "Players",
        minimum=0, maximum=256,
        help="Blocks around spawn that non-operators cannot build in. 0 disables it.",
        inline_help=True,
    ),

    Setting(
        "motd", "Server description", Kind.TEXT, "", "Appearance",
        help="Shown under the server name in the multiplayer list.",
    ),

    Setting(
        "view-distance", "View distance", Kind.INT, "10", "Performance",
        minimum=3, maximum=32,
        help="Chunks sent to players. The biggest single lever on server load.",
    ),
    Setting(
        "simulation-distance", "Simulation distance", Kind.INT, "8", "Performance",
        minimum=3, maximum=32,
        help="Chunks where mobs and redstone actually tick.",
    ),
    Setting(
        "max-tick-time", "Watchdog timeout", Kind.INT, "60000", "Performance",
        minimum=-1, maximum=600000,
        help="Milliseconds before the server kills itself as hung. -1 disables it, "
             "which is often wise on a heavy modpack.",
        inline_help=True,
    ),
    Setting(
        "entity-broadcast-range-percentage", "Entity view range", Kind.INT, "100",
        "Performance", minimum=10, maximum=500,
        help="Percent. Lower means entities appear at shorter range and less is sent.",
    ),
)

# Appearance first: the description and icon are what the host is most likely to
# want to change, and they are the cheapest to get right.
SETTING_GROUPS: tuple[str, ...] = ("Appearance", "Gameplay", "Players", "Performance")


def settings_in(group: str) -> list[Setting]:
    return [s for s in SETTINGS if s.group == group]


def boolean_command_value(setting: Setting, value: str) -> str:
    """``whitelist`` takes on/off rather than true/false."""
    if setting.key == "white-list":
        return "on" if value == "true" else "off"
    return value


# --- Game rules ---------------------------------------------------------------

# Rules taking a number rather than a switch. Two of these currently hold 0 or 1
# on a fresh world, so guessing from the value alone would render them as
# checkboxes and quietly clamp them.
NUMERIC_GAME_RULES = frozenset({
    "fire_spread_radius_around_player",
    "max_block_modifications",
    "max_command_forks",
    "max_command_sequence_length",
    "max_entity_cramming",
    "max_minecart_speed",
    "max_position_deviation",
    "max_snow_accumulation_height",
    "players_nether_portal_creative_delay",
    "players_nether_portal_default_delay",
    "players_sleeping_percentage",
    "random_tick_speed",
    "respawn_radius",
})

# The handful people actually reach for, surfaced above the rest.
COMMON_GAME_RULES: tuple[str, ...] = (
    "keep_inventory",
    "mob_griefing",
    "advance_time",
    "advance_weather",
    "show_death_messages",
    "fire_damage",
    "fall_damage",
    "drowning_damage",
    "spawn_mobs",
    "spawn_phantoms",
    "pvp",
    "immediate_respawn",
    "players_sleeping_percentage",
    "random_tick_speed",
)

# What each rule actually does, in plain terms. Game rule names are terse and
# several are actively misleading (`mob_griefing` covers far more than mobs
# breaking blocks), so every one gets an explanation rather than only the ones
# that seemed unclear.
GAME_RULE_HELP = {
    "advance_time": "Whether the sun moves. Off freezes the time of day.",
    "advance_weather": "Whether weather changes on its own. Off keeps the current weather.",
    "allow_entering_nether_using_portals": "Whether nether portals work at all.",
    "block_drops": "Whether broken blocks drop items. Off means blocks just vanish.",
    "block_explosion_drop_decay": "Whether blocks destroyed by bed/respawn-anchor blasts still drop.",
    "command_block_output": "Whether command blocks announce what they did in chat.",
    "command_blocks_work": "Whether command blocks run at all.",
    "drowning_damage": "Whether players take damage from running out of air.",
    "elytra_movement_check": "Anti-cheat for elytra flight. Turn off if a mod causes false kicks.",
    "ender_pearls_vanish_on_death": "Whether thrown ender pearls disappear if the thrower dies.",
    "entity_drops": "Whether things like minecarts and item frames drop when destroyed.",
    "fall_damage": "Whether players take damage from falling.",
    "fire_damage": "Whether players take damage from fire and lava.",
    "fire_spread_radius_around_player": "How far from a player fire is allowed to spread, in blocks.",
    "forgive_dead_players": "Whether angered neutral mobs calm down once the player they were angry at dies.",
    "freeze_damage": "Whether powder snow can freeze and hurt players.",
    "global_sound_events": "Whether everyone hears server-wide events like the ender dragon dying.",
    "immediate_respawn": "Skip the death screen and respawn straight away.",
    "keep_inventory": "Keep your items when you die instead of dropping them.",
    "lava_source_conversion": "Whether lava can form new source blocks, as water does.",
    "limited_crafting": "Players can only craft recipes they have unlocked.",
    "locator_bar": "Whether the locator bar showing nearby players is displayed.",
    "log_admin_commands": "Whether operator commands are written to the server log.",
    "max_block_modifications": "Cap on how many blocks one command may change, guarding against hangs.",
    "max_command_forks": "Cap on how many contexts a single command can branch into.",
    "max_command_sequence_length": "Cap on chained command length, guarding against infinite loops.",
    "max_entity_cramming": "How many entities may occupy one block before they start taking damage. 0 disables it.",
    "max_minecart_speed": "Top speed of minecarts.",
    "max_position_deviation": "How far a player may drift from the server's idea of their position before being corrected.",
    "max_snow_accumulation_height": "How many layers snow may pile up to.",
    "mob_drops": "Whether killed mobs drop loot.",
    "mob_explosion_drop_decay": "Whether blocks destroyed by creepers and similar still drop.",
    "mob_griefing": "Whether mobs can change the world - creepers cratering, endermen moving blocks, villagers farming. Turning it off stops a lot of accidental damage.",
    "natural_health_regeneration": "Whether health refills from a full hunger bar.",
    "player_movement_check": "Anti-cheat movement validation. Turn off if a mod causes 'moved too quickly' spam.",
    "players_nether_portal_creative_delay": "Seconds a creative-mode player stands in a portal before travelling.",
    "players_nether_portal_default_delay": "Ticks a survival player stands in a portal before travelling. 80 is four seconds.",
    "players_sleeping_percentage": "What percent of players must sleep to skip the night. 0 lets one person skip it.",
    "projectiles_can_break_blocks": "Whether arrows and similar can break things like decorated pots.",
    "pvp": "Whether players can damage each other.",
    "raids": "Whether pillager raids can trigger.",
    "random_tick_speed": "How fast crops grow, leaves decay and fire spreads. Higher is faster but costs performance.",
    "reduced_debug_info": "Hides coordinates and other detail from the F3 screen.",
    "respawn_radius": "How far from the spawn point new players may appear, in blocks.",
    "send_command_feedback": "Whether commands print their result in chat.",
    "show_advancement_messages": "Whether advancement announcements appear in chat.",
    "show_death_messages": "Whether death messages appear in chat.",
    "spawn_mobs": "Master switch for all natural mob spawning.",
    "spawn_monsters": "Whether hostile mobs spawn.",
    "spawn_patrols": "Whether pillager patrols spawn in the world.",
    "spawn_phantoms": "Whether phantoms appear when players go too long without sleeping.",
    "spawn_wandering_traders": "Whether wandering traders turn up.",
    "spawn_wardens": "Whether wardens can emerge in deep dark biomes.",
    "spawner_blocks_work": "Whether monster spawner blocks produce mobs.",
    "spectators_generate_chunks": "Whether flying around in spectator mode generates new terrain.",
    "spread_vines": "Whether vines grow and spread.",
    "tnt_explodes": "Whether TNT detonates at all.",
    "tnt_explosion_drop_decay": "Whether blocks destroyed by TNT still drop items.",
    "universal_anger": "Angered neutral mobs attack every nearby player, not just the one who provoked them.",
    "water_source_conversion": "Whether water can form new source blocks - how infinite water pools work.",
}

FRIENDLY_RULE_NAMES = {
    "keep_inventory": "Keep inventory on death",
    "mob_griefing": "Mobs can change blocks",
    "advance_time": "Day/night cycle",
    "advance_weather": "Weather changes",
    "show_death_messages": "Show death messages",
    "fire_damage": "Fire damage",
    "fall_damage": "Fall damage",
    "drowning_damage": "Drowning damage",
    "spawn_mobs": "Spawn mobs",
    "spawn_phantoms": "Spawn phantoms",
    "pvp": "Player versus player",
    "immediate_respawn": "Respawn instantly",
    "players_sleeping_percentage": "Percent asleep to skip night",
    "random_tick_speed": "Random tick speed",
}


@dataclass
class GameRule:
    name: str
    value: int
    numeric: bool

    @property
    def label(self) -> str:
        friendly = FRIENDLY_RULE_NAMES.get(self.name)
        if friendly:
            return friendly
        return self.name.replace("_", " ").capitalize()

    @property
    def help(self) -> str:
        """Explanation for the tooltip, always ending with the real rule name."""
        description = GAME_RULE_HELP.get(self.name, "")
        return f"{description}\n\n({self.name})" if description else self.name

    @property
    def as_bool(self) -> bool:
        return bool(self.value)

    def command_value(self, value: int | bool) -> str:
        if self.numeric:
            return str(int(value))
        return "true" if value else "false"


def _is_numeric(name: str, value: int) -> bool:
    # Known numeric rules first; then anything holding a value a switch could
    # not, so a rule added by a future Minecraft version still renders sensibly.
    return name in NUMERIC_GAME_RULES or value not in (0, 1)


def read_game_rules(world_dir: Path) -> list[GameRule]:
    """Read the world's game rules. Empty list if the world has never loaded."""
    path = Path(world_dir) / GAME_RULES_FILE
    if not path.is_file():
        return []

    try:
        data = read_nbt_file(path).get("data") or {}
    except NbtError:
        return []

    rules: list[GameRule] = []
    for raw_name, value in data.items():
        if not isinstance(value, int):
            continue
        name = str(raw_name).split(":")[-1]
        rules.append(GameRule(name=name, value=value, numeric=_is_numeric(name, value)))

    rules.sort(key=lambda r: (r.name not in COMMON_GAME_RULES, r.label.lower()))
    return rules


def read_world_seed(world_dir: Path) -> int | None:
    """The world seed, for display. Read-only - changing it would break the world."""
    path = Path(world_dir) / WORLD_GEN_FILE
    if not path.is_file():
        return None
    try:
        data = read_nbt_file(path).get("data") or {}
    except NbtError:
        return None
    seed = data.get("seed")
    return int(seed) if isinstance(seed, int) else None


def properties_differ(current: dict[str, str], snapshot: dict[str, str]) -> list[str]:
    """Keys where the file no longer matches what we last saw.

    Used to notice that server.properties was edited outside the launcher, so
    the user can be told rather than having their edit silently overwritten.
    Only keys the launcher actually manages are compared - the file has plenty
    of entries we neither show nor touch.
    """
    managed = {setting.key for setting in SETTINGS}
    changed: list[str] = []
    for key in managed:
        if key not in snapshot:
            continue
        if current.get(key) != snapshot.get(key):
            changed.append(key)
    return sorted(changed)


def label_for(key: str) -> str:
    for setting in SETTINGS:
        if setting.key == key:
            return setting.label
    return key


@dataclass
class PendingChanges:
    """Edits waiting for the server to be running before they can be applied."""

    game_rules: dict[str, str] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.game_rules and not self.commands

    def as_commands(self) -> list[str]:
        return [f"gamerule {name} {value}" for name, value in self.game_rules.items()] + list(
            self.commands
        )

    def clear(self) -> None:
        self.game_rules.clear()
        self.commands.clear()
