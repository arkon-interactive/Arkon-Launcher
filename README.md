# Arkon Launcher

Turns a CurseForge Minecraft instance into a real dedicated Fabric server. Pick
the instance, pick a world, press Start — the server runs your world with the
same mods, and you drive it from a console with full command access.

Built to replace hosting through Essential Mod's "invite friends" feature, where
the host has to stay in the game the whole time. With this, the host closes
Minecraft entirely and the server keeps running.

## What it does

- **Finds your instances** automatically, or you point it at one.
- **Makes the modpack server-safe.** Client-only mods are removed, duplicate mod
  versions are resolved, and broken dependencies are repaired (see below).
- **Runs your world in place.** No copying, no syncing — the server uses the same
  save folder your client does, so nothing has to be moved back and forth.
- **Console with command input.** Colour-coded log, filter box, command history.
- **Backs up before every start**, on a schedule, and on demand. Restore is one
  click and always saves the current world first. Scheduled backups are off by
  default; when on, they can warn players first and save wherever you like.
- **Start, Stop, Reload and Restart** each wait three seconds before firing, so
  a misclick is recoverable — click again to go now, right-click or press Esc to
  cancel.
- **Crash triage.** If a mod stops the server, it names the mod and offers to
  disable it for the server only.
- **Server settings as controls**, not a text file: difficulty, game mode, PVP,
  whitelist, view distance and the rest. Anything with a matching console
  command is applied to the running server immediately; the rest are marked
  "restart needed" rather than silently doing nothing.
- **All 60 game rules** — keep inventory, mob griefing, daylight cycle and so on
  — with the common ones surfaced at the top under plain-English names. Editable
  while the server is stopped; the changes are queued and applied automatically
  the moment it next starts.
- **Players, ops and whitelist**, plus a full **permissions editor** when
  LuckPerms is installed — see below.
- **Connection help**: opens the port automatically over UPnP when your router
  allows it, or sets up a playit.gg tunnel when it doesn't.

## Installing

Run `ArkonLauncherSetup.exe`. The first page asks how you want it:

| | Install | Portable |
|---|---|---|
| Location | `C:\Program Files\Arkon Launcher` | anywhere you choose |
| Start Menu entry | yes | no |
| Uninstaller | yes | no |
| Settings stored in | `%LOCALAPPDATA%\Arkon Launcher` | `data\` next to the exe |

Windows will show **"Windows protected your PC"** the first time, because the
installer isn't code-signed. Click **More info → Run anyway**. The only real fix
is a code-signing certificate, which costs money annually.

### Updating

Run the newer installer — there's no need to uninstall first. It finds the
existing install, updates it in place, and keeps your settings and world
backups. It also closes the app first if it's running, replaces the old program
files completely rather than layering over them, and warns you if you're about
to install an *older* version than the one you have.

Portable copies write nothing to the registry, which is the point of them — so
they aren't detected. To update a portable copy, unpack the new one over the old
folder; your `data\` folder is left alone.

You do **not** need Java or Python installed. It uses the Java that CurseForge
already ships with your instance.

## Using it

1. Pick your instance and a world. A world that's currently open in Minecraft is
   marked and can't be started — close it first.
2. Tick the Minecraft EULA box. This is Mojang's agreement with *you*; the app
   won't tick it for you.
3. Press **Start server**.
4. Give friends the address from the **Connection** tab.

First start downloads the Fabric server launcher (about 180 KB) and Fabric then
fetches the game server and its libraries. Later starts skip all of that.

### Getting friends connected

The Connection tab works down three options:

1. **UPnP** — asks your router to open the port. Direct, no extra latency, no
   third party. Only works if your router has UPnP enabled and you aren't behind
   carrier-grade NAT (the app tells you if you are).
2. **playit.gg** — a relay tunnel. Works behind any router including CGNAT, and
   your friends install nothing. Costs about 10–50 ms of ping. Needs a free
   playit.gg account; the app opens the sign-up page but you create the account
   yourself.

   If playit.gg is **already installed** on the PC, the app finds it and offers
   to **launch it** rather than downloading a second copy, and tells you when
   it's already running.
3. **Manual** — it shows your addresses and what to forward.

**Simple Voice Chat needs UDP port 24454** in addition to the game port. It's the
most common thing to miss.

If you'd rather not expose a public address at all, [Tailscale](https://tailscale.com)
is a good alternative — it's the closest thing to what Essential does
technically — but every friend has to install it and join your network.

## Running the server

Four buttons, each on a three-second countdown with a green sweep. Click again
to skip the wait; right-click or press Esc to cancel (the button flashes red).

| | What it does |
|---|---|
| **Start** | Starts the server for the selected world |
| **Stop** | Saves the world and shuts down |
| **Reload** | Re-reads datapacks, loot tables and server config **without disconnecting anyone**. Does *not* pick up mod or `server.properties` changes |
| **Restart** | Full stop and start. Everyone is disconnected. Needed for mod, memory and most settings changes |

Whoever created the world is made an operator on first start — read from the
world file itself, not guessed — unless you untick that option or they already
have it.

## Backups

Alongside the automatic pre-start backup, the Backups tab has:

- **Back up automatically**, off by default, every 1 / 2 / 6 / 12 / 24 hours.
- **Where backups are saved** — the default keeps them beside the instance so
  they survive uninstalling the launcher, but you can point them at another
  drive, which is the more useful place if the disk is what you're worried
  about.
- **Warnings before a scheduled backup** — add as many as you like, each a set
  number of minutes or seconds beforehand, broadcast to everyone online so
  nobody is caught mid-build by the pause.

## Permissions

With LuckPerms installed, the Permissions tab is a real editor rather than a
command prompt. It needs the server **running**, because LuckPerms only answers
while it is up.

**Groups.** Known permissions on the left, the group's own on the right. Drag
between the two boxes, or use the Allow / Deny / Remove buttons — both work.
Denying is a distinct state from not granting, and is shown as such.

**Inheritance.** A group can inherit from other groups, and those can inherit
further. Inherited permissions appear greyed out and italic, labelled with the
group they came from, and can't be removed from the child — the UI points you at
the parent instead. Group **weight** decides who wins when two inherited groups
disagree about the same node.

**Tracks** are ordered ladders — `default → member → mod` — that the Promote and
Demote buttons step players along.

### Where the permission list comes from

There is no master list of permissions to look up. On Bukkit, plugins declare
theirs in `plugin.yml`; **Fabric has no equivalent**, so a node is just a string
a mod checks whenever it feels like it. Nothing declares them in advance, which
is why LuckPerms itself has no "list all permissions" command.

Four sources are combined instead:

| Source | What it gives |
|---|---|
| The server's command list | `minecraft.command.<name>` for every registered command, modded ones included — about 200 on a large pack |
| LuckPerms' own nodes | Fixed, documented set |
| **Record permissions** | The nodes your pack actually checks, captured live |
| Already-assigned nodes | Whatever any group already uses |

The **Mod** dropdown narrows the list to one mod — it lists the mods actually
loaded on the server, by their real names. Attribution is a best guess from the
node name: `worldedit.brush.sphere` belongs to WorldEdit, and
`minecraft.command.waystones` to Waystones because the command matches the mod
id. A mod that adds a command under an unrelated name (Arkon Essentials
providing `/home`, say) shows up under Minecraft until recording finds its real
nodes.

Discovery is what finds mod-specific nodes like `waystones.command.waystones.gui`
that no name-based guess could produce. It watches which permissions LuckPerms
actually checks and remembers them for next time. Two ways to run it:

- **Keep watching in the background** (on by default) — runs quietly the whole
  time the server is up and adds nodes as it sees them. Verbose output is
  diverted, so it never reaches the console. Measured on an 89-mod server: an
  idle server produces **zero** lines, and about two a second while commands are
  being used.
- **Record permissions…** — a manual window if you'd rather control when.

Either way it can only see checks that actually happen, so nodes only appear once
someone uses the feature they belong to.

The list is therefore useful but never provably complete, which is why the box
is labelled "Known permissions" and free-text entry is always available.

## How the modpack is made server-safe

A client modpack won't run as a server unmodified. Three problems, all handled
automatically:

1. **Client-only mods.** Anything declaring `environment: "client"` is dropped —
   Sodium, Iris, Essential, Litematica and so on.
2. **Mods that declare both but aren't.** A shipped list catches the common ones.
   It is *deliberately incomplete*, because no static list can cover every pack —
   crash triage catches the rest and remembers them.
3. **Dependency fallout.** Removing a mod can strand a server-side mod that
   depends on it. If the dependency was only removed by the heuristic list, it
   goes back in (a hard dependency beats a guess). If it's genuinely client-only,
   the mod depending on it is removed instead, cascading until stable.

Duplicate mod ids — two versions of the same mod left in the folder — are
resolved to the newest, which Fabric otherwise refuses to start with.

Your client's `mods` folder is never modified. The server gets its own folder of
hardlinks, rebuilt on every start so CurseForge mod updates are picked up.

## Where things are kept

```
%LOCALAPPDATA%\Arkon Launcher\      settings, logs, downloaded server jars
<instance>\.arkonlauncher\
  servers\<World>\                  the server's working directory
    mods\                           hardlinks to server-safe mods
    world                           junction to your actual save
  backups\<World>\*.zip             world backups
```

**Backups live with the instance, not with the app.** Uninstalling Arkon
Launcher never touches them.

## Building from source

```bash
pip install -r requirements.txt
python build.py
```

`build.py` runs PyInstaller, then Inno Setup if `ISCC.exe` is present
([free download](https://jrsoftware.org/isdl.php)). Without Inno Setup you still
get a complete, runnable app folder in `dist/`.

```bash
python build.py --exe      # skip the installer
python build.py --clean    # wipe build artifacts first
python arkon.py            # run from source
python tools/boot_test.py  # headless: provision a world and boot it
```

`tools/boot_test.py` is the useful one when a pack won't start — it does
everything the app does with no UI in the way, and prints the server log.

## Known limits

- **Fabric only.** Forge and NeoForge packs are detected and refused rather than
  half-supported.
- **Windows only.** It relies on directory junctions and CurseForge's Windows
  layout.
- **Your PC still has to be on.** This removes the need to be *in the game*, not
  the need for a machine to run it.
- **Not code-signed**, so SmartScreen warns on first run.
