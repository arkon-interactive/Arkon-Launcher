# Arkon Essentials ↔ Arkon Launcher

What the launcher reads from the mod, and the exact formats it accepts. Both are
optional: without either, the launcher works normally and simply hides the
corresponding UI.

The launcher side is already implemented (`arkon_launcher/essentials.py`), so
these light up as soon as the mod provides them.

---

## 1. Ability manifest — a resource in the jar

**Why a resource and not an API:** the launcher needs this with the server
stopped, before anything is running. Reading it from the jar means no protocol,
no port, no ordering problem, and it is versioned with the mod automatically.

Ship it at:

```
assets/arkonessentials/permissions.json
```

Either a bare array, or an object with an `abilities` (or `permissions`) key:

```json
[
  {
    "node": "arkonessentials.admin.mode",
    "label": "Admin Mode",
    "category": "Admin",
    "description": "Bypass protections and see hidden players."
  },
  {
    "node": "arkonessentials.demigod.flight",
    "label": "Flight",
    "category": "Movement"
  },
  {
    "node": "arkonessentials.build.nv",
    "label": "Night Vision",
    "category": "Build"
  }
]
```

| Field | Required | Notes |
|---|---|---|
| `node` | yes | The exact string LuckPerms grants. `permission` also accepted |
| `label` | no | Shown to the user; falls back to the node. `name` also accepted |
| `category` | no | Groups the toggles into sections. `group` also accepted. Defaults to "General" |
| `description` | no | Tooltip |

**The `node` must be the string a permission check actually tests.** The mod
currently registers via Fabric's `PermissionNode` with `Identifier`s — whatever
LuckPerms sees for those is what belongs here. If in doubt: grant one to a test
group, run `/lp user <name> permission check <node>`, and confirm it resolves.

From scraping the current jar the gates appear to be `ADMIN_MODE`, `ADMIN_GHOST`,
`ADMIN_TP`, `ADMIN_HOME`, `ADMIN_HOME_LIMIT`, `ADMIN_SEE_HIDDEN`, `BUILD_NV`,
`BUILD_REACH`, `DEMIGOD_FLIGHT`, `FLY_SPEED`, `AFK_TOGGLE`, `AFK_REASON`,
`TP_ALL`, `TP_BACK`, `TP_COORDS`, `TP_DEATH`, `TP_HERE`, `TP_OTHERS`,
`TP_THERE`, `TP_TOP`, `TP_IMMUNE`, `FAKE_JOIN`, `FAKE_LEAVE`, `GRANT_IMMUNE`,
`HOME_LIMIT`, `HOME_NAMED` — 26 in total. That list came from the constant pool
and may be incomplete, which is exactly why it should be declared rather than
guessed.

Once present, the launcher shows an **Essentials Abilities** section on each
player with a toggle per ability, grouped by category.

---

## 2. Live telemetry — lines on stdout

**Why stdout and not a socket:** the launcher already owns the server's stdin
and stdout. That is a private, bidirectional channel with no port to bind, no
firewall prompt, and no authentication to get wrong. A socket only becomes worth
it if something other than the launcher needs the data.

Print one JSON object per line, prefixed:

```
[ARKON] {"type":"players","players":[{"name":"FenixRysing","ping":42,"session":915}]}
```

| Field | Meaning |
|---|---|
| `name` | Player name — required, the row is ignored without it |
| `ping` | Round-trip latency in ms. `ping_ms` or `latency` also accepted |
| `session` | Seconds connected. `session_seconds` or `online_for` also accepted |
| `afk` | Boolean |

Anything else in the object is kept and available, so you can add fields without
a launcher change.

**Per-player ping is the motivating case.** Minecraft does not report it to the
console at all, so the launcher currently shows "not available". The server
knows it — it is in the player list packet — it just never reaches stdout.

Emit on a timer (every 5–10s is plenty) and/or on join and leave. These lines
are **filtered out of the console pane**, so they will not spam it.

### Please don't

- Print telemetry every tick. It goes to `latest.log` whether displayed or not.
- Put anything sensitive in it. It lands in the log file.
- Assume the launcher is present — the mod must work standalone.

---

## Testing it

With the mod installed and the server running:

```bash
python tools/test_integration.py
```

Reports whether the manifest was found, how many abilities parsed, and whether
any telemetry lines were seen.
