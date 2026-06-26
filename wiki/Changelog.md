# Changelog

Release history for Aquarius Bot Manager. Downloads are on the [Releases page](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/releases); the version is also shown in the dashboard header and via `abm --version`.

## v3.2.2 — *latest*

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
