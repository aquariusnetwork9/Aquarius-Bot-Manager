# Changelog

Release history for Aquarius Bot Manager. Downloads are on the [Releases page](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/releases); the version is also shown in the dashboard header and via `abm --version`.

## v1.3.0 — *latest*

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
