# Live Control Surface (Mission Control)

The **Control Surface** turns the bot manager into a live, front-facing cockpit for
each bot — every module, the world map, vitals, and a command palette on one page.
AquariusProxy bots get this **built in**; **ZenithProxy** bots get the same surface by
installing the [`zenith-abm-bridge`](https://github.com/aquariusnetwork9/zenith-abm-bridge)
plugin (see [Requirements](#requirements-bot-side) below). The proxy console is still there
as a fallback; the Control Surface is the human-friendly way to drive a bot day to day.

> Added in **ABM v3.0.0**. Served per-bot at `/control?inst=<name>` and relayed over the
> same authenticated tunnel as the live viewer — the bot only binds its viewer to
> loopback, so nothing is exposed publicly.

![Mission Control — Live Map](https://raw.githubusercontent.com/wiki/aquariusnetwork9/Aquarius-Bot-Manager/control-v1-mission-control.png)

---

## Opening it

- From a bot's **viewer drawer → Control tab**, click **⛶ Open full control surface**.
- Or go straight to **`/control?inst=<bot-name>`** in the dashboard (same login/session).
- Pick the appearance with **`&style=v1|v2|v3`**, or use the **appearance dropdown** in the
  top bar (your choice is remembered for next time).

The page boots into the **Live Map** for the selected bot and starts streaming.

---

## Appearance — three themes

The surface is the **same data and the same live wiring** rendered three ways. Switch any
time from the top-bar dropdown; the Live Map is built into all of them.

| Theme | Style key | Best for |
|-------|-----------|----------|
| **Mission Control** (default) | `v1` | A module rail + one focused workspace. Calm, scales to deep per-module config. |
| **Aurora Glass** | `v2` | Frosted-glass cards, a pill filter, every module visible at a glance. |
| **Console Pro** | `v3` | Dense, mono, power-user 3-pane (rail → tables → inspector). Wants ≥ 1080p. |

<table>
<tr>
<td><img src="https://raw.githubusercontent.com/wiki/aquariusnetwork9/Aquarius-Bot-Manager/control-v2-aurora-glass.png" alt="Aurora Glass"></td>
<td><img src="https://raw.githubusercontent.com/wiki/aquariusnetwork9/Aquarius-Bot-Manager/control-v3-console-pro.png" alt="Console Pro"></td>
</tr>
<tr><td align="center"><b>V2 · Aurora Glass</b></td><td align="center"><b>V3 · Console Pro</b></td></tr>
</table>

---

## What's live right now

| Feature | Status | Notes |
|---|---|---|
| **Module states + status dots** | ✅ live | From `/control/state`, refreshed every few seconds. |
| **Module enable toggles** | ✅ live | Sends `<module> on/off`; the state poll shows the truth a moment later. |
| **Action buttons** (▶ run / ■ stop) | ✅ live | Best-effort per module; Elytra's **Fly there** sends `fly trip <dim> <x> <z>`. |
| **Vitals** (health, food, position, dimension, speed) | ✅ live | From the ~20 Hz SSE viewer feed. |
| **Live Map** | ✅ live | Real bot-centred map tile + SSE entity overlay; **click the map to set an Elytra destination**. |
| **Command palette / runner** | ✅ live | Runs any console command and shows the structured result. |
| **Live configuration (per module)** | ✅ live *(v3.1)* | A **⚙ Live configuration** panel on each module reads the bot's real config and writes single fields back instantly (`GET`/`POST /control/config`); secrets are redacted and never writable. The friendly mockup subcards remain as a read-only overview. |
| **List editors — trades · trips · pearls** | ✅ live *(v3.10, needs AquariusProxy 5.9.0+)* | **Villager trades**, **saved Elytra trips**, and **pearl-stasis locations** can be **added, edited and deleted** from the surface. Each shows the bot's real entries with a per-row 🗑; **＋ New …** opens a guided form (the trade builder validates against the real villager catalog and includes chest coordinates) that writes straight to the bot's config. Gated by the per-module **config** permission. |
| **Pearl pins on the map** | 🧩 planned | A later release; needs new bot-side data exposure. |

> The settings subcards show **sensible defaults**, not the bot's live config yet. Don't
> treat the values there as the bot's current configuration until v3.1 wires them to the
> real config. Everything marked ✅ above reflects the **real, live bot**.

---

## The Live Map

- **Bot-centred** real map tile that refreshes continuously, with the bot at the centre.
- **Entity overlay** from the live feed — players (blue), hostiles (red), passives (green),
  items (amber).
- **Click anywhere → set an Elytra destination.** A crosshair drops, the world coordinates
  are computed from the bot's position, and **▶ Send to Elytra** dispatches
  `fly trip <dimension> <x> <z>`.
- **⛶ Fullscreen** expands the map; **◎ Recenter** re-centres on the bot.

> The click-to-coordinate math uses an approximate map span. For pinpoint long-haul
> targets, confirm the X/Z in the Elytra module's **Destination** card.

---

## Requirements (bot side)

The surface needs the bot to serve the loopback viewer + control endpoints. How you turn
those on depends on the fork.

### AquariusProxy (built in)

Set the viewer block in the bot's `config.json`:

```jsonc
"server": {
  "viewer": {
    "enabled": true,     // serves the live feed (map / vitals / entities)
    "control": true,     // ALLOWS the surface to run commands & toggle modules
    "bindHost": "127.0.0.1",
    "port": 2998
  }
}
```

### ZenithProxy (the bridge plugin)

Stock ZenithProxy doesn't ship the viewer/control endpoints — the
[`zenith-abm-bridge`](https://github.com/aquariusnetwork9/zenith-abm-bridge) plugin adds
them, so a ZenithProxy bot lights up the Control Surface exactly like an AquariusProxy one.

1. Download `ZenithABMBridge-<version>.jar` from the plugin's
   [Releases](https://github.com/aquariusnetwork9/zenith-abm-bridge/releases) and drop it in
   the bot's `plugins/` directory.
2. Start/restart the bot, then in its console:
   ```
   abmBridge on            # serves the live feed (map / vitals / entities)
   abmBridge control on    # ALLOWS the surface to run commands & toggle modules
   ```
3. That's it — ABM auto-detects the bridge (its port lives in
   `plugins/config/abm-bridge.json`, default `2998`, same as AquariusProxy's viewer).

The module list ABM shows for a ZenithProxy bot reflects **that build's own modules** — the
[Module reference](#module-reference) below is AquariusProxy's set.

### Either fork

- With **control off** (`control: false` on AquariusProxy, or never running `abmBridge
  control on`) the surface still works as a **read-only viewer** — vitals and the map render,
  but toggles/commands return `403` and do nothing.
- The viewer binds to **loopback only**; the dashboard fetches it server-side and relays it
  over the authenticated (and, across boxes, SSH-tunnelled) connection. Nothing the bot
  serves is reachable directly from the internet.

---

## Security & safety

- **Secrets are never shown.** The MSA password, SOCKS proxy password, and Discord token
  render as masked fields with a reveal-eye. Don't paste tokens into chat or screenshots.
- **Coordinate masking.** The floating **🙈 Mask coordinates** button blurs every coordinate
  field for streaming/screenshots.
- **Authorization gates carry over.** Whisper Control and Proxy Bridge still honour their
  allow-lists — the surface doesn't bypass RBAC.
- **2b2t / anti-cheat.** Aggressive movement and combat modules (Elytra e-bounce, Combat
  Assist, Spawn Patrol) carry detection/ban risk on anarchy servers. Test in safe areas.

---

## Module reference

Friendly name first, with the raw config key in `code`. ⚠ marks the warnings/caveats worth
knowing before you flip a module on. Modules marked **command-driven** have no persistent
toggle — run their command instead.

### Control surfaces

| Module | What it does | ⚠ Warnings & caveats |
|---|---|---|
| **Live Map** · `LiveViewer` | The live world map this page is built around. | Read-only viewer; needs `viewer.enabled`. Entity/positions are from the SSE feed; map span is approximate. |
| **Elytra Autopilot** · `ElytraPilot` | Trip planner & long-haul flight (cruise / highway / e-bounce). | ⚠ A **worn elytra silently breaks e-bounce** — carry spare elytras, don't fly in one you also want to bounce with. Pre-flight audit expects **≥ 2 elytras, ≥ 2 totems, fireworks**. E-bounce is tuned Grim-accepted (~30–38 b/s) but anarchy anti-cheat is a moving target. Native nether routing needs the correct world seed. "Last elytra" safety can log you out. |
| **Villager Trading** · `VillagerTrader` | Stationary auto trade hall (emerald economy). | Add/edit/delete trades right on the surface (**v3.10**, AquariusProxy 5.9.0+) — the **＋ New trade** builder validates against the real villager catalog and captures the supply/output **chest coordinates**. |
| **Pearl Stasis** · `PearlManager` | Ender-pearl stasis loader. | Add/edit/delete stasis locations on the surface (**v3.10**, AquariusProxy 5.9.0+) via **＋ Add pearl**. ⚠ Map pin-to-add is still planned (Phase C); coordinates are typed today. |
| **Stash Manager** · `StashScanner` | Indexes & sorts an owned stash. | Operates on **your own** stash; point it at the right chests/region. |
| **Auto Miner** · `AquariusMiner` | Top-down quarry & ore search with auto-deposit. | ⚠ Uses the **ender chest as the field buffer — never carry filled shulkers in the mining inventory** (it'll mis-deposit). On laggy anarchy, drops can fly up to ~2 blocks, so it settles/chases/confirms each break. Pauses for Auto Eat. Set bounds + deposit chests. |
| **Auto Enchanter** · `Enchanter` | Anvil auto-enchant station. | Needs the **anvil/input/output/book** station laid out and coordinates set. |
| **Kit Builder** · `KitMaker` | Fills template shulker kits. | Define the slot template + item sources. |
| **Packet Sniffer** · `AquariusSniffer` | Live packet inspector (debug). | ⚠ Debug tool — **high log/throughput volume**; leave off in normal operation. |
| **Highway Builder** · `HighwayBuilder` | Auto nether-highway paver: clears the tunnel + lays an obsidian road. | ⚠ **Destructive** — it clears blocks along the path. Off by default. Needs obsidian + restock; intended for nether highways. |
| **Schematic Builder** · `Litematica` | Builds `.litematic` / `.nbt` schematics via Baritone. | Needs a real schematic file staged on the bot; build pacing depends on materials + Baritone. |
| **Boat Autopilot** · `Boat` | Open-water boat travel. **Command-driven** (`.boat goto x z`). | ⚠ Open water only; rubber-band risk on high latency (not heavily field-tested). |
| **Auto Regear** · `Regear` | One-shot gear restock from an ender-chest kit shulker. | Needs a kit shulker in the ender chest matching the profile. |
| **Order Filler** · `OrderFiller` | Payment-gated order picker. | ⚠ Requires a configured **database + stash**; advanced setup. |
| **Pearl Drop** · `PearlDrop` | Throws pearls into stasis chambers. | Pairs with Pearl Stasis; aim/chamber setup matters. |
| **Flight Gear** · `FlightGear` | Pre-flight gear-up — runs Regear with an elytra kit. | Depends on a valid elytra kit profile; run before long flights. |

### Combat

| Module | What it does | ⚠ Warnings & caveats |
|---|---|---|
| **Combat Assist** · `KillAura` | Auto-attacks nearby targets. | ⚠ Aggressive; **anti-cheat / ban risk** on anarchy. Tune target filters. |
| **Auto Bow** · `AutoBow` | Auto-draws & fires a bow / crossbow at range. | Same anti-cheat caution as melee assist. |
| **Auto Armor** · `AutoArmor` | Equips the best available armor. | ⚠ Can fight the worn elytra during flight — be mindful when combining with Elytra Autopilot. |

### Survival

| Module | What it does | ⚠ Warnings & caveats |
|---|---|---|
| **Auto Eat** · `AutoEat` | Eats automatically when low on health/hunger. | ⚠ **Pauses** other tasks (mining/building) while eating; keep food stocked. |
| **Auto Totem** · `AutoTotem` | Keeps a totem in your off-hand. | Competes for the off-hand slot — features that use the off-hand are designed to be AutoTotem-safe, but watch for conflicts. |
| **Auto Respawn** · `AutoRespawn` | Respawns immediately on death. | Will respawn you into danger if your spawn is hot. |
| **Auto Mend** · `AutoMend` | Repairs held gear with XP (Mending). | Needs an XP source; only mends Mending-enchanted gear. |
| **Auto Disconnect** · `AutoDisconnect` | Logs out when things go wrong. | Set thresholds (low totems/health) sensibly so it doesn't bail too early/late. |

### Connection

| Module | What it does | ⚠ Warnings & caveats |
|---|---|---|
| **Account & Login** · `Authentication` | Microsoft login, target server, optional login proxy. | 🔒 Holds **secrets** (MSA + proxy passwords) — masked in the UI; never expose. Editing here is preview; change account details carefully. |
| **Auto Reconnect** · `AutoReconnect` | Rejoins after a disconnect. | Pairs with Auto Re-queue on queued servers. |
| **Anti-Kick** · `AntiKick` | Survives the 2b2t inactivity kick. | Tuned for 2b2t timing. |
| **Anti-AFK** · `AntiAFK` | Small actions so you look active. | ⚠ Patterns can still be flagged; vary behaviour. |
| **Action Limiter** · `ActionLimiter` | Locks down what the account may do (movement, interactions, illegal items). | A **safety guardrail** — over-restricting will block other modules' actions. |
| **Auto Re-queue** · `Requeue` | Rejoins the server queue. | Handled together with Auto Reconnect. |
| **Queue Alert** · `QueueWarning` | Alerts as you near the front of the queue. | Notification only. |
| **Active Hours** · `ActiveHours` | Only stays online during scheduled times. | ⚠ Will **disconnect** the bot outside its window. |
| **Session Time Limit** · `SessionTimeLimit` | Warns/acts on the 2b2t session time limit. | Configure the action (warn vs disconnect). |

### Privacy & security

| Module | What it does | ⚠ Warnings & caveats |
|---|---|---|
| **Coordinate Privacy** · `CoordObfuscation` | Feeds the server fake coordinates to hide your base. | ⚠ Can interfere with features that rely on real coordinates — use deliberately. |
| **Anti-Leak** · `AntiLeak` | Blocks chat that leaks numbers near your coordinates. | Heuristic; tune sensitivity. |
| **Visual Range Alerts** · `VisualRange` | Alerts when players enter render distance. | Notification/awareness only. |
| **Whisper Control** · `WhisperControl` | Authorized players drive the bot by whisper (`protect` / `come` / `goto` / `patrol` / `mine`). | 🔒 **RBAC** — keep the allow-list tight; anyone on it can move/command the bot. |
| **Proxy Bridge** · `Bridge` | Links to the ProxyBridge client mod (waypoints + allow-listed commands). | 🔒 Allow-list gated; pairs with the ProxyBridge Fabric mod. |

### Automation

| Module | What it does | ⚠ Warnings & caveats |
|---|---|---|
| **Auto Fish** · `AutoFish` | Automatic AFK fishing. | Standard AFK caveats. |
| **Auto Drop** · `AutoDrop` | Drops unwanted items as they pile up. | ⚠ Double-check the drop list — it **discards** items. |
| **Chat Broadcaster** · `Spammer` | Posts chat / whisper messages on a timer. | ⚠ **Spam gets you muted/banned** — use responsibly and within server rules. |
| **Auto Reply** · `AutoReply` | Auto-replies to incoming whispers. | ⚠ Can **loop** with other auto-repliers; rate-limit. |
| **Auto Portal** · `AutoPortal` | Builds & lights a nether portal. **Command-driven** (`.portal build`). | Uses the off-hand air-place primitive; needs obsidian + flint & steel. |
| **Bad Omen** · `AutoOmen` | Manages Bad Omen / Ominous bottles for raids. | Niche; raid-specific. |
| **Chat** · `Chat` | Chat display, filtering, prefix/suffix & clickable links. | Cosmetic/utility. |

### Diagnostics

| Module | What it does | ⚠ Warnings & caveats |
|---|---|---|
| **Replay Recorder** · `ReplayMod` | Records sessions to ReplayMod files. | ⚠ **Disk usage** grows with session length. |
| **Spawn Patrol** · `SpawnPatrol` | Patrols spawn and engages fresh players. | ⚠ **PvP at spawn** — risky; combines with combat modules. |
| **Discord Notifications** · `Discord` | Discord bot — status, alerts, chat relay. | 🔒 The Discord **token is a secret** (masked); never expose it. |

---

## How it works (under the hood)

The surface is static assets (`control/`) served by the manager and made live by
`control-live.js`, which talks to the bot's loopback endpoints **through the dashboard's
authenticated relay**:

- `GET /control/state` → module list + enabled flags (the rail status dots + header chips).
- `POST /control/command` → runs a console command, returns its structured output.
- `GET /control/config` → the live config tree (**secrets redacted**); `POST /control/config` sets/edits config and persists. `{path,value}` (or `{op:"set",…}`) sets one scalar field by dot-path — powers the **⚙ Live configuration** panel. `{op:"put",path,key,value}` / `{op:"add",path,value}` / `{op:"remove",path,key|index}` add or remove whole entries in a config **map** or **list** (deserialized into the real config type) — powers the **trades / trips / pearls** list editors (AquariusProxy 5.9.0+).
- `GET /viewer/stream` (SSE, ~20 Hz) → position, vitals, dimension, nearby entities.
- `GET /viewer/map.png` → the bot-centred map tile (Live Map backdrop).
- `GET /viewer/inventory` → vitals + armor + inventory (used by the cockpit panels).

All three themes load the **same** model and the same live brain, so a fix or a new live
binding lands everywhere at once. The relay is **fork-agnostic** — it talks to the same
endpoints whether they come from AquariusProxy natively or from the
[`zenith-abm-bridge`](https://github.com/aquariusnetwork9/zenith-abm-bridge) plugin on a
ZenithProxy bot.

See also: **[Usage](Usage)** · **[Security](Security)** · **[Architecture & Limitations](Architecture-and-Limitations)**.
