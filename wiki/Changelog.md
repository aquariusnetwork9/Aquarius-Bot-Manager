# Changelog

Release history for Aquarius Bot Manager. Downloads are on the [Releases page](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/releases); the version is also shown in the dashboard header and via `abm --version`.

## v1.2.0 — *latest*

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
