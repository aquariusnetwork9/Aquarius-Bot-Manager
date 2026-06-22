# Aquarius Bot Manager

A control plane for a whole VPS of **AquariusProxy** and **ZenithProxy** bots — each in its own tmux session — from a CLI and a web UI. Spin up a fresh box, install the manager, deploy proxies, then monitor and operate them. Pure Python stdlib + tmux. No pip installs, no Docker.

AquariusProxy is a ZenithProxy fork, so they share the same launch model and config structure; this manager drives either (or a custom fork) on the same host.

**Highlights:** lifecycle + live console with quick-command presets · structured config editor · proxy host/port editor with bulk/round-robin assignment · live whole-VPS + per-instance CPU/RAM/disk gauges with alert thresholds · enforced per-instance memory/CPU caps (cgroups) · a jailed file manager · one-click proxy deployment (Aquarius / Zenith / custom fork) · a fresh-VPS installer · **multi-VPS controller** (connect other boxes over SSH tunnels, switch into any box's dashboard in one tab, fleet-wide bulk actions, DigitalOcean connect/provision/destroy) · in-place self-update · config backup/restore · themes + custom background · **selectable sidebar navigation** (icon rail / full / command center, left- or right-oriented) with dedicated **Fleet, Activity and per-bot Telemetry** pages and a **⌘K command palette** that searches your bots across every connected box.

![Aquarius Bot Manager dashboard](https://raw.githubusercontent.com/wiki/aquariusnetwork9/Aquarius-Bot-Manager/dashboard.png)

*One card per bot (status, live CPU/RAM, start/stop/restart) under a host gauge strip; bulk lifecycle, Deploy, Proxies, Files, **Boxes** and Settings are one click away. Full screenshot tour in the [wiki](https://github.com/aquariusnetwork9/Aquarius-Bot-Manager/wiki).*

Pick a layout in **Settings → Appearance** — the classic header, or a sidebar (icon rail / full with pinned host vitals / command center with a live bot roster), left or right:

![Sidebar layout with the Fleet page](https://raw.githubusercontent.com/wiki/aquariusnetwork9/Aquarius-Bot-Manager/sidebar-full.png)

Hit **⌘K** (Ctrl-K) anywhere to search bots — on this box *and* every connected box — and jump straight to a bot or a page:

![⌘K command palette](https://raw.githubusercontent.com/wiki/aquariusnetwork9/Aquarius-Bot-Manager/command-palette.png)

## Files
- `manager.py` — the program (CLI + web server, single source of truth)
- `schema.py` — curated AquariusProxy/ZenithProxy config schema for the structured editor
- `abm` — short CLI wrapper (`abm restart bot1`)
- `fleet.py` — **experimental** multi-droplet fleet controller (DigitalOcean); `abmfleet` is its wrapper. See the wiki's _Fleet (DigitalOcean)_ page
- `install.sh` — fresh-VPS installer (curl | sudo bash)
- `cloud-init.yaml` — first-boot provisioning template for cloud providers
- `aquarius-bot-manager.service` — systemd unit for the web UI
- `aquarius-bot-manager-boot.service` — systemd oneshot unit that starts autostart instances on boot
- `instances.example.json` — config schema example
- `nodes.example.json` — controller node-registry example (other boxes reached over SSH tunnels)

## Requirements
- `python3` (3.8+) and `tmux`: `sudo apt install tmux`

## Provision a fresh VPS (recommended)
On a new Ubuntu box, **one command** installs everything (deps, the manager, systemd units, lingering for resource caps), starts the web UI, and prints the exact line to reach it:
```bash
curl -fsSL https://raw.githubusercontent.com/aquariusnetwork9/Aquarius-Bot-Manager/main/install.sh | sudo bash
```
It asks how you want to reach the UI:
- **SSH tunnel** (default, most secure) — stays on `127.0.0.1`; the installer prints a ready-to-paste `ssh -L … you@<your-detected-ip>` line. Paste it on your computer, open `http://localhost:8765`.
- **Public HTTPS** — fronts the manager with Caddy so you just open `https://<vps>` (give it a domain for a trusted cert, or accept a one-time self-signed warning).

There's **no CLI password step** — the first time you open the UI it walks you through creating your login, then you click **🚀 Deploy** to add proxies. That's the whole flow.

Scripted / non-interactive overrides:
```bash
# pick the access mode up front (no prompt); override any default
sudo ABM_ACCESS=tunnel bash install.sh
sudo ABM_ACCESS=https ABM_DOMAIN=bots.example.com bash install.sh
sudo ABM_RUN_USER=ubuntu ABM_PORT=8765 ABM_BASE_DIR=/home/ubuntu/zenith bash install.sh
```

To provision at boot, paste `cloud-init.yaml` into your provider's user-data field (edit `ABM_RUN_USER` if not `ubuntu`; add `ABM_ACCESS=https ABM_DOMAIN=…` for public HTTPS).

## Manual install
```bash
sudo mkdir -p /opt/aquarius-bot-manager
sudo cp manager.py schema.py abm /opt/aquarius-bot-manager/
sudo ln -s /opt/aquarius-bot-manager/abm /usr/local/bin/abm   # optional: `abm ...` anywhere
```

## Configure
Three ways to populate `instances.json`:

1. **Discover** dirs under a base folder (looks for launch.sh/start.sh/run.sh or a `.jar`):
   ```bash
   abm discover /home/ubuntu/zenith
   ```
2. **Scan + adopt** tmux sessions you already started by hand (see below).
3. **Add** one explicitly:
   ```bash
   abm add bot1 /home/ubuntu/zenith/bot1
   ```

Per-instance schema:
- `name` — unique label (letters, digits, `.` `_` `-`). For managed instances it becomes the tmux session `abm_<name>`.
- `dir` — working directory
- `launch_cmd` — start command (default `./launch.sh`; e.g. `java -jar AquariusProxy.jar nogui` or `java -jar ZenithProxy.jar nogui`)
- `config_file` — file shown/edited in the web UI (default `config.json`)
- `stop_keys` — tmux send-keys for graceful shutdown (default `["C-c"]`; e.g. `["stop","Enter"]`)
- `stop_timeout` — seconds to wait before force-killing (default 15)
- `autostart` — start this instance on host boot (default `false`; see Auto-restart below)
- `session` — *(adopted instances only)* the existing tmux session this is pinned to

A `settings` block is also stored in the file (theme + system-action toggle); managed via the UI or `abm settings`.

## CLI
```bash
# lifecycle
abm list
abm status
abm start   bot1            # or: all
abm stop    bot1
abm restart all
abm logs    bot1 --lines 200

# managing the roster
abm add     bot1 /home/ubuntu/zenith/bot1 [--launch-cmd ...] [--stop-keys "stop,Enter"]
abm delete  bot1 [--force]                # --force stops it first if running
abm scan                                  # list unmanaged tmux sessions, proxies flagged
abm adopt   <session> [--name NAME]       # bind an existing session as a managed instance
abm autostart bot1 --on                   # or --off; mark for launch on boot
abm boot                                  # start all autostart instances (run at host boot)
abm send    bot1 killAura on              # send a command to the live console
abm proxies                               # list each instance's proxy host:port
abm proxy   bot1 --host 1.2.3.4 --port 1080   # set proxy (view if no flags)
abm proxybulk --list "1.2.3.4:1080,5.6.7.8:1080" [--targets a,b,c|all] [--mode roundrobin|same] [--restart]
                                          # assign/rotate proxies across many instances at once
                                          # entries may carry creds: host:port:user:pass or user:pass@host:port
abm webshare count  --token <KEY>         # fetch your Webshare list and report how many (no changes)
abm webshare import --token <KEY> [--auth userpass|ip] [--targets all] [--mode roundrobin|same]
                    [--countries US,CA] [--valid-only is default; --all-proxies to include invalid]
                    [--plan-id N] [--save-token] [--restart]
                                          # pull a Webshare subscription's proxies and assign them

# deploy / limits / files
abm deploy  bot1 --source aquarius        # or zenith, or custom --repo owner/repo; [--dir ...] [--memory 2G] [--cpu 200]
abm limits  bot1 --memory 2G --cpu 200    # enforce caps (--clear to remove; no flags to view)
abm files   [path]                        # list files under the allowed roots (jailed)

# multi-VPS controller (other boxes reached over SSH tunnels)
abm node list                             # registered nodes + tunnel status
abm node add box2 ubuntu@1.2.3.4[:port]  # register + bring up the tunnel + test [--key F --basic-user U --basic-pass P]
abm node test box2                        # probe a node over its tunnel
abm node remove box2                      # drop a node (its bots keep running on that VPS)

# host / settings / auth / self-update
abm sysinfo
abm settings [--theme ember] [--accent "#ff7a45"] [--enable-system | --disable-system]
abm update                                # apt-get update && upgrade (system actions must be enabled)
abm reboot                                # reboot the host (system actions must be enabled)
abm selfupdate                            # update the manager in place (git pull + restart; bots untouched)
abm autoupdate on|off|status             # daily self-update systemd timer
abm setpassword                           # set the web UI login (prompts for user + password)
abm logout-all                            # invalidate active web sessions
```

## Web UI
```bash
abm serve --host 127.0.0.1 --port 8765
```
Browse to http://127.0.0.1:8765.
- Cards per instance: start / stop / restart, live console (tmux capture), JSON config editor (validates on save), delete, plus live CPU/RAM bars (warn-colored past thresholds, scaled to caps when limited). Bulk start/stop/restart-all.
- A sticky **host gauge strip** (CPU load / memory / disk vs capacity).
- **🚀 Deploy** — download a fork's launcher (AquariusProxy / ZenithProxy / a custom `owner/repo`) into a new dir and register it, with a live deploy log; optional memory/CPU caps.
- **📁 Files** — a jailed file manager (browse/create/edit/rename/delete) over the allowed roots.
- **+ New instance** — register an existing dir via a form (incl. optional resource caps).
- **⟲ Scan existing** — detect unmanaged tmux sessions and adopt them.
- **🖥 Boxes** — connect and drive other VPSes from this one (see _Multi-VPS controller_ below).
- **⚙ Settings** — Appearance (themes, custom background, density), System (host dashboard, manager self-update, backup/restore, reboot/update).
- **🌐 Proxies** — quick host/port **and user/password** editor for instances using `client.connection.proxy`. Each row has host, port, optional username + password (blank password keeps the existing one; clearing the username drops the saved credentials), with **Save** and **⟳** (save **& restart**). A **Bulk assign / rotate** panel lets you paste a list of `host:port` proxies and apply them across selected instances — **round-robin** (cycle the list across targets) or **same to all** — with an optional restart-after.
- Per-instance drawer (⋯): **Console** tab has a live command bar (sends to tmux stdin) plus **quick-command preset buttons**; **Config** tab is a structured AquariusProxy/ZenithProxy config/module editor (toggles, numbers, lists, filter) with a Raw JSON fallback and **Save** / **Save & Restart**; **Limits** tab sets the memory/CPU caps. The ★ on each card toggles autostart.

## Multi-VPS controller (Boxes, Fleet, DigitalOcean)
One manager can act as a **controller** for your other boxes. Each other box runs the same manager (in lightweight **node mode**), and the controller reaches it over a **controller-managed SSH tunnel** — nodes stay bound to `127.0.0.1` and are never exposed to the internet. Open it with the header's **🖥 Boxes** button.

![Boxes panel — Fleet view + connect a box](https://raw.githubusercontent.com/wiki/aquariusnetwork9/Aquarius-Bot-Manager/boxes.png)

*The **🖥 Boxes** panel: this box + every connected node at a glance (bots running, host load/mem), fleet-wide Start / Restart / Stop all and Update all nodes, and the connect-a-box form. The sticky bar at the top is the in-page box switcher.*

- **Connect a box (SSH):** paste `user@host` (add `:port` if SSH isn't on 22). The controller opens a self-healing `ssh -N -L` tunnel to that box and registers it. Optional advanced fields: SSH key path, the node's manager port, and the node's web login (only if it enforces one — the controller presents it automatically when proxying).
- **In-page box switcher:** a sticky bar at the top lets you switch which box you're viewing. Selecting a box **reverse-proxies its full native dashboard into the same tab** — console, config, files, proxies, everything — no extra tunnel or browser tab.
- **Fleet view:** the Boxes panel shows every box at a glance (reachable, bots running, host load/mem) with one-click **Start / Restart / Stop all** across the fleet and **Update all nodes** (pushes `selfupdate` to each).
- **DigitalOcean:** save a DO API token, then **connect existing droplets** or **spin up a new 1GB node-mode droplet** (region/size picker, default `s-1vcpu-1gb`) — the controller auto-uploads its own SSH key, installs the manager via cloud-init, and registers the new box. DO-backed boxes also get a **Destroy** button (deletes the droplet, with a typed confirmation).
- **All-boxes launcher:** under **🔗 Connect**, download a one-double-click script that opens an SSH tunnel to every box (controller + nodes) on distinct local ports — a direct-access fallback for when the controller itself is down.

![Boxes → DigitalOcean — connect existing droplets or provision a new node](https://raw.githubusercontent.com/wiki/aquariusnetwork9/Aquarius-Bot-Manager/digitalocean.png)

*Switch the connect form to **DigitalOcean** to provision a fresh 1GB node-mode droplet (region + size picker) or connect a droplet you already run — the controller handles the SSH key, cloud-init install, and registration.*

Node registry lives in `nodes.json` (gitignored; SSH targets + the DO token + any node web creds, stored base64-obfuscated). Manage nodes headless with `abm node list|add|remove|test`, e.g. `abm node add box2 ubuntu@1.2.3.4`. Install a box as a node with `curl -fsSL …/install.sh | ABM_ACCESS=node bash`.

### Console presets
The buttons above the console command bar are editable in **Settings → Console** (label + the command it types). They're stored under `settings.console_presets` in `instances.json`. Defaults are `Reconnect` (`connect`), `Disconnect` (`disconnect`), and `Status` (`info`) — adjust them to your proxy's commands.

### Bulk / round-robin proxies
If you rotate through a pool of proxy IPs, paste them (one `host:port` per line) into the Proxies → Bulk panel, pick the targets, and choose **round-robin** to spread them out or **same to all** to point everyone at one. The same is available headless: `abm proxybulk --list ... --mode roundrobin --restart`. Each write goes to that instance's `config.json` and applies on (optional) restart. Entries may include credentials (`host:port:user:pass` or `user:pass@host:port`); they're written to `proxy.user`/`proxy.password`.

### Import from Webshare
If your proxies come from a [Webshare](https://www.webshare.io/) subscription, **🌐 Proxies → Import from Webshare** pulls the live list straight from their API and round-robins it across your bots — no copy-paste. Paste your **API token** (from the Webshare dashboard → API), pick the auth model, and hit **Count** to preview or **Import & assign**:

- **User / pass** — writes each proxy's `host:port` *and* its username/password into `client.connection.proxy`. Works anywhere, no whitelisting; the password lands in `config.json`.
- **IP-authorized** — writes `host:port` only and wipes any stale creds. First add this VPS's public IP to Webshare → **IP Authorization**, then the proxies need no credentials.

Optional **Countries** filter (e.g. `US,CA`), **Valid only** (default), **Save token** (kept under `settings.webshare` in `instances.json`, base64-obfuscated at rest — not real encryption — and reused so you don't paste it again), and **Restart after**. The target set is shared with the Bulk panel. Each imported proxy is enabled and set to type `HTTP`. Headless equivalent:
```bash
abm webshare count  --token <KEY>                          # how many would import
abm webshare import --token <KEY> --auth userpass --save-token --restart
abm webshare import --auth ip --countries US,CA            # reuses the saved token
```
The token can also come from the `WEBSHARE_TOKEN` env var. A 🔒 on a proxy row means credentials are set (hover for the username); the password is never shown in the UI or API.

## Detecting proxies you already run (scan / adopt)
Your manually-started sessions have arbitrary names, so scan inspects every live tmux session and flags likely Aquarius/Zenith proxy sessions using three signals (any one is enough):
- `aquarius` or `zenith` in the session's working path or process args
- a `java` process in the pane
- a launcher (`launch.sh`/`start.sh`/`run.sh`) or `.jar` in the session's directory

**Adopt = bind, don't restart.** Adopting writes an instance with a `session` field pinned to the live session, so it shows `running` immediately and stop/restart/logs act on it. `dir` and `launch_cmd` are auto-filled from the live session. Already-managed sessions are excluded from scans.

## Monitoring & alerts
The dashboard polls a host gauge strip (CPU load vs cores, memory, disk) and per-instance CPU% / RAM (read from `/proc` of each tmux pane's process tree). Bars turn warn/crit colored once usage crosses the thresholds in **Settings → Monitoring** (`settings.thresholds`, defaults 85/85/90 %).

## Resource limits (cgroups)
Give an instance a memory and/or CPU cap (New-instance form, the drawer's **Limits** tab, or `abm limits`). When set, the instance launches inside a transient systemd **user scope** (`systemd-run --user --scope` with `MemoryMax`/`MemoryHigh`/`CPUQuota`) so a runaway bot can't OOM the box or hog the cores. Takes effect on next start/restart. This needs systemd user lingering — the installer runs `loginctl enable-linger`; if a host can't enforce, caps are saved but the UI flags them as not enforced and the proxy still starts normally.

## File manager
**📁 Files** browses, creates, edits, renames and deletes files/folders — **jailed** to an allowlist of roots (`settings.file_roots`, defaulting to the proxy base dir + the manager dir). Paths are realpath-checked against the roots, so `..` and symlink escapes are blocked; root dirs can't be deleted; only UTF-8 text files under ~1 MB open in the editor.

## Deploy proxies
**🚀 Deploy** stands up a new proxy from scratch: pick **AquariusProxy**, **ZenithProxy**, or a **custom** GitHub `owner/repo` (any fork that publishes a `launcher-v3` release), give it a name and dir, and the manager downloads that fork's platform launcher, unzips it, and registers the instance. The launcher self-bootstraps Java and the proxy jar on first start — so deploying needs nothing pre-installed. Same headless: `abm deploy <name> --source aquarius|zenith|custom [--repo owner/repo]`.

## Settings — appearance
Theme presets: `midnight` (default), `ember`, `ice`, `amethyst`, `paper`, `obsidian`, `forest`, `rose`, `ocean`, `gold`, `sand`. Accent colour via a picker, hex field, or one-click swatches. A **custom background image** (any http/https URL) with a readability **dim** slider, and a **density** control (comfortable / compact / spacious). Everything previews live and persists in `instances.json`.

![Settings → Appearance](https://raw.githubusercontent.com/wiki/aquariusnetwork9/Aquarius-Bot-Manager/appearance.png)

Set a background image URL and the whole dashboard takes it on (here the `ice` theme over a wallpaper, dim at 50%):

![Dashboard with a custom background image](https://raw.githubusercontent.com/wiki/aquariusnetwork9/Aquarius-Bot-Manager/custom-background.png)

## Settings — backup & restore
**Settings → System → Backup & restore.** Download a portable bundle of this box's configs (`instances.json` + the node registry) — the file contains secrets, so keep it safe. Restoring overwrites the current configs (a timestamped `.pre-restore-*.bak` copy is saved first) and may require logging in again. When you're viewing a node via the box switcher, backup/restore targets that node.

## Settings — manager self-update
**Settings → System → Manager updates.** Update the manager in place (`git pull --ff-only` + restart the web UI — bots are untouched thanks to `KillMode=process`) with the **🔄 Update manager now** button, which shows an **"update available"** badge when the box is behind. Toggle **Auto-update daily** to install a systemd timer. Headless: `abm selfupdate`, `abm autoupdate on|off`.

![Settings → System](https://raw.githubusercontent.com/wiki/aquariusnetwork9/Aquarius-Bot-Manager/system.png)

*The System tab: a read-only host dashboard, the self-update button with its "update available" badge, config backup/restore, and the (off-by-default) system-action toggle for reboot / OS update.*

## Settings — system actions (reboot / OS update)
**Off by default.** Enable in the Settings → System tab, or `abm settings --enable-system`.
- **Update OS** — runs `apt-get update && apt-get -y upgrade` non-interactively; output streams to the panel. Won't auto-reboot even if a kernel update lands.
- **Reboot VPS** — runs `sudo reboot` after a ~2s delay so the HTTP response returns first. Drops all proxies and takes the manager down with the host.
- Read-only host dashboard: OS, CPU cores, load avg, memory, disk, uptime.

### Required: passwordless sudo (do NOT store your password)
The manager runs as a normal process, so these two commands need sudo. Scope it tightly — never put your password in a config or the web UI:
```bash
sudo visudo -f /etc/sudoers.d/aquarius-bot-manager
# add (replace ubuntu with the account running the manager):
ubuntu ALL=(root) NOPASSWD: /usr/sbin/reboot, /usr/bin/apt-get
```
Confirm paths with `which reboot apt-get`. The manager calls `sudo -n` (non-interactive); if sudo isn't configured it fails cleanly rather than hanging.

## Auto-restart proxies after reboot
tmux sessions don't survive a reboot, so "auto-restart" means **re-launch on boot**.

> If you used the **curl installer**, this is already done for you — both the manager web UI *and* the boot unit are `systemctl enable`d, and deployed bots default to autostart. A VPS reboot brings the dashboard and your starred bots back with no SSH. The steps below are only for manual installs or to toggle which bots come back.

1. Mark which instances should come back: the ★ star on each web card, `abm autostart <name> --on`, or `--autostart` on `abm add` / `abm adopt`. (Bots deployed via 🚀 Deploy are starred by default.)
2. Install the boot unit so they launch when the host comes up:
   ```bash
   sudo cp aquarius-bot-manager-boot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable aquarius-bot-manager-boot     # runs `manager.py boot` once at boot
   ```
`abm boot` is idempotent — it starts only autostart instances and skips any already running. Adopted instances relaunch under their pinned session name using their saved `dir` + `launch_cmd`. This pairs with the Reboot button: reboot the box, and the starred proxies return on their own.

## Run on boot (systemd)
1. Edit `aquarius-bot-manager.service`: set `User=` and paths (and auth env if exposing it).
2. Install + enable:
   ```bash
   sudo cp aquarius-bot-manager.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now aquarius-bot-manager
   ```

## Remote access & login
The web UI has a real login page with server-side sessions (HttpOnly cookie, 7-day expiry), a salted PBKDF2 password hash stored in `instances.json` (never plaintext), and login rate-limiting (5 tries / 5 min per IP).

**First run = a browser setup wizard.** Before any login exists, the whole UI is replaced by a one-screen "create your admin login" form (the rest of the app is locked until you do). Submitting it sets the password and signs you in — no CLI step. You can still skip it from that screen to run open on localhost (e.g. behind an SSH tunnel), and you can change credentials later:
```bash
abm setpassword       # set/replace the login from the CLI (also works headless)
abm logout-all        # invalidate active sessions
```

### Reconnecting (close browser / restart PC / drop connection)
Your bots and the manager both run on the VPS, independent of your browser — closing the tab, restarting your PC, or losing your connection does **not** stop them. Getting back in:

- **HTTPS mode:** just open your bookmarked `https://<vps>` — you're back (it only asks you to log in again once the session has expired). One step.
- **Tunnel mode:** the SSH tunnel is a process on *your* machine, so it ends when you restart/disconnect. Re-open the tunnel, then the bookmark. To make that one double-click, open the dashboard's **🔗 Connect** panel and download a **reconnect shortcut** (`.bat` / `.command` / `.sh`) — it opens the tunnel *and* the dashboard for you. The panel also shows your bookmark URL and the exact tunnel command with copy buttons.

Sessions last 7 days, so you usually won't even re-login between reconnects. Deployed bots default to **autostart**, so if the *VPS* itself reboots, the boot unit relaunches them automatically.

### Recommended: SSH tunnel (no new exposure)
Keep the manager on `127.0.0.1` (default) and forward the port from your local machine:
```bash
ssh -L 8765:127.0.0.1:8765 ubuntu@YOUR_VPS_IP
```
Leave that open, then browse `http://localhost:8765` on your own computer (you browse localhost — SSH forwards it to the VPS). Traffic is encrypted by SSH; nothing new is exposed.

### Alternative: expose directly (needs HTTPS)
Plain HTTP sends the password in cleartext. If you must expose it, put a TLS reverse proxy (e.g. Caddy + a domain → automatic Let's Encrypt) in front, keep the manager on `127.0.0.1`, and set a password. Don't bind the manager to `0.0.0.0` over plain HTTP.

## Security
- The UI can reboot the box, edit configs, and send console commands — treat access as full control.
- Default bind is `127.0.0.1`; the server warns at startup if bound to a non-local address with no password.
- System actions (reboot/update) are separately gated by the Settings toggle (off by default).
- Legacy `ABM_USER`/`ABM_PASS` (and `ZP_USER`/`ZP_PASS`) env vars still work as a fallback if no password is set.

## How it works / notes
- One tmux session per instance, named `abm_<name>` (hyphens preserved; `.`/`:` become `_`). Adopted instances keep their original session name. Attach manually: `tmux attach -t abm_bot1`.
- `remain-on-exit on` is set, so a crashed instance keeps its session + last output; status shows `crashed` and the console still shows why.
- Stop = send `stop_keys` (Ctrl-C default → proxy shutdown hook), wait up to `stop_timeout`, then kill the session.
- Deleting an instance only edits `instances.json` — it never touches files on disk.
- Overrides: config path via `--config PATH` or `ABM_CONFIG`; session prefix via `ABM_PREFIX`. (Legacy `ZP_CONFIG` / `ZP_PREFIX` are still honored.)
