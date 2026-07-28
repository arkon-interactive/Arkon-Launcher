# Changelog

Versions are `MAJOR.MINOR.PATCH`:

- **MAJOR** — reserved for 1.0, meaning "handed to someone who isn't you".
- **MINOR** — a new capability (a tab, a subsystem, a feature you'd notice).
- **PATCH** — fixes and refinements to what's already there.

The version lives in one place, `arkon_launcher/__init__.py`, and flows from
there into the window title, the installer, and Add/Remove Programs.

---

## 0.6.0

- **Auto-update.** Checks GitHub releases on startup and offers a newer version.
  Nothing downloads without a yes and nothing runs without a second yes; the
  download is size-checked, portable copies are pointed at the releases page
  instead of being replaced, and a running server prompts a warning first. See
  `RELEASING.md`.
- **Config file editor** — browse and edit the pack's config files, with an
  optional `/reload` after saving. Edits go to the instance's config folder,
  which is the source of truth; the server's copy is a mirror rebuilt on every
  start, so writing there would be silently undone.
- **Console player heads.** Online players appear as their real Minecraft
  faces, fetched from Mojang's own session server — no third-party render
  service — cached on disk, with a coloured initial as a fallback. Clicking one
  appends the name to whatever you're typing.
- **Command autocompletion** in the console, from the live command tree plus
  online player names. Completes the word under the cursor, so commands complete
  at the start and names complete anywhere after.
- Backups moved into Settings, with the existing-backups list bottom-most.
- New **Extra** settings page: a join greeting with a `{player}` placeholder,
  and scheduled restarts with warnings in hours or days plus an optional spoken
  countdown.

## 0.5.0

- All 60 game rules now carry a real explanation rather than their raw name.
- Appearance moved above Gameplay; **server icon** picker added, scaling any
  image to the 64x64 PNG Minecraft expects.
- Players tab shows **Operator** ahead of World owner, since that's the fact
  that actually grants power.
- **Settings no longer autosave.** Edits are pending until you press Save, with
  a Save and restart button when a change needs one, and Refresh replacing
  Reload from disk.
- **Drift detection**: `server.properties` edited outside the launcher is
  noticed, and you're offered a refresh rather than having your edit silently
  overwritten.
- **Editable whitelist** that no longer requires a prior connection. Names are
  resolved through the running server, or queued and applied on next start.
  Shown only when the whitelist toggle is on.

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

Nothing outstanding from the current round. Ideas worth considering:

- [ ] Sub-argument completion in the console (player names after `/tp`, item ids
      after `/give`) rather than just command names
- [ ] Restore a single world from a backup taken on another machine
- [ ] Forge / NeoForge support
