# Changelog

Versions are `MAJOR.MINOR.PATCH`:

- **MAJOR** — reserved for 1.0, meaning "handed to someone who isn't you".
- **MINOR** — a new capability (a tab, a subsystem, a feature you'd notice).
- **PATCH** — fixes and refinements to what's already there.

The version lives in one place, `arkon_launcher/__init__.py`, and flows from
there into the window title, the installer, and Add/Remove Programs.

---

## 0.13.0

- **Essentials abilities are now a granting tool, not a second permission
  editor.** A responsive grid, each ability's name with a switch under it, green
  when it applies. Permissions stay on the Permissions tab where they belong.
  - Built on Arkon Essentials 0.34.0's schema 2 manifest, so the structure is
    declared rather than guessed. `kind` separates the modes, the config-backed
    values and the rest — one of those values is a boolean, so the old
    type-based guess called it an ordinary toggle.
  - `parent` drives what nests under what, `inheritsFrom` drives where a grant
    flows from, and they disagree on 16 of 48 nodes. Flight's speed belongs
    inside Flight; `admin.mode` inherits from `admin` without being shown inside
    it. Either field alone gets the other job wrong.
  - Modes turn each other off, matching the commands, so the panel cannot
    express a state the server would refuse.
  - Tooltips carry the real command. Only the seven modes have a grant command;
    everything else says the grant goes through the permission provider rather
    than inventing syntax for forty nodes.
  - **Teleport tools**: player to player with a direction flip, coordinates, and
    last death point.
- **Fixed: adding a player to a group did nothing.** The Players tab passes the
  player object while the Permissions tab passes a name, and the handler assumed
  a name — so the command went out malformed and the server rejected it in
  silence.
- **Fixed: permission inheritance was inconsistent between groups and between
  refreshes.** Both value parsers required LuckPerms' `[LP]` tag, but that only
  appears on the first line of a wrapped message, so entries past the wrap were
  dropped — and where the wrap falls depends on how long the group and node
  names are.
- **Fixed: tick time was always blank.** A single missed `/tps` reply disabled
  that command for the rest of the session, and MSPT has no other source. It now
  takes three consecutive failures.
- **A tick rate of 0.0 on an empty server is no longer alarming.**
  `pause-when-empty-seconds` stops the tick loop after a minute alone, so the
  panel says "paused — no players" and explains why, rather than showing 0.0/20
  in red.
- **Ability changes apply in a batch.** One command per click queued a burst on
  the server thread and timed the connection out.
- **The console can hide repeated lines.** Right-click one to hide every copy; a
  Hidden button lists them and puts any back. Nothing is discarded — lines that
  arrive while hidden are still there when you unhide.
- **The permissions editor is quicker.** LuckPerms answers in one burst, so the
  wait per command dropped from 0.6s to 0.25s, and the group list is cached
  across player selections instead of refetched.
- Mod update checks report how many mods are pending across the whole pack,
  rather than listing the versions of the two tracked on GitHub.

## 0.12.0

- **Config files can be edited as a form.** Pick a config on the Mods tab and,
  when the file is shaped like settings, you get checkboxes, number fields and
  text boxes with the mod author's own comments shown as the description — which
  is usually the only documentation a setting has. TOML, JSON/JSON5 and
  `.properties` are recognised.
  - **Editing rewrites one value on one line.** The file is not parsed and
    written back out, because that would reformat everything and discard the
    comments. Verified against all 202 form-capable configs in the reference
    pack: an untouched file round-trips byte-for-byte, and changing one setting
    changes exactly one line.
  - Files that are data rather than settings — loot tables, recipe lists — are
    detected by their repeated keys and sent to the text view, where the
    structure is actually visible. The text view is always available.
  - Lists, nested values and commented-out lines are shown read-only. A form
    that silently mangled them would be worse than not offering one.
  - **On comments:** a line like `#enableFoo = true` is a disabled setting,
    while `#Default: 1.5` is documentation, and both look the same to a parser.
    The heuristic is biased towards calling things documentation, since
    describing a setting costs nothing while offering to "re-enable" a line of
    prose would write nonsense into a config.
- **A dependency checker.** Mods declaring something the server does not have
  are listed with what is missing and why — not installed, or installed but
  client-only and therefore unusable on a server. Dependencies that are merely
  switched off can be switched back on from the dialog; nothing is downloaded.
  When nothing can be fixed automatically the dialog says so instead of offering
  a button that would do nothing.
- **An Essentials tab in Settings**, when Arkon Essentials is installed. Its
  settings render through the same form. Saving while the server is running also
  sends `/arkon config`, because the running mod holds its own copy and would
  otherwise overwrite a file edited underneath it.
- **The Mods tab no longer hitches on load.** Two causes: the config folder was
  re-scanned once per mod (~9s across a 141-mod pack, now 0.08s for the whole
  pack), and the table re-measured every cell in five columns on every insert.
  Populating the table now blocks for 10ms.
- **"Browse all config files" opens the config folder**, which is what it
  sounded like it did.
- Finishing a mod scan now reports how many mods were read and how many the
  server loads, instead of leaving "Reading mods..." on screen.

## 0.11.0

- **A new dark theme.** Still dark, but built out of layered surfaces, one
  accent colour and a real type scale rather than boxes and grey lines. Colours
  live in one file, so a hardcoded hex anywhere else is now a bug rather than a
  style choice. Checkbox ticks and dropdown arrows are drawn as icons — Qt
  renders the CSS-border approach as blank squares under the Fusion style.
- **The Players tab is now a per-player home.** Pick someone from the list and
  the panel beside it shows who they are and everything you can do to them:
  avatar, a status dot for online/offline, session length, ping, and their
  groups and permissions.
  - Op and ban **arm rather than ask** — click to arm, click again to confirm,
    right-click or Esc to cancel. That is the same gesture as Start and Stop, so
    there is one thing to learn for every consequential action in the app.
  - The status dot means presence and nothing else. Reusing it to show a
    half-confirmed action would hide whether the player is actually online at
    the exact moment you are deciding to ban them.
  - Permissions granted by a group are shown greyed, next to the player's own —
    so it is clear which ones can be changed here and which belong to the group.
- **Permissions tab no longer has its own Players sub-tab.** Per-player
  permissions live with the player; the tab keeps Groups and Tracks.
- **Arkon Essentials integration, against the real mod.** With Essentials
  installed, the Players tab gains an "Essentials abilities" section listing
  every permission the mod declares, grouped by category, and fills in per-player
  ping — something Minecraft exposes to no tool on its own.
  - Abilities are read from the mod's own manifest inside the jar, so the list
    cannot go stale, and it works with the server stopped.
  - Resolution comes from `/arkon perms`, not from LuckPerms. It answers for
    whichever permission provider is installed, works for players who have never
    connected, and distinguishes a real grant from the mod's own fallback. That
    last distinction is the point: for an operator the fallback is always yes,
    which is why a permission tier looks like it works until it is tested on an
    unopped account.
  - Nodes the mod reads from a server setting rather than gating on a permission
    are shown as values naming that setting, not as toggles. A toggle there
    would look identical to a working one and do nothing.
  - Commands are retried once, because they execute on the server thread and a
    large world's autosave can outlast a single reply window.
  - Verified end to end against Essentials 0.32.0 on a running server. The
    contract is recorded in `INTEGRATION.md`.

## 0.10.0

- **Turn mods on and off.** Disabled mods now appear in the list marked "(off)"
  instead of vanishing, and can be switched back on. Uses the `.jar.disabled`
  convention, so CurseForge understands it and nothing is deleted.
- **Install and uninstall mods by hand**, for anything that did not come from
  CurseForge. Installing validates the jar really is a Fabric mod *before*
  copying — a non-mod jar in the folder stops the whole pack loading with an
  error that names a file rather than explaining anything — and warns if what
  you added is client-only. Uninstall offers "Disable instead" as the default,
  since that is reversible.
- **Older duplicates are highlighted** and marked "(older copy)", with a
  "Disable this copy" button next to the update button, so the two obvious
  responses sit side by side.
- Mod names are plain ASCII in the table. A warning glyph looked better but
  cannot be encoded to cp1252, and mod names reach the console and log files.

## 0.9.0

- **Updates for every mod, not just first-party ones.** CurseForge's own
  manifest records the newest available file for each mod it installed, with a
  direct download link — so the whole pack can be checked with no API key and no
  network call. Update one at a time or all at once; the Mods tab shows a count
  in its label. Downloads are size- and SHA1-checked before the old jar is
  removed, and the manifest is updated so CurseForge stays consistent.
- Mods listed in the manifest whose jar is no longer present are **skipped**
  rather than offered — reinstalling something deliberately deleted is not an
  update.
- **Duplicate cleanup.** Mods installed twice are flagged with a count, and you
  choose which version to keep. The others are renamed to `.jar.disabled`
  — CurseForge's own convention — rather than deleted, so it can be undone.
- **The config editor now lives in the Mods tab**, with a Configure button that
  scopes it to the selected mod's files. Browsing all config files is still
  possible. The separate Config files tab is gone.
- **Fixed** two bugs where a sorted table was addressed by source-row index:
  searching hid the wrong rows, and selecting a mod could return a different
  one. Both showed up precisely on duplicated mods, which is the case the
  feature exists for.

## 0.8.0

- **Mods tab.** Every installed mod with its version, which side it declares,
  and — the useful column — whether the server actually loads it and why not.
  Client-only mods, older duplicates and anything stranded by a missing
  dependency are all named, instead of that only being discoverable by reading
  the startup log.
- **Configs are listed against the mod they belong to.** Double-click a row to
  edit them; mods with several configs offer a pick-list. Matching is by
  filename, since nothing in a Fabric mod declares where its config lives.
- **Mod update checks now report when everything is current.** Previously a
  successful check with nothing to do said nothing at all, which is
  indistinguishable from the check being broken. There is also a manual
  "Check for mod updates" button.

## 0.7.0

- **Server stats** replace the instance and world pickers once a server is
  running — tick rate, tick time, uptime, players, CPU, memory and the connect
  address. Tick rate uses the pack's `/tps` command when it has one and
  otherwise measures itself from the game's tick counter, which needs no mod at
  all. **CPU is normalised across cores**, so a server using two of sixteen
  reads 12% rather than an alarming 200%.
- **Clicking a player's head opens an action menu** — op and whitelist as
  toggles that read current state first, kick, ban, game modes — instead of only
  appending the name.
- **Custom player actions** are editable in Extra settings, with a placeholder
  and colour-code reference.
- **Arkon Essentials update check.** If it's installed, the launcher compares it
  against the mod's own GitHub releases and offers to replace the jar. Source
  and javadoc jars are excluded, the download is size-checked and confirmed to
  contain a `fabric.mod.json` before the old file is removed.

## 0.6.1

- **Fixed a crash when closing the window with the server running.** Three
  things compounded: `closeEvent` blocked the GUI thread inside `stop()` while
  the reader thread kept delivering signals into a widget being destroyed; a
  state change during that could open a modal crash-triage dialog on top; and
  the window was then destroyed with signals still queued. Callbacks are now
  detached first and late handlers are guarded.
- **Leftover servers are found and can be stopped.** A launcher that died
  mid-stop left a java process holding the world lock and the port, which made
  the next start fail as though the world were broken — and it was easy to miss
  among other `java.exe` entries.
- **Ctrl+click Stop** force quits after a 10 second countdown, for a server that
  has stopped responding.
- Stop reads **"Server stopped"** when there is nothing to stop.

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
