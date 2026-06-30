# Changelog

Release history for Aquarius Bot Manager. Downloads are on the [Releases page](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/releases); the version is also shown in the dashboard header and via `abm --version`.

## v3.19.0 — *latest*

- **Fullscreen world map, rebuilt as a live satellite map.** The ⛶ Fullscreen map is now a full Leaflet slippy-map with a **"satellite" base layer** rendered from the [2b2t.place](https://2b2t.place) tile snapshot of the greater-spawn region — pan and **zoom out to Xaero World-Map scale** (the whole ±500k spawn area) or in to ~1 block/pixel. A **Layers** panel toggles **Satellite** terrain, an **Obsidian** overlay, **New chunks**, the bot's own **Live render**, a world-aligned **Grid**, and **Highways** (Xaero-style vector lines for the 8 nether roads — axes + diagonals from origin). The map **follows the bot** (unlocks on drag; ⌖ Recenter re-locks), keeps the live entity overlay + click-to-set-an-Elytra-destination, and switches dimension with the bot (Overworld / Nether / End).
  - **The ~24 TB snapshot is never stored on your VPS.** Tiles are fetched on demand the first time they scroll into view and kept in a **hard-capped LRU disk cache** (default 64 MB via `ABM_TILECACHE_MB`; `0` = pure pass-through) — so a panned-over area is instant, the cache can't grow unbounded, and void tiles are negative-cached. The tile proxy (`/api/map/tile/…`) is SSRF-guarded and served by whichever box your browser is on; `?tilesrc=direct` bypasses the VPS and pulls tiles straight to the browser.

## v3.18.0

- **Kit Maker — a live kit builder + reusable kit library.** Build shulker-kit templates right in the Control Surface: a visual slot grid, an item picker from the real item catalog, independent **per-slot enchant matching** (e.g. silk-touch vs fortune pickaxe), and a green **"kits buildable" estimate** from your current stock. Templates save to a **kit library** and get assigned to the bot's Kit Maker module — no more building a physical example kit by hand.
- **Proxies: random-unique assignment** — bulk-assign every bot a **distinct** random proxy (no collisions) in one click.
- *v3.18.1–3.18.2* — fixes to the villager trade-builder modal (builder singletons exposed on `window`; the modal no longer sticks on a librarian).

## v3.15.0 – v3.15.1

- **Villager trade groups — shared supply, self-refill & park.** Group several trades so they **share one input-supply profile** (give-chests, restock thresholds, carry caps, overflow) while each keeps its own output chest. An emerald-**earner** member (sells items for emeralds) funds the group: when a group runs low the spender detours to the earner, and when every trade is exhausted the bot **parks in place** instead of wandering, then auto-resumes once the chests are refilled. The Control Surface gains a **Groups manager** (repurposing the old left panel) and a group selector on the trade form. **v3.15.1** adds bulk-adding existing trades to a group via a member checklist. Also fixes the trade builder so buildable outputs like **bookshelves / glass / clocks** (not just enchanted books) are selectable.

## v3.14.0 – v3.14.2

- **Stand up AquariusProxy bots cleanly — viewer & control plane on by default.** The deploy/migrate path now **places and pins the bot's jar from `api.github.com`**, so a fresh AquariusProxy bot never dies with "jar not found" (the `github.2b2t.vc` mirror only serves rfresh2 repos). New bots come up with the **live viewer + control plane enabled by default** (`--no-viewer` opts out), plus a **one-click viewer toggle** on each bot card.
- **v3.14.1** — the viewer-enable button is now reliable and reports success/failure honestly instead of silently no-op'ing.
- **v3.14.2** — **fix garbled POV at far-out bases:** the first-person POV renderer uses a **floating origin**, so the WebGL view no longer falls apart at the large coordinates typical of a real base.

## v3.13.0

- **Fix: the ⛶ Fullscreen map errored for every bot.** The fullscreen button opened `control-v4-spatial-map.html`, which was only ever a design mockup — it was never shipped into the control assets (so it 404'd) and the button used a relative URL with no bot id. There's now a **real, live fullscreen map**: it reads `?inst=<bot>`, renders that bot's actual bot-centred map tile with a live entity overlay + vitals from the 20 Hz feed, supports zoom, and **click-to-set-an-Elytra-destination** (▶ Send to Elytra) — all per-bot through the same relay. The ⛶ buttons now open it for the correct bot, and it shows a clear "viewer offline" hint if the bot isn't in-game.

## v3.12.0

- **New: `abm viewer <name> on` — one-command setup to make a bot viewable/controllable.** Enabling a bot's Live Map + control surface used to mean hand-editing its `config.json` and picking a port that didn't clash with other bots on the box. Now `abm viewer <bot> on` does it: it **auto-assigns the next free viewer port** (so co-located bots never collide), enables `server.viewer.enabled` + `control`, binds loopback, and restarts the bot. `abm viewer <bot> off` disables it; `--no-control` makes it view-only; `--no-restart` writes config without restarting. (The bot is stopped before its config is edited so it can't overwrite the change on shutdown.)

## v3.11.2

- **Fix: a bot without its own viewer showed *another* bot's live map / control feeds.** The viewer port resolver defaulted every bot to `2998`, so any bot whose viewer wasn't enabled (or that shared the default port) got routed to whichever bot actually owns `2998` — its Live Map, vitals, and control surface silently showed the *wrong* bot. Now a bot whose viewer is disabled resolves to **no port** and correctly reads **"Viewer offline"** instead. To actually view/control a second bot on the same box, enable its viewer on a **unique** `server.viewer.port` (two bots can't share one) — the "Viewer offline" panel spells out the steps.

## v3.11.1

- **Fix: the 📍 "use look target" button did nothing.** ABM's control relay forwards a fixed allow-list of bot endpoints, and the new `/control/lookingat` wasn't on it — so the request 404'd at the relay before ever reaching the bot. Added it; the button now works (bot must be in-game).

## v3.11.0

- **Edit entries, not just add/delete.** Every trade / saved trip / pearl now has an **✎ edit** button that opens the same form pre-filled with its current values; saving overwrites it in place (the villager builder reverse-maps a stored trade — profession, items, output, book enchant — back into the validated picker). *(Needs AquariusProxy 5.9.1+ for editing **pearl** locations in place.)*
- **📍 "use the block the bot is looking at" for every coordinate.** Next to each x/y/z field (chest positions, pearl interact-block, trip destination) there's a 📍 button — aim the bot at the block and click it to fill the coordinates from the bot's crosshair instead of typing them. Backed by the bot's new `GET /control/lookingat` (a 96-block raycast). If the bot isn't in-game or nothing's in its crosshair, it tells you.

## v3.10.0

- **The control surface's "add an entry" buttons actually work now.** Three lists in Mission Control were drawn but never wired — clicking **＋ New trade** (Villager Trading), **＋ New trip** (Elytra Autopilot), or **＋ Add pearl** (Pearl Stasis) did nothing. They now open a real form and write straight to the bot's live config:
  - **Villager trades** — a guided builder (profession → give → receive, validated against the real Java-Edition trade catalog so you can't configure an offer the bot would wait on forever) plus the supply/output **chest coordinates**, restock thresholds, and per-trade limits. Librarian book trades map the chosen enchantment to the correct registry id automatically. The running trader is told its list changed so a new trade takes effect mid-run.
  - **Saved Elytra trips** — name + destination (overworld or nether); the nether leg is projected automatically for a direct open-nether flight.
  - **Pearl-stasis locations** — name + the interact-block coordinates.
  - Each list now shows the bot's **real, live entries** (not the old mock preview) with a per-row **🗑 delete**. Everything is gated by the existing per-module **config** permission and works in all three themes.
- **Requires AquariusProxy 5.9.0+** (the bot exposes new add/remove operations on its `/control/config` API). Older bots still show the lists read-only.

## v3.9.0

- **Elytra pitstop & goal-stop controls** surfaced in the Elytra Autopilot card (logout / set-spawn at an intermediate geofence or at the destination).

## v3.8.0

- **ZenithProxy bots get the full live dashboard, via a plugin.** AquariusProxy bots already get the live map, POV viewer, inventory/vitals, module toggles, command runner, and live config editing built in. Now ZenithProxy bots can get the *same* surface by dropping in the new **[`zenith-abm-bridge`](https://github.com/aquariusnetwork9/zenith-abm-bridge)** plugin — no fork switch required. Install the jar, run `abmBridge on` (and `abmBridge control on` to allow command/config writes), and the bot's Live Map / control surface light up in ABM exactly like an AquariusProxy bot. The bridge is **loopback-bound, disabled by default, and write-gated**; ABM relays it over its own authenticated tunnel.
- **Auto-detect the bridge port.** ABM now resolves a bot's viewer port from the bridge's `plugins/config/abm-bridge.json` too (AquariusProxy uses `server.viewer.port`); both default to `2998`, so a freshly-installed bridge is found with zero config. The viewer's "offline" hint now tells you how to enable it for *either* fork.

## v3.7.1

- **Fix: Tailscale Funnel guidance (no more "command timed-out").** Tailscale Funnel needs a **one-time tailnet enablement** beyond signing in — ABM wasn't surfacing that step and instead showed a confusing *command timed-out*. The Funnel attempt now runs detached (so a slow first HTTPS-cert provisioning never blocks or leaves stuck processes), and when the tailnet hasn't enabled Funnel the Share panel shows a direct **"Enable Funnel"** link. Click it once (in your Tailscale admin) and the address goes live automatically. *(The Cloudflare Quick Tunnel remains the zero-setup option if you just want a public link now.)*
- **Fix: invite links warn when they're private.** Like share links, the one-time invite link now warns if it points at a `localhost`/private address — turn on **Public sharing** first so the invitee can actually open it.

## v3.7.0

- **One-click "Migrate to AquariusProxy".** Any bot detected as **ZenithProxy** now shows an **⇪ Aquarius** button on its card (owner/admin). It converts the bot to AquariusProxy **in place, keeping its `config.json` and Minecraft account** (`mc_auth_cache.json`): it stops the bot, **backs up** `config.json` / `mc_auth_cache.json` / `launch_config.json` / `launch` / the jar to a `premigrate-…` folder, repoints the launcher to `aquariusnetwork9/AquariusProxy` (keeping the valid version), **swaps the `launch` binary** for the Aquarius launcher (the step that actually makes it stick), and restarts. A dialog shows the steps + a live log and warns that external plugin jars won't load on AquariusProxy (many have baked-in equivalents). A **Roll back** button restores the backup if anything looks off. Works on remote-box bots too.

## v3.6.0

- **Fine-grained per-user permissions — owners decide exactly what each user can do.** On top of the role tiers, open **👤 Users → Permissions** on any operate/config user to control, per user:
  - **Which modules** they can **use** (see + toggle on/off) and **configure** (the ⚙ live-config panel) — a per-module matrix grouped by category, or "all modules".
  - Whether they get the **free-form console** (typing arbitrary bot commands) at all.
  - Whether they can **start / stop / restart** bots.
  - It's **enforced on the server**, not just hidden: a user without the console grant can only send exact module on/off toggles for modules they're allowed to use (anything richer is refused); config writes are checked per module; lifecycle and out-of-scope actions are blocked. The control surface also hides what a user can't touch. Permissions only *restrict* within a user's role; clearing them returns to the role default. Changes apply live (no re-login). Admins and the owner are unaffected.
- **Fix (important): bot Start / Stop / Restart was broken.** A variable introduced in v3.4.0 shadowed the internal `restart` action, so the per-bot and "all" start/stop/restart buttons threw a 500 (your bots were unaffected — only the buttons failed). Fixed.

## v3.5.0

- **Named user accounts with roles (multi-user RBAC).** Beyond the single owner login and anonymous share links, you can now give people their **own username + password** with a role and a set of bots — open **👤 Users** (owner/admin only). Roles: **View** (read-only) · **Operate** (+ start/stop/restart, console commands, module toggles) · **Config** (+ edit settings / live config) · **Admin** (full control — a second owner who can also manage users). Each non-admin user is **scoped to the bots you pick** (or fleet-wide), enforced on every request exactly like share links — out-of-scope bots return 404, owner-only actions return 403.
  - **Two ways to add people:** create a user directly (set their password), or generate a **one-time invite link** with a preset role + bots — they open it, choose their own password, and they're in (no password handoff). Invites can carry a preset username and an expiry.
  - **Live control:** change a user's role, **disable** (instant lockout), **reset their password** (signs out their other sessions), or **delete** them — all take effect immediately, even mid-session. Passwords are stored as salted **PBKDF2** hashes, never plaintext; the owner account stays separate and always full-access.
  - `manager.py`-only — the proxy bots are untouched. See **[Security → Named user accounts](Security#named-user-accounts)**.

## v3.4.2

- **Fix: choosing/enabling a tunnel provider could time out** (Tailscale especially). Two causes, both fixed: (1) the install (a tens-of-MB download) and the Tailscale start (`tailscaled` + `tailscale up` + funnel) ran **synchronously in the web request**, so the browser gave up before they finished — both now run in the **background** and the panel shows live "setting up… / waiting for sign-in…" progress; (2) with a remote box selected in the switcher, the public-sharing call was being **proxied to that node** (with a short timeout) instead of staying on the dashboard you're actually exposing — public-sharing requests are now always controller-local.

## v3.4.1

- **Public sharing: pick from a dropdown, and ABM installs the provider for you.** The provider list is now a **dropdown** that reveals a single card for whatever you choose (instead of five stacked option boxes). And when you pick a provider that needs a helper installed, **ABM downloads and sets it up automatically — no root, no manual steps**:
  - **Cloudflare** (quick & domain) → fetches `cloudflared`; **ngrok** → fetches the ngrok binary; **Tailscale** → downloads the static binaries and runs it in **userspace** (so it needs no root and no system service), then shows you a one-time **sign-in link** and brings the funnel up automatically once you've signed in.
  - There's a manual **Set up** button as a fallback/retry, and **Custom URL** stays install-free (you supply the address).

## v3.4.0

- **Public sharing is now a menu of providers — pick whatever fits.** The Share panel's **Public sharing** card lets you choose *how* your dashboard gets a public HTTPS address, so a guest link works for anyone:
  - **Cloudflare Quick Tunnel** — no account, no domain, one click (the v3.3 default). URL re-rolls on reboot.
  - **Tailscale Funnel** — free, sign in once on the VPS; a **stable** `https://<machine>.<tailnet>.ts.net` with a valid cert that **survives reboots**. The best set-and-forget option if you don't own a domain.
  - **ngrok** — free account + authtoken; reserve one free static `*.ngrok-free.app` domain and the address stays put across restarts.
  - **Cloudflare Tunnel (your domain)** — paste a Zero-Trust tunnel token + your public hostname for a **stable custom domain**.
  - **My own domain / reverse proxy** — already serving the dashboard behind Caddy/nginx? Just tell ABM the URL; it runs nothing and builds links from it.
  - Secrets (tokens/authtokens) are obfuscated at rest, never returned to the browser, and only re-sent when you type a new one. Still refuses to expose a no-login dashboard. Switching providers cleanly tears the old one down; managed tunnels are still **adopted across manager restarts** so the URL only changes on the provider's own terms (e.g. a quick-tunnel reboot). See **[Security → Public sharing](Security#public-sharing)**.

## v3.3.1

- **Public sharing: the tunnel URL now stays stable.** The Cloudflare quick tunnel runs detached and ABM **adopts** the running tunnel across manager updates/restarts instead of respawning it — so the public address (and therefore your share links) only changes on an actual VPS reboot or crash, not every time the manager restarts. Teardown is also more reliable. *(No domain needed — this is the no-account, no-domain option. For a URL that survives even reboots without a domain, use the new Tailscale-Funnel provider in v3.4.0.)*

## v3.3.0

- **One-click public sharing for guest links.** Guest links only work for people who can reach the dashboard — which, on the default SSH-tunnel setup, is just you. The Share panel now has a **Public sharing** toggle: flip it on and ABM spins up a **Cloudflare quick tunnel** (downloads `cloudflared`, runs it, no account/domain/cert) to give the dashboard a public HTTPS address. Every link you create then uses that address, so someone who's never touched your VPS can click it and land straight in, scoped to their bots. Requires a dashboard password (it refuses to expose an open/no-login dashboard). The quick-tunnel URL is **ephemeral** (changes if the tunnel restarts), so generate links fresh; a stable named-tunnel mode is coming.

## v3.2.3

- **Fix:** the sidebar (full layout) now scrolls instead of clipping its bottom nav items on short windows. Completes the modal/scroll audit started in v3.2.2 — modals, drawer, logs, lists, control tab and sidebar all scroll their overflow now.

## v3.2.2

- **Fix:** modals now scroll instead of clipping. A tall modal (e.g. the Share panel with many bots + an audit log) was capped at the window height and its cards got squished — buttons and content cut off with no way to scroll to them. The modal's cards no longer shrink, so the modal scrolls and everything is reachable. Applies to all modals (Settings/Files/Proxies too).

## v3.2.1

- **Fix:** the **👥 Share** button was only in the classic header — it's now also in the sidebar nav and quick-action menu, so it shows in the full (sidebar) layout too. Still owner-only.

## v3.2.0

- **Shareable-link guest access (tiered).** Hand someone a single URL that lets them operate **only the bots you choose**, at a capability tier — **View** (read-only) / **Operate** (+ start/stop/restart + console commands) / **Config** (+ edit settings). No guest accounts, no password handout. Open the **👥 Share** panel (owner header) to create a link (pick bots — including bots on remote boxes — a capability, and an expiry), copy it **once** (only its hash is stored), and share it. Manage or **revoke** links anytime; **Revoke all** kills every active link instantly.
  - **Security:** the grant is re-checked on **every request**, so expiry/revoke/scope-edits take effect immediately — even for a guest who's already in. Out-of-scope bots return 404 (indistinguishable from missing); guests can never list/switch boxes, see node credentials, or touch fleet/owner settings (delete, rename, deploy, proxies, system are owner-only). Redemption is rate-limited; the link travels over whatever transport the manager is exposed on, so use an HTTPS access mode before sharing externally. Recent guest activity is shown in the Share panel.
  - The control surface honors the same tiers (a View guest sees a read-only ⚙ Live configuration and no command runner). `manager.py`-only — the proxy bots are untouched.

## v3.1.0

- **Live configuration editing in the Control Surface.** Each module now has a **⚙ Live configuration** panel that reads the bot's *real* config and writes single fields back to the running bot instantly (toggles, numbers, text), persisted to its config — no restart. Powered by new bot-side endpoints `GET /control/config` (the live tree with **all secrets redacted** — auth/proxy/discord/db passwords + tokens are never exposed) and `POST /control/config` `{path,value}` (sets one field by dot-path; secret paths are refused). The friendly mockup subcards stay as a read-only overview above it. Requires a bot on **AquariusProxy 5.7.0+** with `server.viewer.control=true`.

## v3.0.0

- **Live Control Surface (Mission Control).** Each bot gets a front-facing cockpit at **`/control?inst=<name>`** (open it from the viewer drawer's **Control → ⛶ Open full control surface**) — every module, the world map, vitals, and a command palette on one page. It's served by the manager and relayed over the same authenticated, loopback-only tunnel as the live viewer, so nothing the bot serves is exposed.
  - **Live:** module status dots + **enable toggles**, per-module action buttons, **vitals** (health / food / position / dimension / speed from the ~20 Hz SSE feed), a **command runner**, and an interactive **Live Map** — a real bot-centred map tile with an entity overlay where you **click to set an Elytra destination** (`fly trip <dim> <x> <z>`).
  - **Three appearances**, switchable from the top bar and remembered: **Mission Control** (`v1`, default), **Aurora Glass** (`v2`), **Console Pro** (`v3`) — all share the same live data and the same Live Map.
  - Per-module **settings subcards are read-only previews** for now (honest in-UI banner); live config editing arrives in **v3.1** with the `/control/config` API. Pearl-pins-on-the-map and the trade-list editor follow after.
  - New full guide with screenshots and a **48-module reference (warnings & caveats per module)**: **[[Control Surface]]**.
  - Requires the bot's `server.viewer.enabled` **and** `server.viewer.control` to be `true` (with `control:false` it's a read-only viewer).

## v2.0 – v2.2 — live viewer & first-person POV

- **Per-bot live viewer** — a 2D world map that follows the bot (map / pan / overlays) plus an offline first-person **POV** rendered with raw WebGL (ambient occlusion + fog + selectable 32/48/64 render range; no textures by design). Streamed at ~20 Hz over SSE with per-bot viewer-port auto-detection.
- **Control tab (cockpit precursor)** in the per-bot drawer — vitals, inventory/armor with real MC item icons, module toggles, and an ElytraPilot flight panel. v3.0.0 promotes this to the full standalone Control Surface.

## v1.7.0

- **Automation: pick the target bot from a dropdown.** The "Target bot" field on the Automation page is now a dropdown of every bot on the selected box — **running or not** — plus an **All bots** option, instead of a free-text name. It tracks the **Box** selector: choosing a connected box lists that box's bots, and **All boxes** collapses to just *All bots*. No more typos in bot names.
- **10 selectable fonts.** A new **Font** picker in **Settings → Appearance** swaps the whole UI's display + monospace pairing: **Aquarius** (default · Sora / Space Mono), **System** (no web fetch), **Inter**, **Roboto**, **Rounded** (Nunito / Fira Code), **Grotesk** (Space Grotesk / IBM Plex Mono), **Terminal** (all-mono), **Geometric** (Poppins / Source Code Pro), **Classic** (Work Sans / Ubuntu Mono), and **Editorial** (Libre Franklin / Spline Sans Mono). The choice previews live, persists in `settings.theme.font`, lazy-loads its Google Font, and is also settable via `abm settings --font <name>`.

## v1.6.0

- **Automation — scheduled actions + an on-crash watchdog.** A new **Automation** page (stored in `settings.schedules`) runs jobs on the controller every ~30 s:
  - **Time schedules** — cron (`0 4 * * *`), `every:30m` / `every:2h`, or `daily:HH:MM`.
  - **On-crash watchdog** — auto-restart a crashed bot up to *N* times with a cooldown, then back off.
  - **Actions** — restart / start / stop / send a console command (e.g. `fly resupplyspares`), against one bot or all.
  - **Cross-box** — target this box, a specific connected box, or **all boxes** (runs over the controller's node tunnels).
  - **Discord notifications** — an optional webhook pinged when a job runs/fails or the watchdog restarts a bot.
  - Add / enable / disable / delete jobs and **Run now** from the UI; jobs ride along with config backup/restore.

## v1.5.1

- **Fixed: a connected box could show "offline" in the Fleet view while it was actually reachable.** After a manager restart (e.g. `abm selfupdate`), the previous SSH tunnel was orphaned but kept holding the loopback port, so the new managed tunnel couldn't bind — the supervisor then reported the box as down and couldn't self-heal if the orphan later died. The tunnel supervisor now reaps any stale/orphaned tunnel on the port before spawning, so boxes reconnect cleanly after every restart (and zombie `ssh` children are reaped).
- **The command-center search bar now works** — press **⌘K** (Ctrl-K) anywhere to open a command palette that searches bots on this box *and* on every connected box, then jumps to a bot's telemetry/console or any page.

## v1.5.0

- **Selectable sidebar navigation** — choose a layout in **Settings → Appearance**: the classic header (*off*), a compact **icon rail**, a **full** labeled sidebar with pinned live CPU / memory / disk vitals, or a **command center** with a search palette, a live crash alert and a live bot roster — each **left- or right-oriented**. Defaults to the full sidebar on the left; switch back to the classic header anytime (nothing is removed).
- **Fleet page** — a full-page multi-box overview: per-box health gauges and bot counts (offline boxes included), fleet totals, and a table of the bots on this box. Promotes the Boxes status view to a first-class page.
- **Activity & alerts page** — a live snapshot across the box: crashes, resource-threshold breaches, and proxy-console issues, with at-a-glance counts. (A persistent historical event log is coming next.)
- **Telemetry page** — per-bot detail: live status / CPU / memory / process tiles, a live CPU chart, and an embedded console tail, with a bot switcher. Open it by clicking a bot in the Fleet table or the command-center roster.
- **Automation page** *(preview)* — the planned layout for scheduled actions and auto-recovery is visible as a preview; the scheduler backend lands in a later release.
- **Console log fix** — each instance's live console is now its own scroll area, so a long log no longer clips the top/bottom or pushes the command bar off-screen, and the command bar stays pinned. Scrolling up **pauses** auto-follow (so you can read back) and a **"jump to latest"** pill returns you to the live tail.

## v1.4.0

- **Multi-VPS controller** — one manager can drive your other boxes. Each other box runs the same manager in lightweight **node mode** (`ABM_ACCESS=node`, bound to `127.0.0.1`), reached over a self-healing **controller-managed SSH tunnel** — nodes are never exposed publicly. See **[[Multi-VPS Controller]]**.
  - **🖥 Boxes** panel: connect a box by pasting `user@host`, list/remove boxes, and a live **Fleet view** (reachable, bots running, host load/mem) with fleet-wide **Start / Restart / Stop all** and **Update all nodes**.
  - **In-page box switcher** — a sticky top bar reverse-proxies any connected box's full native dashboard into the same browser tab (console, config, files, everything); no extra tunnel or tab.
  - **DigitalOcean** — save a token, then **connect existing droplets** or **provision a new 1GB node-mode droplet** (auto-uploads the controller's SSH key + cloud-init install), and **Destroy** DO-backed boxes.
  - **All-boxes launcher** under 🔗 Connect — one script that tunnels into every box on distinct local ports (direct-access fallback).
  - CLI: `abm node list|add|remove|test`.
- **"Update available" indicator** — the 🔄 self-update button now shows a badge when the box is behind its upstream (a quiet `git fetch`, no pull).
- **Config backup & restore** — download a portable bundle of a box's configs (instances + node registry) and restore it later (a timestamped pre-restore snapshot is saved first). Settings → System.
- **Theming** — six new presets (`obsidian`, `forest`, `rose`, `ocean`, `gold`, `sand`), a **custom background image** with a dim slider, a **density** control (comfortable / compact / spacious), and one-click accent **colour swatches**.

## v1.3.0

- **Proxy health & auto-fix** — scans each running bot's console for proxy errors (dead / Webshare-removed IPs) and shows which bots are broken (with the matching console line as evidence). One click re-imports fresh IPs from Webshare and reassigns them to the broken bots, then restarts. Fix scope: **errored only**, selected, or all; assignment: **random**, round-robin, or same. Detection patterns are tunable in the config (`settings.proxy_health.patterns`). New `Errored` quick-select in the bulk panel; new CLI `abm proxyhealth`, plus `--mode random` and `--targets errored` on `abm proxybulk` / `abm webshare`.
- **Self-update / auto-update** — update the manager in place with **`abm selfupdate`** (`git pull` + restart, no reinstall) or the **🔄 Update manager** button in Settings → System. Enable **`abm autoupdate on`** (or the "Auto-update daily" toggle) to install a systemd timer that keeps it current automatically.
- **Safe restarts** — the systemd units now set `KillMode=process`, so restarting the manager (including `abm selfupdate`) only stops the manager process and never tears down the running bot `tmux` sessions, even when bots share the service's cgroup.

## v1.2.0

- **Webshare proxy import** — one-click import of a whole [Webshare](https://www.webshare.io/) proxy subscription, supporting both auth models (per-proxy user/pass and IP-authorized). See **[[Proxies]]**.
- **Per-bot proxy credentials** — the proxy editor now carries host / port / username / password per bot, not just host:port.
- **Reconnect-friendly UX** — bots and the dashboard survive your browser closing or your PC rebooting; a **Connect** panel hands you a one-click reconnect shortcut.
- **Experimental [[Fleet (DigitalOcean)]] controller** (`fleet.py` / `abmfleet`) — a multi-droplet layer for the one-bot-per-server model: provision droplets via the DigitalOcean API and manage their agents from one controller. Experimental, opt-in, separate from the single-host base manager.
- Full wiki documentation + UI screenshots.

## v1.1.0

- **Browser-first onboarding** — create your login in the browser on first run via an in-app setup wizard; the installer is smarter about access modes (SSH tunnel vs. exposed HTTPS) and prints the exact next step.

## v1.0.0 — first release

- Rebranded from **ZenithProxy Manager** to **Aquarius Bot Manager** (CLI `zp` → `abm`, env `ZP_*` → `ABM_*` with legacy fallback, tmux prefix `zp_` → `abm_`, systemd units, `/opt/aquarius-bot-manager`). Manages **AquariusProxy and ZenithProxy** (and custom forks).
- **VPS control plane** (built in five phases):
  - **Monitoring** — host gauge strip (CPU / memory / disk) + per-bot CPU% and RAM, with alert thresholds.
  - **Enforced resource limits** — per-bot memory/CPU caps via systemd user scopes (cgroups).
  - **Jailed file manager** — browse/edit configs within an allowlist of roots.
  - **One-click deployer** — stand up a new AquariusProxy / ZenithProxy / custom-fork bot from the official launcher, which self-bootstraps Java and the jar.
  - **Fresh-VPS installer** + `cloud-init.yaml` for first-boot provisioning.
- CLI (`abm`) + pure-stdlib web UI: lifecycle (start/stop/restart/logs, one or all), live console with editable command presets, structured config editor with raw-JSON fallback, bulk / round-robin proxy assignment, and a login/session auth layer.
- Pure Python standard library + `tmux` — no pip, no Docker, no database.
