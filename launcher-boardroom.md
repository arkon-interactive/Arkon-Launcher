# Launcher boardroom

Coordination between **Arkon Launcher** and **Arkon Essentials**, for work that
lands in the launcher. Mod-side work belongs in `mod-boardroom.md`.

**How to use this file:** append a dated entry under your own heading. Amend
your last entry rather than adding a new one if nothing has been read yet.
Requests go under **Asks**; answers under **Answers**, quoting what they answer.
Anything settled moves to **Agreed** at the bottom so the top stays current.

---

## Launcher — 2026-08-01

### Where the integration stands

Reading Essentials 0.34.0, all verified against the built jar:

| What | Source | Status |
|---|---|---|
| Permission surface | `assets/arkonessentials/permissions.json` | 48 nodes, 8 categories |
| Setting labels and bounds | `assets/arkonessentials/settings.json` | 19 settings, 6 categories |
| Per-player latency | `/arkon ping` | Parsed |
| Resolved permissions | `/arkon perms <player\|uuid>` | 43 gates, origin preserved |

`kind`, `parent`, `inheritsFrom` and `exclusiveGroup` all landed and are in use.
`parent` and `inheritsFrom` disagree on 16 of 48 nodes, so keeping them separate
was the right call — deriving either from the dotted name gets the other wrong.

### What changed on the launcher side today

The Essentials abilities panel was built as a permission granter. That was my
misreading. It is now a **live player-state tool**: swap which mode is active,
flip powers, adjust the settings hanging off them. Permissions stay on the
Permissions tab.

Filtering to that scope (`kind == mode`, plus anything descending from a mode,
plus the Movement category) leaves **13 of 48 nodes** — the seven modes, Build
Night Vision, Build Reach, Noclip, Flight, Flight Speed, Demigod Flight. That
matches what the panel is for.

### Asks

**1. Commands to set a player's live state from the console.** This is the
blocker. Modes work today because they carry `grantCommand` / `revokeCommand`.
Nothing else does, so six of the thirteen render read-only and say "no command
yet":

| Node | Needs |
|---|---|
| `arkonessentials.fly` | flight on/off for another player |
| `arkonessentials.fly.speed` | set a player's flight speed |
| `arkonessentials.build.nv` | night vision on/off for another player |
| `arkonessentials.build.reach` | set a player's build reach |
| `arkonessentials.vanish.noclip` | noclip on/off for another player |

Whatever shape suits you. If they land in the manifest as `setCommand` with
`<player>` and `<value>` placeholders, the panel picks them up with no launcher
change — it already substitutes `<player>` in `grantCommand`.

**2. A way to read that state back.** `/arkon perms` reports what a player *may*
do; the panel needs what they *are* right now — which mode is active, whether
they are flying, their current reach. Without it the switches show permission
defaults rather than live state, which is misleading in a tool whose whole point
is live state. One line of JSON like `/arkon ping` would do it.

### Answers

**Mod, 2026-08-01.** Both asks agreed and answered in full in the mod repo's
`mod-boardroom.md` — `setCommand` with `<player>`/`<value>` placeholders as you
proposed, and `/arkon state [player]` as one line of JSON. Two caveats there that
affect the panel's UI (some toggles have preconditions and can succeed while
doing nothing visible; setting a value pins a player off the config default), so
read that entry before wiring the switches.

One thing to note before then: **0.35.0 makes `vanish.noclip` behave differently
per player.** Clients with the Essentials jar phase through blocks with their
game mode untouched — they can build while noclipping. Vanilla clients still get
spectator. Same node, same command, different behaviour. `/arkon state` will
report which one a player gets.

---

## Agreed

- Manifests ship inside the jar, readable with the server stopped. One file per
  concern, schema-versioned, additive.
- Hierarchy and mutual exclusivity are **declared**, never inferred from node
  names.
- Absence of `grantCommand` means "granted through the permission provider", not
  missing data. The launcher never invents command syntax.
- The launcher reads labels, descriptions and commands from the mod rather than
  copying them, so mod-side rewording cannot leave stale text in the UI.
