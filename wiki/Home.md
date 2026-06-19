# Aquarius Bot Manager

A **control plane for a whole VPS of Minecraft proxy bots** — [AquariusProxy](https://github.com/aquariusnetwork9) and [ZenithProxy](https://github.com/rfresh2/ZenithProxy) (and custom forks) — driven from a CLI and a web UI. Spin up a fresh box, install the manager, deploy proxies, then monitor and operate them all from one dashboard.

Pure Python standard library + `tmux`. No pip installs, no Docker, no database.

---

## ⚠️ Read this first: the one-host model

**Aquarius Bot Manager controls only the bots that run on the same machine it runs on.** It manages local `tmux` sessions, reads the local `/proc` for monitoring, enforces local cgroup limits, and deploys into local directories. It does **not** reach out over SSH to control bots on other servers.

This means its core design is **many bots consolidated on one VPS**, using **proxies** for per-bot IP diversity. If your setup is **one bot per server** (e.g. a DigitalOcean droplet per bot for separate IPs), the base manager alone won't span them — but the experimental **[[Fleet (DigitalOcean)]]** controller adds a multi-droplet layer on top. Read **[[Architecture and Limitations]]** before you install.

---

## What it does

- **Lifecycle** — start / stop / restart / view logs for every bot, one or all at once.
- **Live console** — attach to each bot's stdin/stdout, with editable quick-command presets.
- **Structured config editor** — a curated AquariusProxy/ZenithProxy settings editor with a raw-JSON fallback; **Save** or **Save & Restart**.
- **Proxies** — per-bot host/port/username/password editor, bulk round-robin assignment, and one-click **import from a Webshare subscription**. See **[[Proxies]]**.
- **Monitoring** — a host gauge strip (CPU / memory / disk) plus per-bot CPU% and RAM, with alert thresholds.
- **Resource limits** — enforce per-bot memory/CPU caps via systemd user scopes (cgroups).
- **One-click deploy** — stand up a new AquariusProxy / ZenithProxy / custom-fork bot from scratch; the launcher self-bootstraps Java and the jar.
- **Jailed file manager** — browse/edit configs within an allowlist of roots.
- **Reconnect-friendly** — bots and the dashboard survive your browser closing or your PC rebooting; a **Connect** panel hands you a one-click reconnect shortcut.

## Documentation

| Page | What's in it |
|------|--------------|
| **[[Installation]]** | Fresh-VPS install, access modes (tunnel vs HTTPS), first-run setup, cloud-init, manual install |
| **[[Usage]]** | Dashboard tour, full CLI reference, lifecycle, console, monitoring, reconnecting |
| **[[Configuration]]** | `instances.json`, per-bot fields, autostart/boot, resource limits, settings |
| **[[Proxies]]** | Manual + bulk proxy assignment, **Webshare import** (both auth models) |
| **[[Fleet (DigitalOcean)]]** | *(experimental)* Multi-droplet: provision via the DigitalOcean API and manage agents from one controller |
| **[[Security]]** | **The full security model, what dashboard access grants, and what's stored on disk — read this** |
| **[[Architecture and Limitations]]** | The single-host model, **the multi-droplet fleet path**, and where the tool does not fit |

## 60-second start

```bash
curl -fsSL https://raw.githubusercontent.com/aquariusnetwork9/Aquarius-Bot-Manager/main/install.sh | sudo bash
```

It installs everything, starts the web UI, asks how you want to reach it, and prints the exact next step. Open the dashboard, create your login in the browser, and click **🚀 Deploy**. Full detail in **[[Installation]]**.
