# Changelog

Versions are `MAJOR.MINOR.PATCH`:

- **MAJOR** — reserved for 1.0, meaning "handed to someone who isn't you".
- **MINOR** — a new capability (a tab, a subsystem, a feature you'd notice).
- **PATCH** — fixes and refinements to what's already there.

The version lives in one place, `arkon_launcher/__init__.py`, and flows from
there into the window title, the installer, and Add/Remove Programs.

---

## Unreleased

Working towards **0.5.0** — settings rework, whitelist editing, console
improvements. See the roadmap at the bottom for what's still outstanding.

## 0.4.1

- Passive permission scanning: watches which permissions the server checks while
  it runs and adds them to the known list automatically. Measured at zero output
  on an idle server; verbose lines are diverted so the console is unaffected.
- Discovered nodes no name-based guess could produce, e.g.
  `waystones.command.waystones.gui`, `balm.command.balm.export.config`.
- Toggle in the Permissions tab footer; manual "Record permissions…" still there.

## 0.4.0

- **Fixed**: players never showed as online, which also left the kick button
  permanently disabled. `X joined the game` turned out to be unreliable in 26.2 —
  it appears for some players and not others — so detection now uses
  `logged in with entity id` and `lost connection`, which always appear.
- **Fixed**: auto-op for the world owner was described but never implemented.
  Now reads `singleplayer_uuid` from the world, resolves the name from
  `usercache.json`, and ops them on first start unless they already are.
  Toggleable.
- Start / Stop / Reload / Restart now arm a three-second countdown with a green
  sweep. Click again to fire now, right-click or Esc to cancel.
- Reload (datapacks and config, nobody disconnected) and Restart (full cycle)
  added, with tooltips explaining the difference.
- Settings now confirm when they save, and say whether a change applied
  immediately, needs a restart, or is queued for next start.
- Backups: scheduled backups (off by default, 1/2/6/12/24 hours), a configurable
  save location, and warnings broadcast to players beforehand.
- Removed the redundant LuckPerms command box from the Players tab.

## 0.3.1

- Detects an existing playit.gg installation and offers to launch it rather than
  downloading a second copy; shows when it is already running.
- Mod dropdown on the permissions list, filtering by the mods actually loaded.

## 0.3.0

- Permissions editor: groups, drag-and-drop *and* arrow buttons, inherited
  permissions shown greyed with their origin, group weights, and promotion
  tracks.
- Permission discovery from the live command tree and LuckPerms verbose.

## 0.2.0

- Settings tab: server.properties as real controls, and all 60 game rules with
  the common ones surfaced. Game rules edited while stopped are queued and
  applied on next start.

## 0.1.1

- **Fixed**: `WinError 448` on start. The world was linked in with a directory
  junction, which Windows' Redirection Guard refuses to traverse when the
  process runs at a different integrity level than whoever created the link.
  The server is now pointed at the saves folder directly with `--universe` and
  `--world`, so no reparse point is involved at all.
- Installer gained a proper update cycle: removes files from the previous
  version, warns on downgrade, skips the directory page when upgrading.

## 0.1.0

First working version.

- Finds CurseForge instances and their worlds; nothing about the machine is
  hardcoded.
- Filters a client modpack into a server-safe mod set: drops client-only mods,
  resolves duplicate mod ids, and repairs the dependency fallout that filtering
  causes.
- Runs the world in place — no copying, no syncing.
- Console with command input, crash triage that names the offending mod, world
  backups, player/ops/whitelist management, and a UPnP → playit.gg → manual
  connection ladder.
- Ships as one installer offering either a normal install or a portable copy.

---

## Roadmap

Outstanding, roughly in order:

- [ ] Explicit Save button on settings, with drift detection against manual edits
- [ ] Backups moved under Settings; Extra sub-tab for join broadcast and
      scheduled restarts
- [ ] Editable whitelist that doesn't require a prior connection
- [ ] Server icon picker
- [ ] Player avatars and command autocompletion in the console
- [ ] Config file editor with reload on save
