# Aquarius Bot Manager

Control many **AquariusProxy** and **ZenithProxy** bot instances — each in its own tmux session — from a CLI and a web UI. Pure Python stdlib + tmux. No pip installs, no Docker.

AquariusProxy is a ZenithProxy fork, so they share the same launch model and config structure; this manager drives either (or a mix) on the same host.

## Files
- `manager.py` — the program (CLI + web server, single source of truth)
- `schema.py` — curated AquariusProxy/ZenithProxy config schema for the structured editor
- `abm` — short CLI wrapper (`abm restart bot1`)
- `aquarius-bot-manager.service` — systemd unit for the web UI
- `aquarius-bot-manager-boot.service` — systemd oneshot unit that starts autostart instances on boot
- `instances.example.json` — config schema example

## Requirements
- `python3` (3.8+) and `tmux`: `sudo apt install tmux`

## Install
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

# host / settings / auth
abm sysinfo
abm settings [--theme ember] [--accent "#ff7a45"] [--enable-system | --disable-system]
abm update                                # apt-get update && upgrade (system actions must be enabled)
abm reboot                                # reboot the host (system actions must be enabled)
abm setpassword                           # set the web UI login (prompts for user + password)
abm logout-all                            # invalidate active web sessions
```

## Web UI
```bash
abm serve --host 127.0.0.1 --port 8765
```
Browse to http://127.0.0.1:8765.
- Cards per instance: start / stop / restart, live console (tmux capture), JSON config editor (validates on save), delete. Bulk start/stop/restart-all.
- **+ New instance** — add via a form.
- **⟲ Scan existing** — detect unmanaged tmux sessions and adopt them.
- **⚙ Settings** — Appearance (theme presets + accent) and System (host dashboard + reboot/update).
- **🌐 Proxies** — quick host/port editor for instances using `client.connection.proxy`. Each row has **Save** and **⟳** (save **& restart**). A **Bulk assign / rotate** panel lets you paste a list of `host:port` proxies and apply them across selected instances — **round-robin** (cycle the list across targets) or **same to all** — with an optional restart-after.
- Per-instance drawer (⋯): **Console** tab has a live command bar (sends to tmux stdin) plus **quick-command preset buttons**; **Config** tab is a structured AquariusProxy/ZenithProxy config/module editor (toggles, numbers, lists, filter) with a Raw JSON fallback and **Save** / **Save & Restart**. The ★ on each card toggles autostart.

### Console presets
The buttons above the console command bar are editable in **Settings → Console** (label + the command it types). They're stored under `settings.console_presets` in `instances.json`. Defaults are `Reconnect` (`connect`), `Disconnect` (`disconnect`), and `Status` (`info`) — adjust them to your proxy's commands.

### Bulk / round-robin proxies
If you rotate through a pool of proxy IPs, paste them (one `host:port` per line) into the Proxies → Bulk panel, pick the targets, and choose **round-robin** to spread them out or **same to all** to point everyone at one. The same is available headless: `abm proxybulk --list ... --mode roundrobin --restart`. Each write goes to that instance's `config.json` and applies on (optional) restart.

## Detecting proxies you already run (scan / adopt)
Your manually-started sessions have arbitrary names, so scan inspects every live tmux session and flags likely Aquarius/Zenith proxy sessions using three signals (any one is enough):
- `aquarius` or `zenith` in the session's working path or process args
- a `java` process in the pane
- a launcher (`launch.sh`/`start.sh`/`run.sh`) or `.jar` in the session's directory

**Adopt = bind, don't restart.** Adopting writes an instance with a `session` field pinned to the live session, so it shows `running` immediately and stop/restart/logs act on it. `dir` and `launch_cmd` are auto-filled from the live session. Already-managed sessions are excluded from scans.

## Settings — appearance
Theme presets: `midnight` (default), `ember`, `ice`, `amethyst`, `paper`. Optional accent override (any hex). Persisted in `instances.json`, applied on load.

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

1. Mark which instances should come back: the ★ star on each web card, `abm autostart <name> --on`, or `--autostart` on `abm add` / `abm adopt`.
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

```bash
abm setpassword       # prompts for username + password
abm logout-all        # invalidate active sessions
```
Until a password is set, the UI is open to anyone who can reach the port — so set one, or keep it on localhost (default).

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
