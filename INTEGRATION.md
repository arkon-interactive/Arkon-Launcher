# Arkon Essentials ↔ Arkon Launcher

What the launcher reads from the mod, and the exact formats it accepts.
Everything here is optional: without any of it the launcher works normally and
simply hides the corresponding UI.

**Status:** all three are implemented on both sides and verified against Arkon
Essentials 0.32.0 on a live server. This document now records the contract as
built, not as proposed.

---

## 1. Permission manifest — a resource in the jar

**Why a resource and not an API:** the launcher needs this with the server
stopped, before anything is running. Reading it from the jar means no protocol,
no port, no ordering problem, and it is versioned with the mod automatically.

Shipped at:

```
assets/arkonessentials/permissions.json
```

```json
{
  "schema": 1,
  "mod": "arkonessentials",
  "namespace": "arkonessentials",
  "permissions": [
    {
      "node": "arkonessentials.home.named",
      "id": "arkonessentials:home.named",
      "label": "Named Homes",
      "category": "Homes",
      "type": "boolean",
      "default": "config",
      "configKey": "playerNamedHomes"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `node` | Dotted form a permission mod grants. **Required.** |
| `id` | Namespaced identifier the mod checks. Used to derive the path. |
| `label` | Shown to the user. Falls back to `node`. |
| `category` | Groups the toggles. Falls back to `General`. |
| `type` | `boolean` or `integer`. Integers are values, not grants. |
| `default` | `public` / `operator` / `config` / `denied` — what applies when nothing grants or denies the node. |
| `configKey` | Which server setting supplies it, when `default` is `config`. |

The reader is deliberately lenient — a bare array works, `permission` is accepted
for `node`, `name` for `label`, `group` for `category` — so the two projects
never have to land a change in the same commit.

### Two things a consumer must get right

**Namespace.** `node` is `arkonessentials.home.named`; `/arkon perms` reports the
*path*, `home.named`. Correlate on the path, not the node.

**The manifest is a superset of what `/arkon perms` reports.** In 0.32.0 it
declares 46 nodes while `/arkon perms` reports 43. The missing three
(`home.limit`, `admin.home.limit`, `fly.demigod`) are config-backed values rather
than permission gates — note that one of them is `type: boolean`, so **type is
not what distinguishes them**. A consumer cannot tell which is which from the
manifest alone; the reliable test is whether the running server reports the path.
The launcher renders anything unreported as a read-only value naming its
`configKey`, because a toggle there would look identical to a working one and do
nothing.

> Worth considering for a future schema bump: an explicit flag (`"gated": false`,
> or `"source": "config"`) would let a tool get this right with the server
> stopped, instead of only once it is running.

---

## 2. Per-player latency — `/arkon ping`

Minecraft exposes per-player latency nowhere a tool can reach: `/list` gives
names only, and there is no vanilla ping command. The mod returns **one line of
JSON**, which matters over RCON — several lines would arrive concatenated into
something the caller has to split first.

```
/arkon ping
```

```json
{"schema":1,"players":[{"name":"Steve","uuid":"…","ping":42,"hidden":false}]}
```

Pulled on demand rather than pushed to stdout. An earlier draft of this document
proposed the mod print `[ARKON] {json}` continuously; the mod chose the command
instead, which is better — no log spam, and the launcher asks only while the
Players tab is open.

`hidden` flags a vanished player rather than omitting them, leaving the decision
to the caller. The launcher does not currently surface it.

The launcher finds this line by shape, since there is no prefix to key off: it
scans the reply for a line ending in `}` that decodes to an object containing
`players`. The server's own `[HH:MM:SS] [Server thread/INFO]:` prefix is
therefore harmless.

---

## 3. Resolved permissions — `/arkon perms <player|uuid>`

```
/arkon perms FenixRysing
/arkon perms 77086af9-eeee-4bf5-90af-d223670841f8
```

```
Permissions for FenixRysing:
  tps = true (default)
  home = true (default)
  admin.vanish = false (denied)
```

Preferred over reading LuckPerms directly, for three reasons: it works with any
permission provider, it answers for players who have never connected (via UUID),
and the origin distinguishes **what the provider said** from **what the mod's own
fallback decided**.

That distinction is the whole value. A node reading `default` means nothing
granted or denied it — and for an operator the fallback is always yes, which is
why a permission tier looks like it works until it is tested on an unopped
account. The launcher shows `granted` and `denied` in colour and everything else
as muted context, so the two are never confused.

Parsed as `<path> = <true|false> (granted|denied|default)`, with any log prefix
stripped first.

---

## What the launcher does with all this

- **Players tab → Essentials abilities** — every declared node, grouped by
  category. With the server stopped it shows declared defaults and says so;
  running, it shows what `/arkon perms` resolved for that player.
- **Players tab → Ping** — from `/arkon ping`, refreshed when a player is
  selected. Reads "not available" rather than inventing a number.
- Both sections stay hidden entirely when the mod is absent or predates them.
