# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Aquarius Bot Manager (ABM) is a control plane for a fleet of AquariusProxy /
ZenithProxy Minecraft bot instances: a CLI plus a web dashboard (served from
the same process) that can start/stop/monitor/configure bots, deploy new
ones, and drive a multi-VPS fleet over SSH tunnels.

**Branch `main`**: Linux-only, pure Python 3.8+ stdlib — no pip dependencies,
no build step. Process supervision is tmux; monitoring is `/proc`; resource
limits are cgroups via `systemd-run`; system actions go through
passwordless `sudo`. Requires `python3` + `tmux` and nothing else.

**Branch `windows-native`**: adds a native Windows port on top of the same
`main` codebase, with its own additional dependencies (see below). Check
`git branch --show-current` before assuming which world you're in — the
two branches genuinely differ in dependency footprint and runtime
behavior, not just a few conditionals.

## Commands

```bash
python3 manager.py serve --host 127.0.0.1 --port 8765   # run the web UI/API
python3 -m py_compile manager.py                        # syntax check (the only "build" step)
python3 manager.py --help                                # full CLI subcommand list
python3 manager.py list / status / start <name|all> / stop / restart / logs <name>
```

There is **no test suite** in this repo (no `tests/` directory, no
`test_*.py` files) — verification is manual: run the CLI/server against a
real or fake bot process and check the result, or use `py_compile` as a
syntax gate. Don't invent a pytest setup; match the existing manual-check
pattern when verifying a change.

**Windows-only, on `windows-native`:**
```powershell
python install_windows_deps.py          # psutil, PySide6 (two-step, see below), pystray
python gui_win.py                        # native control panel (standalone)
python tray_win.py                       # tray icon (standalone)
.\install_windows_service.ps1 -BootMode Always   # or WaitForLogin — registers the NSSM service
.\build_windows_exes.ps1                 # builds AquariusManager/ControlPanel/Tray.exe via PyInstaller
```

## Architecture

### `manager.py` is the single source of truth

Everything — CLI parsing, the `ThreadingHTTPServer`-based web API, the
dashboard's HTML/JS/CSS (embedded as a Python string, not a separate
frontend build), order/instance logic, settings — lives in this one
~16,000-line file. There is no separate backend/frontend split and no
template engine; the dashboard is generated server-side as one big string
response. When changing dashboard behavior, the HTML/JS you're editing is
inline inside `manager.py`, not in a `templates/` or `static/` directory.

`schema.py` holds a curated AquariusProxy/ZenithProxy config schema (for
the structured module editor) — it's about the *bots'* own config.json
shape, unrelated to `instances.json` (the fleet roster) or anything
Windows-specific.

`fleet.py` is a separate, explicitly experimental multi-droplet DigitalOcean
provisioner (`abmfleet` CLI), independent of the main instance-management
flow.

### Config files (all gitignored; `*.example.json` versions are checked in)

- `instances.json` — the roster: name, dir, launch_cmd, limits, autostart,
  per-instance `env` (Windows), `platform` (reserved, not yet read anywhere
  — see `platform_win.py`'s dispatch note below), theme/auth settings.
- `fleet.json` — DigitalOcean token + fleet.py state.
- `nodes.json` — SSH targets + credentials for the multi-VPS controller mode.

### The Linux/Windows split (only relevant on `windows-native`)

A single `IS_WINDOWS = platform.system() == "Windows"` flag near the top of
`manager.py` dispatches between the existing Linux implementation
(unchanged, suffixed `_linux`) and `platform_win.py`'s Windows
implementation, for exactly these functions: `start`, `stop`,
`instance_status`, `instance_stats`, `logs`, `send_command`, rename/adopt
plumbing, `_launch_command`'s limit-wrapping, `_sysinfo`, and
`run_system_action`. Everything else in `manager.py` — the HTTP server, web
UI, CLI, order/routing logic, limit config storage, the SSH/remote-node
path — is OS-agnostic and shared by both platforms unchanged.

**Read `platform_win.py`'s own module docstring before touching it** — it
explains a non-obvious design: each supervised bot on Windows runs through
a *detached helper subprocess* (`platform_win.py` re-invoked with a
`_helper` argument), not a direct child of the CLI process, because a
Windows CLI invocation is short-lived and tmux-equivalent cross-process
session semantics don't exist natively. State (PID, log tail, a control
port) is persisted to a `.abm-state/` directory next to `instances.json` so
a *later*, unrelated CLI invocation can still reach a bot a different
process started. A `_StartLock` file-lock guards the whole
check-then-launch sequence against two near-simultaneous `start` calls
racing each other into two independent helper processes for the same bot
name — this was a real bug, not a defensive guess; don't remove it without
rereading why it's there.

`gui_win.py` (native PySide6 control panel) and `tray_win.py` (tray icon)
are separate processes from the manager itself — they're pure HTTP clients
of the manager's own local API (`client_win.py` is the shared client),
using the manager's existing session-cookie login, never embedding or
reusing the web dashboard's HTML. Every network call in `gui_win.py` runs
off the Qt main thread via `run_async`/`_Worker` — `stop`/`restart` can
legitimately block for the instance's full `stop_timeout` (up to ~30s), and
an earlier version without this froze the whole window for that long.

### Windows-specific dependency footprint

`main` is stdlib-only; `windows-native` adds `psutil` (process
monitoring/adoption) and, only for the GUI/tray, `PySide6-Essentials` +
`PySide6-Addons` + `pystray`. **Do not `pip install PySide6` plain** and
**do not build it with PyInstaller the obvious way** — the `PySide6`
metapackage hard-depends on `PySide6_WebEngine` (an embedded ~130MB
Chromium), which this project deliberately never uses (native widgets
only, confirmed requirement). `install_windows_deps.py` and
`requirements.txt` document the correct two-step install
(`PySide6-Essentials`/`-Addons` first, then `PySide6` itself with
`--no-deps`); `docs/windows-exe-build.md` documents the equivalent
PyInstaller-build-time issue (a stale `VCRUNTIME140.dll` PyInstaller pulls
into the bundle crashes `Qt6Core.dll` a few seconds after launch) and the
separate fix needed so a frozen `AquariusManager.exe` can still spawn its
own helper subprocess (no real `python.exe` inside a frozen exe — a
minimal bundled Python runtime is shipped alongside it in a `pyrt/`
folder).

### Multi-VPS / remote-node mode

Each "node" registered in `nodes.json` is a **fully separate, independent
`manager.py` instance** running on its own remote box, reached over an SSH
tunnel + its own HTTP API (`TunnelManager` in `manager.py`) — not a shared
config. A Windows box's own `instances.json` never mixes in remote-Linux
bots directly; it only ever reaches them through this tunnel/API layer.
Don't assume a single `instances.json` could describe both local-Windows
and remote-Linux bots in one roster — that's not how this works.
