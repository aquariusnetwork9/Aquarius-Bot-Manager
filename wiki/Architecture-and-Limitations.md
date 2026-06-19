# Architecture and Limitations

Read this before you install. It decides whether the tool fits your setup at all.

---

## The single-host model

Aquarius Bot Manager is a **single-host control plane**. It manages the bots that run **on the same machine as the manager**, and only those. Concretely, everything it does is local to its own box:

- It starts/stops bots as local **`tmux`** sessions.
- It reads the local **`/proc`** to compute per-bot CPU/RAM.
- It enforces limits via local **systemd/cgroups**.
- It deploys launchers into **local directories** and edits **local config files**.
- The file manager is jailed to **local** roots.

It has **no concept of remote hosts.** It does not SSH out to other servers, it has no agent/controller split, and it does not aggregate multiple machines into one view. One manager = one VPS = the bots on that VPS.

**Its intended design:** put **many bots on one reasonably-sized VPS**, and give each bot a distinct outbound IP using **[[Proxies]]** (including one-click Webshare import). That's how you get IP diversity without a fleet of servers.

---

## ❌ Not compatible: one bot per server (e.g. DigitalOcean droplet-per-bot)

A common alternative architecture is **one bot per server** — for example, a separate **DigitalOcean droplet** (or any VPS) for each bot, so every bot has its own machine and its own IP.

**Aquarius Bot Manager does not support that model.** A manager running on droplet A cannot see, control, monitor, or deploy the bot on droplet B. There is no central dashboard that spans droplets.

If your setup is one-bot-per-droplet, your options are:

| Option | Reality |
|--------|---------|
| **Install a separate manager on each droplet** | Works, but each is its own island — separate URL, separate login, separate dashboard. You are not getting "one place to manage everything." For 1 bot per box, the manager adds little over just running the bot. |
| **Switch to the consolidated model** | Run multiple bots on **one** bigger VPS and use **proxies** for per-bot IPs. This is what the tool is built for, and it's usually cheaper than N droplets. |
| **Don't use this tool** | If you specifically need physical/host isolation per bot, a single-host manager isn't the right shape. |

### Why one-bot-per-droplet at all?
People do it for **IP diversity** (each droplet has a unique IP, avoiding shared-IP detection/bans), **resource isolation**, and **blast radius** (one bot dying doesn't touch the others). This tool's answer to the first is **proxies**; to the second, **cgroup limits**; the third (true host isolation) it deliberately does not provide.

---

## Other limitations to know

- **Linux + systemd + tmux only.** The monitoring (`/proc`), limits (cgroups/systemd user scopes), deploy download, and installer are Linux-specific. It is not a Windows/macOS server tool.
- **No role separation.** Every authenticated user is a full admin. See **[[Security]]**.
- **Secrets at rest are file-permission protected, not encrypted.** Proxy passwords are plaintext in configs; the Webshare token is base64-obfuscated. See **[[Security]]**.
- **Resource caps need lingering.** Per-bot memory/CPU enforcement requires `loginctl enable-linger` (the installer does this); without it caps are saved but not enforced.
- **`tmux` sessions don't survive reboot.** "Auto-restart" means re-launch on boot via the boot unit + autostart flag — not live process migration. See **[[Configuration]]**.
- **It manages the bots; it is not the bot.** AquariusProxy/ZenithProxy behavior, pathfinding, anti-AFK, etc. are the proxy's domain — this tool only operates them.
- **No multi-host metrics/alerting aggregation.** Alert thresholds are per-host, surfaced in that host's dashboard only.

---

## Sizing guidance (consolidated model)

Since all bots share one box, size it for the **sum** of your bots plus headroom:

- Give each bot a **memory cap** (Limits tab / `abm limits`) so one can't OOM the others.
- Watch the **host gauge strip**; if CPU/mem/disk regularly cross your thresholds, move some bots to a second VPS with its own manager, or get a bigger box.
- Proxies add latency and can fail independently of your bots — the per-bot proxy editor and bulk/Webshare rotation exist to swap them quickly. See **[[Proxies]]**.
