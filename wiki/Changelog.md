# Changelog

Release history for Aquarius Bot Manager. Downloads are on the [Releases page](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/releases); the version is also shown in the dashboard header and via `abm --version`.

## v1.5.0 — *latest*

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
