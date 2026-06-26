# Aquarius Bot Manager

<p align="center">
  <img src="https://img.shields.io/badge/version-3.6.0-blue.svg" alt="Version"/>
  <img src="https://img.shields.io/badge/python-3.8+_stdlib_only-3776AB.svg" alt="Python"/>
  <img src="https://img.shields.io/badge/runs_on-Linux_+_tmux-444.svg" alt="Platform"/>
  <img src="https://img.shields.io/badge/dependencies-none-success.svg" alt="No dependencies"/>
</p>

**Aquarius Bot Manager (ABM)** is a control plane for a whole VPS — or a whole *fleet* of VPSes — of **[AquariusProxy](https://github.com/aquariusnetwork9/AquariusProxy)** and **[ZenithProxy](https://github.com/rfresh2/ZenithProxy)** bots, each in its own tmux session, driven from a CLI and a web UI. Spin up a fresh box, install the manager, deploy proxies, then monitor and operate them. Pure Python stdlib + tmux — no pip installs, no Docker.

AquariusProxy is a ZenithProxy fork, so they share the same launch model and config structure; this manager drives either (or a custom fork) on the same host.

> 📖 **Install guides, the full CLI/feature reference, and configuration live on the [Aquarius Bot Manager Wiki](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki).** This README is just an overview of what's here. Read **[Security](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki/Security)** before exposing the dashboard to anything but your own machine.

![Aquarius Bot Manager dashboard](https://raw.githubusercontent.com/wiki/aquariusnetwork9/Aquarius-Bot-Manager/dashboard.png)

*One card per bot (status, live CPU/RAM, start/stop/restart) under a host gauge strip; bulk lifecycle, Deploy, Proxies, Files, Boxes, Share, Users and Settings are one click away. Full screenshot tour in the [wiki](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki).*

---

## Contents

- [What's new in 3.x](#whats-new-in-3x)
- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [CLI](#cli)
- [Files](#files)
- [Security](#security)
- [Development](#development)

---

## What's new in 3.x

The 3.x line turns the single-host manager into a **front-facing control plane with real access control**. Full notes on the **[Changelog](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki/Changelog)**.

| Feature | What it adds |
| --- | --- |
| **Live Control Surface** | A per-bot cockpit at `/control?inst=<name>` — every module, a world map, vitals, and a command runner on one page, in three switchable themes. [Control Surface →](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki/Control-Surface) |
| **Shareable guest links** | Hand someone a single URL scoped to specific bots at a tier (view / operate / config), with expiry + instant revoke. No account needed. |
| **Public sharing** | Give the dashboard a public HTTPS address from a **menu of providers** ABM installs for you — Cloudflare Quick Tunnel (no account), Tailscale Funnel (userspace, no root), ngrok, a named Cloudflare tunnel, or your own reverse proxy. |
| **Named user accounts (RBAC)** | Real per-person logins with roles **view / operate / config / admin**, each scoped to specific bots — added directly or via a one-time **invite link**. Live disable / reset / delete. |
| **Fine-grained permissions** | Per user, control exactly **which modules** they may use and configure, whether they get the **free-form console**, and whether they may **start/stop/restart** — enforced server-side, not just hidden. |
| **Multi-VPS controller** | Connect other boxes over SSH tunnels, switch into any box's dashboard in one tab, run fleet-wide bulk actions, and connect/provision/destroy DigitalOcean droplets. [Multi-VPS Controller →](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki/Multi-VPS-Controller) |
| **Automation scheduler** | Cron / interval / daily jobs + an on-crash auto-restart watchdog, this box or cross-box, with optional Discord pings. |

---

## What it does

A quick map of the surface — each links to its wiki page for the full guide.

- **Lifecycle & console** — start / stop / restart (per bot or all), a live tmux console with quick-command presets, and a structured config/module editor with a raw-JSON fallback. [Usage →](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki/Usage)
- **Deploy proxies** — one click stands up a new AquariusProxy / ZenithProxy / custom-fork bot: ABM downloads that fork's launcher, unzips it, and registers it (the launcher self-bootstraps Java + the jar). [Configuration →](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki/Configuration)
- **Proxies** — a host/port/user/pass editor with **bulk / round-robin** assignment and one-click **Webshare** import. [Proxies →](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki/Proxies)
- **Monitoring & caps** — whole-VPS and per-bot CPU/RAM/disk gauges with alert thresholds, plus enforced per-bot memory/CPU caps via cgroups.
- **Jailed file manager** — browse / edit / rename / delete files (incl. upload/download and cross-box transfer), realpath-jailed to an allowlist of roots.
- **Access control** — shareable guest links, named user accounts with roles + per-bot scopes, and per-user fine-grained module/console/lifecycle permissions. [Security →](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki/Security)
- **Multi-VPS controller & fleet** — drive your other boxes from one tab over SSH tunnels; DigitalOcean connect / provision / destroy. [Multi-VPS Controller →](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki/Multi-VPS-Controller) · [Fleet (DigitalOcean) →](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki/Fleet-(DigitalOcean))
- **Automation** — scheduled restarts/commands + on-crash watchdog, this box or fleet-wide, with Discord notifications.
- **Appearance** — themes, a custom background image, density, selectable fonts, and a selectable sidebar (icon rail / full / command center, left or right) with dedicated Fleet, Activity and per-bot Telemetry pages and a ⌘K command palette.
- **Self-update & backup** — in-place `git pull` self-update (bots untouched) with an "update available" badge and an optional daily timer; portable config backup/restore.

---

## Quick start

On a fresh Ubuntu box, **one command** installs everything (deps, the manager, systemd units, lingering for resource caps), starts the web UI, and prints the exact line to reach it:

```bash
curl -fsSL https://raw.githubusercontent.com/aquariusnetwork9/Aquarius-Bot-Manager/main/install.sh | sudo bash
```

It asks how you want to reach the UI:

- **SSH tunnel** (default, most secure) — stays on `127.0.0.1`; the installer prints a ready-to-paste `ssh -L … you@<your-ip>` line. Paste it on your computer, open `http://localhost:8765`.
- **Public HTTPS** — fronts the manager with Caddy so you just open `https://<vps>` (a domain gets a trusted cert).

There's **no CLI password step** — the first time you open the UI it walks you through creating your login, then you click **🚀 Deploy** to add proxies. That's the whole flow.

➡️ **Manual install, scripted/non-interactive flags, `cloud-init`, node-mode, and the access modes are on the [Installation wiki page](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki/Installation).**

**Requirements:** `python3` (3.8+) and `tmux` (`sudo apt install tmux`). No other dependencies.

---

## CLI

Everything in the UI has a headless `abm` equivalent. A taste:

```bash
abm list                       # roster + status
abm start bot1                 # or: all      (also: stop / restart / logs)
abm deploy bot1 --source aquarius   # stand up a new proxy (or: zenith / --repo owner/repo)
abm proxybulk --list "1.2.3.4:1080,5.6.7.8:1080" --mode roundrobin --restart
abm webshare import --token <KEY> --auth userpass --save-token --restart
abm node add box2 ubuntu@1.2.3.4    # register another VPS over an SSH tunnel
abm selfupdate                 # update the manager in place (bots untouched)
abm serve --host 127.0.0.1 --port 8765   # run the web UI
```

➡️ **Full command reference on the [Usage wiki page](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki/Usage).**

---

## Files

- `manager.py` — the program (CLI + web server, single source of truth)
- `schema.py` — curated AquariusProxy/ZenithProxy config schema for the structured editor
- `abm` — short CLI wrapper (`abm restart bot1`)
- `control/` — the Live Control Surface assets served by the manager
- `fleet.py` — **experimental** multi-droplet fleet controller (DigitalOcean); `abmfleet` is its wrapper
- `install.sh` / `cloud-init.yaml` — fresh-VPS installer + first-boot provisioning template
- `aquarius-bot-manager*.service` — systemd units (web UI + boot autostart)
- `instances.example.json` / `nodes.example.json` — config + node-registry examples

---

## Security

Dashboard access (for the owner or an admin user) is **equivalent to a shell on the box** — it runs commands inside bots, edits configs, deploys code, and browses files. Scoped roles, guest links, and per-user permissions *do* limit what a given person can reach, but the owner/admin do not.

By default the manager binds **`127.0.0.1`** and is reached over an SSH tunnel. Set a password, and if you expose it, use HTTPS. Public sharing refuses to turn on without a login. **Read the [Security wiki page](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki/Security) in full before exposing the dashboard.**

---

## Development

`manager.py` is a single stdlib-only file (CLI + a `ThreadingHTTPServer` whose SPA is an embedded HTML/JS string). No build step.

```bash
python3 manager.py serve --host 127.0.0.1 --port 8765   # run the web UI locally
python3 -m py_compile manager.py                        # syntax check
```

The VPSes track `main` and update via `abm selfupdate` (`git pull --ff-only` + a `KillMode=process` restart, so bots survive). Tagged **[Releases](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/releases)** are cut for downloaders.

See **[Architecture & Limitations](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki/Architecture-and-Limitations)** for the design and the honest list of what it does *not* do.
