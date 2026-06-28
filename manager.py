#!/usr/bin/env python3
"""
Aquarius Bot Manager
--------------------
Single-file controller for many AquariusProxy / ZenithProxy bot instances
running in tmux on a headless Ubuntu server. Provides a CLI and a stdlib-only
web UI.

No third-party dependencies. Requires: python3, tmux.

Usage:
  python3 manager.py serve [--host H] [--port P]
  python3 manager.py list
  python3 manager.py status
  python3 manager.py start   <name|all>
  python3 manager.py stop    <name|all>
  python3 manager.py restart <name|all>
  python3 manager.py logs    <name> [--lines N]
  python3 manager.py discover <basedir>   # bootstrap instances.json

Config file (instances.json) is looked up next to this script, or via
$ABM_CONFIG, or --config PATH.
"""

import argparse
import base64
import getpass
import hashlib
import hmac
import html
import json
import os
import platform
import posixpath
import random
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

__version__ = "3.11.0"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = (os.environ.get("ABM_CONFIG") or os.environ.get("ZP_CONFIG")
                  or os.path.join(SCRIPT_DIR, "instances.json"))
SESSION_PREFIX = os.environ.get("ABM_PREFIX") or os.environ.get("ZP_PREFIX") or "abm_"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path):
    if not os.path.exists(path):
        die(f"config not found: {path}\nRun:  python3 manager.py discover <basedir>  to create one.")
    with open(path) as f:
        data = json.load(f)
    insts = data.get("instances", [])
    by_name = {}
    for i in insts:
        name = i["name"]
        i.setdefault("launch_cmd", "./launch.sh")
        i.setdefault("config_file", "config.json")
        # stop_keys: list of tmux send-keys args. Default: Ctrl-C.
        i.setdefault("stop_keys", ["C-c"])
        i.setdefault("stop_timeout", 15)
        i.setdefault("autostart", False)
        by_name[name] = i
    # settings block (theme + system-action gating)
    s = data.setdefault("settings", {})
    theme = s.setdefault("theme", {})
    theme.setdefault("preset", "midnight")
    theme.setdefault("accent", "")          # "" = use the preset's accent
    theme.setdefault("font", "aquarius")    # font pairing (see FONT_PRESETS)
    s.setdefault("system_actions_enabled", False)
    s.setdefault("shares", [])               # shareable-link guest grants
    s.setdefault("shares_epoch", 0)          # revoke-all generation for shares
    s.setdefault("users", [])                # named multi-user accounts (RBAC)
    s.setdefault("invites", [])              # pending invite links (preset role+scope)
    ps = s.setdefault("public_share", {})    # public-exposure provider for guest links (multi-provider menu)
    ps.setdefault("enabled", False)
    ps.setdefault("provider", "cloudflare-quick")
    ps.setdefault("providers", {})           # per-provider config (secrets b64-obfuscated)
    return {"raw": data, "instances": insts, "by_name": by_name, "path": path}


def save_config(cfg):
    with open(cfg["path"], "w") as f:
        json.dump(cfg["raw"], f, indent=2)


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------

def session_name(inst):
    # Adopted instances pin to an existing tmux session via "session".
    if inst.get("session"):
        return inst["session"]
    # tmux session names may contain hyphens; '.' and ':' are target separators.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", inst["name"])
    return SESSION_PREFIX + safe


def tmux(*args, check=False):
    try:
        p = subprocess.run(["tmux", *args], capture_output=True, text=True)
    except FileNotFoundError:
        # tmux not installed / not on PATH: degrade gracefully (no sessions) instead of
        # 500-ing every endpoint. check=True callers (start) still get a clear error.
        if check:
            raise RuntimeError("tmux not found on PATH (install it: sudo apt install tmux)")
        return subprocess.CompletedProcess(args, 1, "", "tmux not found")
    if check and p.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)} failed: {p.stderr.strip()}")
    return p


def session_exists(s):
    return tmux("has-session", "-t", s).returncode == 0


def pane_dead(s):
    p = tmux("list-panes", "-t", s, "-F", "#{pane_dead}")
    return p.returncode == 0 and "1" in p.stdout.split()


def instance_status(inst):
    s = session_name(inst)
    if not session_exists(s):
        return "stopped"
    return "crashed" if pane_dead(s) else "running"


# instance name -> epoch of the last manager-initiated (re)start. Powers the brief
# "restarting" connection state so it never sticks (cleared once the bot connects).
_RESTART_FLAG = {}


def start(inst):
    s = session_name(inst)
    if session_exists(s):
        if pane_dead(s):
            tmux("kill-session", "-t", s)
        else:
            return "already running"
    d = inst["dir"]
    if not os.path.isdir(d):
        return f"error: dir not found: {d}"
    outer, note = _launch_command(inst)
    tmux("new-session", "-d", "-s", s, "-c", d, "bash", "-lc", outer, check=True)
    # keep crash output visible after the process exits
    tmux("set-option", "-t", s, "remain-on-exit", "on")
    _RESTART_FLAG[inst.get("name")] = time.time()
    return "started" + note


def stop(inst):
    s = session_name(inst)
    if not session_exists(s):
        return "not running"
    if not pane_dead(s):
        for k in inst.get("stop_keys", ["C-c"]):
            tmux("send-keys", "-t", s, k)
        deadline = time.time() + int(inst.get("stop_timeout", 15))
        while time.time() < deadline:
            if not session_exists(s) or pane_dead(s):
                break
            time.sleep(0.4)
    if session_exists(s):
        tmux("kill-session", "-t", s)
    return "stopped"


def restart(inst):
    stop(inst)
    time.sleep(0.5)
    return start(inst)


def logs(inst, lines=300):
    s = session_name(inst)
    if not session_exists(s):
        return "(not running)"
    p = tmux("capture-pane", "-t", s, "-p", "-J", "-S", f"-{int(lines)}")
    return p.stdout if p.returncode == 0 else f"(error reading logs: {p.stderr})"


# ---------------------------------------------------------------------------
# per-instance resource stats  (Linux /proc; degrades to None elsewhere)
# ---------------------------------------------------------------------------

try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK")
except (ValueError, OSError, AttributeError):
    _CLK_TCK = 100
try:
    _PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except (ValueError, OSError, AttributeError):
    _PAGE_SIZE = 4096

_CPU_SAMPLES = {}   # session -> (total_jiffies, ts) for CPU% deltas between polls


def _pane_pid(session):
    p = tmux("display-message", "-p", "-t", session, "#{pane_pid}")
    if p.returncode != 0:
        return None
    try:
        return int(p.stdout.strip())
    except ValueError:
        return None


def _read_proc_stat(pid):
    """(/proc/<pid>/stat) -> (ppid, utime+stime jiffies, rss_pages) or None."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except OSError:
        return None
    rp = data.rfind(")")           # comm (field 2) is parenthesized and may contain spaces
    if rp < 0:
        return None
    after = data[rp + 2:].split()  # after[0] == field 3 (state)
    try:
        return int(after[1]), int(after[11]) + int(after[12]), int(after[21])
    except (IndexError, ValueError):
        return None


def _proc_tree_stats(root_pid):
    """Sum CPU jiffies / RSS bytes / process count for root_pid and its descendants."""
    entries = {}
    for d in os.listdir("/proc"):          # raises OSError on non-Linux -> caller handles
        if d.isdigit():
            r = _read_proc_stat(int(d))
            if r:
                entries[int(d)] = r
    children = {}
    for pid, (ppid, _, _) in entries.items():
        children.setdefault(ppid, []).append(pid)
    total_j = total_rss = 0
    seen, stack = set(), [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen or pid not in entries:
            continue
        seen.add(pid)
        _, j, rss = entries[pid]
        total_j += j
        total_rss += rss
        stack.extend(children.get(pid, []))
    return total_j, total_rss * _PAGE_SIZE, len(seen)


def instance_stats(inst):
    """Live CPU%/RSS for a running instance, or None. CPU% is normalized to one
    core (100 = a full core; can exceed 100 up to cores*100), sampled between polls."""
    s = session_name(inst)
    if not session_exists(s) or pane_dead(s):
        _CPU_SAMPLES.pop(s, None)
        return None
    pid = _pane_pid(s)
    if not pid:
        return None
    try:
        total_j, rss_bytes, nproc = _proc_tree_stats(pid)
    except OSError:
        return None                         # no /proc (e.g. Windows dev box)
    now = time.time()
    prev = _CPU_SAMPLES.get(s)
    _CPU_SAMPLES[s] = (total_j, now)
    cpu_pct = None
    if prev:
        dj, dt = total_j - prev[0], now - prev[1]
        if dt > 0 and dj >= 0:
            cpu_pct = round(100.0 * dj / (dt * _CLK_TCK), 1)
    return {"pid": pid, "cpu_pct": cpu_pct, "rss": rss_bytes, "procs": nproc}


# tiny mtime cache so we don't re-parse launch_config.json on every poll
_PROXY_CACHE = {}
_VIEWER_PORT_CACHE = {}


def viewer_port_for(inst):
    """Which loopback port a bot's diagnostic viewer feed listens on (server.viewer.port).
    Resolution order: an explicit `viewer_port` in instances.json (our own config) wins;
    otherwise auto-discover it from the bot's own config.json (`server.viewer.port`), so
    several viewer-enabled bots on one box each get their right port with zero manual setup;
    otherwise fall back to the 2998 default. Only the integer port is read — never tokens —
    and the parse is mtime-cached so the hot polling path doesn't re-read the file."""
    explicit = inst.get("viewer_port")
    if explicit:
        try:
            p = int(explicit)
            if 1024 <= p <= 65535:
                return p
        except (ValueError, TypeError):
            pass
    directory = inst.get("dir") or ""
    path = os.path.join(directory, inst.get("config_file") or "config.json")
    # ZenithProxy bots serve the viewer feed via the zenith-abm-bridge plugin, which keeps its
    # port in plugins/config/abm-bridge.json — check that too so bridge bots auto-detect (an
    # AquariusProxy bot has the native server.viewer.port instead). Both default to 2998.
    bridge = os.path.join(directory, "plugins", "config", "abm-bridge.json")
    try:
        cfg_m = os.path.getmtime(path)
    except OSError:
        cfg_m = None
    try:
        br_m = os.path.getmtime(bridge)
    except OSError:
        br_m = None
    if cfg_m is None and br_m is None:
        return 2998
    cached = _VIEWER_PORT_CACHE.get(path)
    if cached and cached[0] == (cfg_m, br_m):
        return cached[1]
    port = 2998
    if cfg_m is not None:                       # AquariusProxy native: server.viewer.port
        try:
            with open(path) as f:
                data = json.load(f)
            sv = (data.get("server") or {}).get("viewer")
            if isinstance(sv, dict):
                p = int(sv.get("port", 2998))
                if 1024 <= p <= 65535:
                    port = p
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    if port == 2998 and br_m is not None:       # ZenithProxy bridge plugin: abm-bridge.json -> port
        try:
            with open(bridge) as f:
                bdata = json.load(f)
            p = int(bdata.get("port", 2998))
            if 1024 <= p <= 65535:
                port = p
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    _VIEWER_PORT_CACHE[path] = ((cfg_m, br_m), port)
    return port


def proxy_info(directory):
    """Which proxy fork + version a bot runs, read from its launcher's launch_config.json.
    Returns {"fork", "version", "version_full"} or None when it can't be determined
    (e.g. a hand-rolled launch with no launch_config.json). Cached by file mtime."""
    path = os.path.join(directory or "", "launch_config.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _PROXY_CACHE.pop(path, None)
        return None
    cached = _PROXY_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path) as f:
            lc = json.load(f)
    except (OSError, ValueError):
        return None
    repo = (lc.get("repo_name") or "").strip()
    owner = (lc.get("repo_owner") or "").strip()
    if repo:
        fork = repo
    elif owner:
        fork = {"aquariusnetwork9": "AquariusProxy", "rfresh2": "ZenithProxy"}.get(owner, owner)
    else:
        fork = "ZenithProxy"          # rfresh's stock launcher layout
    # e.g. "3.5.2+java.1.21.4" -> version "3.5.2", platform "Java" (drop the MC version)
    ver = (lc.get("local_version") or lc.get("version") or "").strip()
    core, _, meta = ver.partition("+")
    platform = ""
    if meta:
        first = meta.split(".", 1)[0].lower()
        if first in ("java", "linux"):
            platform = first.capitalize()
    info = {"fork": fork, "version": core, "platform": platform, "version_full": ver}
    _PROXY_CACHE[path] = (mtime, info)
    return info


_QUEUE_RE = re.compile(r"(?:queue\s*position|position\s+in\s+queue|in\s+queue)\D*?(\d+)", re.I)
# leading "[YYYY/MM/DD HH:MM:SS]" stamp ZenithProxy/AquariusProxy prefix every log line with
_TS_RE = re.compile(r"^\[(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})\]")


def _log_line_age(line):
    """Seconds since a log line's leading timestamp (box-local clock, same as ours),
    or None if it has no parseable stamp."""
    m = _TS_RE.match(line.lstrip())
    if not m:
        return None
    try:
        ts = time.mktime((int(m.group(1)), int(m.group(2)), int(m.group(3)),
                          int(m.group(4)), int(m.group(5)), int(m.group(6)), 0, 0, -1))
        return time.time() - ts
    except (ValueError, OverflowError):
        return None


def _log_connection(inst):
    """Scan a bot's console tail for the most recent connection event and classify it:
    online | in-queue (+position) | offline | updating. Calibrated to ZenithProxy /
    AquariusProxy log tags: MC connection lives in [Client]; the Minecraft world chat
    in [Chat]; queue in 2b2t's "Position in queue: N" + the [QueueWarning] module.
    [Discord] lines are IGNORED — their "Session disconnected/resumed" is the Discord
    bot link, NOT the server connection (matching it made every bot read offline).
    Never returns 'restarting' — a lingering boot banner means 'booted, not connected'
    = offline (that bug stuck bots on 'restarting' forever)."""
    try:
        tail = logs(inst, lines=200)
    except Exception:
        return {"state": "online", "queue": None}
    for line in reversed([l for l in tail.splitlines() if l.strip()][-200:]):
        ll = line.lower()
        if "[discord]" in ll:
            continue                                       # Discord link ≠ MC connection
        if ("downloading version" in ll or "removing existing file" in ll
                or "fetching latest" in ll):
            return {"state": "updating", "queue": None}
        m = _QUEUE_RE.search(line)
        if m:
            pos = int(m.group(1))
            return {"state": "in-queue", "queue": pos} if pos > 0 else {"state": "online", "queue": None}
        if "already queued" in ll:
            return {"state": "in-queue", "queue": None}
        if "[client]" in ll and ("disconnect" in ll or "timed out" in ll
                                 or "connection closed" in ll or "lost connection" in ll):
            return {"state": "offline", "queue": None}
        if "[client]" in ll and "connected to" in ll:
            return {"state": "online", "queue": None}
        if "[chat]" in ll:                                 # seeing world chat = in-game
            return {"state": "online", "queue": None}
        if ("proxy started" in ll or "command to log in" in ll or "booting" in ll
                or "loading modules" in ll or "starting aquariusproxy" in ll
                or "starting zenithproxy" in ll):
            # a FRESH boot banner means it's coming up (restarting); an old one with
            # nothing connection-y since means it booted and never connected (offline)
            age = _log_line_age(line)
            if age is not None and age < 30:
                return {"state": "restarting", "queue": None}
            return {"state": "offline", "queue": None}
    return {"state": "online", "queue": None}


def connection_state(inst, status=None):
    """In-game connection state of a bot: offline | in-queue (+position) | online |
    restarting | updating. Distinct from the OS-process status the badge shows.
    'restarting' is a brief, manager-tracked transient (set when we (re)start a bot,
    cleared the moment it connects or the window lapses) so it never sticks."""
    if status is None:
        status = instance_status(inst)
    name = inst.get("name")
    if status != "running":
        _RESTART_FLAG.pop(name, None)
        return {"state": "offline", "queue": None}
    st = _log_connection(inst)
    if st["state"] in ("online", "in-queue"):
        _RESTART_FLAG.pop(name, None)          # it's up — end any restart window
        return st
    ts = _RESTART_FLAG.get(name)
    if ts is not None and time.time() - ts < 30:
        return {"state": "restarting", "queue": None}
    _RESTART_FLAG.pop(name, None)
    return st                                  # offline or updating


def send_command(inst, command):
    """Type a command into the instance's live console (tmux pane stdin) and press Enter.
    Returns a status string. Raises ValueError if not running."""
    s = session_name(inst)
    if not session_exists(s):
        raise ValueError("instance is not running")
    if pane_dead(s):
        raise ValueError("instance has crashed; restart it before sending commands")
    command = (command or "").rstrip("\n")
    if command == "":
        raise ValueError("empty command")
    # -l sends the text literally (so 'C-c', spaces, etc. aren't read as key names);
    # '--' stops option parsing so commands starting with '-' work; then Enter submits.
    tmux("send-keys", "-t", s, "-l", "--", command)
    tmux("send-keys", "-t", s, "Enter")
    return "sent"


def read_instance_config(inst):
    path = os.path.join(inst["dir"], inst["config_file"])
    if not os.path.exists(path):
        return None, path
    with open(path) as f:
        return f.read(), path


def write_instance_config(inst, text):
    path = os.path.join(inst["dir"], inst["config_file"])
    # validate JSON only if the target is a .json file
    if path.endswith(".json"):
        json.loads(text)
    with open(path, "w") as f:
        f.write(text)
    return path


# ---------------------------------------------------------------------------
# proxy quick-edit  (find host/port fields for IP-proxy instances)
# ---------------------------------------------------------------------------

_HOST_KEYS = ("host", "address", "ip", "server", "hostname")
_PORT_KEYS = ("port",)
_USER_KEYS = ("user", "username", "login")
_PASS_KEYS = ("password", "pass", "pwd", "secret")


def _find_proxy_paths(obj, base=None):
    """Locate proxy host/port (and optional enabled/type) paths in a config object.
    Returns dict of {host:[path], port:[path], enabled?:[path], type?:[path]} or None."""
    base = base or []
    if not isinstance(obj, dict):
        return None
    # 1) a nested object under a key containing 'proxy'
    for k, v in obj.items():
        if "proxy" in k.lower() and isinstance(v, dict):
            host = next((kk for kk in v if kk.lower() in _HOST_KEYS), None)
            port = next((kk for kk in v if kk.lower() in _PORT_KEYS), None)
            if host and port:
                out = {"host": base + [k, host], "port": base + [k, port],
                       "container": base + [k]}
                en = next((kk for kk in v if kk.lower() in ("enabled", "enable", "use")), None)
                ty = next((kk for kk in v if kk.lower() in ("type", "kind", "protocol")), None)
                us = next((kk for kk in v if kk.lower() in _USER_KEYS), None)
                pw = next((kk for kk in v if kk.lower() in _PASS_KEYS), None)
                if en: out["enabled"] = base + [k, en]
                if ty: out["type"] = base + [k, ty]
                if us: out["user"] = base + [k, us]
                if pw: out["password"] = base + [k, pw]
                return out
    # 2) flat keys like proxyHost / proxyPort
    fh = next((k for k in obj if "proxy" in k.lower() and any(h in k.lower() for h in _HOST_KEYS)), None)
    fp = next((k for k in obj if "proxy" in k.lower() and "port" in k.lower()), None)
    if fh and fp:
        return {"host": base + [fh], "port": base + [fp]}
    # 3) recurse into nested objects
    for k, v in obj.items():
        if isinstance(v, dict):
            r = _find_proxy_paths(v, base + [k])
            if r:
                return r
    return None


def _dig(obj, path):
    for k in path:
        obj = obj[k]
    return obj


def _set(obj, path, val):
    for k in path[:-1]:
        obj = obj[k]
    obj[path[-1]] = val


def get_proxy(inst):
    """Return {found, host, port, enabled?, path?} for an instance, or {found:False}."""
    text, _ = read_instance_config(inst)
    if not text:
        return {"found": False, "reason": "no config file"}
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError:
        return {"found": False, "reason": "config not valid JSON"}
    paths = _find_proxy_paths(cfg)
    if not paths:
        return {"found": False, "reason": "no proxy field"}
    out = {"found": True,
           "host": _dig(cfg, paths["host"]),
           "port": _dig(cfg, paths["port"]),
           "host_key": ".".join(map(str, paths["host"]))}
    if "enabled" in paths:
        out["enabled"] = bool(_dig(cfg, paths["enabled"]))
    if "user" in paths:
        out["user"] = _dig(cfg, paths["user"]) or ""
    # report whether credentials are set, but never echo the password back
    pw = _dig(cfg, paths["password"]) if "password" in paths else ""
    out["has_auth"] = bool(out.get("user")) or bool(pw)
    return out


def set_proxy(inst, host=None, port=None, enabled=None, user=None, password=None,
              ptype=None):
    """Update the proxy host/port/enabled/user/password/type in an instance's config.
    user/password/type keys are created under the proxy object if the config lacks
    them (e.g. an IP-auth config that never had credentials). Returns updated values."""
    text, _ = read_instance_config(inst)
    if not text:
        raise ValueError("no config file to edit")
    cfg = json.loads(text)
    paths = _find_proxy_paths(cfg)
    if not paths:
        raise ValueError("no proxy field found in this config")
    container = paths.get("container")

    def _put(key, conventional, val):
        # write to the discovered path, or create the conventional key under the proxy object
        if key in paths:
            _set(cfg, paths[key], val)
        elif container is not None:
            _dig(cfg, container)[conventional] = val

    if host is not None:
        _set(cfg, paths["host"], str(host))
    if port is not None:
        _set(cfg, paths["port"], int(port))
    if enabled is not None:
        _put("enabled", "enabled", bool(enabled))
    if user is not None:
        _put("user", "user", str(user))
    if password is not None:
        _put("password", "password", str(password))
    if ptype is not None:
        _put("type", "type", str(ptype))
    write_instance_config(inst, json.dumps(cfg, indent=2))
    return get_proxy(inst)


def list_proxies(cfg):
    """Per-instance proxy summary for the quick-edit view."""
    rows = []
    for i in cfg["instances"]:
        p = get_proxy(i)
        rows.append({"name": i["name"], **p})
    return rows


def _parse_proxy_entry(entry):
    """Parse one proxy into (host, port, user, password); user/password may be None.

    Accepts:
      - {'host':..,'port':..,'user':..,'password':..}
      - 'host:port'
      - 'user:pass@host:port'              (creds prefix)
      - 'host:port:user:pass'              (Webshare download format)
    """
    user = password = None
    if isinstance(entry, dict):
        host, port = entry.get("host"), entry.get("port")
        user = entry.get("user") if entry.get("user") not in ("", None) else None
        password = entry.get("password") if entry.get("password") not in ("", None) else None
    else:
        s = str(entry).strip()
        if not s:
            raise ValueError("empty proxy entry")
        if "@" in s:                                  # user:pass@host:port
            creds, s = s.rsplit("@", 1)
            if ":" in creds:
                user, password = creds.split(":", 1)
            else:
                user = creds
        parts = s.split(":")
        if len(parts) == 2:                           # host:port
            host, port = parts
        elif len(parts) == 4 and user is None:        # host:port:user:pass
            host, port, user, password = parts
        else:
            raise ValueError(f"proxy '{entry}' must be host:port (optionally with creds)")
    host = (str(host).strip() if host is not None else "")
    if not host:
        raise ValueError("proxy host is empty")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError(f"proxy port is not a number: {port!r}")
    user = str(user).strip() if user not in (None, "") else None
    password = str(password) if password not in (None, "") else None
    return host, port, user, password


def set_proxies_bulk(cfg, target_names, proxies, mode="roundrobin", do_restart=False,
                     enable=None, ptype=None, clear_auth=False):
    """Assign proxies across many instances at once.
    target_names: list of instance names, ['all'], or ['errored'] (only bots whose
        console currently shows proxy errors — see detect_proxy_issues).
    proxies: list of {host,port,user?,password?} dicts or 'host:port[:user:pass]' strings.
    mode: 'roundrobin' (cycle the list across targets), 'same' (first to all), or
        'random' (a random proxy per target — unique when the pool is large enough).
    enable: if not None, set proxy.enabled on each target.
    ptype: if set, write proxy.type (e.g. 'HTTP'/'SOCKS5').
    clear_auth: wipe user/password on each target (IP-authorization mode).
    Returns a list of per-target result dicts."""
    if mode not in ("roundrobin", "same", "random"):
        raise ValueError("mode must be 'roundrobin', 'same', or 'random'")
    parsed = [_parse_proxy_entry(p) for p in (proxies or [])]
    if not parsed:
        raise ValueError("no proxies provided")
    if list(target_names) == ["all"]:
        insts = list(cfg["instances"])
    elif list(target_names) == ["errored"]:
        names = errored_proxy_names(cfg)
        if not names:
            raise ValueError("no instances currently show proxy errors")
        insts = [cfg["by_name"][n] for n in names]
    else:
        insts = []
        for n in target_names:
            inst = cfg["by_name"].get(n)
            if not inst:
                raise ValueError(f"no such instance: {n}")
            insts.append(inst)
    if not insts:
        raise ValueError("no target instances")
    # pick a proxy per target up-front according to the assignment mode
    if mode == "same":
        chosen = [parsed[0]] * len(insts)
    elif mode == "random":
        # unique sampling when the pool is large enough, else random with replacement
        chosen = (random.sample(parsed, len(insts)) if len(parsed) >= len(insts)
                  else [random.choice(parsed) for _ in range(len(insts))])
    else:  # roundrobin
        chosen = [parsed[i % len(parsed)] for i in range(len(insts))]
    results = []
    for idx, inst in enumerate(insts):
        host, port, user, password = chosen[idx]
        if clear_auth:
            user = password = ""        # wipe stale creds for IP-auth proxies
        row = {"name": inst["name"], "host": host, "port": port,
               "auth": bool(user) and not clear_auth}
        try:
            set_proxy(inst, host=host, port=port, user=user, password=password,
                      enabled=enable, ptype=ptype)
            row["ok"] = True
            if do_restart:
                row["restart"] = restart(inst)
        except ValueError as e:
            row["ok"] = False
            row["error"] = str(e)
        results.append(row)
    return results


# ---------------------------------------------------------------------------
# Webshare proxy import  (fetch a subscription's proxy list and assign it)
# ---------------------------------------------------------------------------

WEBSHARE_API = "https://proxy.webshare.io/api/v2/proxy/list/"


def _enc_token(t):
    # Obfuscation at rest, NOT encryption — keeps the token from being
    # eyeball-plaintext in instances.json. The 'b64:' marker lets us tell
    # encoded from any legacy plaintext value.
    t = (t or "").strip()
    return "b64:" + base64.b64encode(t.encode()).decode() if t else ""


def _dec_token(s):
    s = s or ""
    if s.startswith("b64:"):
        try:
            return base64.b64decode(s[4:]).decode()
        except Exception:
            return ""
    return s   # tolerate a legacy plaintext token


def _webshare_token(cfg, token=None):
    """Resolve the API token: explicit arg > env > saved (decoded) setting."""
    saved = cfg["raw"].get("settings", {}).get("webshare", {}).get("token") or ""
    return (token or os.environ.get("WEBSHARE_TOKEN") or _dec_token(saved) or "").strip()


def save_webshare_token(cfg, token):
    cfg["raw"].setdefault("settings", {}).setdefault("webshare", {})["token"] = _enc_token(token)
    save_config(cfg)


def webshare_fetch(token, list_mode="direct", valid_only=True, countries=None,
                   plan_id=None, max_pages=50):
    """Fetch the proxy list from the Webshare API → [{host,port,user,password,country,valid}].
    Raises ValueError on auth/HTTP errors with a readable message."""
    import urllib.request, urllib.parse, urllib.error
    if not token:
        raise ValueError("no Webshare API token (pass --token, set WEBSHARE_TOKEN, or save one)")
    if list_mode not in ("direct", "backbone"):
        raise ValueError("list_mode must be 'direct' or 'backbone'")
    out, page, page_size = [], 1, 100
    while page <= max_pages:
        params = {"mode": list_mode, "page": page, "page_size": page_size}
        if plan_id:
            params["plan_id"] = plan_id
        if valid_only:
            params["valid"] = "true"
        if countries:
            params["country_code__in"] = ",".join(c.strip().upper() for c in countries if c.strip())
        url = WEBSHARE_API + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Token {token}",
                                                   "User-Agent": "aquarius-bot-manager"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise ValueError("Webshare API rejected the token (401/403) — check the API key")
            raise ValueError(f"Webshare API error {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            raise ValueError(f"could not reach Webshare API: {e.reason}")
        for it in data.get("results", []):
            host, port = it.get("proxy_address"), it.get("port")
            if not host or not port:
                continue
            out.append({"host": host, "port": port,
                        "user": it.get("username"), "password": it.get("password"),
                        "country": it.get("country_code"), "valid": it.get("valid")})
        if not data.get("next"):
            break
        page += 1
    return out


def webshare_import(cfg, target_names, auth="userpass", token=None, assign_mode="roundrobin",
                    list_mode="direct", valid_only=True, countries=None, plan_id=None,
                    do_restart=False, ptype="HTTP"):
    """Pull the Webshare proxy list and assign it across instances.
    auth: 'userpass' (write per-proxy credentials) or 'ip' (host:port only, wipe creds).
    Returns {fetched, assigned:[...]}."""
    if auth not in ("ip", "userpass"):
        raise ValueError("auth must be 'ip' or 'userpass'")
    tok = _webshare_token(cfg, token)
    proxies = webshare_fetch(tok, list_mode=list_mode, valid_only=valid_only,
                             countries=countries, plan_id=plan_id)
    if not proxies:
        raise ValueError("Webshare returned no proxies (check the plan, filters, or valid-only)")
    clear_auth = (auth == "ip")
    entries = [{"host": p["host"], "port": p["port"],
                "user": None if clear_auth else p.get("user"),
                "password": None if clear_auth else p.get("password")} for p in proxies]
    results = set_proxies_bulk(cfg, target_names, entries, mode=assign_mode,
                               do_restart=do_restart, enable=True, ptype=ptype,
                               clear_auth=clear_auth)
    return {"fetched": len(proxies), "auth": auth, "assigned": results}


# ---------------------------------------------------------------------------
# proxy health  (scan bot consoles for proxy errors → flag dead/removed IPs)
# ---------------------------------------------------------------------------

# Default patterns that flag a proxy problem in a bot's console (case-insensitive).
# Tunable per-deployment via settings.proxy_health.patterns (a list of regexes) — when
# you see the exact wording your proxies produce, add it there. The scan shows the
# matching line as evidence so you can confirm a pattern is hitting the right thing.
DEFAULT_PROXY_ERROR_PATTERNS = [
    r"prox(?:y|ies)\b.{0,60}(?:refus|timed?\s*out|time-?out|unreachable|unable|fail|error|cannot connect|no route|reset|broken|disconnect|denied)",
    r"(?:refus|timed?\s*out|time-?out|unreachable|unable to connect|connection reset|no route|broken pipe).{0,60}\bprox(?:y|ies)\b",
    r"failed to connect to proxy",
    r"proxy[\s_-]*connect(?:ion)?\s*(?:exception|error|failed|refused|timed)",
    r"proxyconnectexception",
    r"clientconnection.{0,60}prox(?:y|ies)",
]


def _proxy_error_patterns(cfg):
    pats = (cfg.get("raw", {}).get("settings", {}) or {}).get("proxy_health", {}).get("patterns")
    return pats if isinstance(pats, list) and pats else DEFAULT_PROXY_ERROR_PATTERNS


def detect_proxy_issues(cfg, names=None, lines=200):
    """Scan each proxy-using instance's live console for proxy-error lines.

    Returns a list of {name, host, port, running, errored, hits, evidence}; instances
    with no proxy field are skipped. Only RUNNING instances can be flagged 'errored'
    (a stopped bot produces no live errors). 'evidence' is the most recent matching line."""
    try:
        pats = [re.compile(p, re.I) for p in _proxy_error_patterns(cfg)]
    except re.error as e:
        raise ValueError(f"invalid proxy-error pattern: {e}")
    if names:
        insts = [cfg["by_name"][n] for n in names if n in cfg["by_name"]]
    else:
        insts = list(cfg["instances"])
    out = []
    for inst in insts:
        prox = get_proxy(inst)
        if not prox.get("found"):
            continue
        running = session_exists(session_name(inst))
        rec = {"name": inst["name"], "host": prox.get("host"), "port": prox.get("port"),
               "running": running, "errored": False, "hits": 0, "evidence": ""}
        if running:
            text = logs(inst, lines=lines)
            hits = [ln.strip() for ln in text.splitlines() if any(p.search(ln) for p in pats)]
            if hits:
                rec["errored"] = True
                rec["hits"] = len(hits)
                rec["evidence"] = hits[-1][:300]
        out.append(rec)
    return out


def errored_proxy_names(cfg, lines=200):
    """Names of running instances whose console currently shows proxy errors."""
    return [r["name"] for r in detect_proxy_issues(cfg, lines=lines) if r["errored"]]


# ---------------------------------------------------------------------------
# manager self-update  (git pull in place + restart — no full reinstall)
# ---------------------------------------------------------------------------

MANAGER_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_NAME = "aquarius-bot-manager.service"
UPDATE_TIMER = "aquarius-bot-manager-update.timer"


def _git(args, cwd=MANAGER_DIR, timeout=60):
    return subprocess.run(["git", "-C", cwd] + args, capture_output=True, text=True, timeout=timeout)


def self_update(do_restart=True, service=SERVICE_NAME):
    """Update the manager in place: `git pull --ff-only` in MANAGER_DIR, then restart the
    web-UI service so the new code loads. Returns a dict describing what happened.

    Fast-forward only, so it never rewrites history or clobbers local edits; instances.json
    is gitignored, so config is untouched. Raises ValueError if the dir isn't a git clone
    or the pull can't fast-forward (e.g. local commits) — those need manual attention."""
    if not os.path.isdir(os.path.join(MANAGER_DIR, ".git")):
        raise ValueError(f"{MANAGER_DIR} is not a git checkout — was the manager installed "
                         "from the installer/clone? (manual installs can't self-update)")
    before = _git(["rev-parse", "--short", "HEAD"])
    old = before.stdout.strip() if before.returncode == 0 else "?"
    _git(["fetch", "--quiet"])
    pull = _git(["pull", "--ff-only"])
    after = _git(["rev-parse", "--short", "HEAD"])
    new = after.stdout.strip() if after.returncode == 0 else "?"
    if pull.returncode != 0:
        raise ValueError("git pull failed (not a fast-forward?): "
                         + (pull.stderr.strip() or pull.stdout.strip()))
    out = {"old": old, "new": new, "updated": old != new,
           "message": pull.stdout.strip() or pull.stderr.strip()}
    if do_restart:
        # Restart the service so the new manager.py loads — but only AFTER this
        # response has had a chance to flush. A synchronous `systemctl restart`
        # SIGTERMs us mid-request, so the caller (or a controller proxying to us)
        # gets a dropped connection / HTML error page instead of this JSON — that
        # was the "Unexpected token '<'" the update button showed. Detach a tiny
        # helper (its own session, so it survives our SIGTERM under KillMode=process)
        # that waits a beat, then runs the restart.
        try:
            subprocess.Popen(
                ["sh", "-c", "sleep 1.5; sudo -n systemctl restart \"$1\"", "sh", service],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, start_new_session=True)
            out["restarted"] = True
        except Exception as e:  # noqa: BLE001 — report, don't crash the update
            out["restarted"] = False
            out["restart_error"] = str(e)
    return out


def autoupdate_status():
    """Whether the periodic self-update systemd timer is installed + enabled.
    Degrades to disabled/unavailable where systemd isn't present (non-Linux dev)."""
    try:
        r = subprocess.run(["systemctl", "is-enabled", UPDATE_TIMER],
                           capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        return {"enabled": False, "state": "unavailable"}
    state = r.stdout.strip() or r.stderr.strip()
    return {"enabled": r.returncode == 0 and state == "enabled", "state": state}


# cache so the dashboard's "update available?" check doesn't hit the git remote on every
# settings load — a quiet fetch happens at most once per _UPDATE_TTL seconds.
_UPDATE_TTL = 600
_UPDATE_CHECK = {"ts": 0.0, "data": None}


def update_available(force=False):
    """Best-effort 'is the manager behind its upstream branch?' check. Does a quiet
    `git fetch` (cached for _UPDATE_TTL s) and counts commits HEAD..@{u} WITHOUT pulling,
    so it never changes the working tree. Never raises — returns
    {available, behind, current, latest, state}."""
    now = time.time()
    cached = _UPDATE_CHECK["data"]
    if not force and cached is not None and now - _UPDATE_CHECK["ts"] < _UPDATE_TTL:
        return cached
    data = {"available": False, "behind": 0, "current": "?", "latest": "?", "state": "ok"}
    try:
        if not os.path.isdir(os.path.join(MANAGER_DIR, ".git")):
            data["state"] = "no-git"
        else:
            cur = _git(["rev-parse", "--short", "HEAD"])
            if cur.returncode == 0:
                data["current"] = cur.stdout.strip()
            f = _git(["fetch", "--quiet"], timeout=30)
            if f.returncode != 0:
                data["state"] = "fetch-failed"
            else:
                up = _git(["rev-parse", "--short", "@{u}"])
                if up.returncode != 0:
                    data["state"] = "no-upstream"
                else:
                    data["latest"] = up.stdout.strip()
                    cnt = _git(["rev-list", "--count", "HEAD..@{u}"])
                    n = cnt.stdout.strip()
                    if cnt.returncode == 0 and n.isdigit():
                        data["behind"] = int(n)
                        data["available"] = int(n) > 0
    except Exception as e:
        data["state"] = "error"
        data["error"] = str(e)
    _UPDATE_CHECK["ts"] = now
    _UPDATE_CHECK["data"] = data
    return data


def autoupdate_set(enable, schedule="daily"):
    """Install+enable (or disable) the periodic self-update systemd timer.

    Writes a oneshot service that runs `abm selfupdate` and a timer firing on `schedule`
    (a systemd OnCalendar value, default 'daily'). Needs sudo. Returns a status dict."""
    abm = os.path.join(MANAGER_DIR, "abm")
    svc = "aquarius-bot-manager-update.service"
    if enable:
        svc_unit = (
            "[Unit]\n"
            "Description=Aquarius Bot Manager self-update (git pull + restart)\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"User={os.environ.get('USER') or 'ubuntu'}\n"
            f"ExecStart={abm} selfupdate\n"
        )
        timer_unit = (
            "[Unit]\n"
            "Description=Run Aquarius Bot Manager self-update on a schedule\n\n"
            "[Timer]\n"
            f"OnCalendar={schedule}\n"
            "Persistent=true\n"
            "RandomizedDelaySec=300\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )
        for name, body in ((svc, svc_unit), (UPDATE_TIMER, timer_unit)):
            w = subprocess.run(["sudo", "-n", "tee", f"/etc/systemd/system/{name}"],
                               input=body, capture_output=True, text=True)
            if w.returncode != 0:
                raise ValueError(f"could not write {name}: {w.stderr.strip()} "
                                 "(needs passwordless sudo)")
        subprocess.run(["sudo", "-n", "systemctl", "daemon-reload"], capture_output=True, text=True)
        r = subprocess.run(["sudo", "-n", "systemctl", "enable", "--now", UPDATE_TIMER],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise ValueError(f"could not enable {UPDATE_TIMER}: {r.stderr.strip()}")
    else:
        subprocess.run(["sudo", "-n", "systemctl", "disable", "--now", UPDATE_TIMER],
                       capture_output=True, text=True)
    return {**autoupdate_status(), "schedule": schedule if enable else None}


# ---------------------------------------------------------------------------
# per-instance resource limits  (enforced via systemd-run --user --scope cgroups)
# ---------------------------------------------------------------------------

_MEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT])?\s*$", re.I)
_MEM_MULT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
_CGROUP_OK = None   # cached result of the capability probe


def parse_mem_to_bytes(s):
    """'2G' / '512M' / 1073741824 -> bytes (int), or None if unparseable."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return int(s)
    m = _MEM_RE.match(str(s))
    if not m:
        return None
    return int(float(m.group(1)) * _MEM_MULT[(m.group(2) or "").upper()])


def _mem_high(memstr):
    """Soft throttle a touch below the hard cap (systemd accepts a raw byte count)."""
    b = parse_mem_to_bytes(memstr)
    return str(int(b * 0.9)) if b else None


def _clean_limits(raw):
    """Validate a {memory, cpu} dict -> normalized dict (drops empty); raises on bad input."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    mem = raw.get("memory")
    if mem not in (None, "", 0):
        if parse_mem_to_bytes(mem) is None:
            raise ValueError("memory must look like 512M, 2G, or a byte count")
        out["memory"] = str(mem).strip()
    cpu = raw.get("cpu")
    if cpu not in (None, "", 0, "0"):
        try:
            c = int(cpu)
        except (TypeError, ValueError):
            raise ValueError("cpu must be a number (percent of one core)")
        if c > 0:
            out["cpu"] = c
    return out


def limits_view(inst):
    """API shape for an instance's limits: {memory, memory_bytes, cpu} (only set fields)."""
    lim = inst.get("limits") or {}
    out = {}
    if lim.get("memory"):
        out["memory"] = lim["memory"]
        out["memory_bytes"] = parse_mem_to_bytes(lim["memory"])
    if lim.get("cpu"):
        out["cpu"] = lim["cpu"]
    return out


def set_limits(cfg, name, memory=None, cpu=None):
    """Set/clear an instance's resource caps. memory/cpu == None leaves that field
    unchanged; "" (or 0 for cpu) clears it. Returns the resulting limits dict."""
    inst = cfg["by_name"].get(name)
    if not inst:
        raise ValueError(f"no such instance: {name}")
    cur = dict(inst.get("limits") or {})
    if memory is not None:
        if str(memory).strip() == "":
            cur.pop("memory", None)
        elif parse_mem_to_bytes(memory) is None:
            raise ValueError("memory must look like 512M, 2G, or a byte count")
        else:
            cur["memory"] = str(memory).strip()
    if cpu is not None:
        if cpu in ("", 0, "0"):
            cur.pop("cpu", None)
        else:
            try:
                c = int(cpu)
            except (TypeError, ValueError):
                raise ValueError("cpu must be a number (percent of one core)")
            cur["cpu"] = c if c > 0 else cur.pop("cpu", None)
            if not c > 0:
                cur.pop("cpu", None)
    if cur:
        inst["limits"] = cur
    else:
        inst.pop("limits", None)
    save_config(cfg)
    return inst.get("limits") or {}


def _supports_cgroup_limits():
    """True if `systemd-run --user --scope` can set resource controls here. Cached.
    Probes once by running 'true' in a throwaway transient user scope."""
    global _CGROUP_OK
    if _CGROUP_OK is None:
        try:
            r = subprocess.run(
                ["systemd-run", "--user", "--scope", "--quiet", "--collect", "true"],
                capture_output=True, text=True, timeout=8)
            _CGROUP_OK = (r.returncode == 0)
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            _CGROUP_OK = False
    return _CGROUP_OK


def _launch_command(inst):
    """Shell string for the tmux pane. With resource limits (and cgroup support) the
    launch is wrapped in a transient systemd user scope; otherwise it's the bare command
    (byte-identical to the historical behavior). Returns (outer_cmd, note)."""
    launch = inst["launch_cmd"]
    lim = _clean_limits(inst.get("limits"))
    if not lim:
        return f"exec {launch}", ""
    if not _supports_cgroup_limits():
        return f"exec {launch}", " (limits not enforced — see README)"
    props = []
    if lim.get("memory"):
        props += ["-p", f"MemoryMax={lim['memory']}"]
        hi = _mem_high(lim["memory"])
        if hi:
            props += ["-p", f"MemoryHigh={hi}"]
    if lim.get("cpu"):
        props += ["-p", f"CPUQuota={int(lim['cpu'])}%"]
    quoted = " ".join(shlex.quote(x) for x in props)
    inner = shlex.quote("exec " + launch)
    return (f"exec systemd-run --user --scope --quiet --collect {quoted} bash -lc {inner}", "")


# ---------------------------------------------------------------------------
# file manager  (jailed to an allowlist of roots; realpath + symlink-escape guards)
# ---------------------------------------------------------------------------

_FS_MAX_READ = 1024 * 1024            # 1 MB editable-file ceiling
_FS_MAX_UPLOAD = 2 * 1024 * 1024 * 1024   # 2 GB per-file upload ceiling (note: a cross-box
                                          # upload buffers in the controller's RAM en route)


def _base_dir(cfg):
    """Best guess at where proxies live: settings.base_dir, else the common parent
    of instance dirs, else the manager dir. Also used as the default deploy target."""
    s = cfg["raw"].get("settings", {}).get("base_dir")
    if s:
        return os.path.abspath(os.path.expanduser(s))
    dirs = [os.path.abspath(i["dir"]) for i in cfg["instances"] if i.get("dir")]
    if len(dirs) >= 2:
        try:
            return os.path.commonpath(dirs)
        except ValueError:
            pass
    if dirs:
        return os.path.dirname(dirs[0])
    return SCRIPT_DIR


def file_roots(cfg):
    """Allowlist of directories the file manager may touch (realpath'd, existing)."""
    roots = cfg["raw"].get("settings", {}).get("file_roots")
    cand = ([os.path.abspath(os.path.expanduser(r)) for r in roots if r]
            if isinstance(roots, list) and roots else [_base_dir(cfg), SCRIPT_DIR])
    out, seen = [], set()
    for r in cand:
        try:
            rr = os.path.realpath(r)
        except OSError:
            continue
        if rr not in seen and os.path.isdir(rr):
            seen.add(rr)
            out.append(rr)
    return out or [os.path.realpath(SCRIPT_DIR)]


def _resolve_in_roots(path, roots):
    """Realpath `path` and assert it sits inside one of `roots` (blocks .. and symlink
    escapes). Returns the resolved absolute path; raises ValueError otherwise."""
    if not path:
        raise ValueError("path required")
    rp = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    for root in roots:
        rr = os.path.realpath(root)
        if rp == rr or rp.startswith(rr + os.sep):
            return rp
    raise ValueError("path is outside the allowed roots")


def _safe_name(name):
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError("invalid name")
    return name


def _is_root(p, roots):
    real = {os.path.realpath(r) for r in roots}
    return os.path.realpath(p) in real


def fs_list(cfg, path):
    roots = file_roots(cfg)
    p = _resolve_in_roots(path, roots) if path else roots[0]
    if not os.path.isdir(p):
        raise ValueError("not a directory")
    entries = []
    for name in os.listdir(p):
        full = os.path.join(p, name)
        try:
            is_dir = os.path.isdir(full)
            st = os.stat(full)
            entries.append({"name": name, "path": full, "type": "dir" if is_dir else "file",
                            "size": None if is_dir else st.st_size, "mtime": int(st.st_mtime)})
        except OSError:
            continue
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    parent = None
    if not _is_root(p, roots):
        try:
            parent = _resolve_in_roots(os.path.dirname(p), roots)
        except ValueError:
            parent = None
    return {"path": p, "parent": parent, "entries": entries, "roots": roots}


def fs_read(cfg, path):
    p = _resolve_in_roots(path, file_roots(cfg))
    if not os.path.isfile(p):
        raise ValueError("not a file")
    size = os.path.getsize(p)
    if size > _FS_MAX_READ:
        raise ValueError(f"file too large to edit ({size // 1024} KB; limit {_FS_MAX_READ // 1024} KB)")
    with open(p, "rb") as f:
        raw = f.read()
    if b"\x00" in raw:
        raise ValueError("binary file — not editable here")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("not a UTF-8 text file — not editable here")
    return {"path": p, "content": text, "size": size}


def fs_write(cfg, path, content):
    roots = file_roots(cfg)
    p = _resolve_in_roots(path, roots)
    _resolve_in_roots(os.path.dirname(p), roots)     # parent must be inside roots too
    if os.path.isdir(p):
        raise ValueError("path is a directory")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(content if content is not None else "")
    return {"path": p, "size": os.path.getsize(p)}


def fs_mkdir(cfg, parent, name, is_file=False):
    roots = file_roots(cfg)
    base = _resolve_in_roots(parent, roots)
    if not os.path.isdir(base):
        raise ValueError("parent is not a directory")
    target = os.path.join(base, _safe_name(name))
    _resolve_in_roots(target, roots)
    if os.path.exists(target):
        raise ValueError("already exists")
    if is_file:
        open(target, "w").close()
    else:
        os.makedirs(target)
    return {"path": target}


def fs_rename(cfg, path, name):
    roots = file_roots(cfg)
    p = _resolve_in_roots(path, roots)
    if _is_root(p, roots):
        raise ValueError("can't rename a root directory")
    target = os.path.join(os.path.dirname(p), _safe_name(name))
    _resolve_in_roots(target, roots)
    if os.path.exists(target):
        raise ValueError("target already exists")
    os.rename(p, target)
    return {"path": target}


def fs_delete(cfg, path, recursive=False):
    roots = file_roots(cfg)
    p = _resolve_in_roots(path, roots)
    if _is_root(p, roots):
        raise ValueError("can't delete a root directory")
    if os.path.isdir(p):
        if recursive:
            shutil.rmtree(p)
        else:
            try:
                os.rmdir(p)
            except OSError:
                raise ValueError("directory not empty (use recursive delete)")
    else:
        os.remove(p)
    return {"deleted": p}


def fs_upload_target(cfg, dirpath, name):
    """Resolve (and create intermediate dirs for) an upload target under `dirpath`.
    `name` may be a nested a/b/c path (folder uploads carry webkitRelativePath); '..'
    segments and absolute names are rejected, and the realpath'd parent is re-checked
    against the roots so a planted symlink can't escape the jail. Returns the abs path."""
    roots = file_roots(cfg)
    base = _resolve_in_roots(dirpath, roots)
    if not os.path.isdir(base):
        raise ValueError("target is not a directory")
    rel = (name or "").replace("\\", "/").strip("/")
    parts = [seg for seg in rel.split("/") if seg and seg != "."]
    if not parts or any(seg == ".." for seg in parts):
        raise ValueError("invalid name")
    target = os.path.join(base, *parts)
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    _resolve_in_roots(parent, roots)            # symlink-escape guard on the real parent
    if os.path.isdir(target):
        raise ValueError("a directory with that name already exists")
    return target


def fs_download(cfg, path):
    """Resolve a file for download. Returns (abs_path, name, cleanup=False)."""
    p = _resolve_in_roots(path, file_roots(cfg))
    if not os.path.isfile(p):
        raise ValueError("not a file")
    return p, os.path.basename(p), False


def fs_zip_dir(cfg, path):
    """Zip a directory (jailed) to a temp file. Returns (tmp_path, download_name,
    cleanup=True) — the caller must delete tmp_path after streaming."""
    p = _resolve_in_roots(path, file_roots(cfg))
    if not os.path.isdir(p):
        raise ValueError("not a directory")
    base = os.path.basename(p.rstrip("/\\")) or "folder"
    fd, tmp = tempfile.mkstemp(suffix=".zip", prefix="abm_dl_")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            wrote = False
            for root, dirs, files in os.walk(p):
                for fn in files:
                    full = os.path.join(root, fn)
                    if os.path.islink(full):
                        continue
                    try:
                        z.write(full, os.path.join(base, os.path.relpath(full, p)))
                        wrote = True
                    except OSError:
                        continue
                # preserve empty directories so the folder structure round-trips
                if not files and not dirs and root != p:
                    z.writestr(os.path.join(base, os.path.relpath(root, p)) + "/", "")
            if not wrote:
                z.writestr(base + "/", "")      # empty folder -> a single dir entry
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return tmp, base + ".zip", True


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

def guess_launch_cmd(d):
    """Best guess at a launch command for a directory, or None."""
    if not os.path.isdir(d):
        return None
    for cand in ("launch", "launch.sh", "start.sh", "run.sh", "start", "run"):
        if os.path.exists(os.path.join(d, cand)):
            return f"./{cand}"
    try:
        jars = [f for f in os.listdir(d) if f.endswith(".jar")]
    except OSError:
        jars = []
    if jars:
        # prefer a jar that looks like AquariusProxy / ZenithProxy
        jars.sort(key=lambda j: (0 if any(k in j.lower() for k in ("aquarius", "zenith")) else 1, j))
        return f"java -jar {jars[0]} nogui"
    return None


def discover(basedir, out_path):
    basedir = os.path.abspath(basedir)
    found = []
    for name in sorted(os.listdir(basedir)):
        d = os.path.join(basedir, name)
        if not os.path.isdir(d):
            continue
        launch = guess_launch_cmd(d)
        if not launch:
            continue
        found.append({
            "name": name,
            "dir": d,
            "launch_cmd": launch,
            "config_file": "config.json",
            "stop_keys": ["C-c"],
            "stop_timeout": 15,
        })
    data = {"instances": found}
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Discovered {len(found)} instance(s) -> {out_path}")
    for i in found:
        print(f"  - {i['name']}  ({i['launch_cmd']})")
    if not found:
        print("Nothing found. Add instances manually to instances.json.")


# ---------------------------------------------------------------------------
# add / delete
# ---------------------------------------------------------------------------

VALID_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
# Friendly display name for a box (controller or node). Kept to a safe charset so it
# can be dropped into HTML/JS without escaping surprises.
BOX_LABEL_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,40}$")


def clean_box_label(label, default=""):
    """Normalize a user-supplied box display name. '' -> default; raises on bad chars."""
    label = (label or "").strip()
    if not label:
        return default
    if not BOX_LABEL_RE.match(label):
        raise ValueError("name may use letters, digits, spaces, '.', '_' and '-' (max 40)")
    return label


def sanitize_name(name):
    """Turn arbitrary user input into a Linux-safe instance/folder name:
    keep letters/digits/._-, collapse any run of other characters into a single
    '-', and trim leading/trailing separators (so we never make a hidden dir or a
    '..' path component). Returns '' if nothing usable remains."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip())
    s = re.sub(r"-{2,}", "-", s).strip("-._")
    return s


def add_instance(cfg, name, directory, launch_cmd=None, config_file=None,
                 stop_keys=None, stop_timeout=None, autostart=False, limits=None):
    """Add a new instance to the config. Returns the new instance dict.
    Raises ValueError on bad input or duplicate name."""
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    if not VALID_NAME.match(name):
        raise ValueError("name may contain only letters, digits, '.', '_' and '-'")
    if name in cfg["by_name"]:
        raise ValueError(f"instance '{name}' already exists")
    directory = (directory or "").strip()
    if not directory:
        raise ValueError("dir is required")
    directory = os.path.abspath(os.path.expanduser(directory))

    inst = {
        "name": name,
        "dir": directory,
        "launch_cmd": (launch_cmd or "./launch.sh").strip() or "./launch.sh",
        "config_file": (config_file or "config.json").strip() or "config.json",
        "stop_keys": stop_keys if stop_keys else ["C-c"],
        "stop_timeout": int(stop_timeout) if stop_timeout else 15,
        "autostart": bool(autostart),
    }
    cl = _clean_limits(limits)
    if cl:
        inst["limits"] = cl
    cfg["raw"].setdefault("instances", []).append(inst)
    cfg["instances"] = cfg["raw"]["instances"]
    cfg["by_name"][name] = inst
    save_config(cfg)
    return inst


def delete_instance(cfg, name, force=False):
    """Remove an instance from the config. Stops it first if running.
    Does NOT touch the instance's files on disk. Returns a status string."""
    inst = cfg["by_name"].get(name)
    if not inst:
        raise ValueError(f"no such instance: {name}")
    st = instance_status(inst)
    if st in ("running", "crashed"):
        if not force:
            raise ValueError(f"instance '{name}' is {st}; stop it first or use force")
        stop(inst)
    cfg["raw"]["instances"] = [i for i in cfg["raw"].get("instances", []) if i.get("name") != name]
    cfg["instances"] = cfg["raw"]["instances"]
    cfg["by_name"].pop(name, None)
    save_config(cfg)
    return f"deleted (was {st})"


def rename_instance(cfg, old, new, move_dir=True):
    """Rename a bot: its instance key, its tmux session (if one exists), and — when it
    isn't running and its folder is the conventional <base>/<name> — its folder on disk.
    A running bot is renamed in place (live tmux session renamed; folder left until it's
    stopped, since the running process holds it). Returns a summary dict."""
    inst = cfg["by_name"].get(old)
    if not inst:
        raise ValueError(f"no such bot: {old}")
    new = sanitize_name(new)
    if not new:
        raise ValueError("a new name is required (letters & digits, e.g. bot1)")
    if new == old:
        return {"ok": True, "name": old, "dir": inst["dir"], "moved_dir": False, "note": "unchanged"}
    if new in cfg["by_name"]:
        raise ValueError(f"a bot named '{new}' already exists")
    status = instance_status(inst)
    old_session = session_name(inst)
    adopted = bool(inst.get("session"))

    moved, note = False, ""
    if move_dir and os.path.basename(os.path.normpath(inst["dir"])) == old:
        if status == "running":
            note = "folder kept (bot is running) — stop it to rename the folder too"
        else:
            new_dir = os.path.join(os.path.dirname(os.path.normpath(inst["dir"])), new)
            if os.path.exists(new_dir):
                raise ValueError(f"target folder already exists: {new_dir}")
            try:
                os.rename(inst["dir"], new_dir)
            except OSError as e:
                raise ValueError(f"couldn't move folder: {e}")
            inst["dir"] = new_dir
            moved = True

    # rename the live/crashed tmux session so the bot stays managed under the new name
    # (adopted instances pin an external session by name — leave that alone)
    if not adopted and session_exists(old_session):
        new_session = SESSION_PREFIX + re.sub(r"[^A-Za-z0-9_-]", "_", new)
        tmux("rename-session", "-t", old_session, new_session)

    inst["name"] = new
    cfg["by_name"].pop(old, None)
    cfg["by_name"][new] = inst
    save_config(cfg)
    return {"ok": True, "name": new, "dir": inst["dir"], "moved_dir": moved, "note": note}


def set_autostart(cfg, name, enabled):
    """Toggle whether an instance is launched by `boot`. Returns the new value."""
    inst = cfg["by_name"].get(name)
    if not inst:
        raise ValueError(f"no such instance: {name}")
    inst["autostart"] = bool(enabled)
    save_config(cfg)
    return inst["autostart"]


SETTABLE_FIELDS = ("launch_cmd", "dir", "config_file", "stop_timeout")


def set_field(cfg, name, field, value):
    """Edit one editable attribute of an instance. Applies to all if name=='all'."""
    if field not in SETTABLE_FIELDS:
        raise ValueError(f"field must be one of: {', '.join(SETTABLE_FIELDS)}")
    targets = cfg["instances"] if name == "all" else (
        [cfg["by_name"][name]] if name in cfg["by_name"] else None)
    if targets is None:
        raise ValueError(f"no such instance: {name}")
    if field == "stop_timeout":
        value = int(value)
    elif field == "dir":
        value = os.path.abspath(os.path.expanduser(str(value)))
    else:
        value = str(value)
    for inst in targets:
        inst[field] = value
    save_config(cfg)
    return [(i["name"], i[field]) for i in targets]


def boot(cfg):
    """Start every instance flagged autostart=true. Idempotent (skips running ones).
    Intended to be run once at host boot via a systemd oneshot unit.
    Returns a dict of name -> result."""
    results = {}
    for inst in cfg["instances"]:
        if not inst.get("autostart"):
            continue
        st = instance_status(inst)
        if st == "running":
            results[inst["name"]] = "already running"
        else:
            results[inst["name"]] = start(inst)
    return results


# ---------------------------------------------------------------------------
# scan / adopt  (detect already-running tmux sessions)
# ---------------------------------------------------------------------------

def _proc_args(pid):
    """Full command line of pid and its immediate children, best-effort."""
    out = []
    try:
        r = subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                           capture_output=True, text=True)
        if r.stdout.strip():
            out.append(r.stdout.strip())
        r = subprocess.run(["ps", "-o", "args=", "--ppid", str(pid)],
                           capture_output=True, text=True)
        out.extend(l.strip() for l in r.stdout.splitlines() if l.strip())
    except Exception:
        pass
    return " ".join(out)


def list_tmux_sessions():
    """All live tmux sessions with the active pane's path/command/pid."""
    sessions = []
    r = tmux("list-sessions", "-F", "#{session_name}")
    if r.returncode != 0:
        return sessions  # no server / no sessions
    for s in (l for l in r.stdout.splitlines() if l):
        info = tmux("display-message", "-p", "-t", s,
                    "#{pane_current_path}\t#{pane_current_command}\t#{pane_pid}")
        path = cmd = ""
        pid = None
        if info.returncode == 0 and "\t" in info.stdout:
            parts = info.stdout.strip("\n").split("\t")
            path = parts[0] if len(parts) > 0 else ""
            cmd = parts[1] if len(parts) > 1 else ""
            try:
                pid = int(parts[2]) if len(parts) > 2 else None
            except ValueError:
                pid = None
        sessions.append({"session": s, "path": path, "command": cmd, "pid": pid})
    return sessions


def _looks_like_proxy(sess):
    """Heuristic: is this tmux session likely an Aquarius/Zenith proxy? -> (bool, reason)."""
    cmd = (sess.get("command") or "").lower()
    path = (sess.get("path") or "")
    args = _proc_args(sess["pid"]).lower() if sess.get("pid") else ""
    if any(k in path.lower() or k in args for k in ("aquarius", "zenith")):
        return True, "aquarius/zenith in path/args"
    if cmd == "java" or " -jar " in args or args.startswith("java"):
        return True, "java process"
    if guess_launch_cmd(path):
        return True, "launcher/jar in dir"
    return False, "no proxy signal"


def managed_sessions(cfg):
    return {session_name(i) for i in cfg["instances"]}


def scan(cfg):
    """Return unmanaged tmux sessions, flagged by likelihood of being a proxy."""
    managed = managed_sessions(cfg)
    out = []
    for s in list_tmux_sessions():
        if s["session"] in managed:
            continue
        likely, reason = _looks_like_proxy(s)
        out.append({
            "session": s["session"],
            "path": s["path"],
            "command": s["command"],
            "likely_proxy": likely,
            "reason": reason,
            "suggested_launch": guess_launch_cmd(s["path"]) or "./launch.sh",
        })
    # likely proxies first
    out.sort(key=lambda x: (not x["likely_proxy"], x["session"]))
    return out


def adopt_session(cfg, session, name=None, launch_cmd=None, config_file=None,
                  stop_keys=None, stop_timeout=None, autostart=False):
    """Adopt an existing tmux session as a managed instance pinned to it."""
    session = (session or "").strip()
    if not session:
        raise ValueError("session is required")
    live = {s["session"]: s for s in list_tmux_sessions()}
    if session not in live:
        raise ValueError(f"no live tmux session named '{session}'")
    if session in managed_sessions(cfg):
        raise ValueError(f"session '{session}' is already managed")

    name = (name or session).strip()
    if not VALID_NAME.match(name):
        raise ValueError("name may contain only letters, digits, '.', '_' and '-'")
    if name in cfg["by_name"]:
        raise ValueError(f"instance name '{name}' already exists")

    path = live[session]["path"] or ""
    inst = {
        "name": name,
        "dir": os.path.abspath(path) if path else "",
        "launch_cmd": (launch_cmd or "").strip() or guess_launch_cmd(path) or "./launch.sh",
        "config_file": (config_file or "config.json").strip() or "config.json",
        "stop_keys": stop_keys if stop_keys else ["C-c"],
        "stop_timeout": int(stop_timeout) if stop_timeout else 15,
        "session": session,
        "autostart": bool(autostart),
    }
    cfg["raw"].setdefault("instances", []).append(inst)
    cfg["instances"] = cfg["raw"]["instances"]
    cfg["by_name"][name] = inst
    save_config(cfg)
    return inst


# ---------------------------------------------------------------------------
# proxy deployer  (download a fork's launcher-v3 release, register a new instance)
# ---------------------------------------------------------------------------

DEPLOY_SOURCES = {"aquarius": "aquariusnetwork9/AquariusProxy", "zenith": "rfresh2/ZenithProxy"}
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


class DeployJob:
    """The single in-flight deploy, with captured log for polling (mirrors SystemJob)."""
    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.name = None
        self.status = "idle"      # idle | running | done | error
        self.lines = []
        self.started = self.finished = None

    def snapshot(self, tail=400):
        with self.lock:
            return {"name": self.name, "status": self.status, "started": self.started,
                    "finished": self.finished, "output": "".join(self.lines[-tail:])}

    def log(self, msg):
        with self.lock:
            self.lines.append(msg if msg.endswith("\n") else msg + "\n")

    def start(self, name, target):
        import threading
        with self.lock:
            if self.status == "running":
                raise ValueError("a deploy is already running")
            self.name, self.status, self.lines = name, "running", []
            self.started, self.finished = time.time(), None
        threading.Thread(target=self._run, args=(target,), daemon=True).start()

    def _run(self, target):
        try:
            target(self.log)
            with self.lock:
                self.status, self.finished = "done", time.time()
        except Exception as e:
            with self.lock:
                self.lines.append(f"\n[error] {e}\n")
                self.status, self.finished = "error", time.time()


DEPLOY_JOB = DeployJob()


def _resolve_repo(source, owner_repo=None):
    if source in DEPLOY_SOURCES:
        return DEPLOY_SOURCES[source]
    if source == "custom":
        r = (owner_repo or "").strip()
        if not _REPO_RE.match(r):
            raise ValueError("custom source needs an owner/repo like youruser/YourProxyFork")
        return r
    raise ValueError(f"unknown source: {source!r} (use aquarius, zenith, or custom)")


def _detect_platform():
    m = platform.machine().lower()
    arch = "aarch64" if m in ("aarch64", "arm64") else "amd64"
    osname = "alpine" if os.path.exists("/etc/alpine-release") else "linux"
    return osname, arch


def _launcher_asset(repo, osname, arch, log):
    """Find the launcher zip for this platform in <repo>@launcher-v3. Returns (name, url)."""
    import urllib.request
    api = f"https://api.github.com/repos/{repo}/releases/tags/launcher-v3"
    log(f"querying {api}")
    req = urllib.request.Request(api, headers={"User-Agent": "aquarius-bot-manager",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    assets = data.get("assets", [])
    for suffix in (f"-launcher-{osname}-{arch}.zip", f"-launcher-linux-{arch}.zip"):
        for a in assets:
            if a.get("name", "").endswith(suffix):
                return a["name"], a["browser_download_url"]
    have = ", ".join(a.get("name", "?") for a in assets) or "(no assets)"
    raise ValueError(f"no launcher for {osname}-{arch} in {repo}@launcher-v3 (have: {have})")


def deploy_proxy(cfg_path, name, directory, source, owner_repo=None, limits=None,
                 autostart=True):
    """Start a background deploy: download the fork's launcher, unzip into `directory`,
    register the instance. Returns immediately; poll DEPLOY_JOB for progress.
    autostart defaults True so a VPS reboot relaunches the bot via the boot unit.
    `name` is sanitized to a Linux-safe form; when `directory` is omitted the bot's
    folder is created automatically as <base>/<name>."""
    name = sanitize_name(name)
    if not name:
        raise ValueError("a name is required (letters & digits, e.g. bot1)")
    cfg = load_config(cfg_path)
    if name in cfg["by_name"]:
        raise ValueError(f"a bot named '{name}' already exists")
    repo = _resolve_repo(source, owner_repo)
    directory = os.path.abspath(os.path.expanduser(
        directory.strip() if directory and directory.strip() else os.path.join(_base_dir(cfg), name)))
    clean_lim = _clean_limits(limits)

    def target(log):
        import urllib.request, zipfile
        osname, arch = _detect_platform()
        log(f"deploying '{name}' from {repo}  ({osname}-{arch})")
        log(f"install dir: {directory}")
        asset, url = _launcher_asset(repo, osname, arch, log)
        os.makedirs(directory, exist_ok=True)
        zpath = os.path.join(directory, asset)
        log(f"downloading {asset} …")
        req = urllib.request.Request(url, headers={"User-Agent": "aquarius-bot-manager"})
        with urllib.request.urlopen(req, timeout=300) as r, open(zpath, "wb") as f:
            shutil.copyfileobj(r, f)
        log(f"downloaded {os.path.getsize(zpath) // 1024} KB; extracting")
        with zipfile.ZipFile(zpath) as z:
            z.extractall(directory)
        os.remove(zpath)
        launch_cmd = guess_launch_cmd(directory) or "./launch"
        base = launch_cmd[2:] if launch_cmd.startswith("./") else None
        if base and os.path.isfile(os.path.join(directory, base)):
            try:
                os.chmod(os.path.join(directory, base), 0o755)
            except OSError:
                pass
        log(f"launch command: {launch_cmd}")
        fresh = load_config(cfg_path)
        if name in fresh["by_name"]:
            log(f"'{name}' already registered — files are in place, left config as-is")
        else:
            add_instance(fresh, name, directory, launch_cmd=launch_cmd, limits=clean_lim or None,
                         autostart=autostart)
            log(f"registered instance '{name}'"
                + (f" with limits {clean_lim}" if clean_lim else "")
                + (" [autostart on boot]" if autostart else ""))
        log("✓ deploy complete — start it from the dashboard "
            "(the launcher fetches Java + the proxy jar on first run).")

    DEPLOY_JOB.start(name, target)
    return {"ok": True, "name": name, "dir": directory, "repo": repo}


# ---------------------------------------------------------------------------
# in-place fork migration  (ZenithProxy bot -> AquariusProxy, keep config + account)
# ---------------------------------------------------------------------------
# Reuses the deploy launcher-download. The validated recipe (see the migration memo):
# stop -> back up -> repoint launch_config.json (rfresh2/ZenithProxy ->
# aquariusnetwork9/AquariusProxy, keep the valid version) -> SWAP the launch binary for the
# Aquarius launcher (the key gotcha — repointing alone makes rfresh's launcher fall back to the
# old jar) -> start. config.json + mc_auth_cache.json (the account) are kept untouched.
MIGRATE_JOB = DeployJob()      # background run + polled log, same machinery as Deploy


def _premigrate_backups(directory):
    """Existing pre-migration backup subdirs in a bot dir, oldest..newest."""
    try:
        ds = [d for d in os.listdir(directory)
              if d.startswith("premigrate-") and os.path.isdir(os.path.join(directory, d))]
    except OSError:
        return []
    return sorted(ds)


def _find_launch_binary(root):
    """Locate the `launch` executable inside an extracted launcher-v3 tree."""
    for base, _dirs, files in os.walk(root):
        if "launch" in files:
            return os.path.join(base, "launch")
    return None


def migrate_to_aquarius(cfg_path, name):
    """Migrate a ZenithProxy bot to AquariusProxy IN PLACE, keeping config.json + the account
    (mc_auth_cache.json). Background job; poll MIGRATE_JOB."""
    cfg = load_config(cfg_path)
    inst = cfg["by_name"].get(name)
    if not inst:
        raise ValueError("no such bot")
    directory = inst["dir"]
    info = proxy_info(directory) or {}
    if info.get("fork") == "AquariusProxy":
        raise ValueError("this bot is already AquariusProxy")
    if not os.path.isfile(os.path.join(directory, "launch_config.json")):
        raise ValueError("no launch_config.json in the bot dir — can't migrate a hand-rolled launch")

    def target(log):
        import urllib.request, zipfile, tempfile
        lc_path = os.path.join(directory, "launch_config.json")
        log(f"migrating '{name}'  ({info.get('fork') or 'unknown'} {info.get('version') or ''}) -> AquariusProxy")
        log(f"dir: {directory}")
        pdir = os.path.join(directory, "plugins")
        if os.path.isdir(pdir):
            jars = [f for f in os.listdir(pdir) if f.endswith(".jar")]
            if jars:
                log(f"[warn] {len(jars)} external plugin jar(s) will NOT load on AquariusProxy "
                    f"(package rename): {', '.join(jars)}. Several have baked-in Aquarius equivalents.")
        # 1) stop
        log("stopping bot…")
        stop(inst)
        for _ in range(30):
            if instance_status(inst) != "running":
                break
            time.sleep(0.5)
        # 2) back up what migration changes (+ config/account for safety)
        ts = time.strftime("%Y%m%d-%H%M%S")
        bak = os.path.join(directory, "premigrate-" + ts)
        os.makedirs(bak, exist_ok=True)
        backed = []
        for fn in ("config.json", "mc_auth_cache.json", "launch_config.json", "launch"):
            src = os.path.join(directory, fn)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(bak, fn)); backed.append(fn)
        for fn in os.listdir(directory):                       # the existing ZenithProxy.jar
            if fn.lower().endswith(".jar") and os.path.isfile(os.path.join(directory, fn)):
                shutil.copy2(os.path.join(directory, fn), os.path.join(bak, fn)); backed.append(fn)
        log(f"backed up -> {os.path.basename(bak)}  ({', '.join(backed)})")
        # 3) repoint launch_config.json (keep the valid version; let the launcher pull the jar)
        with open(lc_path) as f:
            lc = json.load(f)
        lc["repo_owner"] = "aquariusnetwork9"
        lc["repo_name"] = "AquariusProxy"
        lc["auto_update_launcher"] = False     # we manage the launcher binary ourselves
        lc["auto_update"] = True
        with open(lc_path, "w") as f:
            json.dump(lc, f, indent=2)
        log("repointed launch_config.json -> aquariusnetwork9/AquariusProxy (kept version)")
        # 4) swap the launcher binary — the key step (repointing alone falls back to the old jar)
        osname, arch = _detect_platform()
        asset, url = _launcher_asset("aquariusnetwork9/AquariusProxy", osname, arch, log)
        tmp = tempfile.mkdtemp(prefix="aqmig-")
        try:
            zp = os.path.join(tmp, asset)
            log(f"downloading Aquarius launcher: {asset} …")
            req = urllib.request.Request(url, headers={"User-Agent": "aquarius-bot-manager"})
            with urllib.request.urlopen(req, timeout=300) as r, open(zp, "wb") as f:
                shutil.copyfileobj(r, f)
            with zipfile.ZipFile(zp) as z:
                z.extractall(tmp)
            newlaunch = _find_launch_binary(tmp)
            if not newlaunch:
                raise ValueError("the Aquarius launcher zip has no 'launch' binary")
            shutil.copy2(newlaunch, os.path.join(directory, "launch"))
            os.chmod(os.path.join(directory, "launch"), 0o755)
            log("swapped the launch binary -> Aquarius launcher")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        # 5) start
        log("starting bot…")
        start(inst)
        log("✓ migration done — the Aquarius launcher removes ZenithProxy.jar and pulls AquariusProxy.")
        log("  Watch the console for 'config.json loaded.' then 'AquariusProxy started!', then run `connect` to log in.")
        log("  Same account + settings are kept. If anything's off, use Roll back (restores "
            + os.path.basename(bak) + ").")

    MIGRATE_JOB.start(name, target)
    return {"ok": True, "name": name, "dir": directory}


def rollback_migration(cfg_path, name):
    """Restore the most recent pre-migration backup (launcher + configs + the ZenithProxy jar) and
    restart on the old fork. Background job; poll MIGRATE_JOB."""
    cfg = load_config(cfg_path)
    inst = cfg["by_name"].get(name)
    if not inst:
        raise ValueError("no such bot")
    directory = inst["dir"]
    baks = _premigrate_backups(directory)
    if not baks:
        raise ValueError("no pre-migration backup found for this bot")
    bak = os.path.join(directory, baks[-1])

    def target(log):
        log(f"rolling '{name}' back from {os.path.basename(bak)}")
        log("stopping bot…")
        stop(inst)
        for _ in range(30):
            if instance_status(inst) != "running":
                break
            time.sleep(0.5)
        for fn in os.listdir(directory):       # drop the AquariusProxy jar the new launcher pulled
            if fn.lower().endswith(".jar") and "aquarius" in fn.lower():
                try:
                    os.remove(os.path.join(directory, fn)); log(f"removed {fn}")
                except OSError:
                    pass
        for fn in os.listdir(bak):
            shutil.copy2(os.path.join(bak, fn), os.path.join(directory, fn))
        lpath = os.path.join(directory, "launch")
        if os.path.exists(lpath):
            os.chmod(lpath, 0o755)
        log(f"restored {', '.join(sorted(os.listdir(bak)))}")
        log("starting bot…")
        start(inst)
        log("✓ rolled back — the original launcher + jar are restored; watch the console.")

    MIGRATE_JOB.start(name, target)
    return {"ok": True, "name": name, "restored_from": os.path.basename(bak)}


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

# AquariusProxy / ZenithProxy config schema (baseline for the hybrid editor; see schema.py source)
ZENITH_SCHEMA = {'Connection': {'client.connection': {'_label': 'Client Connection', 'autoConnect': {'type': 'bool'}, 'proxy.enabled': {'type': 'bool', 'label': 'Proxy enabled'}, 'proxy.type': {'type': 'enum', 'options': ['SOCKS5', 'SOCKS4', 'HTTP']}, 'proxy.host': {'type': 'string'}, 'proxy.port': {'type': 'int', 'min': 0, 'max': 65535}, 'proxy.user': {'type': 'string'}, 'proxy.password': {'type': 'string', 'secret': True}, 'timeout': {'type': 'int', 'unit': 's'}}, 'server': {'_label': 'Destination Server', 'address': {'type': 'string'}, 'port': {'type': 'int', 'min': 0, 'max': 65535}}}, 'Core Modules': {'autoReconnect': {'enabled': {'type': 'bool'}, 'delay': {'type': 'int', 'unit': 's', 'min': 0, 'max': 300}, 'maxAttempts': {'type': 'int', 'min': 0, 'max': 999}}, 'autoEat': {'enabled': {'type': 'bool'}, 'health': {'type': 'int', 'min': 0, 'max': 20}, 'hunger': {'type': 'int', 'min': 0, 'max': 20}, 'warning': {'type': 'bool'}, 'allowUnsafeFood': {'type': 'bool'}, 'mode': {'type': 'enum', 'options': ['all', 'whitelist', 'blacklist']}}, 'autoTotem': {'enabled': {'type': 'bool'}, 'inGame': {'type': 'bool'}, 'health': {'type': 'int', 'min': 0, 'max': 20}, 'popAlert': {'type': 'bool'}, 'noTotemsAlert': {'type': 'bool'}}, 'autoRespawn': {'enabled': {'type': 'bool'}, 'delay': {'type': 'int', 'unit': 'ms', 'min': 0, 'max': 10000}}, 'autoArmor': {'enabled': {'type': 'bool'}}, 'autoMend': {'enabled': {'type': 'bool'}}}, 'AFK & Anti-Kick': {'antiAFK': {'enabled': {'type': 'bool'}, 'rotate': {'type': 'bool'}, 'swing': {'type': 'bool'}, 'walk': {'type': 'bool'}, 'safeWalk': {'type': 'bool'}, 'jump': {'type': 'bool'}, 'sneak': {'type': 'bool'}, 'walkDistance': {'type': 'int', 'unit': 'ticks', 'min': 0, 'max': 100}}, 'antiKick': {'enabled': {'type': 'bool'}, 'playerInactivityKickMins': {'type': 'int', 'unit': 'min', 'min': 0, 'max': 120}, 'minWalkDistance': {'type': 'int', 'unit': 'blocks', 'min': 0, 'max': 50}}, 'sessionTimeLimit': {'enabled': {'type': 'bool'}}}, 'Combat': {'killAura': {'enabled': {'type': 'bool'}, 'attackDelay': {'type': 'int', 'unit': 'ticks', 'min': 0, 'max': 100}, 'tpsSync': {'type': 'bool'}, 'targetPlayers': {'type': 'bool'}, 'targetHostileMobs': {'type': 'bool'}, 'targetNeutralMobs': {'type': 'bool'}, 'targetCustom': {'type': 'bool'}, 'weaponSwitch': {'type': 'bool'}, 'weaponType': {'type': 'enum', 'options': ['any', 'sword', 'axe']}, 'weaponMaterial': {'type': 'enum', 'options': ['any', 'diamond', 'netherite']}, 'raycast': {'type': 'bool'}, 'priority': {'type': 'enum', 'options': ['none', 'nearest']}}, 'spawnPatrol': {'enabled': {'type': 'bool'}, 'maxPatrolRange': {'type': 'int', 'unit': 'blocks', 'min': 0, 'max': 5000}, 'targetOnlyNakeds': {'type': 'bool'}, 'targetAttackers': {'type': 'bool'}, 'nether': {'type': 'bool'}}, 'spook': {'enabled': {'type': 'bool'}, 'mode': {'type': 'enum', 'options': ['visualRange', 'nearest']}}}, 'Auto Disconnect': {'autoDisconnect': {'enabled': {'type': 'bool'}, 'health': {'type': 'int', 'min': 0, 'max': 20}, 'thunder': {'type': 'bool'}, 'unknownPlayer': {'type': 'bool'}, 'totemPop': {'type': 'bool'}, 'whilePlayerConnected': {'type': 'bool'}, 'autoClientDisconnect': {'type': 'bool'}, 'cancelAutoReconnect': {'type': 'bool'}}}, 'Chat & Spam': {'spammer': {'enabled': {'type': 'bool'}, 'whisper': {'type': 'bool'}, 'whilePlayerConnected': {'type': 'bool'}, 'delayTicks': {'type': 'int', 'unit': 'ticks', 'min': 0, 'max': 2000}, 'randomOrder': {'type': 'bool'}, 'appendRandom': {'type': 'bool'}, 'messages': {'type': 'list'}}, 'autoReply': {'enabled': {'type': 'bool'}, 'cooldown': {'type': 'int', 'unit': 's', 'min': 0, 'max': 600}, 'message': {'type': 'string'}}, 'extraChat': {'enabled': {'type': 'bool'}, 'hideChat': {'type': 'bool'}, 'hideWhispers': {'type': 'bool'}, 'hideDeathMessages': {'type': 'bool'}, 'insertClickableLinks': {'type': 'bool'}}, 'chatRelay': {'enabled': {'type': 'bool'}, 'channel': {'type': 'string', 'label': 'Channel ID'}, 'connectionMessages': {'type': 'bool'}, 'whispers': {'type': 'bool'}, 'publicChat': {'type': 'bool'}, 'deathMessages': {'type': 'bool'}, 'whisperMentions': {'type': 'bool'}, 'nameMentions': {'type': 'bool'}, 'sendMessages': {'type': 'bool'}}}, 'Visual Range': {'visualRange': {'enabled': {'type': 'bool'}, 'enter': {'type': 'bool'}, 'leave': {'type': 'bool'}, 'logout': {'type': 'bool'}, 'ignoreFriends': {'type': 'bool'}, 'replayRecording': {'type': 'bool'}}, 'stalk': {'enabled': {'type': 'bool'}}}, 'Discord': {'discord': {'enabled': {'type': 'bool'}, 'channel': {'type': 'string', 'label': 'Channel ID'}, 'token': {'type': 'string', 'secret': True}, 'role': {'type': 'string', 'label': 'Role ID'}, 'manageProfileImage': {'type': 'bool'}, 'manageNickname': {'type': 'bool'}, 'manageDescription': {'type': 'bool'}, 'managePresence': {'type': 'bool'}, 'ignoreOtherBots': {'type': 'bool'}}}, 'Advanced': {'tickRate': {'rate': {'type': 'float', 'min': 0.1, 'max': 5.0, 'step': 0.1}}, 'actionLimiter': {'enabled': {'type': 'bool'}, 'allowMovement': {'type': 'bool'}, 'movementDistance': {'type': 'int', 'unit': 'blocks', 'min': 0, 'max': 1000}, 'allowInventory': {'type': 'bool'}, 'allowBlockBreaking': {'type': 'bool'}, 'allowChat': {'type': 'bool'}}, 'rateLimiter': {'login': {'type': 'bool'}, 'packet': {'type': 'bool'}}}}


THEME_PRESETS = {
    "midnight":  {"bg": "#0a0e12", "panel": "#11171e", "accent": "#3ddc97"},
    "ember":     {"bg": "#120c0a", "panel": "#1d1411", "accent": "#ff7a45"},
    "ice":       {"bg": "#0a0f14", "panel": "#101820", "accent": "#5cc8ff"},
    "amethyst":  {"bg": "#0e0a14", "panel": "#17111f", "accent": "#b388ff"},
    "paper":     {"bg": "#f4f1ea", "panel": "#fffdf7", "accent": "#1f7a55"},
    "obsidian":  {"bg": "#000000", "panel": "#0c0c0c", "accent": "#e6e6e6"},
    "forest":    {"bg": "#0a120d", "panel": "#101c14", "accent": "#5fd17a"},
    "rose":      {"bg": "#140a0f", "panel": "#1f1117", "accent": "#ff6f9c"},
    "ocean":     {"bg": "#08101a", "panel": "#0e1a28", "accent": "#39b8d6"},
    "gold":      {"bg": "#100d06", "panel": "#1b160c", "accent": "#e8b53a"},
    "sand":      {"bg": "#faf6ee", "panel": "#fffdf8", "accent": "#c2691c"},
}
HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Font pairings selectable in Settings -> Appearance. Each entry is a
# (display/sans, monospace) pairing applied to the --sans / --mono CSS vars.
# "q" is the Google Fonts css2 query fragment (families to fetch); "" means the
# pairing is system-only and needs no network fetch. Stored under theme.font.
FONT_PRESETS = {
    "aquarius":  {"label": "Aquarius",  "sans": "'Sora',system-ui,sans-serif",                          "mono": "'Space Mono',ui-monospace,monospace",      "q": "family=Sora:wght@400;600;700;800&family=Space+Mono:wght@400;700"},
    "system":    {"label": "System",    "sans": "system-ui,-apple-system,'Segoe UI',Roboto,sans-serif", "mono": "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace", "q": ""},
    "inter":     {"label": "Inter",     "sans": "'Inter',system-ui,sans-serif",                         "mono": "'JetBrains Mono',ui-monospace,monospace",  "q": "family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;700"},
    "roboto":    {"label": "Roboto",    "sans": "'Roboto',system-ui,sans-serif",                        "mono": "'Roboto Mono',ui-monospace,monospace",     "q": "family=Roboto:wght@400;500;700;900&family=Roboto+Mono:wght@400;700"},
    "rounded":   {"label": "Rounded",   "sans": "'Nunito',system-ui,sans-serif",                        "mono": "'Fira Code',ui-monospace,monospace",       "q": "family=Nunito:wght@400;600;700;800;900&family=Fira+Code:wght@400;700"},
    "grotesk":   {"label": "Grotesk",   "sans": "'Space Grotesk',system-ui,sans-serif",                 "mono": "'IBM Plex Mono',ui-monospace,monospace",   "q": "family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;600"},
    "terminal":  {"label": "Terminal",  "sans": "'IBM Plex Mono',ui-monospace,monospace",               "mono": "'IBM Plex Mono',ui-monospace,monospace",   "q": "family=IBM+Plex+Mono:wght@400;500;600;700"},
    "geometric": {"label": "Geometric", "sans": "'Poppins',system-ui,sans-serif",                       "mono": "'Source Code Pro',ui-monospace,monospace", "q": "family=Poppins:wght@400;500;600;700;800&family=Source+Code+Pro:wght@400;600"},
    "classic":   {"label": "Classic",   "sans": "'Work Sans',system-ui,sans-serif",                     "mono": "'Ubuntu Mono',ui-monospace,monospace",     "q": "family=Work+Sans:wght@400;500;600;700;800&family=Ubuntu+Mono:wght@400;700"},
    "editorial": {"label": "Editorial", "sans": "'Libre Franklin',system-ui,sans-serif",                "mono": "'Spline Sans Mono',ui-monospace,monospace","q": "family=Libre+Franklin:wght@400;500;600;700;800&family=Spline+Sans+Mono:wght@400;600"},
}

# Quick-command buttons shown in each instance's Console tab. Editable in the UI
# (Settings -> Console) and stored under settings.console_presets. These defaults
# are sensible AquariusProxy/ZenithProxy console commands; change them to taste.
DEFAULT_CONSOLE_PRESETS = [
    {"label": "Reconnect", "command": "connect"},
    {"label": "Disconnect", "command": "disconnect"},
    {"label": "Status", "command": "info"},
]

# Alert thresholds (percent). The dashboard flags a host gauge / instance card warn-colored
# once usage crosses these. Stored under settings.thresholds.
DEFAULT_THRESHOLDS = {"cpu_pct": 85, "mem_pct": 85, "disk_pct": 90}


def get_settings(cfg):
    s = cfg["raw"].get("settings", {})
    presets = s.get("console_presets")
    return {
        "theme": {
            "preset": s.get("theme", {}).get("preset", "midnight"),
            "accent": s.get("theme", {}).get("accent", ""),
            "bg_image": s.get("theme", {}).get("bg_image", ""),
            "bg_dim": s.get("theme", {}).get("bg_dim", 0.6),
            "density": s.get("theme", {}).get("density", ""),
            "font": s.get("theme", {}).get("font", "aquarius"),
        },
        "ui": {
            "sidebar": s.get("ui", {}).get("sidebar", "full"),
            "sidebar_side": s.get("ui", {}).get("sidebar_side", "left"),
        },
        "box_name": s.get("box_name") or "Controller",
        "system_actions_enabled": bool(s.get("system_actions_enabled", False)),
        "console_presets": presets if isinstance(presets, list) else DEFAULT_CONSOLE_PRESETS,
        "thresholds": {**DEFAULT_THRESHOLDS, **(s.get("thresholds") or {})},
        "schedules": s.get("schedules") or {"notify_webhook": "", "jobs": []},
        "base_dir": _base_dir(cfg),
        "presets": THEME_PRESETS,
        "fonts": FONT_PRESETS,
        "webshare_saved": bool(s.get("webshare", {}).get("token")),
        "autoupdate": autoupdate_status(),
        # non-blocking cached snapshot; the UI refreshes it via /api/update/check
        "update": _UPDATE_CHECK["data"] or {"state": "checking"},
    }


def save_settings(cfg, theme=None, system_actions_enabled=None, console_presets=None,
                  thresholds=None, ui=None, schedules=None, box_name=None):
    s = cfg["raw"].setdefault("settings", {})
    if box_name is not None:
        s["box_name"] = clean_box_label(box_name, default="Controller")
    if theme is not None:
        t = s.setdefault("theme", {})
        if "preset" in theme:
            p = theme["preset"]
            if p not in THEME_PRESETS:
                raise ValueError(f"unknown theme preset: {p}")
            t["preset"] = p
        if "accent" in theme:
            a = (theme["accent"] or "").strip()
            if a and not HEX_RE.match(a):
                raise ValueError("accent must be a hex color like #3ddc97")
            t["accent"] = a
        if "bg_image" in theme:
            img = (theme["bg_image"] or "").strip()
            if img and not re.match(r"^https?://", img):
                raise ValueError("background must be an http(s) image URL")
            if len(img) > 2000:
                raise ValueError("background URL is too long")
            t["bg_image"] = img
        if "bg_dim" in theme:
            try:
                t["bg_dim"] = max(0.0, min(0.95, float(theme["bg_dim"])))
            except (TypeError, ValueError):
                raise ValueError("bg_dim must be a number between 0 and 0.95")
        if "density" in theme:
            d = (theme["density"] or "").strip()
            if d not in ("", "compact", "spacious"):
                raise ValueError("density must be '', 'compact' or 'spacious'")
            t["density"] = d
        if "font" in theme:
            fk = (theme["font"] or "").strip()
            if fk not in FONT_PRESETS:
                raise ValueError(f"unknown font: {fk}")
            t["font"] = fk
    if system_actions_enabled is not None:
        s["system_actions_enabled"] = bool(system_actions_enabled)
    if console_presets is not None:
        if not isinstance(console_presets, list):
            raise ValueError("console_presets must be a list")
        cleaned = []
        for p in console_presets:
            if not isinstance(p, dict):
                raise ValueError("each console preset must be an object with label + command")
            label = str(p.get("label", "")).strip()
            command = str(p.get("command", "")).strip()
            if label and command:          # silently drop blank rows
                cleaned.append({"label": label, "command": command})
        s["console_presets"] = cleaned
    if thresholds is not None:
        if not isinstance(thresholds, dict):
            raise ValueError("thresholds must be an object")
        cur = dict(s.get("thresholds") or {})
        for k in ("cpu_pct", "mem_pct", "disk_pct"):
            if k in thresholds and thresholds[k] is not None:
                try:
                    v = int(thresholds[k])
                except (TypeError, ValueError):
                    raise ValueError(f"{k} must be a number")
                cur[k] = max(1, min(100, v))
        s["thresholds"] = cur
    if ui is not None:
        if not isinstance(ui, dict):
            raise ValueError("ui must be an object")
        u = s.setdefault("ui", {})
        if "sidebar" in ui:
            sb = (ui["sidebar"] or "").strip()
            if sb not in ("off", "rail", "full", "cmd"):
                raise ValueError("sidebar must be off, rail, full or cmd")
            u["sidebar"] = sb
        if "sidebar_side" in ui:
            sd = (ui["sidebar_side"] or "").strip()
            if sd not in ("left", "right"):
                raise ValueError("sidebar_side must be left or right")
            u["sidebar_side"] = sd
    if schedules is not None:
        s["schedules"] = validate_schedule(schedules)
    save_config(cfg)
    if schedules is not None and SCHEDULER is not None:
        try:
            SCHEDULER._reschedule_all()      # pick up new/changed time jobs immediately
        except Exception:
            pass
    return get_settings(cfg)


# ---------------------------------------------------------------------------
# system actions  (run locally on the VPS via sudo; off by default)
# ---------------------------------------------------------------------------

SYSTEM_COMMANDS = {
    "update": ["sudo", "-n", "sh", "-c",
               "DEBIAN_FRONTEND=noninteractive apt-get update "
               "&& DEBIAN_FRONTEND=noninteractive apt-get -y upgrade"],
}
SYSTEM_COMMANDS_OVERRIDE = {}  # for testing without touching the box


def _sysinfo():
    """Read-only snapshot of the host. Best-effort; missing pieces -> None."""
    info = {}
    try:
        info["cpus"] = os.cpu_count()
    except Exception:
        info["cpus"] = None
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k.strip()] = int(v.strip().split()[0]) * 1024
        total = mem.get("MemTotal")
        avail = mem.get("MemAvailable")
        info["mem_total"] = total
        info["mem_used"] = (total - avail) if (total and avail) else None
    except Exception:
        info["mem_total"] = info["mem_used"] = None
    try:
        st = os.statvfs("/")
        info["disk_total"] = st.f_blocks * st.f_frsize
        info["disk_used"] = (st.f_blocks - st.f_bfree) * st.f_frsize
    except Exception:
        info["disk_total"] = info["disk_used"] = None
    try:
        info["load"] = list(os.getloadavg())
    except Exception:
        info["load"] = None
    try:
        with open("/proc/uptime") as f:
            info["uptime_sec"] = int(float(f.read().split()[0]))
    except Exception:
        info["uptime_sec"] = None
    info["os"] = None
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    info["os"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except Exception:
        pass
    info["tmux_sessions"] = len(list_tmux_sessions())
    info["cgroup_limits"] = _supports_cgroup_limits()
    return info


class SystemJob:
    """Tracks the single in-flight system job (e.g. OS update)."""
    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.name = None
        self.status = "idle"      # idle | running | done | error
        self.lines = []
        self.started = None
        self.finished = None

    def snapshot(self, tail=400):
        with self.lock:
            return {
                "name": self.name, "status": self.status,
                "started": self.started, "finished": self.finished,
                "output": "".join(self.lines[-tail:]),
            }

    def start(self, name, argv):
        import threading
        with self.lock:
            if self.status == "running":
                raise ValueError(f"a system job ({self.name}) is already running")
            self.name = name
            self.status = "running"
            self.lines = []
            self.started = time.time()
            self.finished = None
        threading.Thread(target=self._run, args=(argv,), daemon=True).start()

    def _run(self, argv):
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in p.stdout:
                with self.lock:
                    self.lines.append(line)
            p.wait()
            with self.lock:
                self.status = "done" if p.returncode == 0 else "error"
                self.lines.append(f"\n[exit code {p.returncode}]\n")
                self.finished = time.time()
        except Exception as e:
            with self.lock:
                self.status = "error"
                self.lines.append(f"\n[failed to run: {e}]\n")
                self.finished = time.time()


SYS_JOB = SystemJob()


def run_system_action(cfg, action):
    if not get_settings(cfg)["system_actions_enabled"]:
        raise PermissionError("system actions are disabled (enable them in Settings)")
    if action == "reboot":
        cmd = SYSTEM_COMMANDS_OVERRIDE.get("reboot", ["sudo", "-n", "reboot"])
        delayed = ["sh", "-c", f"sleep 2; {' '.join(cmd)}"]
        subprocess.Popen(delayed, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "action": "reboot",
                "note": "rebooting in ~2s; the manager goes down with the host"}
    if action == "update":
        cmd = SYSTEM_COMMANDS_OVERRIDE.get("update", SYSTEM_COMMANDS["update"])
        SYS_JOB.start("update", cmd)
        return {"ok": True, "action": "update",
                "note": "OS update started; poll /api/system/job for output"}
    raise ValueError(f"unknown system action: {action}")


# ---------------------------------------------------------------------------
# auth: password hashing + sessions
# ---------------------------------------------------------------------------
# Password is stored in settings.auth as {user, salt, hash} where
# hash = PBKDF2-HMAC-SHA256(password, salt). Never plaintext.
# Sessions are in-memory: token -> expiry epoch. Cookie is HttpOnly.

_SESSIONS = {}                       # token -> {"exp": epoch, "gen": session_epoch}
SESSION_TTL = 7 * 24 * 3600          # 7 days
_LOGIN_FAILS = {}                    # ip -> [timestamps] for rate limiting
PBKDF2_ROUNDS = 200_000


def _hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                               PBKDF2_ROUNDS).hex()


def session_epoch(cfg):
    """Generation counter; bumping it invalidates all existing sessions across processes."""
    return int(cfg["raw"].get("settings", {}).get("session_epoch", 0))


def bump_session_epoch(cfg):
    s = cfg["raw"].setdefault("settings", {})
    s["session_epoch"] = session_epoch(cfg) + 1
    save_config(cfg)
    return s["session_epoch"]


def set_password(cfg, username, password):
    if not username or not password:
        raise ValueError("username and password are required")
    if len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    salt = secrets.token_hex(16)
    cfg["raw"].setdefault("settings", {})["auth"] = {
        "user": username,
        "salt": salt,
        "hash": _hash_password(password, salt),
    }
    # invalidate all sessions everywhere (including a separate running server)
    cfg["raw"]["settings"]["session_epoch"] = session_epoch(cfg) + 1
    save_config(cfg)
    _SESSIONS.clear()


def auth_configured(cfg):
    a = cfg["raw"].get("settings", {}).get("auth")
    return bool(a and a.get("hash") and a.get("salt"))


def verify_password(cfg, username, password):
    a = cfg["raw"].get("settings", {}).get("auth")
    if not a:
        return False
    if username != a.get("user"):
        return False
    calc = _hash_password(password, a["salt"])
    return hmac.compare_digest(calc, a["hash"])


def _new_session(gen=0, scope=None, user=None):
    """Mint a session token. Owner sessions pass scope=None/user=None. Guest sessions (from a share
    link) pass scope={grant_id, targets, all, capability, shares_epoch} → principal=guest. Named-user
    sessions pass a user record → principal=user (carrying uid + pwgen for live revalidation)."""
    tok = secrets.token_urlsafe(32)
    s = {"exp": time.time() + SESSION_TTL, "gen": gen}
    if scope is not None:
        s["scope"] = scope
        s["principal"] = "guest"
    elif user is not None:
        s["principal"] = "user"
        s["uid"] = user["id"]
        s["pwgen"] = int(user.get("pwgen", 0))
    _SESSIONS[tok] = s
    return tok


def _session_valid(tok, gen=0):
    s = _SESSIONS.get(tok)
    if not s:
        return False
    if time.time() > s["exp"] or s.get("gen", 0) != gen:
        _SESSIONS.pop(tok, None)
        return False
    return True


def _rate_limited(ip):
    """Allow at most 5 failed logins per 5 minutes per ip."""
    now = time.time()
    fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < 300]
    _LOGIN_FAILS[ip] = fails
    return len(fails) >= 5


def _record_fail(ip):
    _LOGIN_FAILS.setdefault(ip, []).append(time.time())


# ---------------------------------------------------------------------------
# shareable-link guest access: share grants
# ---------------------------------------------------------------------------
# A "share" is a URL that grants a guest scoped access to specific bots at a
# capability tier. Stored in settings.shares; only the sha256 of the token is
# persisted (the plaintext is revealed once at creation). Capability tiers are
# ordered view < operate < config. shares_epoch is the revoke-all generation.

CAP_RANK = {"view": 0, "operate": 1, "config": 2}


def _share_token_hash(token):
    # 256-bit tokens => unsalted sha256 is sufficient and fast on the redemption path
    return hashlib.sha256(token.encode()).hexdigest()


def _shares(cfg):
    return cfg["raw"].setdefault("settings", {}).setdefault("shares", [])


def shares_epoch(cfg):
    return int(cfg["raw"].get("settings", {}).get("shares_epoch", 0))


def bump_shares_epoch(cfg):
    """Revoke-all: bumping the epoch invalidates every existing share grant + guest session."""
    s = cfg["raw"].setdefault("settings", {})
    s["shares_epoch"] = shares_epoch(cfg) + 1
    save_config(cfg)
    return s["shares_epoch"]


def new_share(cfg, label, targets, all_local, capability, ttl_days):
    if capability not in CAP_RANK:
        raise ValueError("invalid capability")
    token = secrets.token_urlsafe(32)
    now = time.time()
    grant = {
        "id": secrets.token_hex(4),
        "token_hash": _share_token_hash(token),
        "label": (label or "").strip()[:80] or "Shared link",
        "targets": targets or [],          # [{"node": <name|null>, "name": <bot>}]
        "all": bool(all_local),            # all current+future LOCAL bots (targets ignored)
        "capability": capability,
        "created": now,
        "expires": (now + float(ttl_days) * 86400) if ttl_days else None,
        "revoked": False,
        "epoch": shares_epoch(cfg),
    }
    _shares(cfg).append(grant)
    save_config(cfg)
    return grant, token


def _share_active(cfg, g):
    if g.get("revoked"):
        return False
    if g.get("epoch", 0) != shares_epoch(cfg):
        return False
    exp = g.get("expires")
    if exp is not None and time.time() > exp:
        return False
    return True


def find_share_by_token(cfg, token):
    """Constant-time-ish scan: compare the token hash against every grant, accept an active match."""
    th = _share_token_hash(token)
    match = None
    for g in _shares(cfg):
        if hmac.compare_digest(g.get("token_hash", ""), th) and _share_active(cfg, g):
            match = g
    return match


def find_share_by_id(cfg, sid):
    for g in _shares(cfg):
        if g.get("id") == sid:
            return g
    return None


def revoke_share(cfg, sid):
    g = find_share_by_id(cfg, sid)
    if not g:
        return False
    g["revoked"] = True
    save_config(cfg)
    return True


def share_status(cfg, g):
    if g.get("revoked") or g.get("epoch", 0) != shares_epoch(cfg):
        return "revoked"
    exp = g.get("expires")
    if exp is not None and time.time() > exp:
        return "expired"
    return "active"


def share_public_view(cfg, g):
    """Owner-facing view of a grant — never includes the token hash."""
    v = {k: g.get(k) for k in ("id", "label", "targets", "all", "capability", "created", "expires")}
    v["status"] = share_status(cfg, g)
    return v


_GUEST_AUDIT = []        # in-memory ring buffer of recent guest mutations (best-effort)


def audit_guest(entry):
    _GUEST_AUDIT.append(entry)
    if len(_GUEST_AUDIT) > 500:
        del _GUEST_AUDIT[:len(_GUEST_AUDIT) - 500]


# ---------------------------------------------------------------------------
# Named multi-user accounts (RBAC)
# ---------------------------------------------------------------------------
# Beyond the single owner + anonymous share links: real accounts, each with its
# own login. A non-admin user is authorization-identical to a guest link (same
# scope = {targets, all, capability} at tier view < operate < config); an admin
# user is owner-equivalent. Accounts live in settings.users; invite links (a
# preset role+scope the invitee redeems by setting their own password) live in
# settings.invites. Like guest grants, the user is re-resolved from cfg on every
# request, so role/scope edits, disable, and delete take effect instantly.

USER_ROLES = ("view", "operate", "config", "admin")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{1,31}$")


def role_capability(role):
    """Map a role to its guest-style capability tier. Admin is owner-equivalent (handled separately);
    we report 'config' for completeness."""
    return "config" if role == "admin" else role


# ---------------------------------------------------------------------------
# Fine-grained permissions — what modules/actions a named user may use
# ---------------------------------------------------------------------------
# Control surface module registry (id, friendly name, raw config-class key, category),
# ported from control/abm-control-data.js (the single source of truth). Used to map a
# /control/command toggle (`<rawlower> on|off`) and a /control/config path
# (client.extra.<lcfirst(raw)>.…) back to a module id so the manager can authorize per module.
CONTROL_MODULES = [
    ("livemap", "Live Map", "LiveViewer", "control"),
    ("elytra", "Elytra Autopilot", "ElytraPilot", "control"),
    ("trader", "Villager Trading", "VillagerTrader", "control"),
    ("pearl", "Pearl Stasis", "PearlManager", "control"),
    ("stash", "Stash Manager", "StashScanner", "control"),
    ("miner", "Auto Miner", "AquariusMiner", "control"),
    ("enchanter", "Auto Enchanter", "Enchanter", "control"),
    ("kitmaker", "Kit Builder", "KitMaker", "control"),
    ("sniffer", "Packet Sniffer", "AquariusSniffer", "control"),
    ("highway", "Highway Builder", "HighwayBuilder", "control"),
    ("schematic", "Schematic Builder", "Litematica", "control"),
    ("boat", "Boat Autopilot", "Boat", "control"),
    ("regear", "Auto Regear", "Regear", "control"),
    ("orderfiller", "Order Filler", "OrderFiller", "control"),
    ("pearldrop", "Pearl Drop", "PearlDrop", "control"),
    ("flightgear", "Flight Gear", "FlightGear", "control"),
    ("killaura", "Combat Assist", "KillAura", "combat"),
    ("autobow", "Auto Bow", "AutoBow", "combat"),
    ("autoarmor", "Auto Armor", "AutoArmor", "combat"),
    ("autoeat", "Auto Eat", "AutoEat", "survival"),
    ("autototem", "Auto Totem", "AutoTotem", "survival"),
    ("autorespawn", "Auto Respawn", "AutoRespawn", "survival"),
    ("automend", "Auto Mend", "AutoMend", "survival"),
    ("autodisconnect", "Auto Disconnect", "AutoDisconnect", "survival"),
    ("account", "Account & Login", "Authentication", "connection"),
    ("autoreconnect", "Auto Reconnect", "AutoReconnect", "connection"),
    ("antikick", "Anti-Kick", "AntiKick", "connection"),
    ("antiafk", "Anti-AFK", "AntiAFK", "connection"),
    ("actionlimiter", "Action Limiter", "ActionLimiter", "connection"),
    ("requeue", "Auto Re-queue", "Requeue", "connection"),
    ("queuewarn", "Queue Alert", "QueueWarning", "connection"),
    ("activehours", "Active Hours", "ActiveHours", "connection"),
    ("sessionlimit", "Session Time Limit", "SessionTimeLimit", "connection"),
    ("coordprivacy", "Coordinate Privacy", "CoordObfuscation", "privacy"),
    ("antileak", "Anti-Leak", "AntiLeak", "privacy"),
    ("visualrange", "Visual Range Alerts", "VisualRange", "privacy"),
    ("whispercontrol", "Whisper Control", "WhisperControl", "privacy"),
    ("bridge", "Proxy Bridge", "Bridge", "privacy"),
    ("autofish", "Auto Fish", "AutoFish", "automation"),
    ("autodrop", "Auto Drop", "AutoDrop", "automation"),
    ("spammer", "Chat Broadcaster", "Spammer", "automation"),
    ("autoreply", "Auto Reply", "AutoReply", "automation"),
    ("autoportal", "Auto Portal", "AutoPortal", "automation"),
    ("autoomen", "Bad Omen", "AutoOmen", "automation"),
    ("chat", "Chat", "Chat", "automation"),
    ("replaymod", "Replay Recorder", "ReplayMod", "diagnostics"),
    ("spawnpatrol", "Spawn Patrol", "SpawnPatrol", "diagnostics"),
    ("discord", "Discord Notifications", "Discord", "diagnostics"),
]
_MOD_BY_ID = {m[0]: {"id": m[0], "name": m[1], "raw": m[2], "cat": m[3]} for m in CONTROL_MODULES}
# raw config key (lowercased) -> module id, used to identify the target of a command/config path
_RAW2MOD = {m[2].lower(): m[0] for m in CONTROL_MODULES}
_RAW2MOD.update({"liveviewer": "livemap", "pearlmanager": "pearl"})   # live-name aliases (control-live.js)
_TOGGLE_RE = re.compile(r"^([A-Za-z0-9_]+)\s+(on|off)$", re.I)


def module_catalog():
    """[{id,name,cat}] for the permissions editor UI."""
    return [{"id": m[0], "name": m[1], "cat": m[3]} for m in CONTROL_MODULES]


def resolve_perms(role, perms):
    """Resolve a user's effective control permissions from their role tier + optional perms override.
    Perms only RESTRICT within the tier. Absent perms = full-within-tier (no regression). Returns
    {use_all, config_all, modules:{id:{use,config}}, console, lifecycle, can_use(id), can_config(id)}."""
    p = perms or {}
    has = perms is not None
    tier = role_capability(role)
    rank = CAP_RANK.get(tier, 0)
    op = rank >= CAP_RANK["operate"]
    cf = rank >= CAP_RANK["config"]
    use_all = (not has) or bool(p.get("use_all"))
    config_all = ((not has) or bool(p.get("config_all")))
    mods = p.get("modules") or {}
    console = op and ((not has) or bool(p.get("console", True)))
    lifecycle = op and ((not has) or bool(p.get("lifecycle", True)))

    def can_use(mid):
        if not op:
            return False
        if use_all or config_all:
            return True
        e = mods.get(mid) or {}
        return bool(e.get("use") or e.get("config"))

    def can_config(mid):
        if not cf:
            return False
        if config_all:
            return True
        e = mods.get(mid) or {}
        return bool(e.get("config"))

    return {"use_all": use_all and op, "config_all": config_all and cf,
            "modules": mods, "console": console, "lifecycle": lifecycle,
            "can_use": can_use, "can_config": can_config}


def perms_public(role, perms):
    """JSON-safe effective perms for the browser (control-live.js + the editor): per-module use/config
    booleans + console/lifecycle, with the function fields dropped."""
    r = resolve_perms(role, perms)
    return {"use_all": r["use_all"], "config_all": r["config_all"], "console": r["console"],
            "lifecycle": r["lifecycle"],
            "modules": {m[0]: {"use": r["can_use"](m[0]), "config": r["can_config"](m[0])}
                        for m in CONTROL_MODULES}}


def sanitize_perms(p):
    """Coerce an incoming perms object from the UI into the stored shape (or None to clear)."""
    if not isinstance(p, dict):
        return None
    mods = {}
    for mid, e in (p.get("modules") or {}).items():
        if mid in _MOD_BY_ID and isinstance(e, dict):
            mods[mid] = {"use": bool(e.get("use")), "config": bool(e.get("config"))}
    return {"use_all": bool(p.get("use_all")), "config_all": bool(p.get("config_all")),
            "modules": mods, "console": bool(p.get("console")), "lifecycle": bool(p.get("lifecycle"))}


def _users(cfg):
    return cfg["raw"].setdefault("settings", {}).setdefault("users", [])


def _invites(cfg):
    return cfg["raw"].setdefault("settings", {}).setdefault("invites", [])


def _owner_username(cfg):
    return (cfg["raw"].get("settings", {}).get("auth", {}) or {}).get("user", "") or ""


def find_user_by_id(cfg, uid):
    for u in _users(cfg):
        if u.get("id") == uid:
            return u
    return None


def find_user_by_name(cfg, username):
    un = (username or "").strip().lower()
    if not un:
        return None
    for u in _users(cfg):
        if (u.get("username") or "").lower() == un:
            return u
    return None


def _validate_new_username(cfg, username):
    un = (username or "").strip()
    if not _USERNAME_RE.match(un):
        raise ValueError("username must be 2–32 chars: letters, digits, _ . -")
    if un.lower() == _owner_username(cfg).lower():
        raise ValueError("that name is the owner account")
    if find_user_by_name(cfg, un):
        raise ValueError("username already exists")
    return un


def new_user(cfg, username, password, role, all_, targets):
    if role not in USER_ROLES:
        raise ValueError("invalid role")
    un = _validate_new_username(cfg, username)
    if not password or len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    salt = secrets.token_hex(16)
    u = {
        "id": secrets.token_hex(4),
        "username": un,
        "salt": salt,
        "hash": _hash_password(password, salt),
        "role": role,
        "all": True if role == "admin" else bool(all_),
        "targets": [] if role == "admin" else (targets or []),
        "pwgen": 0,
        "disabled": False,
        "created": time.time(),
        "last_login": None,
    }
    _users(cfg).append(u)
    save_config(cfg)
    return u


def verify_user(cfg, username, password):
    u = find_user_by_name(cfg, username)
    if not u or u.get("disabled"):
        return None
    calc = _hash_password(password, u.get("salt", ""))
    return u if hmac.compare_digest(calc, u.get("hash", "")) else None


def set_user_password(cfg, uid, password):
    u = find_user_by_id(cfg, uid)
    if not u:
        return False
    if not password or len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    u["salt"] = secrets.token_hex(16)
    u["hash"] = _hash_password(password, u["salt"])
    u["pwgen"] = int(u.get("pwgen", 0)) + 1   # invalidate the user's other live sessions
    save_config(cfg)
    return True


def update_user(cfg, uid, role=None, all_=None, targets=None, disabled=None, perms="__keep__"):
    u = find_user_by_id(cfg, uid)
    if not u:
        return None
    if role is not None:
        if role not in USER_ROLES:
            raise ValueError("invalid role")
        u["role"] = role
        if role == "admin":
            u["all"] = True
            u["targets"] = []
    if all_ is not None and u["role"] != "admin":
        u["all"] = bool(all_)
    if targets is not None and u["role"] != "admin":
        u["targets"] = targets
    if disabled is not None:
        u["disabled"] = bool(disabled)
    if perms != "__keep__":
        if perms is None:
            u.pop("perms", None)                 # clear -> full-within-tier
        else:
            u["perms"] = sanitize_perms(perms)
    save_config(cfg)
    return u


def delete_user(cfg, uid):
    us = _users(cfg)
    before = len(us)
    us[:] = [u for u in us if u.get("id") != uid]
    save_config(cfg)
    return len(us) < before


def touch_user_login(cfg, uid):
    u = find_user_by_id(cfg, uid)
    if u:
        u["last_login"] = time.time()
        save_config(cfg)


def user_public_view(cfg, u):
    """Owner-facing view of a user — never includes the salt/hash. Includes the stored perms (may be
    None = full-within-tier) plus the resolved effective perms for the editor."""
    v = {k: u.get(k) for k in
         ("id", "username", "role", "all", "targets", "disabled", "created", "last_login")}
    v["perms"] = u.get("perms")
    v["effective"] = perms_public(u.get("role", "view"), u.get("perms"))
    return v


def new_invite(cfg, label, role, all_, targets, username, ttl_days):
    if role not in USER_ROLES:
        raise ValueError("invalid role")
    un = (username or "").strip()
    if un:
        _validate_new_username(cfg, un)        # fail now rather than issue a doomed invite
    token = secrets.token_urlsafe(32)
    now = time.time()
    inv = {
        "id": secrets.token_hex(4),
        "token_hash": _share_token_hash(token),
        "label": (label or "").strip()[:80] or "Invite",
        "role": role,
        "all": True if role == "admin" else bool(all_),
        "targets": [] if role == "admin" else (targets or []),
        "username": un or None,
        "created": now,
        "expires": (now + float(ttl_days) * 86400) if ttl_days else None,
        "revoked": False,
        "used_by": None,
    }
    _invites(cfg).append(inv)
    save_config(cfg)
    return inv, token


def _invite_active(inv):
    if inv.get("revoked") or inv.get("used_by"):
        return False
    exp = inv.get("expires")
    if exp is not None and time.time() > exp:
        return False
    return True


def find_invite_by_token(cfg, token):
    th = _share_token_hash(token)
    match = None
    for inv in _invites(cfg):
        if hmac.compare_digest(inv.get("token_hash", ""), th) and _invite_active(inv):
            match = inv
    return match


def find_invite_by_id(cfg, iid):
    for inv in _invites(cfg):
        if inv.get("id") == iid:
            return inv
    return None


def revoke_invite(cfg, iid):
    inv = find_invite_by_id(cfg, iid)
    if not inv:
        return False
    inv["revoked"] = True
    save_config(cfg)
    return True


def invite_status(inv):
    if inv.get("used_by"):
        return "used"
    if inv.get("revoked"):
        return "revoked"
    exp = inv.get("expires")
    if exp is not None and time.time() > exp:
        return "expired"
    return "pending"


def invite_public_view(inv):
    v = {k: inv.get(k) for k in ("id", "label", "role", "all", "targets", "username", "created", "expires")}
    v["status"] = invite_status(inv)
    return v


def redeem_invite(cfg, token, username, password):
    """Consume an active invite → create the user it confers; returns the user record. Raises ValueError."""
    inv = find_invite_by_token(cfg, token)
    if not inv:
        raise ValueError("This invite is invalid, already used, or expired.")
    un = (inv.get("username") or username or "").strip()
    u = new_user(cfg, un, password, inv["role"], inv.get("all"), inv.get("targets"))
    inv["used_by"] = u["id"]
    save_config(cfg)
    return u


# ---------------------------------------------------------------------------
# Public sharing — a pluggable menu of exposure providers
# ---------------------------------------------------------------------------
# A guest share link is only useful if the person can actually reach the
# dashboard. By default the manager is loopback-only (SSH tunnel), so a link
# points at localhost and is useless to anyone else. These providers each give
# the dashboard a public HTTPS address that share_base_url() builds links from.
# We offer a menu so users can pick whatever fits — no account, free account,
# or their own domain:
#
#   cloudflare-quick  no account / no domain, one click; URL re-rolls on restart
#   tailscale         free, sign in once, stable *.ts.net, survives reboots
#   ngrok             free account + authtoken, one reserved *.ngrok-free.app
#   cloudflare-named  your own domain via a Zero-Trust tunnel token; stable host
#   custom            you already run a reverse proxy/domain — just name the URL
#
# Each provider implements start(port, conf)/stop()/status(conf) returning
# {enabled, running, url, error}; _ShareManager picks the active one from
# settings.public_share and feeds its URL to share_base_url(). Secrets in the
# per-provider config are b64-obfuscated at rest (same as the Webshare token).
_AQ_DIR = os.path.join(os.path.expanduser("~"), ".aquarius")
_CF_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_CF_DL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
_CF_BIN = ("/usr/local/bin/cloudflared", "/usr/bin/cloudflared")


class _ProcTunnel:
    """Base for providers that run a long-lived helper process we own. The process is spawned
    detached (its own session) so it survives manager restarts; on the next start() we *adopt*
    the running one (matched by pgrep) instead of respawning, keeping the public URL stable
    across selfupdates. Output is captured to a per-provider logfile we parse for the URL."""
    id = name = blurb = ""
    needs = []                       # [{key,label,secret,placeholder,help}]
    installable = True               # ABM can fetch/set this provider up itself
    bin_names = ()                   # absolute system paths to prefer
    bin_cache = ""                   # ~/.aquarius/<bin> fallback (auto-downloaded)
    download_url = None              # raw binary URL; override ensure_binary for archives
    url_re = None                    # regex to pull the public URL out of the logfile

    def __init__(self):
        self.lock = threading.Lock()
        self.url = None
        self.enabled = False
        self.error = None
        self.port = None
        self._mon = None
        self.logfile = os.path.join(_AQ_DIR, self.id + ".log")

    # ---- binary acquisition -------------------------------------------------
    def bin_path(self):
        for c in self.bin_names:
            if os.path.exists(c):
                return c
        w = shutil.which(os.path.basename(self.bin_cache))
        return w or self.bin_cache

    def installed(self):
        """Is the helper binary present (system or our cached copy)?"""
        p = self.bin_path()
        return bool(p) and os.path.exists(p)

    def install(self, conf=None):
        """Fetch the helper binary now (so 'choose a provider' can set it up before enabling).
        Returns True on success; on failure records self.error and returns False."""
        try:
            self.ensure_binary()
            return True
        except Exception as e:
            with self.lock:
                self.error = (self.name + " download failed: " + str(e))[:200]
            return False

    def ensure_binary(self):
        p = self.bin_path()
        if os.path.exists(p):
            return p
        if not self.download_url:
            raise RuntimeError(self.name + " isn't installed and can't be auto-downloaded.")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".dl"
        with urllib.request.urlopen(self.download_url, timeout=180) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        os.chmod(tmp, 0o755)
        os.replace(tmp, p)
        return p

    # ---- subclass hooks -----------------------------------------------------
    def argv(self, port, binp, conf):
        raise NotImplementedError

    def match_pat(self, port, conf):
        """pgrep -f pattern that uniquely identifies *our* running process for adopt/liveness."""
        raise NotImplementedError

    def stop_pat(self):
        """pkill -f pattern to tear our process(es) down. Must not match other providers."""
        raise NotImplementedError

    def preflight(self, binp, conf):
        """Return an error string to abort start (e.g. missing token), or None to proceed."""
        return None

    def url_now(self, conf):
        return self._url_from_log()

    # ---- shared lifecycle ---------------------------------------------------
    def _url_from_log(self):
        if not self.url_re:
            return None
        try:
            with open(self.logfile, "r", errors="replace") as f:
                found = self.url_re.findall(f.read())
            return found[-1] if found else None
        except OSError:
            return None

    def _pid_for(self, port, conf):
        try:
            r = subprocess.run(["pgrep", "-f", self.match_pat(port, conf)],
                               capture_output=True, text=True, timeout=8)
            pids = [int(x) for x in r.stdout.split() if x.strip().isdigit()]
            return pids[0] if pids else None
        except Exception:
            return None

    def _spawn(self, port, binp, conf):
        os.makedirs(os.path.dirname(self.logfile), exist_ok=True)
        logf = open(self.logfile, "wb")          # truncate — only the current run's output lives here
        try:
            subprocess.Popen(self.argv(port, binp, conf), stdout=logf,
                             stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            logf.close()

    def start(self, port, conf):
        with self.lock:
            self.enabled = True
            self.port = port
            self.error = None
        try:
            binp = self.ensure_binary()
        except Exception as e:
            with self.lock:
                self.error = str(e)[:200]
            return self.status(conf)
        err = self.preflight(binp, conf)
        if err:
            with self.lock:
                self.error = err[:200]
            return self.status(conf)
        # adopt an already-running process (survived a manager restart) instead of respawning
        if self._pid_for(port, conf) is None:
            try:
                self._spawn(port, binp, conf)
            except Exception as e:
                with self.lock:
                    self.error = str(e)[:200]
                return self.status(conf)
        with self.lock:
            self.url = self.url_now(conf)
            if self._mon is None or not self._mon.is_alive():
                self._mon = threading.Thread(target=self._monitor, args=(conf,), daemon=True)
                self._mon.start()
        return self.status(conf)

    def stop(self):
        with self.lock:
            self.enabled = False
            self.url = None
        try:
            subprocess.run(["pkill", "-f", self.stop_pat()], timeout=8)
        except Exception:
            pass
        try:
            os.remove(self.logfile)
        except OSError:
            pass
        return {"enabled": False, "running": False, "url": None, "error": None}

    def status(self, conf):
        with self.lock:
            port, enabled, url, err = self.port, self.enabled, self.url, self.error
        running = bool(enabled and self._pid_for(port, conf))
        if running and not url:                  # lazily refresh (e.g. after adopt-on-restart)
            url = self.url_now(conf)
            if url:
                with self.lock:
                    self.url = url
        return {"enabled": enabled, "running": running, "url": url, "error": err}

    def _monitor(self, conf):
        """Keep self.url fresh; relaunch if our process dies while still enabled."""
        while True:
            with self.lock:
                enabled, port = self.enabled, self.port
            if not enabled:
                return
            u = self.url_now(conf)
            if u:
                with self.lock:
                    self.url = u
            if self._pid_for(port, conf) is None:
                with self.lock:
                    if not self.enabled:
                        return
                    self.url = None
                try:
                    self._spawn(port, self.bin_path(), conf)
                except Exception:
                    pass
                time.sleep(3)
            time.sleep(2)


class CloudflareQuick(_ProcTunnel):
    id = "cloudflare-quick"
    name = "Cloudflare Quick Tunnel"
    blurb = ("No account, no domain — one click. You get a random https://….trycloudflare.com "
             "address. It changes whenever the tunnel restarts (e.g. after a reboot), so create "
             "links fresh when you need them.")
    needs = []
    bin_names = _CF_BIN
    bin_cache = os.path.join(_AQ_DIR, "cloudflared")
    download_url = _CF_DL
    url_re = _CF_URL_RE

    def argv(self, port, binp, conf):
        return [binp, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]

    def match_pat(self, port, conf):
        return f"cloudflared .*--url http://127.0.0.1:{port}"

    def stop_pat(self):
        return "cloudflared tunnel --no-autoupdate --url"


class CloudflareNamed(_ProcTunnel):
    id = "cloudflare-named"
    name = "Cloudflare Tunnel (your domain)"
    blurb = ("Stable custom hostname on a domain you own. In the Cloudflare Zero Trust dashboard "
             "create a tunnel, add a Public Hostname routing to http://127.0.0.1:<this port>, then "
             "paste the tunnel token + that hostname here. Survives reboots; valid cert.")
    needs = [
        {"key": "token", "label": "Tunnel token", "secret": True,
         "help": "Zero Trust → Networks → Tunnels → your tunnel → Configure → copy the token "
                 "(the long string in the install command)."},
        {"key": "hostname", "label": "Public hostname", "secret": False,
         "placeholder": "bots.example.com",
         "help": "The Public Hostname you routed this tunnel to (it must point at "
                 "http://127.0.0.1:<this dashboard's port>)."},
    ]
    bin_names = _CF_BIN
    bin_cache = os.path.join(_AQ_DIR, "cloudflared")
    download_url = _CF_DL

    def argv(self, port, binp, conf):
        return [binp, "tunnel", "--no-autoupdate", "run", "--token", _b64dec(conf.get("token", ""))]

    def match_pat(self, port, conf):
        return "cloudflared tunnel .*run --token"

    def stop_pat(self):
        return "cloudflared tunnel .*run --token"

    def url_now(self, conf):
        h = (conf.get("hostname") or "").strip().replace("https://", "").replace("http://", "").strip("/")
        return ("https://" + h) if h else None

    def preflight(self, binp, conf):
        if not _b64dec(conf.get("token", "")):
            return "Paste your Cloudflare tunnel token first."
        if not (conf.get("hostname") or "").strip():
            return "Enter the public hostname you configured for this tunnel."
        return None


class Ngrok(_ProcTunnel):
    id = "ngrok"
    name = "ngrok"
    blurb = ("Free account + authtoken. Reserve one free static domain (*.ngrok-free.app) in the "
             "ngrok dashboard so the address stays the same across restarts. NA + EU regions.")
    needs = [
        {"key": "authtoken", "label": "ngrok authtoken", "secret": True,
         "help": "ngrok dashboard → Your Authtoken."},
        {"key": "domain", "label": "Static domain", "secret": False,
         "placeholder": "your-name.ngrok-free.app",
         "help": "Reserve a free static domain in the ngrok dashboard (Universal Gateway → Domains) "
                 "and paste it here. Leave blank for a random URL that changes each restart."},
    ]
    bin_names = ("/usr/local/bin/ngrok", "/usr/bin/ngrok")
    bin_cache = os.path.join(_AQ_DIR, "ngrok")
    _DL = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz"

    def ensure_binary(self):
        p = self.bin_path()
        if os.path.exists(p):
            return p
        os.makedirs(os.path.dirname(p), exist_ok=True)
        import tarfile, io
        with urllib.request.urlopen(self._DL, timeout=180) as r:
            data = r.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = next((m for m in t.getmembers() if os.path.basename(m.name) == "ngrok"), None)
            if member is None:
                raise RuntimeError("ngrok binary not found in download archive")
            with t.extractfile(member) as src, open(p + ".dl", "wb") as f:
                shutil.copyfileobj(src, f)
        os.chmod(p + ".dl", 0o755)
        os.replace(p + ".dl", p)
        return p

    def argv(self, port, binp, conf):
        a = [binp, "http", "--log", "stdout", "--log-format", "logfmt"]
        dom = (conf.get("domain") or "").strip().replace("https://", "").replace("http://", "").strip("/")
        if dom:
            a += ["--url", "https://" + dom]
        a += [str(port)]
        return a

    def match_pat(self, port, conf):
        return f"ngrok http .* {port}$"

    def stop_pat(self):
        return "ngrok http "

    def preflight(self, binp, conf):
        tok = _b64dec(conf.get("authtoken", ""))
        if not tok:
            return "Paste your ngrok authtoken first."
        try:
            subprocess.run([binp, "config", "add-authtoken", tok], capture_output=True, timeout=20)
        except Exception as e:
            return "ngrok authtoken setup failed: " + str(e)[:120]
        return None

    def url_now(self, conf):
        dom = (conf.get("domain") or "").strip().replace("https://", "").replace("http://", "").strip("/")
        if dom:
            return "https://" + dom
        try:                                      # random URL — read it from ngrok's local API
            with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=4) as r:
                d = json.load(r)
            for t in d.get("tunnels", []):
                if (t.get("public_url") or "").startswith("https"):
                    return t["public_url"]
        except Exception:
            pass
        return None


class Tailscale:
    """Tailscale Funnel — ABM installs and runs it itself, no root required. We download the static
    Linux binaries into ~/.aquarius and run `tailscaled` in **userspace-networking** mode against our
    own socket/state dir (so it needs no /dev/net/tun and no system service). The user signs in once
    via a login URL we surface; tailscaled then persists that login + the funnel config across
    reboots, giving a stable https://<machine>.<tailnet>.ts.net with a valid cert."""
    id = "tailscale"
    name = "Tailscale Funnel"
    blurb = ("Free, no domain. ABM installs Tailscale for you (no root) and runs it in userspace; you "
             "sign in once via a link, then get a stable https://<machine>.<tailnet>.ts.net address "
             "with a valid cert that survives reboots. The best set-and-forget option without a domain.")
    needs = []
    installable = True
    _PKGS = "https://pkgs.tailscale.com/stable/"
    _STATE = os.path.join(_AQ_DIR, "ts-state")
    _SOCK = os.path.join(_AQ_DIR, "tailscaled.sock")
    _LOG = os.path.join(_AQ_DIR, "tailscaled.log")
    _FLOG = os.path.join(_AQ_DIR, "ts-funnel.log")

    _FUNNEL_URL_RE = re.compile(r"https://login\.tailscale\.com/f/funnel\S*")

    def __init__(self):
        self.lock = threading.Lock()
        self.enabled = False
        self.error = None
        self.auth_url = None
        self.funnel_url = None          # tailnet "enable Funnel" URL (a separate one-time step)
        self.port = None
        self._mon = None
        self._worker = None
        self.installing = False
        self._last_funnel_try = 0.0

    def _bin(self, name):
        for c in ("/usr/bin/" + name, "/usr/local/bin/" + name):
            if os.path.exists(c):
                return c
        w = shutil.which(name)
        return w or os.path.join(_AQ_DIR, name)

    def installed(self):
        return os.path.exists(self._bin("tailscale")) and os.path.exists(self._bin("tailscaled"))

    def install(self, conf=None):
        """Download the static tailscale + tailscaled binaries into ~/.aquarius (no root)."""
        if self.installed():
            return True
        import tarfile, io
        try:
            with urllib.request.urlopen(self._PKGS + "?mode=json", timeout=30) as r:
                meta = json.load(r)
            fname = (meta.get("Tarballs") or {}).get("amd64")
            if not fname:
                raise RuntimeError("could not resolve the latest Tailscale version")
            with urllib.request.urlopen(self._PKGS + fname, timeout=300) as r:
                data = r.read()
            os.makedirs(_AQ_DIR, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
                for want in ("tailscale", "tailscaled"):
                    m = next((x for x in t.getmembers()
                              if os.path.basename(x.name) == want and x.isfile()), None)
                    if not m:
                        raise RuntimeError(want + " not found in the Tailscale archive")
                    dst = os.path.join(_AQ_DIR, want)
                    with t.extractfile(m) as src, open(dst + ".dl", "wb") as f:
                        shutil.copyfileobj(src, f)
                    os.chmod(dst + ".dl", 0o755)
                    os.replace(dst + ".dl", dst)
            with self.lock:
                self.error = None
            return True
        except Exception as e:
            with self.lock:
                self.error = ("Tailscale install failed: " + str(e))[:200]
            return False

    def _daemon_pid(self):
        try:
            r = subprocess.run(["pgrep", "-f", "tailscaled .*" + re.escape(self._SOCK)],
                               capture_output=True, text=True, timeout=8)
            pids = [int(x) for x in r.stdout.split() if x.strip().isdigit()]
            return pids[0] if pids else None
        except Exception:
            return None

    def _ensure_daemon(self):
        if not self.installed():
            return False
        if self._daemon_pid():
            return True
        os.makedirs(self._STATE, exist_ok=True)
        logf = open(self._LOG, "ab")
        try:
            subprocess.Popen([self._bin("tailscaled"), "--tun=userspace-networking",
                              "--statedir=" + self._STATE, "--socket=" + self._SOCK],
                             stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            logf.close()
        for _ in range(24):                     # wait briefly for the control socket
            if os.path.exists(self._SOCK):
                break
            time.sleep(0.25)
        return True

    def _ts_args(self, *a):
        return [self._bin("tailscale"), "--socket=" + self._SOCK] + list(a)

    def _status_json(self):
        try:
            r = subprocess.run(self._ts_args("status", "--json"), capture_output=True, text=True, timeout=10)
            return json.loads(r.stdout or "{}")
        except Exception:
            return {}

    def _funnel_on(self, dn):
        try:
            r = subprocess.run(self._ts_args("funnel", "status"), capture_output=True, text=True, timeout=10)
            return bool(dn) and dn in ((r.stdout or "") + (r.stderr or ""))
        except Exception:
            return False

    def status(self, conf):
        if not self.installed():
            return {"enabled": self.enabled, "running": False, "url": None, "installed": False,
                    "error": self.error or "Not set up yet — choose Set up to install Tailscale."}
        if not self._daemon_pid():
            return {"enabled": self.enabled, "running": False, "url": None, "installed": True,
                    "error": self.error}
        d = self._status_json()
        state = d.get("BackendState")
        if state in (None, "NoState", "NeedsLogin"):
            return {"enabled": self.enabled, "running": False, "url": None, "installed": True,
                    "needs_login": True, "auth_url": self.auth_url,
                    "error": self.error or "Sign in to Tailscale to finish (open the link below)."}
        dn = ((d.get("Self") or {}).get("DNSName") or "").rstrip(".")
        url = ("https://" + dn) if dn else None
        on = self._funnel_on(dn)                # operational truth; _ShareManager ANDs with settings.enabled
        if on:                                  # live — drop any stale enable-Funnel guidance
            with self.lock:
                self.funnel_url = None
                if self.error and "Funnel" in self.error:
                    self.error = None
        else:
            self._read_funnel_log()             # refresh the enable URL if the tailnet hasn't enabled Funnel
        out = {"enabled": self.enabled, "running": bool(on), "url": url if on else None,
               "installed": True, "error": self.error}
        if not on and self.funnel_url:          # signed in, but the tailnet must enable Funnel (one-time)
            out["needs_funnel"] = True
            out["funnel_url"] = self.funnel_url
        return out

    def start(self, port, conf):
        # NON-BLOCKING: everything slow (download, daemon, `tailscale up`, funnel) runs in a background
        # worker so the HTTP request returns instantly; the UI polls status for progress/login/URL.
        with self.lock:
            self.enabled = True
            self.error = None
            self.port = port
        self._start_worker()
        return self.status(conf)

    def _start_worker(self):
        with self.lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run, daemon=True)
            self._worker.start()

    def _run(self):
        if not self.installed():
            with self.lock:
                self.installing = True
            try:
                ok = self.install()
            finally:
                with self.lock:
                    self.installing = False
            if not ok:
                return
        self._ensure_daemon()
        d = self._status_json()
        if d.get("BackendState") in (None, "NoState", "NeedsLogin"):
            # kick off login and capture the auth URL for the UI (returns after the short timeout)
            try:
                r = subprocess.run(self._ts_args("up", "--hostname=aquarius-bots", "--timeout=8s"),
                                   capture_output=True, text=True, timeout=25)
                out = (r.stderr or "") + (r.stdout or "")
                mu = re.search(r"https://login\.tailscale\.com/\S+", out)
                if mu:
                    with self.lock:
                        self.auth_url = mu.group(0)
            except Exception:
                pass
        self._monitor()        # loops while enabled: brings the funnel up once signed in

    def _kill_stale_funnel(self):
        try:
            subprocess.run(["pkill", "-f", "tailscale .*funnel --bg"], timeout=6)
        except Exception:
            pass

    def _funnel_running(self):
        try:
            r = subprocess.run(["pgrep", "-f", "tailscale .*funnel --bg"],
                               capture_output=True, text=True, timeout=6)
            return any(x.strip().isdigit() for x in r.stdout.split())
        except Exception:
            return False

    def _funnel_up(self, port):
        """Spawn `tailscale funnel --bg <port>` DETACHED, capturing its output. We don't wait on it:
        the first run blocks for a long time provisioning the HTTPS cert, and if the tailnet hasn't
        enabled Funnel it prints an enable URL — both are read from the log, never via a blocking
        timeout (which is what produced the old "command timed-out")."""
        self._kill_stale_funnel()              # never let attempts pile up
        with self.lock:
            self._last_funnel_try = time.time()
        try:
            logf = open(self._FLOG, "wb")      # truncate — only this attempt's output matters
            try:
                subprocess.Popen(self._ts_args("funnel", "--bg", str(port)),
                                 stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
            finally:
                logf.close()
        except Exception as e:
            with self.lock:
                self.error = str(e)[:200]

    def _read_funnel_log(self):
        """Pull the 'enable Funnel' URL out of the funnel attempt's log, if the tailnet hasn't enabled it."""
        try:
            with open(self._FLOG, "r", errors="replace") as f:
                out = f.read()
        except OSError:
            return None
        mu = self._FUNNEL_URL_RE.search(out)
        if mu:
            with self.lock:
                self.funnel_url = mu.group(0)
                self.error = ("Funnel isn't enabled on your tailnet yet — open the “Enable Funnel” "
                              "link below (one-time, in your Tailscale admin console).")
            return mu.group(0)
        return None

    def _monitor(self):
        """While enabled and signed in, bring the funnel up automatically. Spawns at most one funnel
        attempt at a time and backs off — Funnel needs a one-time tailnet enable + a slow first cert."""
        while True:
            with self.lock:
                enabled, port, last = self.enabled, self.port, self._last_funnel_try
            if not enabled:
                return
            d = self._status_json()
            if d.get("BackendState") == "Running":
                dn = ((d.get("Self") or {}).get("DNSName") or "").rstrip(".")
                if dn and self._funnel_on(dn):
                    with self.lock:               # we're live — clear any stale guidance
                        self.funnel_url = None
                        self.error = None
                else:
                    self._read_funnel_log()        # surface the enable URL if it was printed
                    if not self._funnel_running() and (time.time() - last) > 15:
                        self._funnel_up(port)
            time.sleep(6)

    def stop(self):
        with self.lock:
            self.enabled = False
        if self.installed() and self._daemon_pid():
            try:
                subprocess.run(self._ts_args("funnel", "--bg", "off"), capture_output=True, timeout=15)
            except Exception:
                try:
                    subprocess.run(self._ts_args("funnel", "reset"), capture_output=True, timeout=15)
                except Exception:
                    pass
        return {"enabled": False, "running": False, "url": None, "error": None}


class CustomUrl:
    """Bring-your-own: the dashboard is already exposed at a public HTTPS address (Caddy, nginx,
    a domain, …). We run nothing — just record the URL so share links build against it."""
    id = "custom"
    name = "My own domain / reverse proxy"
    blurb = ("You already serve this dashboard at a public HTTPS address (Caddy, nginx, a domain). "
             "Tell ABM that address and it builds share links from it. ABM runs nothing here.")
    needs = [
        {"key": "url", "label": "Public base URL", "secret": False,
         "placeholder": "https://bots.example.com",
         "help": "The HTTPS address this dashboard is reachable at from outside — no trailing path."},
    ]
    installable = False              # nothing to install — you bring the URL

    def __init__(self):
        self.enabled = False

    def installed(self):
        return True

    def _url(self, conf):
        return (conf.get("url") or "").strip().rstrip("/")

    def start(self, port, conf):
        self.enabled = True
        u = self._url(conf)
        return {"enabled": True, "running": bool(u), "url": u or None,
                "error": None if u else "Enter your public URL."}

    def stop(self):
        self.enabled = False
        return {"enabled": False, "running": False, "url": None, "error": None}

    def status(self, conf):
        # report operational truth (a URL is configured); _ShareManager ANDs with the settings flag
        u = self._url(conf)
        return {"enabled": self.enabled, "running": bool(u), "url": u or None, "error": None}


class _ShareManager:
    """Owns the provider instances, tracks which one settings.public_share selects, and dispatches
    start/stop/status. Feeds the active provider's public URL to share_base_url()."""
    def __init__(self):
        provs = (CloudflareQuick(), Tailscale(), Ngrok(), CloudflareNamed(), CustomUrl())
        self.providers = {p.id: p for p in provs}
        self.order = [p.id for p in provs]
        self._last_active = None
        self._installing = set()        # provider ids whose (async) install is in flight

    def install_async(self, cfg, pid):
        """Kick off a provider install in the background (downloads can take many seconds — never block
        the HTTP request on them). Returns immediately; status()/catalog() report installing=True until done."""
        prov = self.providers.get(pid)
        if not prov or pid in self._installing:
            return
        conf = self.conf_for(cfg, pid)
        self._installing.add(pid)
        def _do():
            try:
                prov.install(conf)
            finally:
                self._installing.discard(pid)
        threading.Thread(target=_do, daemon=True).start()

    def _ps(self, cfg):
        return (cfg["raw"].get("settings", {}) or {}).get("public_share", {}) or {}

    def active_id(self, cfg):
        pid = self._ps(cfg).get("provider") or "cloudflare-quick"
        return pid if pid in self.providers else "cloudflare-quick"

    def conf_for(self, cfg, pid):
        return ((self._ps(cfg).get("providers", {}) or {}).get(pid, {}) or {})

    def enabled(self, cfg):
        return bool(self._ps(cfg).get("enabled"))

    def status(self, cfg):
        pid = self.active_id(cfg)
        p = self.providers[pid]
        st = p.status(self.conf_for(cfg, pid))
        st["provider"] = pid
        st["enabled"] = self.enabled(cfg)
        st["running"] = bool(st.get("running") and self.enabled(cfg))
        st["installing"] = (pid in self._installing) or bool(getattr(p, "installing", False))
        return st

    def _public_conf(self, p, conf):
        d = {}
        for n in getattr(p, "needs", []):
            if n.get("secret"):
                d[n["key"] + "_set"] = bool(conf.get(n["key"]))
            else:
                d[n["key"]] = conf.get(n["key"]) or ""
        return d

    def catalog(self, cfg):
        out = []
        for pid in self.order:
            p = self.providers[pid]
            try:
                inst = p.installed()
            except Exception:
                inst = True
            out.append({"id": pid, "name": p.name, "blurb": p.blurb,
                        "needs": [dict(n) for n in getattr(p, "needs", [])],
                        "installable": bool(getattr(p, "installable", True)),
                        "installed": bool(inst),
                        "installing": (pid in self._installing) or bool(getattr(p, "installing", False)),
                        "config": self._public_conf(p, self.conf_for(cfg, pid))})
        return out

    def reconcile(self, cfg, port, restart_active=False):
        """Bring reality in line with settings: stop the provider we switched away from, then
        start/stop the active one per the enabled flag. restart_active forces a stop+start (used
        after a config change so a new domain/token actually takes effect)."""
        active = self.active_id(cfg)
        if self._last_active and self._last_active != active:
            try:
                self.providers[self._last_active].stop()
            except Exception:
                pass
        self._last_active = active
        p = self.providers[active]
        if not self.enabled(cfg):
            return p.stop()
        if restart_active:
            try:
                p.stop()
            except Exception:
                pass
        return p.start(port, self.conf_for(cfg, active))

    def base_url(self, cfg, fallback):
        if not self.enabled(cfg):
            return fallback
        st = self.status(cfg)
        return st["url"] if st.get("running") and st.get("url") else fallback


SHARE = _ShareManager()


def share_base_url(handler):
    """Public base URL for share links: the active exposure provider's URL if it's up, else the
    owner's current scheme+host (works when they're already on a public access mode)."""
    proto = handler.headers.get("X-Forwarded-Proto") or "http"
    host = handler.headers.get("Host") or "this-host"
    fallback = f"{proto}://{host}"
    try:
        cfg = handler._cfg()
    except Exception:
        return fallback
    return SHARE.base_url(cfg, fallback)


# ---------------------------------------------------------------------------
# Connection info  (for the dashboard's reconnect panel + tunnel shortcut)
# ---------------------------------------------------------------------------

_PUBLIC_IP_CACHE = None


def public_ip():
    """Best-effort public IP of this VPS (cached). '' if it can't be determined."""
    global _PUBLIC_IP_CACHE
    if _PUBLIC_IP_CACHE is not None:
        return _PUBLIC_IP_CACHE
    ip = ""
    try:
        import urllib.request
        req = urllib.request.Request("https://api.ipify.org",
                                     headers={"User-Agent": "aquarius-bot-manager"})
        with urllib.request.urlopen(req, timeout=5) as r:
            ip = r.read().decode().strip()
    except Exception:
        ip = ""
    _PUBLIC_IP_CACHE = ip
    return ip


def _run_user():
    return (os.environ.get("SUDO_USER") or os.environ.get("USER")
            or os.environ.get("USERNAME") or getpass.getuser() or "ubuntu")


def reconnect_script(ostype, ip, user, port):
    """Generate a one-double-click reconnect helper for the user's LOCAL machine:
    open the SSH tunnel to the VPS, then open the dashboard in a browser.
    Returns (filename, mimetype, text)."""
    ip = (ip or "").strip() or "YOUR_VPS_IP"
    user = (user or _run_user()).strip()
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 8765
    fwd = f"{port}:127.0.0.1:{port}"
    url = f"http://localhost:{port}"
    if ostype == "windows":
        text = ("@echo off\r\n"
                "REM Aquarius Bot Manager - reconnect (open SSH tunnel + dashboard)\r\n"
                f'start "" ssh -N -L {fwd} {user}@{ip}\r\n'
                "timeout /t 2 >nul\r\n"
                f'start "" {url}\r\n')
        return "reconnect-aquarius.bat", "application/octet-stream", text
    # mac (.command) and linux (.sh) share a body; opener differs
    opener = "open" if ostype == "mac" else "xdg-open"
    name = "reconnect-aquarius.command" if ostype == "mac" else "reconnect-aquarius.sh"
    text = ("#!/bin/bash\n"
            "# Aquarius Bot Manager - reconnect (open SSH tunnel + dashboard)\n"
            f"ssh -fNL {fwd} {user}@{ip} 2>/dev/null || true\n"
            "sleep 1\n"
            f"{opener} {url} >/dev/null 2>&1 &\n")
    return name, "application/octet-stream", text


def multi_reconnect_script(ostype, conn, nodes):
    """A launcher that opens one SSH tunnel per box (the controller + each node) on
    distinct local ports and opens every dashboard — the direct-access fallback for
    when the controller itself isn't up. Returns (filename, mimetype, text)."""
    cip = (conn.get("ip") or "").strip() or "CONTROLLER_VPS_IP"
    cuser = (conn.get("user") or _run_user()).strip()
    try:
        base = int(conn.get("port") or 8765)
    except (TypeError, ValueError):
        base = 8765
    # rows: (user, host, ssh_port, remote_port, local_port)
    rows = [(cuser, cip, 22, base, base)]
    lp = base
    for n in nodes:
        lp += 1
        rows.append((n.get("ssh_user") or "ubuntu", n.get("ssh_host") or "NODE_IP",
                     int(n.get("ssh_port") or 22), int(n.get("remote_port") or 8765), lp))
    if ostype == "windows":
        out = ["@echo off",
               "REM Aquarius Bot Manager - open every box's tunnel + dashboard"]
        for u, h, sp, rp, l in rows:
            out.append(f'start "" ssh -N -p {sp} -L {l}:127.0.0.1:{rp} {u}@{h}')
        out.append("timeout /t 2 >nul")
        for u, h, sp, rp, l in rows:
            out.append(f'start "" http://localhost:{l}')
        return "reconnect-all-aquarius.bat", "application/octet-stream", "\r\n".join(out) + "\r\n"
    opener = "open" if ostype == "mac" else "xdg-open"
    name = "reconnect-all-aquarius.command" if ostype == "mac" else "reconnect-all-aquarius.sh"
    out = ["#!/bin/bash", "# Aquarius Bot Manager - open every box's tunnel + dashboard"]
    for u, h, sp, rp, l in rows:
        out.append(f"ssh -fNL {l}:127.0.0.1:{rp} -p {sp} {u}@{h} 2>/dev/null || true")
    out.append("sleep 1")
    for u, h, sp, rp, l in rows:
        out.append(f"{opener} http://localhost:{l} >/dev/null 2>&1 &")
    return name, "application/octet-stream", "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Multi-VPS controller: node registry + SSH tunnels
# ---------------------------------------------------------------------------
# A "node" is another box running the manager. The controller keeps an SSH tunnel
# (ssh -N -L <local_port>:127.0.0.1:<remote_port>) to each node so it can reach the
# node's manager over loopback, then proxies/aggregates it. Nodes stay bound to
# 127.0.0.1 and are never exposed publicly — the SSH key is the only way in.

DEFAULT_NODES = (os.environ.get("ABM_NODES_CONFIG")
                 or os.path.join(SCRIPT_DIR, "nodes.json"))
_LOCAL_PORT_BASE = 8801          # controller-side loopback ports start here


def _b64enc(t):
    """Obfuscate-at-rest (base64). NOT encryption — just not eyeball-plaintext."""
    t = (t or "").strip()
    return "b64:" + base64.b64encode(t.encode()).decode() if t else ""


def _b64dec(s):
    s = s or ""
    if s.startswith("b64:"):
        try:
            return base64.b64decode(s[4:]).decode()
        except Exception:
            return ""
    return s


def load_nodes(path=DEFAULT_NODES):
    if not os.path.exists(path):
        return {"_path": path, "nodes": [], "settings": {}}
    with open(path) as f:
        raw = json.load(f)
    raw.setdefault("nodes", [])
    raw.setdefault("settings", {})
    raw["_path"] = path
    return raw


def do_token_saved(reg):
    """The DigitalOcean API token (decoded), or '' if none saved."""
    return _b64dec(reg.get("settings", {}).get("do_token", ""))


def set_do_token(reg, token):
    reg.setdefault("settings", {})["do_token"] = _b64enc(token)
    save_nodes(reg)


def make_backup(cfg):
    """A portable bundle of this box's configs (instances + connected nodes). Includes
    secrets so a restore is complete — the downloaded file is sensitive."""
    bundle = {"abm_backup": 1, "version": __version__, "created": time.time(),
              "host": socket.gethostname(), "files": {}}
    try:
        with open(cfg["path"]) as f:
            bundle["files"]["instances.json"] = json.load(f)
    except Exception:
        bundle["files"]["instances.json"] = cfg.get("raw")
    if os.path.exists(DEFAULT_NODES):
        try:
            with open(DEFAULT_NODES) as f:
                bundle["files"]["nodes.json"] = json.load(f)
        except Exception:
            pass
    return bundle


def restore_backup(cfg, bundle):
    """Write configs from a backup bundle after snapshotting the current files
    (.pre-restore-<ts>.bak alongside them). Returns a summary."""
    if not isinstance(bundle, dict) or not bundle.get("abm_backup"):
        raise ValueError("not an Aquarius Bot Manager backup file")
    files = bundle.get("files") or {}
    if not files:
        raise ValueError("backup contains no files")
    ts = time.strftime("%Y%m%d-%H%M%S")
    written = []
    for fname, dest in (("instances.json", cfg["path"]), ("nodes.json", DEFAULT_NODES)):
        if fname not in files:
            continue
        if os.path.exists(dest):
            try:
                shutil.copy2(dest, f"{dest}.pre-restore-{ts}.bak")
            except Exception:
                pass
        tmp = dest + ".tmp"
        with open(tmp, "w") as f:
            json.dump(files[fname], f, indent=2)
        os.replace(tmp, dest)
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
        written.append(fname)
    return {"restored": written, "snapshot_suffix": f".pre-restore-{ts}.bak"}


def save_nodes(reg):
    path = reg["_path"]
    out = {k: v for k, v in reg.items() if not k.startswith("_")}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=2)
    os.replace(tmp, path)
    try:                          # creds at rest — restrict perms (best-effort; no-op on Windows)
        os.chmod(path, 0o600)
    except OSError:
        pass


def find_node(reg, name):
    for n in reg["nodes"]:
        if n.get("name") == name:
            return n
    return None


def node_creds(node):
    """Decoded (user, pass) the controller presents to the node's manager when it
    enforces auth. Empty user => the node runs open behind its tunnel."""
    return node.get("basic_user") or "", _b64dec(node.get("basic_pass"))


def _assign_local_port(reg):
    used = {n.get("local_port") for n in reg["nodes"]}
    p = _LOCAL_PORT_BASE
    while p in used:
        p += 1
    return p


def add_node(reg, name, ssh_host, ssh_user="ubuntu", ssh_port=22, remote_port=8765,
             ssh_key=None, basic_user=None, basic_pass=None, local_port=None):
    name = (name or "").strip()
    if not name:
        raise ValueError("node name is required")
    if not (ssh_host or "").strip():
        raise ValueError("ssh_host is required")
    if find_node(reg, name):
        raise ValueError(f"node already exists: {name}")
    node = {
        "name": name,
        "ssh_host": ssh_host.strip(),
        "ssh_user": (ssh_user or "ubuntu").strip(),
        "ssh_port": int(ssh_port or 22),
        "remote_port": int(remote_port or 8765),
        "local_port": int(local_port) if local_port else _assign_local_port(reg),
    }
    if ssh_key:
        node["ssh_key"] = ssh_key.strip()
    if basic_user:
        node["basic_user"] = basic_user.strip()
    if basic_pass:
        node["basic_pass"] = _b64enc(basic_pass)
    reg["nodes"].append(node)
    save_nodes(reg)
    return node


def set_node_label(reg, name, label):
    """Set (or clear) a node's friendly display name. The registry `name` key — used by
    tunnels, targeting and selection — is left untouched. Returns the cleaned label."""
    node = find_node(reg, name)
    if not node:
        raise ValueError(f"no such box: {name}")
    label = clean_box_label(label, default="")
    if label and label != node.get("name"):
        node["label"] = label
    else:
        node.pop("label", None)          # blank or same-as-name => no override
    save_nodes(reg)
    return node.get("label") or node.get("name")


def remove_node(reg, name):
    n = find_node(reg, name)
    if not n:
        raise ValueError(f"no such node: {name}")
    reg["nodes"] = [x for x in reg["nodes"] if x.get("name") != name]
    save_nodes(reg)
    return n


def node_public_view(node, tunnels=None):
    """Registry row for the UI/CLI — never echoes the stored credential."""
    u, _ = node_creds(node)
    row = {
        "name": node.get("name"),
        "label": node.get("label") or node.get("name"),
        "ssh_host": node.get("ssh_host"),
        "ssh_user": node.get("ssh_user"),
        "ssh_port": node.get("ssh_port", 22),
        "remote_port": node.get("remote_port", 8765),
        "local_port": node.get("local_port"),
        "has_creds": bool(u),
        "do": bool(node.get("do_droplet_id")),
    }
    if tunnels is not None:
        st = tunnels.status().get(node.get("name"))
        row["tunnel"] = st or {"alive": False, "pid": None}
    return row


def node_request(node, method, path, body=None, timeout=10):
    """HTTP to a node's manager over its loopback tunnel, adding the node's
    Basic-auth creds when configured. Raises on transport/HTTP error."""
    port = node["local_port"]
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "User-Agent": "abm-controller"}
    u, pw = node_creds(node)
    if u:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{u}:{pw}".encode()).decode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


CONTROLLER_ROW = "(this box)"          # the controller's own row name in the Fleet view

# ---------------------------------------------------------------------------
# cross-box file transfer (scp over the controller's stored SSH creds)
# ---------------------------------------------------------------------------

# A box id of "" / one of these aliases means the controller's own filesystem;
# anything else is a registered node name.
_LOCAL_BOX_IDS = ("", "local", "controller", "this", CONTROLLER_ROW)


def _is_local_box(box):
    return (box or "") in _LOCAL_BOX_IDS


def _scp_opts(node):
    """scp option list for a node, mirroring the tunnel's SSH conventions
    (note: scp wants -P for the port and -i for the key)."""
    opts = ["-o", "ConnectTimeout=15",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes",
            "-P", str(node.get("ssh_port", 22))]
    if node.get("ssh_key"):
        opts += ["-i", node["ssh_key"]]
    return opts


def _scp_remote(node, path):
    return f"{node.get('ssh_user', 'ubuntu')}@{node['ssh_host']}:{path}"


def _run_scp(args, timeout):
    cmd = ["scp", "-r", "-q", "-p"] + args
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError("transfer timed out")
    except FileNotFoundError:
        raise ValueError("scp not found on the controller (install openssh-client)")
    if r.returncode != 0:
        msg = (r.stdout.decode(errors="replace").strip() or f"scp exited {r.returncode}")
        raise ValueError("transfer failed: " + msg[:600])


def _node_roots(node):
    """A node's file-manager roots (asked over its tunnel), for jailing the remote side."""
    try:
        return node_request(node, "GET", "/api/files", timeout=8).get("roots") or []
    except Exception as e:
        raise ValueError(f"couldn't reach box '{node['name']}' to check its allowed roots: {e}")


def _jail_remote(node, path):
    """Best-effort jail of a remote path to the node's roots. scp bypasses the node's
    manager (it writes over raw SSH), so this is a guard against accidents rather than a
    hard boundary — the controller already holds full SSH to the box. Symlinks on the
    node aren't resolved here (no realpath without another round-trip)."""
    if not path:
        raise ValueError("path required")
    # remote boxes are Linux: normalize as POSIX so this is correct regardless of the
    # controller's own OS (and so the path scp sees keeps forward slashes).
    norm = posixpath.normpath(path.replace("\\", "/"))
    for r in _node_roots(node):
        rr = posixpath.normpath(r)
        if norm == rr or norm.startswith(rr + "/"):
            return norm
    raise ValueError(f"path is outside box '{node['name']}'s allowed roots")


def box_roots(cfg, reg, box):
    """The file-manager roots for a box (controller-local or a node), for the UI's
    destination-folder default."""
    if _is_local_box(box):
        return file_roots(cfg)
    node = find_node(reg, box)
    if not node:
        raise ValueError(f"no such box: {box}")
    return _node_roots(node)


def transfer_between(cfg, reg, src_box, src_path, dst_box, dst_dir, timeout=900):
    """Copy a file/folder between two boxes via scp, using the controller's stored SSH
    creds. Either side may be the controller ("" box). Node↔node is relayed through a
    controller temp dir (per-box keys make a single `scp -3` impractical). Each side's
    path is jailed to that box's file_roots."""
    if not src_path or not dst_dir:
        raise ValueError("source path and destination folder are required")
    src_local, dst_local = _is_local_box(src_box), _is_local_box(dst_box)
    src_node = None if src_local else find_node(reg, src_box)
    dst_node = None if dst_local else find_node(reg, dst_box)
    if not src_local and src_node is None:
        raise ValueError(f"no such source box: {src_box}")
    if not dst_local and dst_node is None:
        raise ValueError(f"no such destination box: {dst_box}")

    # both on the controller: a plain jailed copy, no SSH
    if src_local and dst_local:
        roots = file_roots(cfg)
        sp = _resolve_in_roots(src_path, roots)
        dd = _resolve_in_roots(dst_dir, roots)
        if not os.path.exists(sp):
            raise ValueError("source not found")
        if not os.path.isdir(dd):
            raise ValueError("destination is not a directory")
        target = os.path.join(dd, os.path.basename(sp.rstrip("/\\")))
        if os.path.isdir(sp):
            shutil.copytree(sp, target, dirs_exist_ok=True)
        else:
            shutil.copy2(sp, target)
        return {"ok": True, "via": "local copy", "dest": target}

    # jail/normalize each side
    src_path = _resolve_in_roots(src_path, file_roots(cfg)) if src_local else _jail_remote(src_node, src_path)
    if src_local and not os.path.exists(src_path):
        raise ValueError("source not found")
    if dst_local:
        dst_dir = _resolve_in_roots(dst_dir, file_roots(cfg))
        if not os.path.isdir(dst_dir):
            raise ValueError("destination is not a directory")
    else:
        dst_dir = _jail_remote(dst_node, dst_dir)

    if src_local:                                   # controller -> node
        _run_scp(_scp_opts(dst_node) + [src_path, _scp_remote(dst_node, dst_dir)], timeout)
        return {"ok": True, "via": "scp", "direction": f"→ {dst_node['name']}"}
    if dst_local:                                   # node -> controller
        _run_scp(_scp_opts(src_node) + [_scp_remote(src_node, src_path), dst_dir], timeout)
        return {"ok": True, "via": "scp", "direction": f"{src_node['name']} →"}

    # node -> node: stage through a controller temp dir
    tmp = tempfile.mkdtemp(prefix="abm_xfer_")
    try:
        _run_scp(_scp_opts(src_node) + [_scp_remote(src_node, src_path), tmp], timeout)
        staged = [os.path.join(tmp, n) for n in os.listdir(tmp)]
        if not staged:
            raise ValueError("nothing was copied from the source box")
        _run_scp(_scp_opts(dst_node) + [staged[0], _scp_remote(dst_node, dst_dir)], timeout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"ok": True, "via": "scp relay", "direction": f"{src_node['name']} → {dst_node['name']}"}


def fleet_aggregate(cfg, timeout=8):
    """The controller + every node, summarized for the Fleet view. Never raises
    per-row — unreachable boxes are reported as such."""
    rows = []
    box_name = (cfg["raw"].get("settings", {}).get("box_name") or "Controller")
    try:
        ci = []
        for i in cfg["instances"]:
            st = instance_status(i)
            entry = {"name": i.get("name"), "status": st, "stats": None}
            if st == "running":
                try:
                    entry["stats"] = instance_stats(i)
                except Exception:
                    pass
            ci.append(entry)
        rows.append({"name": CONTROLLER_ROW, "label": box_name, "controller": True,
                     "reachable": True, "bots": len(ci),
                     "running": sum(1 for c in ci if c["status"] == "running"),
                     "bot_names": [c["name"] for c in ci],
                     "instances": ci, "host": _sysinfo()})
    except Exception as e:
        rows.append({"name": CONTROLLER_ROW, "label": box_name, "controller": True,
                     "reachable": False, "error": str(e), "bots": 0, "running": 0,
                     "instances": []})
    for n in load_nodes()["nodes"]:
        row = node_public_view(n, TUNNELS)
        row["controller"] = False
        try:
            inst = node_request(n, "GET", "/api/instances", timeout=timeout).get("instances", [])
            row["reachable"] = True
            row["bots"] = len(inst)
            row["running"] = sum(1 for i in inst if i.get("status") == "running")
            row["bot_names"] = [i.get("name") for i in inst]
            row["instances"] = [{"name": i.get("name"), "status": i.get("status"),
                                 "stats": i.get("stats")} for i in inst]
            try:
                row["host"] = node_request(n, "GET", "/api/system/info", timeout=timeout)
            except Exception:
                row["host"] = None
        except Exception as e:
            row.update(reachable=False, error=str(e), bots=0, running=0, instances=[])
        rows.append(row)
    return rows


def fleet_action(cfg, action, targets):
    """start/stop/restart-all across the controller and/or nodes. targets is a list of
    names (CONTROLLER_ROW for this box) or ['all']. Returns per-target results."""
    if action not in ("start", "stop", "restart"):
        raise ValueError("action must be start, stop or restart")
    fn = {"start": start, "stop": stop, "restart": restart}[action]
    reg = load_nodes()
    allnames = [CONTROLLER_ROW] + [n["name"] for n in reg["nodes"]]
    want = allnames if (not targets or "all" in targets) else targets
    results = []
    for name in want:
        if name == CONTROLLER_ROW:
            try:
                results.append({"name": name, "ok": True,
                                "results": {i["name"]: fn(i) for i in cfg["instances"]}})
            except Exception as e:
                results.append({"name": name, "ok": False, "error": str(e)})
            continue
        n = find_node(reg, name)
        if not n:
            results.append({"name": name, "ok": False, "error": "no such node"})
            continue
        try:
            r = node_request(n, "POST", f"/api/{action}_all", timeout=25)
            results.append({"name": name, "ok": True, "results": r.get("results")})
        except Exception as e:
            results.append({"name": name, "ok": False, "error": str(e)})
    return results


def fleet_update(targets):
    """Trigger selfupdate on node targets. The controller is intentionally excluded —
    it has its own update button, and restarting it would drop this request."""
    reg = load_nodes()
    want = [n["name"] for n in reg["nodes"]] if (not targets or "all" in targets) else \
        [t for t in targets if t != CONTROLLER_ROW]
    results = []
    for name in want:
        n = find_node(reg, name)
        if not n:
            results.append({"name": name, "ok": False, "error": "no such node"})
            continue
        try:
            r = node_request(n, "POST", "/api/selfupdate", body={"restart": True}, timeout=60)
            results.append({"name": name, "ok": True, **r})
        except Exception as e:
            results.append({"name": name, "ok": False, "error": str(e)})
    return results


def _port_open(port, host="127.0.0.1", timeout=0.5):
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


# ---- DigitalOcean: connect existing droplets + provision node-mode droplets ----
# Reuses fleet.py's DO client (imported lazily so the manager stays standalone if
# fleet.py isn't present). New droplets install the manager in node mode via cloud-init
# and trust the controller's own SSH key so the tunnel comes up automatically.

DO_NODE_IMAGE = "ubuntu-24-04-x64"
DO_NODE_SIZE = "s-1vcpu-1gb"           # 1GB default; the manager is tiny, the bot's JVM is capped


def _fleet_mod():
    try:
        import fleet
        return fleet
    except Exception as e:
        raise ValueError(f"DigitalOcean support needs fleet.py alongside the manager: {e}")


def controller_ssh_pubkey():
    """The controller's SSH public key text + matching private key path. Reuses an
    existing key if present, else generates an ed25519 one. (None, None) on failure."""
    sshdir = os.path.expanduser("~/.ssh")
    for name in ("id_ed25519", "id_rsa", "id_ecdsa"):
        pub, priv = os.path.join(sshdir, name + ".pub"), os.path.join(sshdir, name)
        if os.path.exists(pub) and os.path.exists(priv):
            with open(pub) as f:
                return f.read().strip(), priv
    try:
        os.makedirs(sshdir, exist_ok=True)
        priv = os.path.join(sshdir, "id_ed25519")
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", priv, "-q"], check=True)
        with open(priv + ".pub") as f:
            return f.read().strip(), priv
    except Exception:
        return None, None


def do_ensure_controller_key(token):
    """Make sure the controller's public key is registered in the DO account; return
    (do_key_id, private_key_path) so new droplets trust it and the tunnel just works."""
    fleet = _fleet_mod()
    pub, priv = controller_ssh_pubkey()
    if not pub:
        raise ValueError("controller has no SSH key and couldn't generate one (install ssh-keygen)")
    body = pub.split()[1] if len(pub.split()) >= 2 else pub
    for k in fleet.do_list_ssh_keys(token):
        kp = k.get("public_key") or ""
        if len(kp.split()) >= 2 and kp.split()[1] == body:
            return k.get("id"), priv
    name = f"aquarius-controller-{socket.gethostname()}"
    created = fleet._do_request(token, "POST", "/account/keys",
                               {"name": name, "public_key": pub})
    return created.get("ssh_key", {}).get("id"), priv


def node_cloud_init(install_url=None):
    """cloud-init that turns a fresh droplet into a node-mode manager (binds 127.0.0.1,
    no Caddy). The controller reaches it over the SSH tunnel it trusts."""
    install_url = install_url or ("https://raw.githubusercontent.com/aquariusnetwork9/"
                                  "Aquarius-Bot-Manager/main/install.sh")
    return ("#cloud-config\n"
            "package_update: true\n"
            "packages: [python3, tmux, unzip, wget, git, curl]\n"
            "runcmd:\n"
            f"  - 'curl -fsSL {install_url} | ABM_ACCESS=node bash'\n")


def do_provision_node(reg, token, name, region, size, log, image=DO_NODE_IMAGE, vpc_uuid=None):
    """Create a node-mode droplet, wait for its IP, register it as an SSH-tunnel node.
    `log` is a callback for progress lines. Returns the node's public view."""
    fleet = _fleet_mod()
    if find_node(reg, name):
        raise ValueError(f"a node named {name} already exists")
    log("ensuring the controller's SSH key is in your DigitalOcean account…")
    key_id, priv = do_ensure_controller_key(token)
    log(f"creating droplet '{name}' ({size}, {region}, {image})…")
    dro = fleet.do_create_droplet(token, name, region, size, image,
                                  ssh_keys=[key_id] if key_id else [],
                                  user_data=node_cloud_init(), tags=["aquarius-node"],
                                  vpc_uuid=vpc_uuid)
    did = dro.get("id")
    log(f"droplet id {did}; waiting for it to become active…")
    pub = ""
    for i in range(72):                # up to ~6 min
        time.sleep(5)
        d = fleet.do_get_droplet(token, did)
        st = (d or {}).get("status", "?")
        if d and st == "active":
            pub, _priv = fleet.droplet_ips(d)
            if pub:
                break
        if i % 3 == 0:
            log(f"  …status={st} ({(i + 1) * 5}s)")
    if not pub:
        raise ValueError(f"droplet created (id {did}) but no public IP after ~6 min — "
                         "connect it later from the droplet list")
    log(f"public IP {pub}; registering as a node (ssh root@{pub})…")
    node = add_node(reg, name, pub, ssh_user="root", ssh_port=22,
                    remote_port=8765, ssh_key=priv)
    node["do_droplet_id"] = did        # so it can be destroyed from here later
    save_nodes(reg)
    log("bringing up the SSH tunnel (cloud-init may still be installing — it self-heals)…")
    try:
        ensure_node_tunnel(node, wait=10)
    except Exception:
        pass
    log("done — the node will come online once cloud-init finishes the install (~1-2 min).")
    return node_public_view(node, TUNNELS)


class ProvisionJob:
    """Tracks one in-flight DigitalOcean provision (runs in a background thread)."""
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"           # idle | running | done | error
        self.name = None
        self.lines = []
        self.node = None
        self.started = None
        self.finished = None

    def snapshot(self):
        with self.lock:
            return {"status": self.status, "name": self.name, "node": self.node,
                    "started": self.started, "finished": self.finished,
                    "output": "".join(self.lines[-400:])}

    def _log(self, msg):
        with self.lock:
            self.lines.append(str(msg).rstrip("\n") + "\n")

    def start(self, name, fn):
        with self.lock:
            if self.status == "running":
                raise ValueError("a provision is already running")
            self.status, self.name, self.lines = "running", name, []
            self.node, self.started, self.finished = None, time.time(), None

        def run():
            try:
                node = fn(self._log)
                with self.lock:
                    self.status, self.node, self.finished = "done", node, time.time()
            except Exception as e:
                self._log(f"[error] {e}")
                with self.lock:
                    self.status, self.finished = "error", time.time()
        threading.Thread(target=run, daemon=True).start()


PROVISION_JOB = ProvisionJob()


def do_connect_existing(reg, token, droplet_id, name=None, ssh_user="root"):
    """Register an already-running DO droplet as an SSH-tunnel node. The droplet must
    already trust an SSH key the controller holds (we don't push keys to existing boxes)."""
    fleet = _fleet_mod()
    d = fleet.do_get_droplet(token, droplet_id)
    if not d:
        raise ValueError(f"droplet {droplet_id} not found")
    pub, _priv = fleet.droplet_ips(d)
    if not pub:
        raise ValueError("droplet has no public IP")
    name = name or d.get("name") or f"droplet-{droplet_id}"
    _pub, priv = controller_ssh_pubkey()
    node = add_node(reg, name, pub, ssh_user=ssh_user or "root", ssh_port=22,
                    remote_port=8765, ssh_key=priv)
    node["do_droplet_id"] = d.get("id")
    save_nodes(reg)
    return node


def do_destroy_node(reg, token, name):
    """Destroy the DigitalOcean droplet backing a node, then remove the node. Only
    works for nodes provisioned/connected here (they carry do_droplet_id)."""
    n = find_node(reg, name)
    if not n:
        raise ValueError(f"no such node: {name}")
    did = n.get("do_droplet_id")
    if not did:
        raise ValueError(f"node '{name}' isn't a DigitalOcean droplet managed here "
                         "(remove it instead — destroying would need its droplet id)")
    fleet = _fleet_mod()
    fleet.do_destroy_droplet(token, did)
    if TUNNELS is not None:
        TUNNELS.drop(name)
    remove_node(reg, name)
    return {"destroyed_droplet": did, "name": name}


class TunnelManager:
    """Maintains one `ssh -N -L` tunnel per registered node and self-heals dead ones.
    Re-reads nodes.json each supervision tick so add/remove takes effect live."""

    def __init__(self, nodes_path=DEFAULT_NODES, interval=15):
        self.nodes_path = nodes_path
        self.interval = interval
        self._procs = {}              # name -> Popen
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._started = False

    def _ssh_cmd(self, node):
        cmd = ["ssh", "-N",
               "-o", "ExitOnForwardFailure=yes",
               "-o", "ConnectTimeout=10",
               "-o", "ServerAliveInterval=30",
               "-o", "ServerAliveCountMax=3",
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "BatchMode=yes",
               "-p", str(node.get("ssh_port", 22)),
               "-L", f"127.0.0.1:{node['local_port']}:127.0.0.1:{node.get('remote_port', 8765)}"]
        if node.get("ssh_key"):
            cmd += ["-i", node["ssh_key"]]
        cmd += [f"{node.get('ssh_user', 'ubuntu')}@{node['ssh_host']}"]
        return cmd

    def ensure(self, node):
        """Spawn the tunnel for `node` if it isn't already running."""
        name = node["name"]
        with self._lock:
            p = self._procs.get(name)
            if p and p.poll() is None:
                return
            if p:
                try:
                    p.wait(timeout=0)        # reap our dead child so it isn't left a zombie
                except Exception:
                    pass
            # A prior manager that exited under systemd KillMode=process leaves its
            # `ssh -N -L 127.0.0.1:<port>` tunnel orphaned (reparented to init) but still
            # holding the loopback port. With ExitOnForwardFailure=yes a fresh tunnel then
            # can't bind, so the supervisor would loop forever spawning dead children while
            # the unmanaged orphan does the forwarding (tunnel reported down, no self-heal
            # if it dies). Reap any stale tunnel on this port first so our managed one binds.
            self._kill_stale(node.get("local_port"))
            try:
                self._procs[name] = subprocess.Popen(
                    self._ssh_cmd(node), stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            except Exception:
                self._procs.pop(name, None)

    def _kill_stale(self, port):
        """SIGTERM any ssh tunnel forwarding this loopback port that we don't own
        (orphaned by a KillMode=process restart), so a fresh managed tunnel can bind."""
        if not port:
            return
        ours = {pp.pid for pp in self._procs.values() if pp and pp.poll() is None}
        try:
            out = subprocess.run(["pgrep", "-f", f"ssh -N.*-L 127.0.0.1:{port}:"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=5).stdout.decode()
        except Exception:
            return
        for tok in out.split():
            try:
                pid = int(tok)
            except ValueError:
                continue
            if pid in ours:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass

    def drop(self, name):
        with self._lock:
            p = self._procs.pop(name, None)
        if p:
            try:
                p.terminate()
            except Exception:
                pass

    def status(self):
        with self._lock:
            return {name: {"pid": p.pid, "alive": p.poll() is None}
                    for name, p in self._procs.items()}

    def start(self):
        if self._started:
            return
        self._started = True
        reg = load_nodes(self.nodes_path)
        for n in reg["nodes"]:
            self.ensure(n)
        threading.Thread(target=self._supervise, daemon=True).start()

    def _supervise(self):
        while not self._stop.wait(self.interval):
            try:
                reg = load_nodes(self.nodes_path)
            except Exception:
                continue
            names = {n["name"] for n in reg["nodes"]}
            for gone in [n for n in self.status() if n not in names]:
                self.drop(gone)
            for n in reg["nodes"]:
                self.ensure(n)

    def stop(self):
        self._stop.set()
        for name in list(self.status()):
            self.drop(name)


# the live tunnel supervisor (set by serve() when nodes are registered)
TUNNELS = None


# ---------------------------------------------------------------------------
# Automation scheduler (v1.6): time-based jobs (cron / interval / daily) and an
# on-crash watchdog, targeting this box or any connected node, with optional
# Discord notifications. Job *definitions* persist in settings.schedules; runtime
# state (next/last run, watchdog attempts) is kept in-memory by the Scheduler.
# ---------------------------------------------------------------------------
SCHED_ACTIONS = ("restart", "start", "stop", "command")


def _cron_field(expr, lo, hi):
    """Parse one cron field (supports '*', 'a', 'a-b', 'a,b', '*/n', 'a-b/n') -> set of ints."""
    out = set()
    for part in str(expr).split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = max(1, int(s))
        if part in ("*", ""):
            seq = range(lo, hi + 1, step)
        elif "-" in part:
            a, b = part.split("-", 1)
            seq = range(int(a), int(b) + 1, step)
        else:
            v = int(part)
            seq = range(v, v + 1)
        for v in seq:
            if lo <= v <= hi:
                out.add(v)
    if not out:
        raise ValueError(f"empty cron field: {expr!r}")
    return out


def parse_cron(spec):
    """'min hour dom mon dow' -> tuple of 5 sets (dow 0=Sun..6=Sat, 7=Sun too). Raises ValueError."""
    f = spec.split()
    if len(f) != 5:
        raise ValueError("cron needs 5 fields: 'min hour day month weekday'")
    dows = _cron_field(f[4], 0, 7)
    if 7 in dows:
        dows = (dows - {7}) | {0}
    return (_cron_field(f[0], 0, 59), _cron_field(f[1], 0, 23),
            _cron_field(f[2], 1, 31), _cron_field(f[3], 1, 12), dows)


def _cron_match(parsed, t):
    lt = time.localtime(t)
    mins, hrs, doms, mons, dows = parsed
    return (lt.tm_min in mins and lt.tm_hour in hrs and lt.tm_mon in mons
            and lt.tm_mday in doms and ((lt.tm_wday + 1) % 7) in dows)


def normalize_when(when):
    """Accept 'every:Nm|Nh|Nd', 'daily:HH:MM', or a 5-field cron. Returns ('interval', secs)
    or ('cron', parsed). Raises ValueError on bad input."""
    w = (when or "").strip()
    if w.startswith("every:"):
        v = w[6:].strip().lower()
        unit = v[-1] if v and v[-1] in "smhd" else "m"
        num = int(v[:-1] if v and v[-1] in "smhd" else v)
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        secs = max(30, num * mult)
        return ("interval", secs)
    if w.startswith("daily:"):
        hh, mm = w[6:].split(":")
        return ("cron", parse_cron(f"{int(mm)} {int(hh)} * * *"))
    return ("cron", parse_cron(w))


def next_fire(when, after_ts, last_run=None):
    """Next epoch time a time-trigger should fire strictly after `after_ts`."""
    kind, val = normalize_when(when)
    if kind == "interval":
        base = last_run if last_run else after_ts
        nxt = base + val
        return nxt if nxt > after_ts else after_ts + val
    t = (int(after_ts) // 60) * 60 + 60          # next minute boundary
    for _ in range(367 * 24 * 60):               # search up to ~1 year
        if _cron_match(val, t):
            return t
        t += 60
    raise ValueError("cron schedule never matches")


def validate_schedule(sched):
    """Validate + normalize a settings.schedules object. Returns the cleaned dict."""
    if not isinstance(sched, dict):
        raise ValueError("schedules must be an object")
    hook = (sched.get("notify_webhook") or "").strip()
    if hook and not re.match(r"^https://", hook):
        raise ValueError("notify_webhook must be an https URL")
    jobs = sched.get("jobs") or []
    if not isinstance(jobs, list):
        raise ValueError("schedules.jobs must be a list")
    clean = []
    for j in jobs:
        if not isinstance(j, dict):
            raise ValueError("each job must be an object")
        trigger = j.get("trigger", "time")
        if trigger not in ("time", "on_crash"):
            raise ValueError("job trigger must be 'time' or 'on_crash'")
        action = j.get("action", "restart")
        if action not in SCHED_ACTIONS:
            raise ValueError(f"job action must be one of {', '.join(SCHED_ACTIONS)}")
        cj = {
            "id": str(j.get("id") or ("j_" + secrets.token_hex(4))),
            "name": str(j.get("name", "")).strip(),
            "enabled": bool(j.get("enabled", True)),
            "trigger": trigger,
            "box": str(j.get("box", "")).strip(),            # "" = this box, "*" = all, else node name
            "target": str(j.get("target", "all")).strip() or "all",   # "all" or a bot name
            "action": action,
            "command": str(j.get("command", "")).strip(),
            "notify": bool(j.get("notify", False)),
        }
        if action == "command" and not cj["command"]:
            raise ValueError("a 'command' job needs a command")
        if trigger == "time":
            cj["when"] = str(j.get("when", "")).strip()
            next_fire(cj["when"], time.time())              # validates the schedule
        else:
            cj["max_tries"] = max(1, int(j.get("max_tries", 3)))
            cj["cooldown"] = max(10, int(j.get("cooldown", 60)))
        clean.append(cj)
    return {"notify_webhook": hook, "jobs": clean}


def _discord_notify(webhook, text):
    if not webhook:
        return
    try:
        data = json.dumps({"content": text[:1900]}).encode()
        req = urllib.request.Request(webhook, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass


class Scheduler:
    """Background runner for settings.schedules. Re-reads the config each tick so edits
    take effect live; never throws out of the loop (a bad job is reported, not fatal)."""

    def __init__(self, cfg_path, interval=30):
        self.cfg_path = cfg_path
        self.interval = interval
        self._stop = threading.Event()
        self._started = False
        self._rt = {}            # job id -> {next_run,last_run,last_result,tries}
        self._lock = threading.Lock()

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        # prime next_run for time jobs so we don't fire everything immediately on boot
        self._reschedule_all()
        while not self._stop.wait(self.interval):
            try:
                self._tick()
            except Exception as e:
                print(f"[scheduler] tick error: {e}")

    def _reschedule_all(self):
        try:
            sched = validate_schedule(load_config(self.cfg_path)["raw"]
                                      .get("settings", {}).get("schedules", {}))
        except Exception:
            return
        now = time.time()
        with self._lock:
            for j in sched["jobs"]:
                if j["trigger"] == "time":
                    rt = self._rt.setdefault(j["id"], {})
                    rt["next_run"] = next_fire(j["when"], now, rt.get("last_run"))

    def snapshot(self):
        with self._lock:
            return {k: dict(v) for k, v in self._rt.items()}

    def run_now(self, cfg, job_id):
        sched = validate_schedule(cfg["raw"].get("settings", {}).get("schedules", {}))
        job = next((j for j in sched["jobs"] if j["id"] == job_id), None)
        if not job:
            raise ValueError("no such job")
        return self._fire(cfg, job, sched.get("notify_webhook", ""), manual=True)

    def _tick(self):
        cfg = load_config(self.cfg_path)
        sched = validate_schedule(cfg["raw"].get("settings", {}).get("schedules", {}))
        hook = sched.get("notify_webhook", "")
        now = time.time()
        ids = {j["id"] for j in sched["jobs"]}
        with self._lock:                       # forget runtime for deleted jobs
            for gone in [k for k in self._rt if k not in ids]:
                self._rt.pop(gone, None)
        for job in sched["jobs"]:
            if not job.get("enabled"):
                continue
            try:
                if job["trigger"] == "time":
                    rt = self._rt.setdefault(job["id"], {})
                    if "next_run" not in rt:
                        rt["next_run"] = next_fire(job["when"], now, rt.get("last_run"))
                    if now >= rt["next_run"]:
                        self._fire(cfg, job, hook)
                        rt["last_run"] = now
                        rt["next_run"] = next_fire(job["when"], now, now)
                else:
                    self._watchdog(cfg, job, hook, now)
            except Exception as e:
                self._rt.setdefault(job["id"], {})["last_result"] = f"error: {e}"

    def _statuses(self, cfg, box):
        """Map bot name -> status for a box ('' = this box, else node name)."""
        if not box or box == "(this box)":
            return {i["name"]: instance_status(i) for i in cfg["instances"]}
        node = next((n for n in load_nodes()["nodes"] if n["name"] == box), None)
        if not node:
            return {}
        try:
            inst = node_request(node, "GET", "/api/instances", timeout=8).get("instances", [])
            return {i.get("name"): i.get("status") for i in inst}
        except Exception:
            return {}

    def _boxes_for(self, job):
        if job["box"] == "*":
            return [""] + [n["name"] for n in load_nodes()["nodes"]]
        return [job["box"]]

    def _watchdog(self, cfg, job, hook, now):
        for box in self._boxes_for(job):
            statuses = self._statuses(cfg, box)
            targets = ([job["target"]] if job["target"] != "all" else list(statuses))
            for bot in targets:
                st = statuses.get(bot)
                key = f"{job['id']}@{box}/{bot}"
                rt = self._rt.setdefault(key, {})
                if st == "running":
                    rt["tries"] = 0          # healthy -> reset
                    continue
                if st != "crashed":
                    continue
                if rt.get("tries", 0) >= job["max_tries"]:
                    continue
                if now - rt.get("last_run", 0) < job["cooldown"]:
                    continue
                rt["tries"] = rt.get("tries", 0) + 1
                rt["last_run"] = now
                res = self._dispatch(cfg, box, bot, "restart", "")
                rt["last_result"] = res
                self._rt.setdefault(job["id"], {})["last_result"] = (
                    f"watchdog: {bot} {res} (try {rt['tries']}/{job['max_tries']})")
                if job.get("notify"):
                    _discord_notify(hook, f"🔁 watchdog restarted **{bot}**"
                                    + (f" on {box}" if box else "")
                                    + f" (try {rt['tries']}/{job['max_tries']}) — {res}")

    def _fire(self, cfg, job, hook, manual=False):
        results = []
        for box in self._boxes_for(job):
            statuses = self._statuses(cfg, box)
            targets = ([job["target"]] if job["target"] != "all" else list(statuses))
            for bot in targets:
                results.append(f"{bot}:{self._dispatch(cfg, box, bot, job['action'], job['command'])}")
        summary = ("manual " if manual else "") + f"{job['action']} -> " + ", ".join(results or ["(no targets)"])
        self._rt.setdefault(job["id"], {})["last_result"] = summary
        if job.get("notify"):
            label = job.get("name") or job["action"]
            _discord_notify(hook, f"⏱ **{label}** ran — {summary}")
        return summary

    def _dispatch(self, cfg, box, bot, action, command):
        """Run one action on one bot, locally or on a node. Returns a short status string."""
        try:
            if not box or box == "(this box)":
                inst = cfg["by_name"].get(bot)
                if not inst:
                    return "no-such-bot"
                if action == "command":
                    send_command(inst, command)
                    return "sent"
                return {"start": start, "stop": stop, "restart": restart}[action](inst)
            node = next((n for n in load_nodes()["nodes"] if n["name"] == box), None)
            if not node:
                return "no-such-box"
            if action == "command":
                node_request(node, "POST", f"/api/instances/{urllib.parse.quote(bot)}/command",
                             {"command": command}, timeout=12)
                return "sent"
            node_request(node, "POST", f"/api/instances/{urllib.parse.quote(bot)}/{action}", timeout=12)
            return "ok"
        except Exception as e:
            return f"error:{e}"


SCHEDULER = None


def ensure_node_tunnel(node, wait=8.0):
    """Bring up a node's tunnel and block until its loopback port answers (best-effort).
    Used after adding a node at runtime — also starts the supervisor so the tunnel
    self-heals even if the controller had no nodes at boot."""
    global TUNNELS
    if TUNNELS is None:
        TUNNELS = TunnelManager()
    if not TUNNELS._started:
        TUNNELS.start()          # ensures all registered nodes + launches the supervisor
    else:
        TUNNELS.ensure(node)
    deadline = time.time() + wait
    while time.time() < deadline:
        if _port_open(node["local_port"]):
            return True
        time.sleep(0.25)
    return _port_open(node["local_port"])


# A sticky top bar injected into every page (the controller's own and proxied node
# pages) so you can switch which box you're viewing without leaving the browser tab.
SWITCHER_BAR = """
<div id="abmNodeBar" style="position:sticky;top:0;z-index:99999;display:flex;align-items:center;gap:.6rem;padding:.3rem .8rem;background:#0a1016;border-bottom:1px solid #1c2a36;font:600 .8rem/1.25 system-ui,-apple-system,sans-serif;color:#cdd9e2">
  <span style="opacity:.6;font-weight:400">Viewing</span>
  <select id="abmNodeSel" onchange="abmSelectNode(this.value)" style="font:600 .8rem system-ui,sans-serif;background:#06090c;color:#e6f0f7;border:1px solid #1c2a36;border-radius:7px;padding:.22rem .5rem;cursor:pointer">
    <option value="">★ Controller</option>
  </select>
  <span id="abmNodeWhich" style="opacity:.5;font-weight:400"></span>
  <span style="flex:1"></span>
  <a href="#" onclick="abmSelectNode('');return false" style="color:#7fb0d8;text-decoration:none;font-weight:400">controller home</a>
</div>
<script>
window.ABM_CURRENT_NODE="__ABM_NODE__";
window.ABM_CURRENT_LABEL="__ABM_LABEL__";
function abmEsc(s){return (s||'').replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
async function abmSelectNode(name){
  try{ await fetch('/api/node/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name})}); }catch(e){}
  location.href='/';
}
(async function(){
  try{
    var d=await fetch('/api/nodes').then(function(r){return r.json();});
    var sel=document.getElementById('abmNodeSel'); if(!sel)return;
    var cur=window.ABM_CURRENT_NODE||'';
    var html='<option value="">★ Controller</option>';
    (d.nodes||[]).forEach(function(n){
      var off=(n.tunnel&&n.tunnel.alive)?'':' (offline)';
      html+='<option value="'+abmEsc(n.name)+'"'+(n.name===cur?' selected':'')+'>'+abmEsc(n.label||n.name)+off+'</option>';
    });
    sel.innerHTML=html; if(cur)sel.value=cur;
    var w=document.getElementById('abmNodeWhich'); if(w)w.textContent=cur?'remote node':'';
  }catch(e){}
})();
</script>
"""


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

# Legacy basic-auth env vars (still honored as a fallback if no password set).
# ZP_USER / ZP_PASS are also accepted for backward compatibility.
ABM_USER = os.environ.get("ABM_USER") or os.environ.get("ZP_USER")
ABM_PASS = os.environ.get("ABM_PASS") or os.environ.get("ZP_PASS")


class Handler(BaseHTTPRequestHandler):
    server_version = "AquariusBotManager"
    cfg_path = DEFAULT_CONFIG
    bind_port = 8765

    def log_message(self, *a):
        pass  # quiet

    # ---- auth ----
    def _cookie_token(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith("abm_session="):
                return part[len("abm_session="):]
        return None

    def _auth_required(self, cfg):
        # Auth is enforced if a password has been set, or legacy env creds exist.
        return auth_configured(cfg) or bool(ABM_USER and ABM_PASS)

    def _needs_setup(self, cfg):
        # First run: no login configured at all and the user hasn't explicitly
        # chosen to run open. The web setup wizard then takes over the whole UI
        # so onboarding is "open the page → create a login" with no CLI step.
        if auth_configured(cfg) or (ABM_USER and ABM_PASS):
            return False
        return not bool(cfg["raw"].get("settings", {}).get("setup_skipped"))

    def _principal(self, cfg):
        """Resolve the requester to {"type": "owner"|"guest", "scope": ...}, or None if unauthenticated.
        Guest grants are re-validated from cfg on every request, so revoke / expiry / scope edits take
        effect immediately (a now-stale guest session is dropped and returns None)."""
        if not self._auth_required(cfg):
            return {"type": "owner", "scope": None}
        tok = self._cookie_token()
        if tok:
            sess = _SESSIONS.get(tok)
            if sess and _session_valid(tok, session_epoch(cfg)):
                if sess.get("principal") == "guest":
                    scope = sess.get("scope") or {}
                    g = find_share_by_id(cfg, scope.get("grant_id"))
                    if not g or not _share_active(cfg, g):
                        _SESSIONS.pop(tok, None)
                        return None
                    return {"type": "guest", "grant_id": g["id"],
                            "scope": {"targets": g.get("targets", []), "all": bool(g.get("all")),
                                      "capability": g.get("capability", "view")}}
                if sess.get("principal") == "user":
                    # re-resolve the account from cfg every request: role/scope edits, disable, delete,
                    # and password changes (pwgen) all take effect immediately for a live session.
                    u = find_user_by_id(cfg, sess.get("uid"))
                    if (not u or u.get("disabled")
                            or int(u.get("pwgen", 0)) != int(sess.get("pwgen", 0))):
                        _SESSIONS.pop(tok, None)
                        return None
                    role = u.get("role", "view")
                    return {"type": "user", "uid": u["id"], "username": u.get("username", ""),
                            "role": role, "perms": resolve_perms(role, u.get("perms")),
                            "scope": {"targets": u.get("targets", []),
                                      "all": bool(u.get("all")) or role == "admin",
                                      "capability": role_capability(role)}}
                return {"type": "owner", "scope": None}   # sessions without a principal key = owner
        # legacy basic-auth fallback (e.g. behind a tunnel without a set password) — owner
        if ABM_USER and ABM_PASS:
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    u, p = base64.b64decode(h[6:]).decode().split(":", 1)
                    if u == ABM_USER and p == ABM_PASS:
                        return {"type": "owner", "scope": None}
                except Exception:
                    pass
        return None

    def _auth_ok(self, cfg):
        return self._principal(cfg) is not None

    # ---- authorization guards (owner vs scoped guest) ----
    def _is_owner(self, princ):
        # the configured owner, OR a named user with the admin role (admin = a second owner)
        if princ is None:
            return False
        if princ.get("type") == "owner":
            return True
        return princ.get("type") == "user" and princ.get("role") == "admin"

    def _cap_ok(self, princ, level):
        if princ is None:
            return False
        if self._is_owner(princ):
            return True
        return CAP_RANK.get(princ["scope"].get("capability"), -1) >= CAP_RANK.get(level, 99)

    def _guest_target(self, princ, name):
        """For a guest, is `name` in scope and on which node? Returns (in_scope, node|None)."""
        sc = princ["scope"]
        for t in sc.get("targets", []):
            if t.get("name") == name:
                return True, (t.get("node") or None)
        if sc.get("all"):
            return True, None        # `all` covers local bots only
        return False, None

    def _guard_owner(self, princ):
        if self._is_owner(princ):
            return True
        self._json({"error": "forbidden"}, 403)
        return False

    def _guard_cap(self, princ, level):
        if self._cap_ok(princ, level):
            return True
        self._json({"error": "forbidden"}, 403)
        return False

    def _guard_target(self, princ, name):
        """Owner: always ok (node from cookie). Guest: must be in scope, else 404 (enumeration-resistant).
        Returns (ok, node) — node is the box the bot is on for guests (None=local); None for owners."""
        if self._is_owner(princ):
            return True, None
        ok, node = self._guest_target(princ, name)
        if not ok:
            self._json({"error": "no such instance"}, 404)
            return False, None
        return True, node

    # ---- guest route classification (default-deny: anything not listed is owner-only) ----
    _INST_RE = re.compile(r"^/api/instances/([^/]+)/(.+)$")

    @staticmethod
    def _inst_tier(rest, method):
        """Capability tier required for an instance sub-route, or None = owner-only/unknown."""
        if rest.startswith("viewer/"):
            return "view"
        if rest.startswith("control/"):
            sub = rest[len("control/"):]
            if sub in ("state", "commands"):
                return "view"
            if sub == "config":
                return "config" if method == "POST" else "view"
            if sub == "command":
                return "operate"
            return None
        if method == "GET":
            return "view" if rest in ("logs", "config") else None
        if rest in ("start", "stop", "restart", "command"):
            return "operate"
        if rest in ("config", "proxy", "limits", "autostart"):
            return "config"
        return None     # delete / rename / anything else => owner-only

    def _classify_guest(self, path, method, q):
        """Classify a guest request: ('open',) allowed non-instance; ('inst', name, tier) instance-scoped;
        None = deny (owner-only / unknown). Default-deny is the safe posture."""
        if path in ("/", "/index.html", "/logout", "/api/authstatus", "/api/instances"):
            return ("open",)
        if path in ("/control", "/control/"):
            name = (q.get("inst", [""])[0] or "").strip()
            return ("inst", name, "view") if name else None
        if path.startswith("/control/"):        # static js/html asset, no bot data
            return ("open",)
        m = self._INST_RE.match(path)
        if m:
            tier = self._inst_tier(m.group(2), method)
            return ("inst", m.group(1), tier) if tier else None
        return None

    def _guest_gate(self, princ, path, method, q):
        """Enforce a scoped principal's reach for this request. Returns (allow, node): allow=False means a
        response was already sent; node is the box to route instance requests to (None=local). Owners
        (incl. admin users) pass through unchanged. Anonymous guest links AND named non-admin users share
        the same scope={targets,all,capability} gating."""
        if self._is_owner(princ):
            return True, self._selected_node()      # owner/admin: cookie-driven node as today
        if not princ or princ.get("type") not in ("guest", "user"):
            return True, self._selected_node()
        cls = self._classify_guest(path, method, q)
        if cls is None:
            self._json({"error": "forbidden"}, 403)
            return False, None
        if cls[0] == "inst":
            _, name, tier = cls
            ok, gnode = self._guard_target(princ, name)
            if not ok:
                return False, None                  # 404 sent
            if not self._guard_cap(princ, tier):
                return False, None                  # 403 sent
            # fine-grained: a named user without the lifecycle grant can't start/stop/restart bots
            if princ.get("type") == "user":
                action = path.rsplit("/", 1)[-1]
                if action in ("start", "stop", "restart") and not (princ.get("perms") or {}).get("lifecycle"):
                    self._json({"error": "forbidden"}, 403)
                    return False, None
            if method == "POST":                     # audit scoped mutations (best-effort)
                audit_guest({"ts": time.time(), "grant_id": princ.get("grant_id") or princ.get("uid"),
                             "user": princ.get("username"),
                             "ip": self._client_ip(), "path": path, "target": name, "tier": tier})
            return True, gnode                       # route to the bot's box (None=local)
        return True, None                            # 'open' → handled locally

    # ---- /s/<token> share-link redemption ----
    _SHARE_RE = re.compile(r"^/s/([A-Za-z0-9_-]+)$")

    def _redeem_share(self, cfg, token):
        ip = self._client_ip()
        if _rate_limited(ip):
            return self._json({"error": "too many attempts"}, 429)
        grant = find_share_by_token(cfg, token)
        if not grant:
            _record_fail(ip)
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            body = (b"<!doctype html><meta charset=utf-8><title>Link unavailable</title>"
                    b"<div style='max-width:460px;margin:14vh auto;font:400 15px/1.6 system-ui,sans-serif;"
                    b"color:#cdd9e2;background:#0d141b;border:1px solid #1c2a36;border-radius:12px;padding:1.4rem'>"
                    b"<h2 style='margin:.2rem 0 .6rem'>This link is invalid or has expired</h2>"
                    b"<p style='opacity:.8'>Ask the owner for a new share link.</p></div>")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        # mint a scoped guest session; Lax cookie so the link works clicked from elsewhere
        scope = {"grant_id": grant["id"], "targets": grant.get("targets", []),
                 "all": bool(grant.get("all")), "capability": grant.get("capability", "view"),
                 "shares_epoch": shares_epoch(cfg)}
        tok = _new_session(session_epoch(cfg), scope=scope)
        self.send_response(302)
        self.send_header("Location", "/")
        self._set_session_cookie(tok, samesite="Lax")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- /i/<token> invite redemption (create a named account) ----
    _INVITE_RE = re.compile(r"^/i/([A-Za-z0-9_-]+)$")

    def _serve_invite_page(self, cfg, token):
        inv = find_invite_by_token(cfg, token)
        if not inv:
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            body = (b"<!doctype html><meta charset=utf-8><title>Invite unavailable</title>"
                    b"<div style='max-width:460px;margin:14vh auto;font:400 15px/1.6 system-ui,sans-serif;"
                    b"color:#cdd9e2;background:#0d141b;border:1px solid #1c2a36;border-radius:12px;padding:1.4rem'>"
                    b"<h2 style='margin:.2rem 0 .6rem'>This invite is invalid, used, or expired</h2>"
                    b"<p style='opacity:.8'>Ask the owner for a new invite link.</p></div>")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        # render the account-creation page (token + any preset username are baked in)
        scope = "all bots" if inv.get("all") else (", ".join(t.get("name", "") for t in inv.get("targets", [])) or "selected bots")
        html = (INVITE_PAGE
                .replace("__TOKEN__", token)
                .replace("__ROLE__", inv.get("role", "view"))
                .replace("__SCOPE__", scope)
                .replace("__PRESET_USER__", inv.get("username") or ""))
        return self._html(html)

    def _authstatus(self, cfg):
        princ = self._principal(cfg)
        out = {"required": self._auth_required(cfg),
               "authed": princ is not None,
               "needs_setup": self._needs_setup(cfg)}
        if princ is None:
            out["principal"] = "anon"
        elif princ["type"] == "owner":
            out["principal"] = "owner"
        elif princ["type"] == "user":
            sc = princ["scope"]
            out["principal"] = "user"
            out["username"] = princ.get("username")
            out["role"] = princ.get("role")
            out["is_admin"] = princ.get("role") == "admin"
            out["capability"] = sc.get("capability")
            out["targets"] = sc.get("targets", [])
            out["all"] = sc.get("all", False)
            # effective control permissions (control-live.js hides what isn't granted; server enforces)
            _u = find_user_by_id(cfg, princ.get("uid"))
            out["perms"] = perms_public(princ.get("role"), _u.get("perms") if _u else None)
        else:
            sc = princ["scope"]
            out["principal"] = "guest"
            out["capability"] = sc.get("capability")
            out["targets"] = sc.get("targets", [])
            out["all"] = sc.get("all", False)
        return self._json(out)

    def _client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    def _set_session_cookie(self, token, clear=False, samesite="Strict"):
        # SameSite=Lax is used only on the /s/<token> redemption hop so a link clicked from
        # Discord/email keeps the cookie; owner login stays Strict. Lax still blocks CSRF on the
        # mutating POSTs (none are top-level navigations).
        if clear:
            self.send_header("Set-Cookie",
                             f"abm_session=; Path=/; HttpOnly; SameSite={samesite}; Max-Age=0")
        else:
            self.send_header("Set-Cookie",
                             f"abm_session={token}; Path=/; HttpOnly; SameSite={samesite}; "
                             f"Max-Age={SESSION_TTL}")

    # ---- helpers ----
    def _json(self, obj, code=200, cookie=None, clear_cookie=False):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie is not None or clear_cookie:
            self._set_session_cookie(cookie, clear=clear_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, code=200):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_file(self, abspath, download_name, cleanup=False):
        """Stream a file to the client as an attachment (chunked, never fully buffered).
        If cleanup, delete abspath afterwards (used for on-the-fly folder zips)."""
        try:
            size = os.path.getsize(abspath)
            f = open(abspath, "rb")
        except OSError as e:
            return self._json({"error": str(e)}, 404)
        try:
            ascii_name = download_name.encode("ascii", "replace").decode("ascii").replace('"', "_")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{urllib.parse.quote(download_name)}")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if self.command != "HEAD":
                shutil.copyfileobj(f, self.wfile, 64 * 1024)
        finally:
            f.close()
            if cleanup:
                try:
                    os.remove(abspath)
                except OSError:
                    pass

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def _cfg(self):
        return load_config(self.cfg_path)

    def _find(self, cfg, name):
        return cfg["by_name"].get(name)

    # ---- controller: node selection + reverse proxy ----
    def _selected_node(self):
        """The node chosen via the abm_node cookie, or None for the local controller."""
        raw = self.headers.get("Cookie", "")
        name = None
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith("abm_node="):
                name = part[len("abm_node="):]
        if not name:
            return None
        return find_node(load_nodes(), name)

    def _is_switcher_path(self, path):
        # controller-level paths always served locally so box-switching keeps working
        # even while a remote node is selected
        return (path in ("/logout", "/api/authstatus", "/api/transfer", "/api/box/roots")
                or path.startswith("/api/node/")     # /api/node/select
                or path.startswith("/api/nodes")     # /api/nodes, .../remove, .../do*
                or path.startswith("/api/fleet/")
                or path.startswith("/api/shares")    # share grants live on the controller
                or path.startswith("/api/share/")    # public-sharing tunnel exposes THIS dashboard, never a node
                or path.startswith("/api/users")     # named accounts live on the controller
                or path.startswith("/api/invite"))   # invite links + redemption are controller-local

    def _inject_switcher_str(self, text, current, label=""):
        if "<body>" not in text or "abmNodeBar" in text:
            return text
        bar = (SWITCHER_BAR
               .replace("__ABM_NODE__", html.escape(current or "", quote=True))
               .replace("__ABM_LABEL__", html.escape(label or current or "", quote=True)))
        return text.replace("<body>", "<body>" + bar, 1)

    def _proxy_to_node(self, node):
        """Forward this request verbatim to the node's manager over its loopback tunnel,
        inject the node's Basic-auth, and splice the switcher bar into HTML responses."""
        url = f"http://127.0.0.1:{node['local_port']}{self.path}"
        data = self._read_body() if self.command in ("POST", "PUT", "PATCH", "DELETE") else None
        fwd = {}
        for h in ("Content-Type", "Accept", "User-Agent"):
            v = self.headers.get(h)
            if v:
                fwd[h] = v
        u, pw = node_creds(node)
        if u:
            fwd["Authorization"] = "Basic " + base64.b64encode(f"{u}:{pw}".encode()).decode()
        req = urllib.request.Request(url, data=data, method=self.command, headers=fwd)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            status, rheaders, rbody = resp.status, resp.getheaders(), resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            rheaders = list(e.headers.items()) if e.headers else []
            try:
                rbody = e.read()
            except Exception:
                rbody = b""
        except Exception as e:
            return self._node_unreachable(node, str(e))
        ctype = next((v for k, v in rheaders if k.lower() == "content-type"), "")
        if "text/html" in ctype.lower():
            try:
                rbody = self._inject_switcher_str(
                    rbody.decode("utf-8", "replace"), node["name"],
                    node.get("label") or node["name"]).encode("utf-8")
            except Exception:
                pass
        self.send_response(status)
        skip = ("content-length", "transfer-encoding", "connection",
                "set-cookie", "content-encoding", "keep-alive")
        for k, v in rheaders:
            if k.lower() in skip:
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(rbody)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(rbody)

    def _node_unreachable(self, node, err):
        page = (
            "<!doctype html><html><head><meta charset='utf-8'><title>box unreachable</title>"
            "</head><body><div style='max-width:560px;margin:13vh auto;"
            "font:400 15px/1.5 system-ui,sans-serif;color:#cdd9e2;background:#0d141b;"
            "border:1px solid #1c2a36;border-radius:12px;padding:1.4rem'>"
            f"<h2 style='margin:.2rem 0 .6rem'>“{html.escape(node['name'])}” is unreachable</h2>"
            "<p style='opacity:.8'>The controller couldn't reach this box over its SSH tunnel:</p>"
            "<pre style='white-space:pre-wrap;background:#06090c;border:1px solid #1c2a36;"
            f"border-radius:8px;padding:.6rem;font-size:12px;color:#e08a8a'>{html.escape(err)}</pre>"
            "<p style='opacity:.8'>The tunnel keeps retrying in the background. Use the bar above "
            "to return to the controller or pick another box.</p>"
            "<button onclick=\"abmSelectNode('')\" style='background:#1c5;border:0;color:#06240f;"
            "font-weight:700;border-radius:8px;padding:.5rem .9rem;cursor:pointer'>"
            "Back to controller</button></div></body></html>")
        body = self._inject_switcher_str(page, node["name"],
                                         node.get("label") or node["name"]).encode("utf-8")
        self.send_response(502)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---- live control surface (Mission Control) ----
    CONTROL_STYLES = {"v1": "index.html", "v2": "v2.html", "v3": "v3.html"}
    CONTROL_ASSETS = {"abm-control-data.js", "control-live.js",
                      "index.html", "v2.html", "v3.html"}

    def _control_dir(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "control")

    def _serve_control_page(self, q):
        """Serve the live Mission-Control surface page (themeable via ?style=v1|v2|v3).
        Assets are referenced absolutely (/control/<file>) so the page URL's trailing
        slash doesn't matter; the live wiring is in control-live.js."""
        style = (q.get("style", ["v1"])[0] or "v1").lower()
        fname = self.CONTROL_STYLES.get(style, "index.html")
        fp = os.path.join(self._control_dir(), fname)
        if not os.path.isfile(fp):
            fp = os.path.join(self._control_dir(), "index.html")
        try:
            with open(fp, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"error": "control UI not installed on this box"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_control_asset(self, path):
        """Serve a whitelisted static asset from the repo's control/ dir."""
        rel = path[len("/control/"):]
        if rel not in self.CONTROL_ASSETS:
            return self._json({"error": "not found"}, 404)
        fp = os.path.join(self._control_dir(), rel)
        if not os.path.isfile(fp):
            return self._json({"error": "not found"}, 404)
        ctype = ("application/javascript; charset=utf-8" if rel.endswith(".js")
                 else "text/html; charset=utf-8" if rel.endswith(".html")
                 else "application/octet-stream")
        try:
            with open(fp, "rb") as f:
                body = f.read()
        except OSError:
            return self._json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _viewer_relay(self, path, q):
        """Relay a bot's loopback viewer feed to the dashboard. The bot binds the viewer to 127.0.0.1 only, so the
        browser can't reach it directly — we fetch it server-side and pass it through. Cross-box is automatic: a
        selected remote node already proxies this whole request to that box's manager, which serves its local bot.
        Path: /api/instances/<name>/viewer/{state|map}. Port is resolved per-bot (instances.json
        `viewer_port`, else the bot's own config.json `server.viewer.port`, else 2998); ?port= overrides."""
        parts = path.split("/")
        # ['', 'api', 'instances', '<name>', 'viewer', 'state'|'map'|'chunks'|'inventory']
        if len(parts) < 6 or parts[4] != "viewer" or parts[5] not in ("state", "map", "chunks", "inventory"):
            return self._json({"error": "not found"}, 404)
        sub = parts[5]
        name = urllib.parse.unquote(parts[3])
        inst = self._cfg()["by_name"].get(name) or {}
        port = viewer_port_for(inst)
        if q.get("port"):                          # explicit override (ad-hoc / testing)
            try:
                p = int(q["port"][0])
                if 1024 <= p <= 65535:
                    port = p
            except (ValueError, TypeError, IndexError):
                pass
        upstream = {"state": "state.json", "map": "map.png", "chunks": "chunks", "inventory": "inventory"}[sub]
        url = f"http://127.0.0.1:{port}/viewer/{upstream}"
        if sub == "map":
            try:
                url += f"?size={max(64, min(512, int(q.get('size', ['256'])[0])))}"
            except (ValueError, TypeError, IndexError):
                pass
        elif sub == "chunks":
            try:
                r = max(8, min(64, int(q.get('r', ['48'])[0])))
                yb = max(8, min(96, int(q.get('yb', ['48'])[0])))
                ya = max(8, min(96, int(q.get('ya', ['48'])[0])))
                url += f"?r={r}&yb={yb}&ya={ya}"
            except (ValueError, TypeError, IndexError):
                pass
        try:
            resp = urllib.request.urlopen(url, timeout=5)
            body = resp.read()
            ctype = resp.headers.get("Content-Type") or ("application/json" if sub in ("state", "inventory") else "image/png")
        except Exception as e:
            if sub in ("state", "inventory"):
                return self._json({"offline": True, "error": str(e)[:120]})
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        for hk in ("X-Center-X", "X-Center-Z", "X-Size", "X-Encoding"):  # map pan precision + chunk encoding
            hv = resp.headers.get(hk)
            if hv:
                self.send_header(hk, hv)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _control_post_allowed(self, princ, sub, data):
        """Fine-grained authorization for a named user's control POST (toggles / free-form commands /
        config writes). Owners/admins resolve to all-true perms so they pass. Guests never reach here as
        'user'. Deny-by-default: a non-console user may only send an exact `<allowed-module> on|off`."""
        perms = princ.get("perms") or {}
        can_use = perms.get("can_use") or (lambda _i: False)
        can_config = perms.get("can_config") or (lambda _i: False)
        try:
            body = json.loads(data or b"{}")
        except (ValueError, TypeError):
            return False
        if sub == "command":
            if perms.get("console"):
                return True
            m = _TOGGLE_RE.match((body.get("command") or "").strip())
            if not m:
                return False
            mid = _RAW2MOD.get(m.group(1).lower())
            return bool(mid and can_use(mid))
        if sub == "config":
            parts = (body.get("path") or "").split(".")
            if len(parts) >= 3 and parts[0] == "client" and parts[1] == "extra":
                mid = _RAW2MOD.get(parts[2].lower())
                return bool(mid and can_config(mid))
            return False                          # non-module roots (auth/discord/db/server) = owner-only
        return True

    def _control_relay(self, path, q):
        """Relay a bot's loopback /control/* endpoints. GET state|commands; POST command (forwards the JSON body).
        Same per-bot port resolution + cross-box behaviour as the viewer relay. The bot 403s `command` unless its
        `server.viewer.control` flag is on — that status is forwarded through."""
        parts = path.split("/")  # ['', 'api', 'instances', '<name>', 'control', 'state'|'commands'|'command'|'config']
        if len(parts) < 6 or parts[4] != "control" or parts[5] not in ("state", "commands", "command", "config"):
            return self._json({"error": "not found"}, 404)
        sub = parts[5]
        name = urllib.parse.unquote(parts[3])
        inst = self._cfg()["by_name"].get(name) or {}
        port = viewer_port_for(inst)
        if q.get("port"):
            try:
                p = int(q["port"][0])
                if 1024 <= p <= 65535:
                    port = p
            except (ValueError, TypeError, IndexError):
                pass
        url = f"http://127.0.0.1:{port}/control/{sub}"
        # command is always a POST; config mirrors the incoming method (GET read / POST field write)
        is_post = sub == "command" or (sub == "config" and self.command == "POST")
        # read the POST body ONCE up front so we can both authorize it (per-module/console) and relay it
        data = self._read_body() if (is_post and self.command == "POST") else b"{}"
        if is_post and self.command == "POST":
            princ = self._principal(self._cfg())
            if princ and princ.get("type") == "user" and not self._control_post_allowed(princ, sub, data):
                return self._json({"error": "forbidden"}, 403)
        try:
            if is_post:
                req = urllib.request.Request(url, data=data or b"{}", method="POST",
                                            headers={"Content-Type": "application/json"})
                resp = urllib.request.urlopen(req, timeout=20)   # a command may take a while
            else:
                resp = urllib.request.urlopen(url, timeout=8)
            body = resp.read()
            ctype = resp.headers.get("Content-Type") or "application/json"
        except urllib.error.HTTPError as e:                       # forward the bot's status (e.g. 403 control off)
            try:
                body = e.read()
            except Exception:
                body = b"{}"
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        except Exception as e:
            return self._json({"offline": True, "error": str(e)[:120]})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _viewer_stream(self, path, q, node):
        """Stream a bot's ~20 Hz SSE state feed (text/event-stream) straight through to the browser, unbuffered.
        Picks its upstream symmetrically: on the controller (a node is selected) it streams from that box's
        manager — which runs this same code with no node selected and so streams from its local bot; on a leaf
        box it streams from the bot's loopback /viewer/stream. The browser's EventSource falls back to polling
        if this can't be opened, so a 503 here is a soft failure."""
        parts = path.split("/")  # ['', 'api', 'instances', '<name>', 'viewer', 'stream']
        if len(parts) < 6:
            return self._json({"error": "not found"}, 404)
        name = urllib.parse.unquote(parts[3])
        hdrs = {"Accept": "text/event-stream"}
        if node is not None:
            url = f"http://127.0.0.1:{node['local_port']}{self.path}"
            u, pw = node_creds(node)
            if u:
                hdrs["Authorization"] = "Basic " + base64.b64encode(f"{u}:{pw}".encode()).decode()
        else:
            inst = self._cfg()["by_name"].get(name) or {}
            port = viewer_port_for(inst)
            if q.get("port"):
                try:
                    p = int(q["port"][0])
                    if 1024 <= p <= 65535:
                        port = p
                except (ValueError, TypeError, IndexError):
                    pass
            url = f"http://127.0.0.1:{port}/viewer/stream"
        try:
            up = urllib.request.urlopen(urllib.request.Request(url, headers=hdrs), timeout=10)
        except Exception:
            self.send_response(503)        # browser EventSource sees this and falls back to polling
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")   # discourage any intermediary buffering
            self.end_headers()
            while True:
                line = up.readline()       # forwards each SSE line as it arrives (low latency)
                if not line:
                    break
                self.wfile.write(line)
                self.wfile.flush()
        except Exception:
            pass                            # client/upstream disconnected — end the stream quietly
        finally:
            try:
                up.close()
            except Exception:
                pass

    # ---- routing ----
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)
        cfg = self._cfg()

        # whether auth is on, expose it so the login page can decide what to show
        if path == "/api/authstatus":
            return self._authstatus(cfg)

        # share-link redemption: mints a scoped guest session (before the auth gate)
        msh = self._SHARE_RE.match(path)
        if msh:
            return self._redeem_share(cfg, msh.group(1))

        # invite-link landing page: the invitee sets a username+password to create their account
        # (before the auth gate — they don't have one yet)
        miv = self._INVITE_RE.match(path)
        if miv:
            return self._serve_invite_page(cfg, miv.group(1))

        # First run: the setup wizard owns the UI until a login is created
        # (or the user explicitly skips to run open on localhost).
        if self._needs_setup(cfg):
            if path in ("/", "/index.html", "/login", "/setup"):
                return self._html(SETUP_PAGE)
            return self._json({"error": "setup required"}, 401)

        princ = self._principal(cfg)
        if princ is None:
            # unauthenticated: only the login page and its check are reachable
            if path in ("/", "/index.html", "/login"):
                return self._html(LOGIN_PAGE)
            return self._json({"error": "unauthorized"}, 401)

        # controller: a selected node proxies the whole UI through its SSH tunnel.
        # Guests never drive box-switching — _guest_gate enforces scope/capability and
        # resolves the node from the grant's target instead of the abm_node cookie.
        allow, node = self._guest_gate(princ, path, "GET", q)
        if not allow:
            return
        # The viewer SSE feed must stream, so it bypasses the buffering node-proxy: handle it here and
        # let _viewer_stream pick its upstream (the selected box's manager, else the local bot).
        if path.startswith("/api/instances/") and path.endswith("/viewer/stream"):
            return self._viewer_stream(path, q, node)
        if node is not None and not self._is_switcher_path(path):
            return self._proxy_to_node(node)

        if path == "/" or path == "/index.html":
            page = PAGE.replace("__ABM_VERSION__", __version__)
            if load_nodes()["nodes"]:
                page = self._inject_switcher_str(page, "")
            return self._html(page)

        if path == "/logout":
            tok = self._cookie_token()
            if tok:
                _SESSIONS.pop(tok, None)
            self.send_response(302)
            self.send_header("Location", "/")
            self._set_session_cookie(None, clear=True)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/control" or path == "/control/":
            return self._serve_control_page(q)
        if path.startswith("/control/"):
            return self._serve_control_asset(path)

        if path.startswith("/api/instances/") and "/viewer/" in path:
            return self._viewer_relay(path, q)

        if path.startswith("/api/instances/") and "/control/" in path:
            return self._control_relay(path, q)

        if path == "/api/scan":
            return self._json({"sessions": scan(self._cfg())})

        if path == "/api/proxies":
            return self._json({"proxies": list_proxies(self._cfg())})

        if path == "/api/files":
            try:
                return self._json(fs_list(self._cfg(), q.get("path", [""])[0]))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        if path == "/api/files/read":
            try:
                return self._json(fs_read(self._cfg(), q.get("path", [""])[0]))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        # download a file (streamed) or a folder (zipped on the fly)
        if path == "/api/files/download":
            try:
                target = q.get("path", [""])[0]
                rp = _resolve_in_roots(target, file_roots(cfg))
                if os.path.isdir(rp):
                    abspath, name, cleanup = fs_zip_dir(cfg, target)
                else:
                    abspath, name, cleanup = fs_download(cfg, target)
                return self._send_file(abspath, name, cleanup=cleanup)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        if path == "/api/schema":
            return self._json({"schema": ZENITH_SCHEMA})

        if path == "/api/settings":
            return self._json(get_settings(self._cfg()))

        # is the manager behind its upstream branch? (cached quiet git fetch; no pull)
        if path == "/api/update/check":
            force = q.get("force", ["0"])[0] in ("1", "true", "yes")
            return self._json(update_available(force=force))

        # controller: registered nodes (other boxes) + live tunnel status. Always served
        # locally (never proxied) so the box-switcher works while a node is selected.
        if path == "/api/nodes":
            reg = load_nodes()
            return self._json({"nodes": [node_public_view(n, TUNNELS) for n in reg["nodes"]]})

        # controller: a box's file-manager roots (for the cross-box transfer dest default).
        # Served locally so it works even while a node is selected.
        if path == "/api/box/roots":
            try:
                return self._json({"roots": box_roots(self._cfg(), load_nodes(),
                                                       q.get("box", [""])[0])})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        # automation: job definitions + live runtime (next/last run, watchdog tries)
        if path == "/api/schedules":
            sched = self._cfg()["raw"].get("settings", {}).get("schedules") or {"notify_webhook": "", "jobs": []}
            rt = SCHEDULER.snapshot() if SCHEDULER is not None else {}
            return self._json({"schedules": sched, "runtime": rt})

        # controller: aggregated status of this box + every node (the Fleet view)
        if path == "/api/fleet/status":
            return self._json({"fleet": fleet_aggregate(self._cfg())})

        # controller: DigitalOcean droplets + regions/sizes for the connect/provision UI
        if path == "/api/nodes/do":
            reg = load_nodes()
            tok = do_token_saved(reg)
            if not tok:
                return self._json({"token_saved": False})
            try:
                fleet = _fleet_mod()
                droplets = []
                for d in fleet.do_list_droplets(tok):
                    pub, _priv = fleet.droplet_ips(d)
                    droplets.append({"id": d.get("id"), "name": d.get("name"),
                                     "region": (d.get("region") or {}).get("slug"),
                                     "status": d.get("status"),
                                     "size": (d.get("size") or {}).get("slug") or d.get("size_slug"),
                                     "public_ip": pub})
                regions = [{"slug": r.get("slug"), "name": r.get("name")}
                           for r in fleet.do_list_regions(tok)]
                sizes = sorted(
                    ({"slug": s.get("slug"), "memory": s.get("memory"),
                      "vcpus": s.get("vcpus"), "price": s.get("price_monthly")}
                     for s in fleet.do_list_sizes(tok) if str(s.get("slug", "")).startswith("s-")),
                    key=lambda s: (s.get("price") or 0))
                return self._json({"token_saved": True, "droplets": droplets,
                                   "regions": regions, "sizes": sizes,
                                   "default_size": DO_NODE_SIZE})
            except ValueError as e:
                # token is saved but the DO call failed (bad token / network) — surface
                # the message in-band so the UI can show it (200, not an HTTP error)
                return self._json({"token_saved": True, "error": str(e)})

        # controller: progress of an in-flight droplet provision
        if path == "/api/nodes/do/job":
            return self._json(PROVISION_JOB.snapshot())

        if path == "/api/system/info":
            return self._json(_sysinfo())

        # connection info for the reconnect panel (bookmark URL + tunnel command)
        if path == "/api/connection":
            return self._json({"port": self.bind_port, "user": _run_user(),
                               "public_ip": public_ip()})

        # download a reconnect shortcut for the user's local machine
        if path == "/api/connection/script":
            ostype = q.get("os", ["windows"])[0]
            if ostype not in ("windows", "mac", "linux"):
                return self._json({"error": "os must be windows|mac|linux"}, 400)
            fname, mime, text = reconnect_script(
                ostype, q.get("ip", [""])[0], q.get("user", [""])[0],
                q.get("port", [str(self.bind_port)])[0])
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # download a launcher that tunnels into EVERY box (controller + nodes)
        if path == "/api/connection/multiscript":
            ostype = q.get("os", ["windows"])[0]
            if ostype not in ("windows", "mac", "linux"):
                return self._json({"error": "os must be windows|mac|linux"}, 400)
            conn = {"ip": q.get("ip", [""])[0], "user": q.get("user", [""])[0],
                    "port": q.get("port", [str(self.bind_port)])[0]}
            fname, mime, text = multi_reconnect_script(ostype, conn, load_nodes()["nodes"])
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # download a config backup of this box (instances + connected nodes)
        if path == "/api/backup":
            bundle = json.dumps(make_backup(self._cfg()), indent=2).encode()
            fname = f"abm-backup-{socket.gethostname()}-{time.strftime('%Y%m%d')}.json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(bundle)))
            self.end_headers()
            self.wfile.write(bundle)
            return

        if path == "/api/system/job":
            return self._json(SYS_JOB.snapshot())

        if path == "/api/deploy/job":
            return self._json(DEPLOY_JOB.snapshot())

        if path == "/api/migrate/job":
            return self._json(MIGRATE_JOB.snapshot())

        if path == "/api/instances":
            cfg = self._cfg()

            def _row(i):
                st = instance_status(i)
                row = {
                    "name": i["name"],
                    "dir": i["dir"],
                    "launch_cmd": i["launch_cmd"],
                    "status": st,
                    "autostart": bool(i.get("autostart")),
                    "limits": limits_view(i),
                    "proxy": proxy_info(i["dir"]),
                    "conn": connection_state(i, st),
                }
                if st == "running":
                    try:
                        row["stats"] = instance_stats(i)
                    except Exception:
                        row["stats"] = None
                return row

            insts = cfg["instances"]
            if princ and not self._is_owner(princ) and princ.get("type") in ("guest", "user"):
                # scoped principals (guest links + non-admin users) see only their granted bots —
                # local filtered here, remote fetched per-node
                sc = princ["scope"]
                local_names = {t["name"] for t in sc.get("targets", []) if not t.get("node")}
                local = cfg["instances"] if sc.get("all") else [i for i in insts if i["name"] in local_names]
                out = [_row(i) for i in local]
                remote = {}
                for t in sc.get("targets", []):
                    if t.get("node"):
                        remote.setdefault(t["node"], set()).add(t["name"])
                for nodename, names in remote.items():
                    nd = find_node(load_nodes(), nodename)
                    if not nd:
                        continue
                    try:
                        data = node_request(nd, "GET", "/api/instances", timeout=8)
                        for row in (data or {}).get("instances", []):
                            if row.get("name") in names:
                                row["node"] = nodename
                                out.append(row)
                    except Exception:
                        pass
                return self._json({"instances": out})

            return self._json({"instances": [_row(i) for i in insts]})

        if path == "/api/shares":
            if not self._guard_owner(princ):
                return
            cfg = self._cfg()
            return self._json({"shares": [share_public_view(cfg, g) for g in _shares(cfg)]})

        if path == "/api/shares/audit":
            if not self._guard_owner(princ):
                return
            return self._json({"audit": list(reversed(_GUEST_AUDIT[-60:]))})

        if path == "/api/users":
            if not self._guard_owner(princ):
                return
            cfg = self._cfg()
            return self._json({"users": [user_public_view(cfg, u) for u in _users(cfg)],
                               "invites": [invite_public_view(i) for i in _invites(cfg)
                                           if invite_status(i) == "pending"],
                               "modules": module_catalog(),
                               "owner": _owner_username(cfg)})

        if path == "/api/share/tunnel":
            if not self._guard_owner(princ):
                return
            cfg = self._cfg()
            st = SHARE.status(cfg)
            st["password_set"] = auth_configured(cfg)
            st["providers"] = SHARE.catalog(cfg)
            return self._json(st)

        m = re.match(r"^/api/instances/([^/]+)/logs$", path)
        if m:
            cfg = self._cfg()
            inst = self._find(cfg, m.group(1))
            if not inst:
                return self._json({"error": "no such instance"}, 404)
            lines = int(q.get("lines", ["300"])[0])
            return self._json({"logs": logs(inst, lines)})

        m = re.match(r"^/api/instances/([^/]+)/config$", path)
        if m:
            cfg = self._cfg()
            inst = self._find(cfg, m.group(1))
            if not inst:
                return self._json({"error": "no such instance"}, 404)
            text, p = read_instance_config(inst)
            return self._json({"path": p, "exists": text is not None, "config": text or ""})

        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        cfg = self._cfg()

        # First-run setup wizard: create the first login from the browser
        # (replaces `abm setpassword`). Only reachable while no auth exists,
        # so it can never be used to reset an existing password.
        if path == "/api/setup":
            if auth_configured(cfg):
                return self._json({"error": "a login is already configured"}, 409)
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            try:
                set_password(cfg, p.get("username", "").strip(), p.get("password", ""))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            # log the new admin straight in
            cfg = self._cfg()
            return self._json({"ok": True}, cookie=_new_session(session_epoch(cfg)))

        # First-run "skip" — run open on localhost, remember the choice so the
        # wizard doesn't reappear every visit.
        if path == "/api/setup/skip":
            if not self._needs_setup(cfg):
                return self._json({"ok": True})
            cfg["raw"].setdefault("settings", {})["setup_skipped"] = True
            save_config(cfg)
            return self._json({"ok": True})

        # login is the one POST reachable without a session
        if path == "/api/login":
            ip = self._client_ip()
            if _rate_limited(ip):
                return self._json({"error": "too many attempts, wait a few minutes"}, 429)
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            user = p.get("username", "")
            pw = p.get("password", "")
            # owner login first (settings.auth or legacy env), then named user accounts
            owner_ok = verify_password(cfg, user, pw) if auth_configured(cfg) else (
                bool(ABM_USER) and user == ABM_USER and pw == ABM_PASS)
            if owner_ok:
                return self._json({"ok": True}, cookie=_new_session(session_epoch(cfg)))
            u = verify_user(cfg, user, pw)
            if u:
                touch_user_login(cfg, u["id"])
                return self._json({"ok": True, "user": u.get("username"), "role": u.get("role")},
                                  cookie=_new_session(session_epoch(cfg), user=u))
            _record_fail(ip)
            return self._json({"error": "invalid credentials"}, 401)

        # invite redemption: create a named account from an invite link, then sign in (pre-auth —
        # the invitee has no account yet). Rate-limited like login/share redemption.
        if path == "/api/invite/redeem":
            ip = self._client_ip()
            if _rate_limited(ip):
                return self._json({"error": "too many attempts, wait a few minutes"}, 429)
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            try:
                u = redeem_invite(cfg, p.get("token", ""), p.get("username", ""), p.get("password", ""))
            except ValueError as e:
                _record_fail(ip)
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True, "user": u.get("username")},
                              cookie=_new_session(session_epoch(cfg), user=u))

        princ = self._principal(cfg)
        if princ is None:
            return self._json({"error": "unauthorized"}, 401)

        # controller: proxy mutating requests to the selected node too (skip switcher controls).
        # Guests are scoped by grant (no box-switch cookie); _guest_gate may route to a remote node.
        pq = parse_qs(urlparse(self.path).query)
        allow, node = self._guest_gate(princ, path, "POST", pq)
        if not allow:
            return
        if node is not None and not self._is_switcher_path(path):
            return self._proxy_to_node(node)

        # live control plane: POST a command to a bot, or write a config field (forwarded to its loopback /control/*)
        if path.startswith("/api/instances/") and (path.endswith("/control/command") or path.endswith("/control/config")):
            return self._control_relay(path, pq)

        # controller: set/clear which box this browser is viewing (abm_node cookie)
        if path == "/api/node/select":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            name = (p.get("name") or "").strip()
            if name and not find_node(load_nodes(), name):
                return self._json({"error": "no such node"}, 404)
            body = json.dumps({"ok": True, "selected": name}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if name:
                self.send_header("Set-Cookie",
                                 f"abm_node={name}; Path=/; SameSite=Strict; Max-Age={SESSION_TTL}")
            else:
                self.send_header("Set-Cookie", "abm_node=; Path=/; SameSite=Strict; Max-Age=0")
            self.end_headers()
            self.wfile.write(body)
            return

        # share links (owner-only; controller-local)
        if path == "/api/shares":
            if not self._guard_owner(princ):
                return
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            cfg = self._cfg()
            try:
                grant, token = new_share(cfg, p.get("label"), p.get("targets") or [],
                                         bool(p.get("all")), p.get("capability", "view"),
                                         p.get("ttl_days"))
            except (ValueError, TypeError) as e:
                return self._json({"error": str(e)}, 400)
            url = f"{share_base_url(self)}/s/{token}"
            return self._json({"ok": True, "share": share_public_view(cfg, grant), "url": url})

        if path == "/api/shares/revoke":
            if not self._guard_owner(princ):
                return
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            cfg = self._cfg()
            return self._json({"ok": revoke_share(cfg, (p.get("id") or "").strip())})

        if path == "/api/shares/revoke_all":
            if not self._guard_owner(princ):
                return
            cfg = self._cfg()
            return self._json({"ok": True, "epoch": bump_shares_epoch(cfg)})

        # named user accounts + invites (owner/admin only; controller-local)
        if path == "/api/users":
            if not self._guard_owner(princ):
                return
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            cfg = self._cfg()
            try:
                u = new_user(cfg, p.get("username", ""), p.get("password", ""),
                             p.get("role", "view"), bool(p.get("all")), p.get("targets") or [])
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True, "user": user_public_view(cfg, u)})

        mu = re.match(r"^/api/users/([^/]+)/password$", path)
        if mu:
            if not self._guard_owner(princ):
                return
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            cfg = self._cfg()
            try:
                ok = set_user_password(cfg, mu.group(1), p.get("password", ""))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True}) if ok else self._json({"error": "no such user"}, 404)

        mu = re.match(r"^/api/users/([^/]+)/delete$", path)
        if mu:
            if not self._guard_owner(princ):
                return
            cfg = self._cfg()
            return self._json({"ok": delete_user(cfg, mu.group(1))})

        mu = re.match(r"^/api/users/([^/]+)$", path)
        if mu:
            if not self._guard_owner(princ):
                return
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            cfg = self._cfg()
            try:
                u = update_user(cfg, mu.group(1), role=p.get("role"), all_=p.get("all"),
                                targets=p.get("targets"), disabled=p.get("disabled"),
                                perms=(p["perms"] if "perms" in p else "__keep__"))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            if not u:
                return self._json({"error": "no such user"}, 404)
            return self._json({"ok": True, "user": user_public_view(cfg, u)})

        if path == "/api/invites":
            if not self._guard_owner(princ):
                return
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            cfg = self._cfg()
            try:
                inv, token = new_invite(cfg, p.get("label"), p.get("role", "view"),
                                        bool(p.get("all")), p.get("targets") or [],
                                        p.get("username"), p.get("ttl_days"))
            except (ValueError, TypeError) as e:
                return self._json({"error": str(e)}, 400)
            url = f"{share_base_url(self)}/i/{token}"
            return self._json({"ok": True, "invite": invite_public_view(inv), "url": url})

        mi = re.match(r"^/api/invites/([^/]+)/revoke$", path)
        if mi:
            if not self._guard_owner(princ):
                return
            cfg = self._cfg()
            return self._json({"ok": revoke_invite(cfg, mi.group(1))})

        if path == "/api/share/tunnel":
            if not self._guard_owner(princ):
                return
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            cfg = self._cfg()
            ps = cfg["raw"].setdefault("settings", {}).setdefault("public_share", {})
            ps.setdefault("providers", {})
            ps.setdefault("provider", "cloudflare-quick")
            action = p.get("action") or ("enable" if "enable" in p else None)
            restart_active = False     # NB: don't name this `restart` — that shadows the module-level restart()
            if action == "config":
                pid = p.get("provider")
                prov = SHARE.providers.get(pid)
                if not prov:
                    return self._json({"error": "unknown provider"}, 400)
                store = ps["providers"].setdefault(pid, {})
                incoming = p.get("config") or {}
                for n in getattr(prov, "needs", []):
                    if n["key"] not in incoming:
                        continue
                    val = (incoming.get(n["key"]) or "").strip()
                    if n.get("secret"):
                        if val:
                            store[n["key"]] = _b64enc(val)      # only overwrite a secret if a new one was typed
                        elif incoming.get(n["key"]) == "":
                            store.pop(n["key"], None)           # blank-on-purpose clears it (handled by UI flag)
                    else:
                        store[n["key"]] = val
                restart_active = True                           # new domain/token must actually take effect
            elif action == "select":
                pid = p.get("provider")
                if pid not in SHARE.providers:
                    return self._json({"error": "unknown provider"}, 400)
                ps["provider"] = pid
            elif action == "enable":
                enable = bool(p.get("enable"))
                # never put the dashboard on the public internet without a login in front of it
                if enable and not auth_configured(cfg):
                    return self._json({"error": "Set a dashboard password first — public sharing would "
                                                "otherwise expose full control to anyone with the URL."}, 400)
                ps["enabled"] = enable
            elif action == "install":
                # download / set up the chosen provider's helper in the BACKGROUND (downloads are tens of
                # MB and would otherwise block the request long enough for the browser to time out). Return
                # immediately; the UI polls until catalog.installed flips true. No settings change / reconcile.
                pid = p.get("provider")
                prov = SHARE.providers.get(pid)
                if not prov:
                    return self._json({"error": "unknown provider"}, 400)
                if not getattr(prov, "installable", True):
                    return self._json({"error": "nothing to install for this provider"}, 400)
                try:
                    already = prov.installed()
                except Exception:
                    already = False
                if not already:
                    SHARE.install_async(cfg, pid)
                st = SHARE.status(cfg)
                st["password_set"] = auth_configured(cfg)
                st["providers"] = SHARE.catalog(cfg)
                return self._json(st)
            else:
                return self._json({"error": "bad request"}, 400)
            save_config(cfg)
            SHARE.reconcile(cfg, self.bind_port, restart_active=restart_active)
            st = SHARE.status(cfg)
            st["password_set"] = auth_configured(cfg)
            st["providers"] = SHARE.catalog(cfg)
            return self._json(st)

        # bulk actions
        m = re.match(r"^/api/(start|stop|restart)_all$", path)
        if m:
            action = {"start": start, "stop": stop, "restart": restart}[m.group(1)]
            results = {i["name"]: action(i) for i in cfg["instances"]}
            return self._json({"results": results})

        # save settings (theme / system toggle)
        if path == "/api/settings":
            try:
                p = json.loads(self._read_body() or b"{}")
                out = save_settings(cfg, theme=p.get("theme"),
                                    system_actions_enabled=p.get("system_actions_enabled"),
                                    console_presets=p.get("console_presets"),
                                    thresholds=p.get("thresholds"),
                                    ui=p.get("ui"),
                                    schedules=p.get("schedules"),
                                    box_name=p.get("box_name"))
                return self._json({"ok": True, "settings": out})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid request: {e}"}, 400)

        # automation: fire a job right now ("Run now")
        if path == "/api/schedules/run":
            try:
                p = json.loads(self._read_body() or b"{}")
                if SCHEDULER is None:
                    return self._json({"error": "scheduler not running"}, 503)
                return self._json({"ok": True, "result": SCHEDULER.run_now(cfg, p.get("id", ""))})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid request: {e}"}, 400)

        # controller: copy a file/folder between two boxes (scp over stored SSH creds).
        # Served locally (switcher path) so it can orchestrate even while a node is selected.
        if path == "/api/transfer":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            try:
                return self._json(transfer_between(
                    self._cfg(), load_nodes(),
                    p.get("src_box", ""), p.get("src_path", ""),
                    p.get("dst_box", ""), p.get("dst_dir", "")))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        # controller: register a new node (other VPS box). Accepts user@host[:port] in
        # "target" or explicit fields. Brings the tunnel up and probes it so the UI
        # can report reachable/not on the spot.
        if path == "/api/nodes":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            tgt_user, host, tgt_port = _parse_ssh_target(p.get("target") or p.get("ssh_host") or "")
            reg = load_nodes()
            try:
                node = add_node(
                    reg, p.get("name"), host,
                    ssh_user=p.get("ssh_user") or tgt_user or "ubuntu",
                    ssh_port=int(p.get("ssh_port") or tgt_port or 22),
                    remote_port=int(p.get("remote_port") or 8765),
                    ssh_key=p.get("ssh_key"), basic_user=p.get("basic_user"),
                    basic_pass=p.get("basic_pass"), local_port=p.get("local_port"))
            except (ValueError, TypeError) as e:
                return self._json({"error": str(e)}, 400)
            test = {"reachable": False}
            try:
                if ensure_node_tunnel(node):
                    n = node_request(node, "GET", "/api/instances", timeout=8).get("instances", [])
                    test = {"reachable": True, "instances": len(n)}
                else:
                    test = {"reachable": False,
                            "error": "tunnel did not come up — check ssh user/host/key"}
            except Exception as e:
                test = {"reachable": False, "error": str(e)}
            return self._json({"ok": True, "node": node_public_view(node, TUNNELS), "test": test})

        # controller: remove a node (POST to stay consistent with the rest of the API)
        if path == "/api/nodes/remove":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            reg = load_nodes()
            try:
                remove_node(reg, p.get("name"))
            except ValueError as e:
                return self._json({"error": str(e)}, 404)
            if TUNNELS is not None:
                TUNNELS.drop(p.get("name"))
            return self._json({"ok": True})

        # controller: set a node's friendly display name (registry key is unchanged)
        if path == "/api/nodes/label":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            reg = load_nodes()
            try:
                label = set_node_label(reg, p.get("name"), p.get("label"))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True, "label": label})

        # controller: re-establish a node's SSH tunnel on demand (manual reconnect)
        if path == "/api/nodes/reconnect":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            node = find_node(load_nodes(), p.get("name"))
            if not node:
                return self._json({"error": f"no such box: {p.get('name')}"}, 404)
            if TUNNELS is not None:
                TUNNELS.drop(node["name"])      # tear down the stale tunnel so it re-binds clean
            try:
                if ensure_node_tunnel(node, wait=10):
                    n = node_request(node, "GET", "/api/instances", timeout=8).get("instances", [])
                    return self._json({"ok": True, "reachable": True, "instances": len(n)})
                return self._json({"ok": True, "reachable": False,
                                   "error": "tunnel did not come up — is the box online? check ssh user/host/key"})
            except Exception as e:
                return self._json({"ok": True, "reachable": False, "error": str(e)})

        # controller: bulk start/stop/restart across this box + nodes
        if path == "/api/fleet/action":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            try:
                res = fleet_action(cfg, p.get("action", ""), p.get("targets") or ["all"])
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True, "results": res})

        # controller: push self-update to all (or selected) nodes
        if path == "/api/fleet/update":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            return self._json({"ok": True, "results": fleet_update(p.get("targets") or ["all"])})

        # controller: save the DigitalOcean API token (stored b64-obfuscated)
        if path == "/api/nodes/do/token":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            reg = load_nodes()
            set_do_token(reg, (p.get("token") or "").strip())
            return self._json({"ok": True, "token_saved": bool(do_token_saved(reg))})

        # controller: register an existing droplet as an SSH-tunnel node
        if path == "/api/nodes/do/connect":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            reg = load_nodes()
            tok = do_token_saved(reg)
            if not tok:
                return self._json({"error": "no DigitalOcean token saved"}, 400)
            try:
                node = do_connect_existing(reg, tok, p.get("droplet_id"),
                                           p.get("name"), p.get("ssh_user") or "root")
            except (ValueError, TypeError) as e:
                return self._json({"error": str(e)}, 400)
            test = {"reachable": False}
            try:
                if ensure_node_tunnel(node):
                    n = node_request(node, "GET", "/api/instances", timeout=8).get("instances", [])
                    test = {"reachable": True, "instances": len(n)}
                else:
                    test = {"reachable": False, "error": "tunnel did not come up "
                            "(does the droplet trust the controller's SSH key?)"}
            except Exception as e:
                test = {"reachable": False, "error": str(e)}
            return self._json({"ok": True, "node": node_public_view(node, TUNNELS), "test": test})

        # controller: provision a new node-mode droplet (runs as a background job)
        if path == "/api/nodes/do/provision":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            tok = do_token_saved(load_nodes())
            if not tok:
                return self._json({"error": "no DigitalOcean token saved"}, 400)
            name = (p.get("name") or "").strip()
            region = (p.get("region") or "").strip()
            size = (p.get("size") or DO_NODE_SIZE).strip()
            if not name or not region:
                return self._json({"error": "name and region are required"}, 400)
            image = (p.get("image") or DO_NODE_IMAGE).strip()
            vpc = p.get("vpc") or None
            try:
                PROVISION_JOB.start(name, lambda log: do_provision_node(
                    load_nodes(), tok, name, region, size, log, image=image, vpc_uuid=vpc))
            except ValueError as e:
                return self._json({"error": str(e)}, 409)
            return self._json({"ok": True, "started": True})

        # controller: destroy the droplet behind a node, then remove the node
        if path == "/api/nodes/do/destroy":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            reg = load_nodes()
            tok = do_token_saved(reg)
            if not tok:
                return self._json({"error": "no DigitalOcean token saved"}, 400)
            try:
                return self._json({"ok": True, **do_destroy_node(reg, tok, p.get("name"))})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        # restore configs from an uploaded backup bundle (snapshots current files first)
        if path == "/api/restore":
            try:
                bundle = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            try:
                return self._json({"ok": True, **restore_backup(self._cfg(), bundle)})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        # system actions (reboot / update) — gated by settings + auth
        m = re.match(r"^/api/system/(reboot|update)$", path)
        if m:
            try:
                return self._json(run_system_action(cfg, m.group(1)))
            except PermissionError as e:
                return self._json({"error": str(e)}, 403)
            except ValueError as e:
                return self._json({"error": str(e)}, 409)

        # deploy a new proxy (download a fork's launcher, register it) — background job
        if path == "/api/deploy":
            try:
                p = json.loads(self._read_body() or b"{}")
                return self._json(deploy_proxy(self.cfg_path, p.get("name"), p.get("dir"),
                                               p.get("source"), owner_repo=p.get("owner_repo"),
                                               limits=p.get("limits"),
                                               autostart=p.get("autostart", True)))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid request: {e}"}, 400)

        # adopt an existing tmux session
        if path == "/api/adopt":
            try:
                p = json.loads(self._read_body() or b"{}")
                sk = p.get("stop_keys")
                if isinstance(sk, str):
                    sk = [s for s in sk.split(",") if s.strip()]
                inst = adopt_session(
                    cfg, p.get("session"), name=p.get("name"),
                    launch_cmd=p.get("launch_cmd"), config_file=p.get("config_file"),
                    stop_keys=sk, stop_timeout=p.get("stop_timeout"),
                )
                return self._json({"ok": True, "instance": {
                    "name": inst["name"], "dir": inst["dir"],
                    "launch_cmd": inst["launch_cmd"], "status": instance_status(inst),
                }})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid request: {e}"}, 400)

        # add a new instance
        if path == "/api/instances/add":
            try:
                p = json.loads(self._read_body() or b"{}")
                sk = p.get("stop_keys")
                if isinstance(sk, str):
                    sk = [s for s in sk.split(",") if s.strip()]
                inst = add_instance(
                    cfg, p.get("name"), p.get("dir"),
                    launch_cmd=p.get("launch_cmd"), config_file=p.get("config_file"),
                    stop_keys=sk, stop_timeout=p.get("stop_timeout"), limits=p.get("limits"),
                )
                return self._json({"ok": True, "instance": {
                    "name": inst["name"], "dir": inst["dir"],
                    "launch_cmd": inst["launch_cmd"], "status": instance_status(inst),
                }})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid request: {e}"}, 400)

        # delete an instance
        m = re.match(r"^/api/instances/([^/]+)/delete$", path)
        if m:
            try:
                body = self._read_body()
                force = bool(json.loads(body).get("force")) if body else False
            except Exception:
                force = False
            try:
                result = delete_instance(cfg, m.group(1), force=force)
                return self._json({"ok": True, "result": result})
            except ValueError as e:
                # 409 = conflict (running, needs force); 404 = missing
                code = 404 if str(e).startswith("no such") else 409
                return self._json({"error": str(e)}, code)

        # rename a bot (instance key + tmux session + folder when stopped)
        m = re.match(r"^/api/instances/([^/]+)/rename$", path)
        if m:
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)
            try:
                res = rename_instance(cfg, m.group(1), p.get("new"),
                                      move_dir=p.get("move_dir", True))
                return self._json(res)
            except ValueError as e:
                code = 404 if str(e).startswith("no such") else 400
                return self._json({"error": str(e)}, code)

        # in-place ZenithProxy -> AquariusProxy migration (owner-only; background job) + rollback
        m = re.match(r"^/api/instances/([^/]+)/migrate$", path)
        if m:
            try:
                return self._json(migrate_to_aquarius(self.cfg_path, urllib.parse.unquote(m.group(1))))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
        m = re.match(r"^/api/instances/([^/]+)/migrate/rollback$", path)
        if m:
            try:
                return self._json(rollback_migration(self.cfg_path, urllib.parse.unquote(m.group(1))))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        # per-instance action
        m = re.match(r"^/api/instances/([^/]+)/(start|stop|restart)$", path)
        if m:
            inst = self._find(cfg, m.group(1))
            if not inst:
                return self._json({"error": "no such instance"}, 404)
            action = {"start": start, "stop": stop, "restart": restart}[m.group(2)]
            return self._json({"result": action(inst), "status": instance_status(inst)})

        # bulk proxy assignment (round-robin / same) across many instances
        if path == "/api/proxies/bulk":
            try:
                p = json.loads(self._read_body() or b"{}")
                res = set_proxies_bulk(
                    cfg, p.get("targets") or [], p.get("proxies") or [],
                    mode=p.get("mode", "roundrobin"), do_restart=bool(p.get("restart")))
                return self._json({"ok": True, "results": res})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid request: {e}"}, 400)

        # import proxies from a Webshare subscription via its API
        if path == "/api/proxies/webshare":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid request: {e}"}, 400)
            token = _webshare_token(cfg, p.get("token"))
            if p.get("save_token") and token:
                save_webshare_token(cfg, token)
                cfg = self._cfg()
            countries = p.get("countries") or None
            if isinstance(countries, str):
                countries = [c for c in re.split(r"[,\s]+", countries) if c]
            try:
                # count-only preview: fetch and report, change nothing
                if p.get("count_only"):
                    got = webshare_fetch(token, list_mode=p.get("list_mode", "direct"),
                                         valid_only=not p.get("all_proxies"),
                                         countries=countries, plan_id=p.get("plan_id"))
                    return self._json({"ok": True, "count": len(got),
                                       "countries": sorted({g["country"] for g in got
                                                            if g.get("country")}),
                                       "saved_token": bool(p.get("save_token") and token)})
                res = webshare_import(
                    cfg, p.get("targets") or [], auth=p.get("auth", "userpass"), token=token,
                    assign_mode=p.get("mode", "roundrobin"), list_mode=p.get("list_mode", "direct"),
                    valid_only=not p.get("all_proxies"), countries=countries,
                    plan_id=p.get("plan_id"), do_restart=bool(p.get("restart")))
                res["ok"] = True
                res["saved_token"] = bool(p.get("save_token") and token)
                return self._json(res)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        # scan bot consoles for proxy errors (dead / removed IPs)
        if path == "/api/proxies/health":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid request: {e}"}, 400)
            try:
                res = detect_proxy_issues(cfg, names=p.get("names") or None,
                                          lines=int(p.get("lines", 200)))
            except (ValueError, TypeError) as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True, "results": res,
                               "errored": [r["name"] for r in res if r["errored"]]})

        # update the manager itself: git pull + restart (no full reinstall)
        if path == "/api/selfupdate":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                p = {}
            try:
                return self._json({"ok": True, **self_update(do_restart=p.get("restart", True))})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        # enable/disable the periodic self-update timer
        if path == "/api/autoupdate":
            try:
                p = json.loads(self._read_body() or b"{}")
            except json.JSONDecodeError:
                p = {}
            try:
                return self._json({"ok": True,
                                   **autoupdate_set(bool(p.get("enable")),
                                                    schedule=p.get("schedule", "daily"))})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        # update proxy host/port
        m = re.match(r"^/api/instances/([^/]+)/proxy$", path)
        if m:
            inst = self._find(cfg, m.group(1))
            if not inst:
                return self._json({"error": "no such instance"}, 404)
            try:
                p = json.loads(self._read_body() or b"{}")
                res = set_proxy(inst, host=p.get("host"), port=p.get("port"),
                                enabled=p.get("enabled"), user=p.get("user"),
                                password=p.get("password"))
                return self._json({"ok": True, "proxy": res})
            except ValueError as e:
                return self._json({"error": str(e)}, 409)
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid request: {e}"}, 400)

        # send a command into the live console
        m = re.match(r"^/api/instances/([^/]+)/command$", path)
        if m:
            inst = self._find(cfg, m.group(1))
            if not inst:
                return self._json({"error": "no such instance"}, 404)
            try:
                p = json.loads(self._read_body() or b"{}")
                return self._json({"ok": True, "result": send_command(inst, p.get("command", ""))})
            except ValueError as e:
                return self._json({"error": str(e)}, 409)
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid request: {e}"}, 400)

        # set / clear per-instance resource limits
        m = re.match(r"^/api/instances/([^/]+)/limits$", path)
        if m:
            try:
                p = json.loads(self._read_body() or b"{}")
                res = set_limits(cfg, m.group(1), memory=p.get("memory"), cpu=p.get("cpu"))
                return self._json({"ok": True, "limits": res})
            except ValueError as e:
                return self._json({"error": str(e)}, 404 if str(e).startswith("no such") else 400)
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid request: {e}"}, 400)

        # toggle autostart
        m = re.match(r"^/api/instances/([^/]+)/autostart$", path)
        if m:
            try:
                p = json.loads(self._read_body() or b"{}")
                val = set_autostart(cfg, m.group(1), bool(p.get("enabled")))
                return self._json({"ok": True, "autostart": val})
            except ValueError as e:
                return self._json({"error": str(e)}, 404)
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid request: {e}"}, 400)

        # save instance config
        m = re.match(r"^/api/instances/([^/]+)/config$", path)
        if m:
            inst = self._find(cfg, m.group(1))
            if not inst:
                return self._json({"error": "no such instance"}, 404)
            try:
                payload = json.loads(self._read_body() or b"{}")
                p = write_instance_config(inst, payload.get("config", ""))
                return self._json({"ok": True, "path": p})
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid JSON: {e}"}, 400)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        # upload a file (raw request body; folder uploads send each file with its
        # webkitRelativePath as ?name). Streamed straight to disk — never JSON-parsed,
        # so this must be matched BEFORE the JSON file-manager block below.
        if path == "/api/files/upload":
            q = parse_qs(urlparse(self.path).query)
            n = int(self.headers.get("Content-Length", 0))
            if n > _FS_MAX_UPLOAD:
                # drain so the connection stays usable, then refuse
                try:
                    self.rfile.read(n)
                except Exception:
                    pass
                return self._json(
                    {"error": f"file too large (limit {_FS_MAX_UPLOAD // (1024 * 1024)} MB)"}, 413)
            try:
                target = fs_upload_target(cfg, q.get("dir", [""])[0], q.get("name", [""])[0])
            except ValueError as e:
                try:
                    self.rfile.read(n)        # consume the body so the socket isn't left mid-message
                except Exception:
                    pass
                return self._json({"error": str(e)}, 400)
            written = 0
            try:
                with open(target, "wb") as out:
                    remaining = n
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            break
                        out.write(chunk)
                        remaining -= len(chunk)
                        written += len(chunk)
                _resolve_in_roots(target, file_roots(cfg))   # final symlink-escape guard
            except (OSError, ValueError) as e:
                try:
                    os.remove(target)
                except OSError:
                    pass
                return self._json({"error": str(e)}, 400)
            return self._json({"ok": True, "path": target, "size": written})

        # file manager (jailed) — create / edit / rename / delete
        if path.startswith("/api/files/"):
            try:
                p = json.loads(self._read_body() or b"{}")
                if path == "/api/files/write":
                    return self._json({"ok": True, **fs_write(cfg, p.get("path"), p.get("content", ""))})
                if path == "/api/files/mkdir":
                    return self._json({"ok": True, **fs_mkdir(cfg, p.get("dir"), p.get("name"))})
                if path == "/api/files/newfile":
                    return self._json({"ok": True, **fs_mkdir(cfg, p.get("dir"), p.get("name"), is_file=True)})
                if path == "/api/files/rename":
                    return self._json({"ok": True, **fs_rename(cfg, p.get("path"), p.get("name"))})
                if path == "/api/files/delete":
                    return self._json({"ok": True, **fs_delete(cfg, p.get("path"), bool(p.get("recursive")))})
                return self._json({"error": "not found"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid request: {e}"}, 400)

        return self._json({"error": "not found"}, 404)


def serve(cfg_path, host, port):
    Handler.cfg_path = cfg_path
    Handler.bind_port = port
    cfg = load_config(cfg_path)  # validate early
    httpd = ThreadingHTTPServer((host, port), Handler)
    if auth_configured(cfg):
        auth = "ON (login required)"
    elif ABM_USER and ABM_PASS:
        auth = "ON (basic auth env)"
    elif not cfg["raw"].get("settings", {}).get("setup_skipped"):
        auth = "SETUP — open the UI to create your login (first-run wizard)"
    else:
        auth = "OFF — running open (skipped); keep this bound to 127.0.0.1"
    print(f"Aquarius Bot Manager {__version__} serving http://{host}:{port}   auth: {auth}")
    if host not in ("127.0.0.1", "localhost", "::1") and auth.startswith("OFF"):
        print("WARNING: listening on a non-local address with NO auth. Anyone who can reach "
              f"{host}:{port} has full control. Set a password or bind to 127.0.0.1.")
    print(f"config: {cfg_path}")
    # controller mode: bring up SSH tunnels to any registered nodes
    global TUNNELS
    reg = load_nodes()
    if reg["nodes"]:
        TUNNELS = TunnelManager()
        TUNNELS.start()
        print(f"controller: starting {len(reg['nodes'])} node tunnel(s): "
              + ", ".join(n["name"] for n in reg["nodes"]))
    # automation: start the schedule runner (time jobs + on-crash watchdog)
    global SCHEDULER
    SCHEDULER = Scheduler(cfg_path)
    SCHEDULER.start()
    njobs = len((cfg["raw"].get("settings", {}).get("schedules") or {}).get("jobs", []))
    if njobs:
        print(f"scheduler: {njobs} job(s) loaded")
    # resume public sharing (active exposure provider) if it was left on and a login is set
    if SHARE.enabled(cfg) and auth_configured(cfg):
        print(f"public sharing: resuming '{SHARE.active_id(cfg)}' provider…")
        threading.Thread(target=lambda: SHARE.reconcile(cfg, port), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        if TUNNELS is not None:
            TUNNELS.stop()
        if SCHEDULER is not None:
            SCHEDULER.stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def targets(cfg, name):
    if name == "all":
        return cfg["instances"]
    inst = cfg["by_name"].get(name)
    if not inst:
        die(f"no such instance: {name}")
    return [inst]


def cli_status(cfg):
    width = max((len(i["name"]) for i in cfg["instances"]), default=4)
    for i in cfg["instances"]:
        print(f"{i['name']:<{width}}  {instance_status(i)}")


def _parse_ssh_target(s):
    """'ubuntu@1.2.3.4:2222' -> (user, host, port). Missing parts -> (None, host, None)."""
    user, port = None, None
    s = (s or "").strip()
    if "@" in s:
        user, s = s.split("@", 1)
    if ":" in s:
        s, p = s.rsplit(":", 1)
        if p.isdigit():
            port = int(p)
    return (user or None), s, port


def _cli_node_test(node, cleanup=True):
    """Bring the node's tunnel up (transiently for CLI), probe its API, print result."""
    mgr = TUNNELS or TunnelManager()
    mgr.ensure(node)
    up = False
    deadline = time.time() + 6
    while time.time() < deadline:
        if _port_open(node["local_port"]):
            up = True
            break
        time.sleep(0.25)
    try:
        if not up:
            die(f"tunnel to {node['name']} did not come up on 127.0.0.1:{node['local_port']} "
                "— check ssh user/host/key (try: ssh "
                f"{node.get('ssh_user', 'ubuntu')}@{node['ssh_host']})")
        try:
            inst = node_request(node, "GET", "/api/instances", timeout=8).get("instances", [])
            print(f"OK  {node['name']}: reachable over the tunnel; {len(inst)} instance(s)")
        except Exception as e:
            die(f"tunnel is up but the node API didn't answer: {e}\n"
                "(is the manager running on the node? if it enforces login, pass "
                "--basic-user/--basic-pass)")
    finally:
        if cleanup and TUNNELS is None:
            mgr.drop(node["name"])


def cli_node(args):
    reg = load_nodes()
    act = args.action
    if act == "list":
        if not reg["nodes"]:
            print("no nodes registered.  add one:  abm node add <name> <user@host>")
            return
        w = max((len(n["name"]) for n in reg["nodes"]), default=4)
        for n in reg["nodes"]:
            v = node_public_view(n)
            print(f"{v['name']:<{w}}  {v['ssh_user']}@{v['ssh_host']}:{v['ssh_port']}"
                  f"  ->  127.0.0.1:{v['local_port']}  (remote {v['remote_port']})"
                  f"  creds={'yes' if v['has_creds'] else 'no'}")
        return
    if act == "add":
        if not args.name or not args.target:
            die("usage:  abm node add <name> <user@host[:port]> [--key FILE] "
                "[--basic-user U --basic-pass P]")
        u, host, port = _parse_ssh_target(args.target)
        try:
            node = add_node(reg, args.name, host,
                            ssh_user=args.user or u or "ubuntu",
                            ssh_port=args.ssh_port or port or 22,
                            remote_port=args.remote_port, ssh_key=args.key,
                            basic_user=args.basic_user, basic_pass=args.basic_pass,
                            local_port=args.local_port)
        except ValueError as e:
            die(str(e))
        print(f"added node '{node['name']}': {node['ssh_user']}@{node['ssh_host']}:"
              f"{node['ssh_port']}  ->  127.0.0.1:{node['local_port']}")
        print("bringing up tunnel + testing ...")
        return _cli_node_test(node)
    if act == "remove":
        if not args.name:
            die("usage:  abm node remove <name>")
        try:
            remove_node(reg, args.name)
        except ValueError as e:
            die(str(e))
        if TUNNELS is not None:
            TUNNELS.drop(args.name)
        print(f"removed node '{args.name}'")
        return
    if act == "test":
        if not args.name:
            die("usage:  abm node test <name>")
        node = find_node(reg, args.name)
        if not node:
            die(f"no such node: {args.name}")
        return _cli_node_test(node)


def main():
    ap = argparse.ArgumentParser(prog="manager.py")
    ap.add_argument("--version", action="version", version=f"Aquarius Bot Manager {__version__}")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)

    sub.add_parser("list")
    sub.add_parser("status")
    for c in ("start", "stop", "restart"):
        p = sub.add_parser(c)
        p.add_argument("name")
    p = sub.add_parser("logs")
    p.add_argument("name")
    p.add_argument("--lines", type=int, default=300)

    p = sub.add_parser("send", help="send a command to an instance's live console")
    p.add_argument("name")
    p.add_argument("command", nargs="+", help="the command (quote it or pass as words)")
    p = sub.add_parser("discover")
    p.add_argument("basedir")

    p = sub.add_parser("add")
    p.add_argument("name")
    p.add_argument("dir")
    p.add_argument("--launch-cmd", default=None)
    p.add_argument("--config-file", default=None)
    p.add_argument("--stop-keys", default=None, help="comma-separated tmux keys, e.g. 'stop,Enter'")
    p.add_argument("--stop-timeout", type=int, default=None)
    p.add_argument("--autostart", action="store_true", help="launch on `boot`")

    p = sub.add_parser("delete")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="stop it first if running")

    sub.add_parser("scan")

    sub.add_parser("proxies", help="list each instance's proxy host:port")

    p = sub.add_parser("proxy", help="view or set an instance's proxy host/port")
    p.add_argument("name")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--enable", dest="p_enable", action="store_true")
    p.add_argument("--disable", dest="p_disable", action="store_true")

    p = sub.add_parser("proxybulk", help="assign proxies across many instances (round-robin / same / random)")
    p.add_argument("--targets", default="all",
                   help="comma-separated instance names, 'all' (default), or 'errored' (only bots with proxy errors)")
    p.add_argument("--list", dest="plist", required=True,
                   help="host:port[:user:pass] entries, comma- or newline-separated")
    p.add_argument("--mode", choices=["roundrobin", "same", "random"], default="roundrobin")
    p.add_argument("--restart", action="store_true", help="restart each instance after assigning")

    p = sub.add_parser("proxyhealth", help="scan bot consoles for proxy errors (dead/removed IPs)")
    p.add_argument("--targets", default="all", help="instance names, or 'all' (default)")
    p.add_argument("--lines", type=int, default=200, help="console lines to scan per bot")

    p = sub.add_parser("webshare", help="import proxies from a Webshare subscription via its API")
    p.add_argument("action", choices=["import", "count"],
                   help="'count' = fetch and report how many (no changes); 'import' = assign them")
    p.add_argument("--token", default=None, help="Webshare API token (else WEBSHARE_TOKEN or saved)")
    p.add_argument("--auth", choices=["ip", "userpass"], default="userpass",
                   help="ip = host:port only (whitelist VPS in Webshare); userpass = per-proxy creds")
    p.add_argument("--targets", default="all",
                   help="instance names, 'all' (default), or 'errored' (only bots with proxy errors)")
    p.add_argument("--mode", choices=["roundrobin", "same", "random"], default="roundrobin")
    p.add_argument("--list-mode", choices=["direct", "backbone"], default="direct",
                   help="Webshare proxy list mode")
    p.add_argument("--countries", default=None, help="comma-separated country codes, e.g. US,CA")
    p.add_argument("--plan-id", default=None, help="target a specific Webshare plan")
    p.add_argument("--all-proxies", action="store_true", help="include proxies Webshare marks invalid")
    p.add_argument("--restart", action="store_true", help="restart each instance after assigning")
    p.add_argument("--save-token", action="store_true", help="save the token for reuse")

    p = sub.add_parser("set", help="edit an instance field (name or 'all')")
    p.add_argument("name")
    p.add_argument("field", choices=SETTABLE_FIELDS)
    p.add_argument("value")

    p = sub.add_parser("limits", help="view/set/clear an instance's resource caps")
    p.add_argument("name")
    p.add_argument("--memory", default=None, help='hard memory cap, e.g. 2G or 512M ("" to clear)')
    p.add_argument("--cpu", default=None, help="CPU cap as percent of one core, e.g. 200 (0 to clear)")
    p.add_argument("--clear", action="store_true", help="remove all limits")

    p = sub.add_parser("files", help="list files under the allowed roots (jailed)")
    p.add_argument("path", nargs="?", default=None, help="directory to list (default: first root)")

    p = sub.add_parser("deploy", help="download a fork's launcher and register a new instance")
    p.add_argument("name")
    p.add_argument("--source", choices=["aquarius", "zenith", "custom"], default="aquarius")
    p.add_argument("--repo", default=None, help="owner/repo for --source custom")
    p.add_argument("--dir", default=None, help="install dir (default: <base>/<name>)")
    p.add_argument("--memory", default=None, help="optional memory cap, e.g. 2G")
    p.add_argument("--cpu", default=None, help="optional CPU cap, percent of one core")
    p.add_argument("--no-autostart", dest="no_autostart", action="store_true",
                   help="don't relaunch this bot on VPS reboot (autostart is on by default)")

    p = sub.add_parser("adopt")
    p.add_argument("session", help="existing tmux session name")
    p.add_argument("--name", default=None, help="instance name (default: session name)")
    p.add_argument("--launch-cmd", default=None)
    p.add_argument("--config-file", default=None)
    p.add_argument("--stop-keys", default=None, help="comma-separated tmux keys")
    p.add_argument("--stop-timeout", type=int, default=None)
    p.add_argument("--autostart", action="store_true", help="launch on `boot`")

    p = sub.add_parser("autostart", help="enable/disable autostart for an instance")
    p.add_argument("name")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", dest="on", action="store_true")
    g.add_argument("--off", dest="off", action="store_true")

    sub.add_parser("boot", help="start all autostart instances (run at host boot)")

    sub.add_parser("sysinfo")

    p = sub.add_parser("setpassword", help="set the web UI login (username + password)")
    p.add_argument("--user", default=None, help="username (prompts if omitted)")
    p.add_argument("--password", default=None, help="password (prompts if omitted; safer to omit)")

    sub.add_parser("logout-all", help="invalidate all active web sessions")

    p = sub.add_parser("settings")
    p.add_argument("--theme", default=None, help="preset name: " + ", ".join(THEME_PRESETS))
    p.add_argument("--accent", default=None, help="hex accent, e.g. #3ddc97")
    p.add_argument("--font", default=None, help="font pairing: " + ", ".join(FONT_PRESETS))
    p.add_argument("--enable-system", dest="enable_system", action="store_true")
    p.add_argument("--disable-system", dest="disable_system", action="store_true")

    sub.add_parser("update")   # OS update (requires system actions enabled)
    sub.add_parser("reboot")   # reboot the host (requires system actions enabled)

    p = sub.add_parser("selfupdate", help="update the manager in place (git pull + restart) — no reinstall")
    p.add_argument("--no-restart", dest="no_restart", action="store_true",
                   help="pull only; don't restart the web-UI service")

    p = sub.add_parser("autoupdate", help="enable/disable the periodic self-update timer")
    p.add_argument("action", choices=["on", "off", "status"])
    p.add_argument("--schedule", default="daily",
                   help="systemd OnCalendar value when enabling (default: daily)")

    p = sub.add_parser("node", help="manage controller nodes (other VPS boxes reached over SSH tunnels)")
    p.add_argument("action", choices=["list", "add", "remove", "test"])
    p.add_argument("name", nargs="?", help="node name (add/remove/test)")
    p.add_argument("target", nargs="?", help="user@host[:port] (add)")
    p.add_argument("--user", default=None, help="ssh user (overrides user@ in target)")
    p.add_argument("--ssh-port", dest="ssh_port", type=int, default=None)
    p.add_argument("--remote-port", dest="remote_port", type=int, default=8765,
                   help="the node manager's bind port (default 8765)")
    p.add_argument("--key", default=None, help="ssh identity file")
    p.add_argument("--local-port", dest="local_port", type=int, default=None)
    p.add_argument("--basic-user", dest="basic_user", default=None,
                   help="node web username (only if the node enforces login)")
    p.add_argument("--basic-pass", dest="basic_pass", default=None, help="node web password")

    args = ap.parse_args()

    if args.cmd == "serve":
        return serve(args.config, args.host, args.port)
    if args.cmd == "discover":
        return discover(args.basedir, args.config)
    if args.cmd == "node":
        return cli_node(args)

    cfg = load_config(args.config)

    if args.cmd == "list":
        for i in cfg["instances"]:
            print(f"{i['name']}\t{i['dir']}\t{i['launch_cmd']}")
    elif args.cmd == "status":
        cli_status(cfg)
    elif args.cmd in ("start", "stop", "restart"):
        action = {"start": start, "stop": stop, "restart": restart}[args.cmd]
        for i in targets(cfg, args.name):
            print(f"{i['name']}: {action(i)}")
    elif args.cmd == "logs":
        print(logs(cfg["by_name"].get(args.name) or die(f"no such instance: {args.name}"), args.lines))
    elif args.cmd == "send":
        inst = cfg["by_name"].get(args.name) or die(f"no such instance: {args.name}")
        try:
            send_command(inst, " ".join(args.command))
            print(f"{args.name}: sent")
        except ValueError as e:
            die(str(e))
    elif args.cmd == "add":
        sk = [s for s in args.stop_keys.split(",") if s.strip()] if args.stop_keys else None
        try:
            inst = add_instance(cfg, args.name, args.dir, launch_cmd=args.launch_cmd,
                                config_file=args.config_file, stop_keys=sk,
                                stop_timeout=args.stop_timeout, autostart=args.autostart)
            extra = "  [autostart]" if inst.get("autostart") else ""
            print(f"added {inst['name']}  ({inst['dir']}){extra}")
        except ValueError as e:
            die(str(e))
    elif args.cmd == "delete":
        try:
            print(f"{args.name}: {delete_instance(cfg, args.name, force=args.force)}")
        except ValueError as e:
            die(str(e))
    elif args.cmd == "scan":
        rows = scan(cfg)
        if not rows:
            print("No unmanaged tmux sessions found.")
        else:
            w = max((len(r["session"]) for r in rows), default=7)
            for r in rows:
                flag = "proxy?" if r["likely_proxy"] else "      "
                print(f"{flag}  {r['session']:<{w}}  {r['path']}  [{r['command']}]  ({r['reason']})")
            print("\nAdopt one with:  manager.py adopt <session> [--name NAME]")
    elif args.cmd == "proxies":
        rows = list_proxies(cfg)
        w = max((len(r["name"]) for r in rows), default=4)
        for r in rows:
            if r.get("found"):
                en = "" if "enabled" not in r else (" (on)" if r["enabled"] else " (off)")
                print(f"{r['name']:<{w}}  {r['host']}:{r['port']}{en}")
            else:
                print(f"{r['name']:<{w}}  — {r.get('reason','no proxy')}")
    elif args.cmd == "proxy":
        inst = cfg["by_name"].get(args.name) or die(f"no such instance: {args.name}")
        en = True if args.p_enable else (False if args.p_disable else None)
        if args.host is None and args.port is None and en is None:
            p = get_proxy(inst)
            print(f"{args.name}: {p['host']}:{p['port']}" if p.get("found") else f"{args.name}: {p.get('reason')}")
        else:
            try:
                r = set_proxy(inst, host=args.host, port=args.port, enabled=en)
                print(f"{args.name}: set to {r['host']}:{r['port']}")
            except ValueError as e:
                die(str(e))
    elif args.cmd == "proxybulk":
        tnames = [t for t in re.split(r"[,\s]+", args.targets) if t]
        if "all" in tnames:
            tnames = ["all"]
        elif "errored" in tnames:
            tnames = ["errored"]
        plist = [s for s in re.split(r"[,\n]+", args.plist) if s.strip()]
        try:
            rows = set_proxies_bulk(cfg, tnames, plist, mode=args.mode, do_restart=args.restart)
        except ValueError as e:
            die(str(e))
        for r in rows:
            if r.get("ok"):
                extra = f"  ({r['restart']})" if "restart" in r else ""
                print(f"{r['name']}: -> {r['host']}:{r['port']}{extra}")
            else:
                print(f"{r['name']}: error: {r.get('error')}")
    elif args.cmd == "proxyhealth":
        names = None if args.targets.strip() == "all" else [t for t in re.split(r"[,\s]+", args.targets) if t]
        try:
            rows = detect_proxy_issues(cfg, names=names, lines=args.lines)
        except ValueError as e:
            die(str(e))
        if not rows:
            print("no proxy-using instances")
        for r in rows:
            tag = "ERRORED" if r["errored"] else ("ok" if r["running"] else "stopped")
            line = f"{r['name']:<20} {(str(r['host']) + ':' + str(r['port'])):<24} {tag}"
            if r["errored"]:
                line += f"  ({r['hits']} hits) {r['evidence']}"
            print(line)
        n = sum(1 for r in rows if r["errored"])
        if rows:
            print(f"\n{n} errored / {len(rows)} proxy bots"
                  + (" — fix with: abm webshare import --targets errored --mode random --restart"
                     if n else ""))
    elif args.cmd == "selfupdate":
        try:
            res = self_update(do_restart=not args.no_restart)
        except ValueError as e:
            die(str(e))
        if res["updated"]:
            print(f"updated {res['old']} -> {res['new']}")
        else:
            print(f"already up to date ({res['new']})")
        if not args.no_restart:
            print("restarted manager service" if res.get("restarted")
                  else f"NOT restarted: {res.get('restart_error', 'unknown')} "
                       f"(run: sudo systemctl restart {SERVICE_NAME})")
    elif args.cmd == "autoupdate":
        if args.action == "status":
            st = autoupdate_status()
            print(f"auto-update timer: {'enabled' if st['enabled'] else 'disabled'} ({st['state']})")
        else:
            try:
                st = autoupdate_set(args.action == "on", schedule=args.schedule)
            except ValueError as e:
                die(str(e))
            if args.action == "on":
                print(f"auto-update enabled ({st.get('schedule')}); timer {st['state']}")
            else:
                print("auto-update disabled")
    elif args.cmd == "webshare":
        tnames = [t for t in re.split(r"[,\s]+", args.targets) if t]
        if "all" in tnames:
            tnames = ["all"]
        elif "errored" in tnames:
            tnames = ["errored"]
        countries = [c for c in re.split(r"[,\s]+", args.countries or "") if c]
        token = _webshare_token(cfg, args.token)
        if args.save_token and token:
            save_webshare_token(cfg, token)
            print("saved Webshare token")
        try:
            if args.action == "count":
                got = webshare_fetch(token, list_mode=args.list_mode,
                                     valid_only=not args.all_proxies, countries=countries or None,
                                     plan_id=args.plan_id)
                print(f"Webshare: {len(got)} proxies"
                      + (f" in {','.join(sorted({p['country'] for p in got if p.get('country')}))}"
                         if got else ""))
            else:
                res = webshare_import(cfg, tnames, auth=args.auth, token=token,
                                      assign_mode=args.mode, list_mode=args.list_mode,
                                      valid_only=not args.all_proxies, countries=countries or None,
                                      plan_id=args.plan_id, do_restart=args.restart)
                print(f"fetched {res['fetched']} proxies, auth={res['auth']}:")
                for r in res["assigned"]:
                    if r.get("ok"):
                        extra = f"  ({r['restart']})" if "restart" in r else ""
                        tag = " +auth" if r.get("auth") else ""
                        print(f"  {r['name']}: -> {r['host']}:{r['port']}{tag}{extra}")
                    else:
                        print(f"  {r['name']}: error: {r.get('error')}")
        except ValueError as e:
            die(str(e))
    elif args.cmd == "set":
        try:
            for nm, val in set_field(cfg, args.name, args.field, args.value):
                print(f"{nm}: {args.field} = {val}")
        except ValueError as e:
            die(str(e))
    elif args.cmd == "limits":
        inst = cfg["by_name"].get(args.name) or die(f"no such instance: {args.name}")
        try:
            if args.clear:
                res = set_limits(cfg, args.name, memory="", cpu=0)
            elif args.memory is None and args.cpu is None:
                res = inst.get("limits") or {}
            else:
                res = set_limits(cfg, args.name, memory=args.memory, cpu=args.cpu)
        except ValueError as e:
            die(str(e))
        enf = "" if _supports_cgroup_limits() or not res else "   (NOTE: not enforced here — see README)"
        print(f"{args.name}: " + (", ".join(f"{k}={v}" for k, v in res.items()) if res else "no limits") + enf)
    elif args.cmd == "files":
        try:
            d = fs_list(cfg, args.path or "")
        except ValueError as e:
            die(str(e))
        print(d["path"])
        for e in d["entries"]:
            tag = "d" if e["type"] == "dir" else "-"
            sz = "" if e["size"] is None else str(e["size"])
            print(f"  {tag} {e['name']}\t{sz}")
    elif args.cmd == "deploy":
        lim = {"memory": args.memory, "cpu": args.cpu} if (args.memory or args.cpu) else None
        try:
            deploy_proxy(args.config, args.name, args.dir, args.source,
                         owner_repo=args.repo, limits=lim, autostart=not args.no_autostart)
        except ValueError as e:
            die(str(e))
        seen = 0                       # stream the background job's log to stdout
        while True:
            snap = DEPLOY_JOB.snapshot()
            if len(snap["output"]) > seen:
                sys.stdout.write(snap["output"][seen:]); sys.stdout.flush()
                seen = len(snap["output"])
            if snap["status"] in ("done", "error"):
                break
            time.sleep(0.3)
        if DEPLOY_JOB.snapshot()["status"] == "error":
            sys.exit(1)
    elif args.cmd == "adopt":
        sk = [s for s in args.stop_keys.split(",") if s.strip()] if args.stop_keys else None
        try:
            inst = adopt_session(cfg, args.session, name=args.name,
                                 launch_cmd=args.launch_cmd, config_file=args.config_file,
                                 stop_keys=sk, stop_timeout=args.stop_timeout,
                                 autostart=args.autostart)
            extra = "  [autostart]" if inst.get("autostart") else ""
            print(f"adopted '{args.session}' as {inst['name']}  (dir={inst['dir']}, launch={inst['launch_cmd']}){extra}")
        except ValueError as e:
            die(str(e))
    elif args.cmd == "autostart":
        try:
            val = set_autostart(cfg, args.name, args.on)
            print(f"{args.name}: autostart {'enabled' if val else 'disabled'}")
        except ValueError as e:
            die(str(e))
    elif args.cmd == "boot":
        results = boot(cfg)
        if not results:
            print("No autostart instances configured.")
        else:
            for n, r in results.items():
                print(f"{n}: {r}")
    elif args.cmd == "setpassword":
        import getpass
        user = args.user or input("Username: ").strip()
        pw = args.password or getpass.getpass("Password: ")
        if not args.password:
            pw2 = getpass.getpass("Confirm password: ")
            if pw != pw2:
                die("passwords do not match")
        try:
            set_password(cfg, user, pw)
            print(f"web login set for user '{user}'. All existing sessions were cleared.")
            print("Note: the running server (if any) picks this up immediately.")
        except ValueError as e:
            die(str(e))
    elif args.cmd == "logout-all":
        bump_session_epoch(cfg)
        _SESSIONS.clear()
        print("all web sessions invalidated (running server included)")
    elif args.cmd == "sysinfo":
        info = _sysinfo()
        def gb(n): return f"{n/1e9:.1f} GB" if n else "?"
        up = info.get("uptime_sec") or 0
        print(f"os:      {info.get('os')}")
        print(f"cpus:    {info.get('cpus')}")
        print(f"memory:  {gb(info.get('mem_used'))} / {gb(info.get('mem_total'))} used")
        print(f"disk:    {gb(info.get('disk_used'))} / {gb(info.get('disk_total'))} used")
        load = info.get("load")
        print(f"load:    {', '.join(f'{x:.2f}' for x in load)}" if load else "load:    ?")
        print(f"uptime:  {up // 86400}d {(up % 86400) // 3600}h {(up % 3600) // 60}m")
        print(f"tmux:    {info.get('tmux_sessions')} sessions")
    elif args.cmd == "settings":
        theme = {}
        if args.theme is not None:
            theme["preset"] = args.theme
        if args.accent is not None:
            theme["accent"] = args.accent
        if args.font is not None:
            theme["font"] = args.font
        en = True if args.enable_system else (False if args.disable_system else None)
        try:
            out = save_settings(cfg, theme=theme or None, system_actions_enabled=en)
            print(f"theme preset:   {out['theme']['preset']}")
            print(f"theme accent:   {out['theme']['accent'] or '(preset default)'}")
            print(f"theme font:     {out['theme'].get('font', 'aquarius')}")
            print(f"system actions: {'enabled' if out['system_actions_enabled'] else 'disabled'}")
        except ValueError as e:
            die(str(e))
    elif args.cmd in ("update", "reboot"):
        try:
            print(run_system_action(cfg, args.cmd).get("note", "ok"))
        except PermissionError as e:
            die(str(e) + "\n(enable with:  manager.py settings --enable-system)")
        except ValueError as e:
            die(str(e))


# ---------------------------------------------------------------------------
# Web page (served as a single string)
# ---------------------------------------------------------------------------

SETUP_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aquarius Bot Manager — Welcome</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Sora:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0e12;--panel:#11171e;--line:#1d2730;--txt:#dfe7ee;--dim:#7b8a98;--acc:#3ddc97;--acc-dim:#1f7a55;--crash:#ff5d5d}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  font-family:'Sora',system-ui,sans-serif;color:var(--txt);
  background:radial-gradient(900px 500px at 80% -10%,#12372a33,transparent 60%),
   radial-gradient(700px 400px at 0% 0%,#10202e55,transparent 55%),var(--bg)}
.card{width:min(400px,92vw);background:linear-gradient(180deg,var(--panel),#0d1319);
  border:1px solid var(--line);border-radius:16px;padding:2rem 1.8rem;box-shadow:0 30px 80px #000a}
.brand{display:flex;align-items:center;gap:.6rem;font-weight:800;font-size:1.2rem;letter-spacing:-.02em;margin-bottom:.3rem}
.brand .dot{width:11px;height:11px;border-radius:50%;background:var(--acc);box-shadow:0 0 14px var(--acc)}
.sub{color:var(--dim);font-size:.82rem;margin-bottom:1.3rem;line-height:1.5}
label{display:block;font-size:.78rem;font-weight:600;color:var(--dim);margin:.8rem 0 .3rem}
input{width:100%;font-family:'Space Mono',monospace;font-size:.85rem;background:#06090c;color:#cdd9e2;
  border:1px solid var(--line);border-radius:9px;padding:.6rem .7rem}
input:focus{outline:none;border-color:var(--acc)}
button{width:100%;margin-top:1.3rem;cursor:pointer;border:1px solid var(--acc-dim);background:var(--panel);
  color:var(--acc);font-weight:700;font-size:.9rem;padding:.65rem;border-radius:10px;font-family:inherit;transition:.15s}
button:hover{background:#15201b}
button:disabled{opacity:.5;cursor:not-allowed}
.skip{margin-top:.9rem;width:100%;background:none;border:none;color:#586675;font-size:.72rem;
  cursor:pointer;text-decoration:underline;padding:.2rem}
.skip:hover{color:var(--dim)}
.msg{margin-top:.9rem;font-family:'Space Mono',monospace;font-size:.74rem;color:var(--crash);min-height:1em;text-align:center}
</style>
</head>
<body>
<div class="card">
  <div class="brand"><span class="dot"></span>Aquarius Bot Manager</div>
  <div class="sub">Welcome — create your admin login to finish setup. This is the only account; you can change it later from Settings.</div>
  <label for="u">Choose a username</label>
  <input id="u" autocomplete="username" autofocus onkeydown="k(event)">
  <label for="p">Choose a password</label>
  <input id="p" type="password" autocomplete="new-password" onkeydown="k(event)">
  <label for="p2">Confirm password</label>
  <input id="p2" type="password" autocomplete="new-password" onkeydown="k(event)">
  <button id="btn" onclick="setup()">Create login &amp; continue</button>
  <button class="skip" onclick="skip()">Skip — run open on localhost only</button>
  <div class="msg" id="msg"></div>
</div>
<script>
const $=id=>document.getElementById(id);
function k(e){ if(e.key==='Enter') setup(); }
async function setup(){
  const username=$('u').value.trim(), password=$('p').value, p2=$('p2').value;
  if(!username||!password){ $('msg').textContent='enter a username and password'; return; }
  if(password.length<6){ $('msg').textContent='password must be at least 6 characters'; return; }
  if(password!==p2){ $('msg').textContent='passwords do not match'; return; }
  $('btn').disabled=true; $('msg').style.color='var(--dim)'; $('msg').textContent='creating…';
  try{
    const r=await fetch('/api/setup',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username,password})});
    const d=await r.json();
    if(r.ok&&d.ok){ location.href='/'; return; }
    $('msg').style.color='var(--crash)'; $('msg').textContent='✗ '+(d.error||'setup failed');
  }catch(e){ $('msg').style.color='var(--crash)'; $('msg').textContent='✗ connection error'; }
  $('btn').disabled=false;
}
async function skip(){
  if(!confirm('Run with no login? Only do this if the manager stays on localhost (e.g. reached over an SSH tunnel). Anyone who can reach the port will have full control.')) return;
  try{
    const r=await fetch('/api/setup/skip',{method:'POST'});
    if(r.ok){ location.href='/'; return; }
  }catch(e){}
  $('msg').style.color='var(--crash)'; $('msg').textContent='✗ could not save';
}
</script>
</body>
</html>
"""

LOGIN_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aquarius Bot Manager — Sign in</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Sora:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0e12;--panel:#11171e;--line:#1d2730;--txt:#dfe7ee;--dim:#7b8a98;--acc:#3ddc97;--acc-dim:#1f7a55;--crash:#ff5d5d}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  font-family:'Sora',system-ui,sans-serif;color:var(--txt);
  background:radial-gradient(900px 500px at 80% -10%,#12372a33,transparent 60%),
   radial-gradient(700px 400px at 0% 0%,#10202e55,transparent 55%),var(--bg)}
.card{width:min(380px,92vw);background:linear-gradient(180deg,var(--panel),#0d1319);
  border:1px solid var(--line);border-radius:16px;padding:2rem 1.8rem;box-shadow:0 30px 80px #000a}
.brand{display:flex;align-items:center;gap:.6rem;font-weight:800;font-size:1.2rem;letter-spacing:-.02em;margin-bottom:.3rem}
.brand .dot{width:11px;height:11px;border-radius:50%;background:var(--acc);box-shadow:0 0 14px var(--acc)}
.sub{color:var(--dim);font-size:.8rem;margin-bottom:1.4rem}
label{display:block;font-size:.78rem;font-weight:600;color:var(--dim);margin:.8rem 0 .3rem}
input{width:100%;font-family:'Space Mono',monospace;font-size:.85rem;background:#06090c;color:#cdd9e2;
  border:1px solid var(--line);border-radius:9px;padding:.6rem .7rem}
input:focus{outline:none;border-color:var(--acc)}
button{width:100%;margin-top:1.3rem;cursor:pointer;border:1px solid var(--acc-dim);background:var(--panel);
  color:var(--acc);font-weight:700;font-size:.9rem;padding:.65rem;border-radius:10px;font-family:inherit;transition:.15s}
button:hover{background:#15201b}
button:disabled{opacity:.5;cursor:not-allowed}
.msg{margin-top:.9rem;font-family:'Space Mono',monospace;font-size:.74rem;color:var(--crash);min-height:1em;text-align:center}
.hint{margin-top:1.2rem;font-size:.68rem;color:#586675;text-align:center;line-height:1.5}
</style>
</head>
<body>
<div class="card">
  <div class="brand"><span class="dot"></span>Aquarius Bot Manager</div>
  <div class="sub" id="sub">Sign in to continue</div>
  <label for="u">Username</label>
  <input id="u" autocomplete="username" autofocus onkeydown="k(event)">
  <label for="p">Password</label>
  <input id="p" type="password" autocomplete="current-password" onkeydown="k(event)">
  <button id="btn" onclick="login()">Sign in</button>
  <div class="msg" id="msg"></div>
  <div class="hint" id="hint"></div>
</div>
<script>
const $=id=>document.getElementById(id);
async function init(){
  try{
    const r=await fetch('/api/authstatus'); const d=await r.json();
    if(!d.required){ $('sub').textContent='No password set yet';
      $('hint').innerHTML='This manager is running open (no login). To add one, run <b>abm setpassword</b> on the server, then reload.';
      $('btn').textContent='Open app'; $('btn').onclick=()=>location.href='/'; }
  }catch(e){}
}
function k(e){ if(e.key==='Enter') login(); }
async function login(){
  const username=$('u').value.trim(), password=$('p').value;
  if(!username||!password){ $('msg').textContent='enter username and password'; return; }
  $('btn').disabled=true; $('msg').style.color='var(--dim)'; $('msg').textContent='signing in…';
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username,password})});
    const d=await r.json();
    if(r.ok&&d.ok){ location.href='/'; return; }
    $('msg').style.color='var(--crash)'; $('msg').textContent='✗ '+(d.error||'login failed');
  }catch(e){ $('msg').style.color='var(--crash)'; $('msg').textContent='✗ connection error'; }
  $('btn').disabled=false;
}
init();
</script>
</body>
</html>
"""

INVITE_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aquarius Bot Manager — Accept invite</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Sora:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0e12;--panel:#11171e;--line:#1d2730;--txt:#dfe7ee;--dim:#7b8a98;--acc:#3ddc97;--acc-dim:#1f7a55;--crash:#ff5d5d}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  font-family:'Sora',system-ui,sans-serif;color:var(--txt);
  background:radial-gradient(900px 500px at 80% -10%,#12372a33,transparent 60%),
   radial-gradient(700px 400px at 0% 0%,#10202e55,transparent 55%),var(--bg)}
.card{width:min(400px,92vw);background:linear-gradient(180deg,var(--panel),#0d1319);
  border:1px solid var(--line);border-radius:16px;padding:2rem 1.8rem;box-shadow:0 30px 80px #000a}
.brand{display:flex;align-items:center;gap:.6rem;font-weight:800;font-size:1.2rem;letter-spacing:-.02em;margin-bottom:.3rem}
.brand .dot{width:11px;height:11px;border-radius:50%;background:var(--acc);box-shadow:0 0 14px var(--acc)}
.sub{color:var(--dim);font-size:.8rem;margin-bottom:1rem;line-height:1.5}
.grant{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1rem}
.chip{font-family:'Space Mono',monospace;font-size:.7rem;padding:.25rem .55rem;border-radius:20px;
  border:1px solid var(--acc-dim);background:#15201b;color:var(--acc)}
.chip.s{border-color:var(--line);background:#0d141b;color:var(--dim)}
label{display:block;font-size:.78rem;font-weight:600;color:var(--dim);margin:.8rem 0 .3rem}
input{width:100%;font-family:'Space Mono',monospace;font-size:.85rem;background:#06090c;color:#cdd9e2;
  border:1px solid var(--line);border-radius:9px;padding:.6rem .7rem}
input:focus{outline:none;border-color:var(--acc)}
input:disabled{opacity:.7}
button{width:100%;margin-top:1.3rem;cursor:pointer;border:1px solid var(--acc-dim);background:var(--panel);
  color:var(--acc);font-weight:700;font-size:.9rem;padding:.65rem;border-radius:10px;font-family:inherit;transition:.15s}
button:hover{background:#15201b}
button:disabled{opacity:.5;cursor:not-allowed}
.msg{margin-top:.9rem;font-family:'Space Mono',monospace;font-size:.74rem;color:var(--crash);min-height:1em;text-align:center}
.hint{margin-top:1.2rem;font-size:.68rem;color:#586675;text-align:center;line-height:1.5}
</style>
</head>
<body>
<div class="card">
  <div class="brand"><span class="dot"></span>Aquarius Bot Manager</div>
  <div class="sub">You've been invited to control bots. Pick a password to create your account.</div>
  <div class="grant"><span class="chip">role: __ROLE__</span><span class="chip s">access: __SCOPE__</span></div>
  <label for="u">Username</label>
  <input id="u" autocomplete="username" autofocus onkeydown="k(event)">
  <label for="p">Password</label>
  <input id="p" type="password" autocomplete="new-password" onkeydown="k(event)" placeholder="at least 6 characters">
  <label for="p2">Confirm password</label>
  <input id="p2" type="password" autocomplete="new-password" onkeydown="k(event)">
  <button id="btn" onclick="redeem()">Create account &amp; sign in</button>
  <div class="msg" id="msg"></div>
  <div class="hint">This link works once. After you create your account, sign in normally with your username and password.</div>
</div>
<script>
const $=id=>document.getElementById(id);
const TOKEN="__TOKEN__", PRESET="__PRESET_USER__";
if(PRESET){ $('u').value=PRESET; $('u').disabled=true; $('p').focus(); }
function k(e){ if(e.key==='Enter') redeem(); }
async function redeem(){
  const username=(PRESET||$('u').value.trim()), password=$('p').value, p2=$('p2').value;
  if(!username){ $('msg').textContent='enter a username'; return; }
  if(!password||password.length<6){ $('msg').textContent='password must be at least 6 characters'; return; }
  if(password!==p2){ $('msg').textContent='passwords do not match'; return; }
  $('btn').disabled=true; $('msg').style.color='var(--dim)'; $('msg').textContent='creating account…';
  try{
    const r=await fetch('/api/invite/redeem',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token:TOKEN,username,password})});
    const d=await r.json();
    if(r.ok&&d.ok){ location.href='/'; return; }
    $('msg').style.color='var(--crash)'; $('msg').textContent='✗ '+(d.error||'could not create account');
  }catch(e){ $('msg').style.color='var(--crash)'; $('msg').textContent='✗ connection error'; }
  $('btn').disabled=false;
}
</script>
</body>
</html>
"""

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aquarius Bot Manager</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Sora:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0a0e12; --panel:#11171e; --panel-2:#0d1319; --line:#1d2730;
  --txt:#dfe7ee; --dim:#7b8a98; --acc:#3ddc97; --acc-dim:#1f7a55;
  --run:#3ddc97; --stop:#5a6b78; --crash:#ff5d5d; --warn:#ffb454;
  --mono:'Space Mono',ui-monospace,monospace; --sans:'Sora',system-ui,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:
   radial-gradient(900px 500px at 85% -10%, #12372a22, transparent 60%),
   radial-gradient(700px 400px at 0% 0%, #10202e44, transparent 55%),
   var(--bg);
  color:var(--txt);font-family:var(--sans);min-height:100vh;}
header{display:flex;align-items:center;justify-content:space-between;gap:1rem;
  padding:1.1rem 1.6rem;border-bottom:1px solid var(--line);
  position:sticky;top:0;backdrop-filter:blur(8px);background:#0a0e12cc;z-index:5;}
.brand{display:flex;align-items:center;gap:.7rem;font-weight:800;letter-spacing:-.02em;font-size:1.15rem}
.brand .dot{width:11px;height:11px;border-radius:50%;background:var(--acc);box-shadow:0 0 14px var(--acc)}
.brand small{font-family:var(--mono);font-weight:400;color:var(--dim);font-size:.7rem;letter-spacing:0}
.bulk{display:flex;gap:.5rem;flex-wrap:wrap}
button{font-family:var(--sans);cursor:pointer;border:1px solid var(--line);
  background:var(--panel);color:var(--txt);padding:.5rem .85rem;border-radius:9px;
  font-weight:600;font-size:.82rem;transition:.15s;}
button:hover{border-color:var(--acc-dim);transform:translateY(-1px)}
button:active{transform:translateY(0)}
button.go{border-color:var(--acc-dim);color:var(--acc)}
button.warn{border-color:#5a3b1f;color:var(--warn)}
button.danger{border-color:#5a1f1f;color:var(--crash)}
button:disabled{opacity:.4;cursor:not-allowed;transform:none}
main{padding:1.6rem;max-width:1200px;margin:0 auto}
.meta{font-family:var(--mono);font-size:.72rem;color:var(--dim);margin-bottom:1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:1rem}
.card{background:linear-gradient(180deg,var(--panel),var(--panel-2));
  border:1px solid var(--line);border-radius:14px;padding:1.05rem 1.1rem;
  position:relative;overflow:hidden}
.card::before{content:"";position:absolute;inset:0 auto 0 0;width:3px;background:var(--stop)}
.card.running::before{background:var(--run);box-shadow:0 0 18px var(--run)}
.card.crashed::before{background:var(--crash);box-shadow:0 0 18px var(--crash)}
.card .top{display:flex;align-items:flex-start;justify-content:space-between;gap:.5rem}
.name{font-weight:700;font-size:1.05rem;letter-spacing:-.01em;word-break:break-all}
.badge{font-family:var(--mono);font-size:.66rem;text-transform:uppercase;letter-spacing:.08em;
  padding:.25rem .5rem;border-radius:6px;border:1px solid var(--line);white-space:nowrap}
.badge.running{color:var(--run);border-color:var(--acc-dim)}
.badge.stopped{color:var(--stop)}
.badge.crashed{color:var(--crash);border-color:#5a1f1f}
.star{cursor:pointer;font-size:1rem;line-height:1;color:var(--stop);user-select:none;transition:.15s}
.star:hover{transform:scale(1.2)}
.star.on{color:var(--warn)}
.path{font-family:var(--mono);font-size:.7rem;color:var(--dim);margin:.45rem 0 .15rem;word-break:break-all}
.cmd{font-family:var(--mono);font-size:.7rem;color:#586675;margin-bottom:.85rem;word-break:break-all}
/* connection state line (replaces the launch-cmd line) — server/queue state, colour-coded */
.cstate{font-family:var(--mono);font-size:.74rem;font-weight:700;margin:.1rem 0 .85rem;display:flex;align-items:center;gap:.45rem;letter-spacing:.01em}
.cstate::before{content:"";width:8px;height:8px;border-radius:50%;background:currentColor;box-shadow:0 0 9px currentColor;flex:none}
.cstate.s-offline{color:#ff3b30}      /* apple red */
.cstate.s-in-queue{color:#ffd60a}     /* yellow */
.cstate.s-online{color:#30d158}       /* light green */
.cstate.s-updating{color:#64d2ff}     /* light blue */
.cstate.s-restarting{color:#d18aff}   /* light purple */
.cstate.s-updating::before,.cstate.s-restarting::before{animation:cpulse 1.1s ease-in-out infinite}
@keyframes cpulse{0%,100%{opacity:1}50%{opacity:.3}}
/* proxy fork + version chip on each bot card */
.ptag{display:inline-flex;align-items:center;gap:.35rem;font-family:var(--mono);font-size:.66rem;font-weight:600;
  color:var(--dim);border:1px solid var(--line);border-radius:6px;padding:.12rem .45rem;margin:.1rem 0 .15rem}
.ptag .pdot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:none}
.ptag.aqua{color:var(--acc);border-color:var(--acc-dim)}
.ptag.zenith{color:#5cc8ff;border-color:#27506b}
.row{display:flex;gap:.4rem;flex-wrap:wrap}
/* Three centre tracks: the icon sits in the left track hugging the label, while the
   label lives in its own centre track so differing ⟳/▶/■ glyph widths can't shove it. */
.row button{flex:1 1 0;min-width:84px;display:grid;grid-template-columns:1fr auto 1fr;align-items:center}
.row button .ic{grid-column:1;justify-self:end;margin-right:.35rem;font-style:normal;display:inline-flex;align-items:center}
.row button .lbl{grid-column:2;justify-self:center}
/* Icon-only utility buttons (menu / rename / delete) stay compact instead of grabbing an
   equal flex share — that crowding was shrinking the action buttons below usable size. */
.row button.mini{flex:0 0 auto;min-width:40px;display:inline-flex;justify-content:center;padding:.5rem .55rem}
.spin{display:inline-block;width:11px;height:11px;border:2px solid #ffffff33;border-top-color:var(--acc);
  border-radius:50%;animation:sp .7s linear infinite;vertical-align:-1px;margin-right:.3rem}
@keyframes sp{to{transform:rotate(360deg)}}
.empty{color:var(--dim);font-family:var(--mono);font-size:.85rem;padding:2rem 0}
/* drawer */
.scrim{position:fixed;inset:0;background:#000a;backdrop-filter:blur(2px);display:none;z-index:20}
.scrim.open{display:block}
.drawer{position:fixed;top:0;right:0;height:100%;width:min(760px,94vw);background:var(--panel);
  border-left:1px solid var(--line);transform:translateX(100%);transition:.22s;z-index:21;
  display:flex;flex-direction:column}
.drawer.open{transform:none}
.drawer header{position:static;background:none;backdrop-filter:none}
.tabs{display:flex;gap:.4rem;padding:0 1.2rem;border-bottom:1px solid var(--line)}
.tab{padding:.6rem .2rem;margin-right:1rem;color:var(--dim);border-bottom:2px solid transparent;
  cursor:pointer;font-weight:600;font-size:.85rem}
.tab.active{color:var(--acc);border-color:var(--acc)}
.drawer .body{flex:1;overflow:auto;padding:1.2rem}
/* console tab: log box is its own internal scroller so the command bar stays pinned
   and a long log never pushes content past the drawer (no more top/bottom cutoff) */
#tabLogs{display:flex;flex-direction:column;height:100%}
#logWrap{position:relative;flex:1;min-height:0;display:flex}
pre.log{font-family:var(--mono);font-size:.74rem;line-height:1.5;white-space:pre-wrap;word-break:break-word;
  background:#06090c;border:1px solid var(--line);border-radius:10px;padding:.9rem;margin:0;color:#b9c7d2;
  flex:1;min-height:0;overflow:auto;overflow-anchor:none}
/* follow-tail "jump to latest" pill — shown only when the user has scrolled up (paused) */
.logpill{position:absolute;left:50%;bottom:.8rem;transform:translateX(-50%);
  background:var(--acc);color:#062014;border:1px solid var(--acc);border-radius:20px;
  padding:.34rem .8rem;font-size:.72rem;font-weight:700;cursor:pointer;
  box-shadow:0 6px 18px #000a;display:flex;align-items:center;gap:.35rem;animation:pillin .18s ease-out}
.logpill:hover{transform:translateX(-50%) translateY(-1px);border-color:var(--acc)}
@keyframes pillin{from{opacity:0;transform:translateX(-50%) translateY(6px)}}
textarea{width:100%;min-height:55vh;font-family:var(--mono);font-size:.78rem;line-height:1.5;
  background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:10px;padding:.9rem;resize:vertical}
/* live command bar */
.cmdbar{display:flex;align-items:center;gap:.5rem;margin-top:.6rem;
  background:#06090c;border:1px solid var(--line);border-radius:10px;padding:.35rem .55rem}
.cmdbar:focus-within{border-color:var(--acc-dim)}
.cmdbar .prompt{font-family:var(--mono);color:var(--acc);font-weight:700}
.cmdbar input{flex:1;background:none;border:none;color:#cdd9e2;font-family:var(--mono);font-size:.78rem;outline:none}
.cmdbar button{padding:.35rem .7rem;font-size:.78rem}
/* structured config form — schema cards */
#cfgForm{display:flex;flex-direction:column;gap:1rem}
.cfggroup{display:flex;flex-direction:column;gap:.5rem}
.cfggroup .gh{font-family:var(--mono);font-size:.64rem;text-transform:uppercase;letter-spacing:.12em;
  color:var(--acc);font-weight:700;padding:.1rem .2rem;opacity:.85}
.modcard{border:1px solid var(--line);border-radius:12px;overflow:hidden;background:linear-gradient(180deg,var(--panel),var(--panel-2));position:relative}
.modcard::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:transparent;transition:.15s}
.modcard.active::before{background:var(--acc);box-shadow:0 0 12px var(--acc)}
.mhd{display:flex;align-items:center;gap:.6rem;cursor:pointer;padding:.6rem .8rem;user-select:none}
.mhd:hover{background:#ffffff08}
.mhd .caret{font-size:.65rem;color:var(--dim);transition:.18s}
.modcard.open .mhd .caret{transform:rotate(90deg)}
.mhd .mtitle{flex:1;font-weight:700;font-size:.9rem;letter-spacing:-.01em}
.mbody{display:none;flex-direction:column;padding:.3rem .8rem .7rem;gap:.1rem;border-top:1px solid var(--line)}
.modcard.open .mbody{display:flex}
.frow{display:flex;align-items:center;gap:.7rem;min-height:34px;padding:.15rem 0}
.frow+.frow{border-top:1px solid #ffffff08}
.flabel{flex:1;font-size:.82rem;color:var(--txt);word-break:break-word}
.flabel .unit{font-family:var(--mono);font-size:.6rem;color:#586675;margin-left:.3rem;padding:.05rem .3rem;border:1px solid var(--line);border-radius:5px}
.fctrl{display:flex;align-items:center;gap:.5rem;justify-content:flex-end;min-width:46%}
.fctrl input[type=text],.fctrl input[type=password]{font-family:var(--mono);font-size:.76rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:7px;padding:.34rem .5rem;width:100%}
.fctrl input:focus,.fctrl select:focus{outline:none;border-color:var(--acc)}
.fctrl select{font-family:var(--sans);font-size:.78rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:7px;padding:.34rem .5rem;cursor:pointer}
.snum{width:62px;font-family:var(--mono);font-size:.76rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:7px;padding:.34rem .4rem;text-align:right}
.snum.wide{width:110px}
.slider{-webkit-appearance:none;appearance:none;height:5px;border-radius:5px;background:#2a3640;flex:1;max-width:170px;cursor:pointer}
.slider::-webkit-slider-thumb{-webkit-appearance:none;width:15px;height:15px;border-radius:50%;background:var(--acc);box-shadow:0 0 8px var(--acc);cursor:pointer}
.slider::-moz-range-thumb{width:15px;height:15px;border:none;border-radius:50%;background:var(--acc);box-shadow:0 0 8px var(--acc);cursor:pointer}
/* toggle */
.tgl{position:relative;width:38px;height:20px;border-radius:20px;background:#2a3640;border:1px solid var(--line);cursor:pointer;transition:.15s;flex:none}
.tgl::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:#8696a3;transition:.15s}
.tgl.on{background:var(--acc-dim);border-color:var(--acc)}
.tgl.on::after{left:20px;background:var(--acc)}
.arrlist{display:flex;flex-direction:column;gap:.3rem;width:100%}
.arrlist .ai{display:flex;gap:.35rem}
.arrlist .ai input{flex:1}
.arrlist .ai button,.arrlist .add{padding:.2rem .5rem;font-size:.72rem}
.bar{display:flex;gap:.5rem;align-items:center;padding:.8rem 1.2rem;border-top:1px solid var(--line)}
.bar .msg{font-family:var(--mono);font-size:.74rem;color:var(--dim);flex:1}
.close{font-size:1.2rem;line-height:1;padding:.2rem .55rem}
/* modal */
.modal{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  width:min(520px,92vw);max-height:92vh;overflow-y:auto;background:var(--panel);border:1px solid var(--line);
  border-radius:16px;padding:1.3rem 1.4rem;display:flex;flex-direction:column;gap:.7rem;
  box-shadow:0 30px 80px #000a}
/* the modal is a flex-column scroll container; stop its children (cards) from shrinking +
   clipping (they have overflow:hidden) when content exceeds max-height — let the modal scroll */
.modal>*{flex-shrink:0}
.mhead{font-weight:800;font-size:1.15rem;letter-spacing:-.02em;margin-bottom:.2rem}
.modal label{display:flex;flex-direction:column;gap:.3rem;font-size:.82rem;font-weight:600;color:var(--dim)}
.modal .hint{font-weight:400;font-size:.68rem;color:#586675;font-family:var(--mono)}
.modal input{font-family:var(--mono);font-size:.82rem;background:#06090c;color:#cdd9e2;
  border:1px solid var(--line);border-radius:9px;padding:.55rem .6rem}
.modal input:focus{outline:none;border-color:var(--acc-dim)}
.mrow{display:flex;gap:.7rem}
.mbar{display:flex;align-items:center;gap:.5rem;margin-top:.4rem}
.mbar .msg{flex:1;font-family:var(--mono);font-size:.72rem;color:var(--crash)}
.scand{border:1px solid var(--line);border-radius:10px;padding:.6rem .7rem;display:flex;
  align-items:center;gap:.6rem;background:var(--panel-2)}
.scand.likely{border-color:var(--acc-dim)}
.scand .si{flex:1;min-width:0}
.scand .sn{font-weight:700;font-size:.9rem;display:flex;align-items:center;gap:.4rem}
.scand .tag{font-family:var(--mono);font-size:.6rem;text-transform:uppercase;letter-spacing:.06em;
  padding:.1rem .4rem;border-radius:5px;border:1px solid var(--acc-dim);color:var(--acc)}
.scand .sp{font-family:var(--mono);font-size:.68rem;color:var(--dim);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.scand .sr{font-family:var(--mono);font-size:.64rem;color:#586675}
.chip{cursor:pointer;border:1px solid var(--line);background:var(--panel-2);border-radius:9px;
  padding:.4rem .6rem;font-size:.78rem;font-weight:600;display:flex;align-items:center;gap:.4rem}
.chip.sel{border-color:var(--acc);color:var(--acc)}
.chip .sw{width:14px;height:14px;border-radius:50%;border:1px solid #fff3}
.sysgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:.6rem}
.sysgrid .s{background:var(--panel-2);border:1px solid var(--line);border-radius:10px;padding:.6rem .7rem}
.sysgrid .s .k{font-family:var(--mono);font-size:.62rem;text-transform:uppercase;letter-spacing:.07em;color:var(--dim)}
.sysgrid .s .v{font-weight:700;font-size:1rem;margin-top:.15rem}
.sysgrid .s .b{height:4px;border-radius:3px;background:#ffffff14;margin-top:.4rem;overflow:hidden}
.sysgrid .s .b i{display:block;height:100%;background:var(--acc)}
/* host gauge strip */
.hoststrip{display:flex;gap:.6rem;flex-wrap:wrap;margin-bottom:1rem}
.gauge{flex:1;min-width:150px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:.5rem .7rem}
.gauge .k{font-family:var(--mono);font-size:.62rem;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);display:flex;justify-content:space-between}
.gauge .v{font-weight:700;font-size:.88rem;margin:.15rem 0 .35rem}
.gauge .b{height:5px;border-radius:3px;background:#ffffff14;overflow:hidden}
.gauge .b i{display:block;height:100%;background:var(--acc);transition:width .4s}
.gauge.warn .b i{background:var(--warn)} .gauge.warn .v{color:var(--warn)}
.gauge.crit .b i{background:var(--crash)} .gauge.crit .v{color:var(--crash)}
/* file manager */
.fbbar{display:flex;gap:.4rem;flex-wrap:wrap;align-items:center;margin-bottom:.5rem}
.fbbar select{font-family:var(--mono);font-size:.74rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:8px;padding:.4rem .5rem;max-width:50%}
.fbpath{font-family:var(--mono);font-size:.7rem;color:var(--dim);margin-bottom:.5rem;word-break:break-all}
.frow2{display:flex;align-items:center;gap:.55rem;padding:.4rem .55rem;border:1px solid var(--line);border-radius:8px;margin-bottom:.3rem;background:var(--panel-2)}
.frow2:hover{border-color:var(--acc-dim)}
.frow2 .ficon{width:1.1rem;text-align:center}
.frow2 .fn{flex:1;cursor:pointer;font-size:.85rem;word-break:break-all}
.frow2 .fmeta{font-family:var(--mono);font-size:.62rem;color:#586675;white-space:nowrap}
.frow2 button{padding:.25rem .5rem;font-size:.72rem}
/* per-card resource bars */
.cstats{display:flex;gap:.6rem;margin:.1rem 0 .75rem}
.cstats .cs{flex:1}
.cstats .cs .cl{font-family:var(--mono);font-size:.62rem;color:var(--dim);display:flex;justify-content:space-between}
.cstats .cs .b{height:4px;border-radius:3px;background:#ffffff14;margin-top:.25rem;overflow:hidden}
.cstats .cs .b i{display:block;height:100%;background:var(--acc);transition:width .4s}
.cstats .cs.warn .b i{background:var(--warn)} .cstats .cs.crit .b i{background:var(--crash)}
/* ===== sidebar shell (v1.5 — selectable in Settings → Appearance) ===== */
.app{display:flex;min-height:100vh;align-items:stretch}
.app.right{flex-direction:row-reverse}
.content{flex:1;min-width:0;display:flex;flex-direction:column}
.app.has-side .content main{max-width:none;margin:0;width:100%}
.side{flex:none;background:linear-gradient(180deg,var(--panel),var(--panel-2));
  border-right:1px solid var(--line);display:flex;flex-direction:column;position:sticky;top:0;height:100vh;width:238px;
  overflow-y:auto}
.app.right .side{border-right:none;border-left:1px solid var(--line)}
.sbrand{display:flex;align-items:center;gap:.6rem;padding:1.05rem 1rem .7rem;font-weight:800;letter-spacing:-.02em;font-size:1.02rem}
.sbrand .dot{width:11px;height:11px;border-radius:50%;background:var(--acc);box-shadow:0 0 14px var(--acc);flex:none}
.sbrand .txt small{display:block;font-family:var(--mono);font-weight:400;color:var(--dim);font-size:.6rem;letter-spacing:0}
.boxchip{margin:0 .8rem .55rem;display:flex;align-items:center;gap:.5rem;padding:.5rem .6rem;border:1px solid var(--line);border-radius:10px;background:var(--panel-2);cursor:pointer;font-size:.8rem}
.boxchip .bdot{width:8px;height:8px;border-radius:50%;background:var(--run);box-shadow:0 0 8px var(--run);flex:none}
.boxchip .car{margin-left:auto;color:var(--dim);font-size:.7rem}
.nav{display:flex;flex-direction:column;gap:.12rem;padding:.3rem .6rem}
.navg{font-family:var(--mono);font-size:.55rem;text-transform:uppercase;letter-spacing:.14em;color:#586675;padding:.7rem .65rem .25rem}
.nav a{display:flex;align-items:center;gap:.75rem;padding:.55rem .65rem;border-radius:9px;color:var(--dim);font-weight:600;font-size:.86rem;text-decoration:none;cursor:pointer;position:relative;white-space:nowrap}
.nav a:hover{background:#ffffff08;color:var(--txt)}
.nav a.active{background:#3ddc9714;color:var(--acc)}
.nav a .ic{width:1.15rem;text-align:center;font-size:1.02rem;flex:none}
.nav a .pip{margin-left:auto;font-family:var(--mono);font-size:.58rem;color:var(--dim);background:#ffffff10;border-radius:10px;padding:.05rem .42rem}
.nav a.active .pip{color:var(--acc);background:#3ddc9722}
.nav a .pip.warn{color:var(--warn);background:#ffb45420}
.sgrow{flex:1}
.sfoot{padding:.5rem .6rem;border-top:1px solid var(--line);display:flex;flex-direction:column;gap:.12rem}
.side.rail{width:64px}
.side.rail .sbrand{justify-content:center;padding:1.05rem 0 .7rem}
.side.rail .sbrand .txt,.side.rail .navg,.side.rail .nav a .lbl,.side.rail .nav a .pip,
.side.rail .boxchip .txt,.side.rail .boxchip .car,.side.rail .sfoot .lbl,.side.rail .squick{display:none}
.side.rail .nav a{justify-content:center;padding:.62rem 0}
.side.rail .boxchip{justify-content:center;padding:.5rem 0;margin:0 .55rem .55rem}
.side.rail .nav a.active{box-shadow:inset 3px 0 0 var(--acc)}
.app.right .side.rail .nav a.active{box-shadow:inset -3px 0 0 var(--acc)}
.railtoggle{margin:.25rem .55rem .35rem;text-align:center;color:var(--dim);cursor:pointer;font-size:.85rem;border:1px dashed var(--line);border-radius:8px;padding:.32rem 0}
.svitals{padding:.55rem .8rem;border-top:1px solid var(--line);display:flex;flex-direction:column;gap:.5rem}
.svit .k{font-family:var(--mono);font-size:.57rem;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);display:flex;justify-content:space-between}
.svit .b{height:5px;border-radius:3px;background:#ffffff14;overflow:hidden;margin-top:.22rem}
.svit .b i{display:block;height:100%;background:var(--acc);transition:width .4s}
.svit.warn .b i{background:var(--warn)} .svit.crit .b i{background:var(--crash)}
.squick{display:flex;gap:.4rem;padding:.45rem .8rem .1rem}
.squick button{flex:1;padding:.42rem 0;font-size:.74rem}
.spalette{margin:.15rem .8rem .55rem;display:flex;align-items:center;gap:.5rem;padding:.5rem .6rem;border:1px solid var(--line);border-radius:10px;background:#06090c;color:var(--dim);font-family:var(--mono);font-size:.72rem;cursor:text}
.spalette .kbd{margin-left:auto;font-size:.58rem;border:1px solid var(--line);border-radius:5px;padding:.05rem .32rem}
.salert{margin:0 .8rem .55rem;display:flex;align-items:center;gap:.5rem;font-size:.75rem;color:var(--warn);border:1px solid #5a3b1f;border-radius:9px;padding:.45rem .6rem;background:#ffb4540d;cursor:pointer}
.roster{flex:1;overflow:auto;padding:.1rem .6rem .5rem}
.rhd{font-family:var(--mono);font-size:.55rem;text-transform:uppercase;letter-spacing:.12em;color:#586675;padding:.55rem .45rem .3rem;display:flex;justify-content:space-between}
.rrow{display:flex;align-items:center;gap:.55rem;padding:.42rem .5rem;border-radius:8px;cursor:pointer}
.rrow:hover{background:#ffffff08}
.rrow.sel{background:#3ddc9714}
.rrow .rd{width:8px;height:8px;border-radius:50%;flex:none;background:var(--stop)}
.rrow.run .rd{background:var(--run);box-shadow:0 0 7px var(--run)}
.rrow.crash .rd{background:var(--crash);box-shadow:0 0 7px var(--crash)}
.rrow .rn{flex:1;font-size:.82rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rrow .rc{font-family:var(--mono);font-size:.6rem;color:var(--dim)}
.topbar{display:flex;align-items:center;gap:.9rem;padding:.95rem 1.6rem;border-bottom:1px solid var(--line);position:sticky;top:0;background:#0a0e12cc;backdrop-filter:blur(8px);z-index:5}
.topbar .pt{font-weight:800;font-size:1.08rem;letter-spacing:-.02em}
.topbar .sub{font-family:var(--mono);font-size:.64rem;color:var(--dim)}
.topbar .sp{flex:1}
/* ===== new-page widgets ===== */
.pagehd{display:flex;align-items:baseline;gap:.8rem;margin-bottom:.1rem;flex-wrap:wrap}
.pagehd h1{font-size:1.5rem;margin:0;letter-spacing:-.02em}
.pagehd .sub{font-family:var(--mono);font-size:.7rem;color:var(--dim)}
.sumstrip{display:flex;gap:.7rem;flex-wrap:wrap;margin:1rem 0}
.sumstrip .s{flex:1;min-width:128px;background:linear-gradient(180deg,var(--panel),var(--panel-2));border:1px solid var(--line);border-radius:12px;padding:.7rem .9rem}
.sumstrip .s .k{font-family:var(--mono);font-size:.59rem;text-transform:uppercase;letter-spacing:.08em;color:var(--dim)}
.sumstrip .s .v{font-weight:800;font-size:1.45rem;margin-top:.1rem;letter-spacing:-.02em}
.sumstrip .s.good .v{color:var(--acc)} .sumstrip .s.bad .v{color:var(--crash)} .sumstrip .s.warnv .v{color:var(--warn)}
table.tbl{width:100%;border-collapse:collapse;font-size:.82rem}
table.tbl th{font-family:var(--mono);font-size:.59rem;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);text-align:left;padding:.55rem .7rem;border-bottom:1px solid var(--line)}
table.tbl td{padding:.55rem .7rem;border-bottom:1px solid #ffffff08}
table.tbl tr:hover td{background:#ffffff05}
.panel{background:linear-gradient(180deg,var(--panel),var(--panel-2));border:1px solid var(--line);border-radius:14px;padding:1.1rem 1.2rem}
/* an offline box card: darkened + muted (theme-safe via opacity); brightens on
   hover so its Reconnect button stays easy to read and click */
.panel.boxoff{opacity:.5;filter:grayscale(.5);border-style:dashed;transition:opacity .15s,filter .15s}
.panel.boxoff:hover{opacity:.92;filter:none}
.panel h3{margin:0 0 .85rem;font-size:.95rem;letter-spacing:-.01em;display:flex;align-items:center;gap:.5rem}
.panel h3 .sub{font-family:var(--mono);font-size:.62rem;color:var(--dim);font-weight:400;margin-left:auto}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
.cols3{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(124px,1fr));gap:.7rem;margin:1rem 0}
.tile{background:linear-gradient(180deg,var(--panel),var(--panel-2));border:1px solid var(--line);border-radius:12px;padding:.7rem .85rem}
.tile .k{font-family:var(--mono);font-size:.57rem;text-transform:uppercase;letter-spacing:.08em;color:var(--dim)}
.tile .v{font-weight:800;font-size:1.2rem;margin-top:.15rem}
.tile .v small{font-size:.68rem;color:var(--dim);font-weight:600}
.chart{width:100%;height:120px;display:block}
.toggrow{display:flex;align-items:center;gap:.7rem;padding:.5rem 0;border-bottom:1px solid #ffffff08}
.toggrow:last-child{border-bottom:none}
.toggrow .tl{flex:1;font-size:.86rem}
.toggrow .td{font-family:var(--mono);font-size:.62rem;color:var(--dim)}
.feed{display:flex;flex-direction:column}
.ev{display:flex;gap:.8rem;padding:.62rem .2rem;border-bottom:1px solid #ffffff08}
.ev .when{font-family:var(--mono);font-size:.66rem;color:var(--dim);width:64px;flex:none;padding-top:.15rem}
.ev .edot{width:9px;height:9px;border-radius:50%;flex:none;margin-top:.4rem;background:var(--stop)}
.ev.ok .edot{background:var(--run)} .ev.warn .edot{background:var(--warn)}
.ev.crit .edot{background:var(--crash)} .ev.info .edot{background:#5aa9e6}
.ev .eb{flex:1;min-width:0}
.ev .eb .m{font-size:.85rem}
.ev .eb .tag{font-family:var(--mono);font-size:.61rem;color:var(--dim);margin-right:.4rem;border:1px solid var(--line);border-radius:5px;padding:.02rem .35rem}
.filters{display:flex;gap:.4rem;flex-wrap:wrap;margin:.7rem 0 .4rem}
.previewbanner{display:flex;align-items:center;gap:.6rem;border:1px dashed #5a3b1f;background:#ffb4540d;color:var(--warn);border-radius:10px;padding:.6rem .8rem;margin:.8rem 0;font-size:.82rem}
/* command palette (⌘K search) */
.palscrim{position:fixed;inset:0;background:#000a;backdrop-filter:blur(3px);display:none;z-index:40}
.palscrim.open{display:block}
.palbox{position:fixed;left:50%;top:13vh;transform:translateX(-50%);width:min(640px,94vw);background:linear-gradient(180deg,var(--panel),var(--panel-2));border:1px solid var(--line);border-radius:14px;box-shadow:0 24px 60px #000b;z-index:41;overflow:hidden}
.palbox input{width:100%;border:none;border-bottom:1px solid var(--line);background:#06090c;color:var(--txt);font-family:var(--sans);font-size:1rem;padding:.9rem 1rem;outline:none}
.palresults{max-height:52vh;overflow:auto;padding:.35rem}
.palitem{display:flex;align-items:center;gap:.6rem;padding:.55rem .7rem;border-radius:9px;cursor:pointer}
.palitem.sel{background:#3ddc9714}
.palitem .pic{width:1.2rem;text-align:center;flex:none}
.palitem .pl{flex:1;font-size:.88rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.palitem .pd{font-family:var(--mono);font-size:.66rem;color:var(--dim)}
.palitem .pk{font-family:var(--mono);font-size:.6rem;color:var(--dim);border:1px solid var(--line);border-radius:5px;padding:.05rem .35rem;white-space:nowrap}
.palhint{padding:.45rem .85rem;font-family:var(--mono);font-size:.62rem;color:#586675;border-top:1px solid var(--line);display:flex;gap:1rem}
</style>
<style>
/* guest access */
.guest-badge{display:inline-flex;align-items:center;gap:.3rem;font-size:.72rem;font-weight:700;color:var(--acc);
  border:1px solid var(--acc);border-radius:8px;padding:.2rem .5rem;background:color-mix(in srgb,var(--acc) 12%,transparent)}
body.guest .owner-only{display:none!important}
/* hide per-card controls above the guest's tier (server enforces regardless) */
body.guest-view .cap-operate,body.guest-view .cap-config,body.guest-operate .cap-config{display:none!important}
.shbots{display:flex;flex-wrap:wrap;gap:.3rem .8rem;max-height:150px;overflow:auto}
.shbot{display:flex;align-items:center;gap:.35rem;color:var(--txt);font-weight:400;font-size:.82rem}
.shrow{display:flex;align-items:center;gap:.6rem;border:1px solid var(--line);border-radius:8px;padding:.4rem .55rem}
.rrow{display:flex;flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400}
.shurl{border:1px solid var(--acc);border-radius:8px;padding:.5rem .6rem;background:color-mix(in srgb,var(--acc) 8%,transparent)}
/* public-sharing provider menu (dropdown + one card for the chosen provider) */
.pf{display:flex;flex-direction:column;gap:.25rem;color:var(--dim);font-size:.8rem}
.pf input,.pf select{font-family:var(--mono);font-size:.78rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:7px;padding:.4rem .5rem}
.provcard{margin-top:.5rem;padding:.7rem .75rem;border:1px solid var(--line);border-radius:12px;background:var(--panel-2);display:flex;flex-direction:column;gap:.45rem}
.provcard .pblurb{color:var(--dim);font-size:.78rem;line-height:1.4}
.setuprow{display:flex;gap:.6rem;align-items:center;padding:.5rem .6rem;border:1px dashed var(--acc-dim);border-radius:10px;background:color-mix(in srgb,var(--acc) 6%,transparent)}
.provcfg{display:flex;flex-direction:column;gap:.4rem;padding:.6rem .65rem;border:1px dashed var(--line);border-radius:10px}
/* per-user permissions matrix */
.lcSub2{font-family:var(--mono);font-size:.62rem;text-transform:uppercase;letter-spacing:.08em;color:var(--acc);font-weight:700;margin-bottom:.2rem}
.permhdr{display:flex;gap:.5rem;align-items:center;font-family:var(--mono);font-size:.58rem;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);padding:.1rem 0}
.permrow{display:flex;gap:.5rem;align-items:center;padding:.18rem .1rem;font-size:.82rem;border-bottom:1px solid #ffffff08}
.permcol{width:48px;text-align:center;flex:none}
.permcol input{width:auto}
</style>
</head>
<body>
<div class="app" id="app"><aside class="side" id="sidebar" style="display:none"></aside><div class="content" id="content">
<header id="classicHeader">
  <div class="brand"><span class="dot"></span>Aquarius Bot Manager <small class="ver">v__ABM_VERSION__</small> <small id="clock"></small></div>
  <div class="bulk">
    <button onclick="manualRefresh(this)" title="Refresh now">🔄 Refresh</button>
    <span id="guestBadge" class="guest-badge" style="display:none"></span>
    <button class="owner-only" onclick="openSettings()">⚙ Settings</button>
    <button class="owner-only" onclick="openConnection()">🔗 Connect</button>
    <button class="owner-only" onclick="openBoxes()">🖥 Boxes</button>
    <button class="owner-only" onclick="openFiles()">📁 Files</button>
    <button class="owner-only" onclick="openProxies()">🌐 Proxies</button>
    <button class="owner-only" onclick="openShares()">👥 Share</button>
    <button class="owner-only" onclick="openUsers()">👤 Users</button>
    <button class="owner-only" onclick="openScan()">⟲ Scan existing</button>
    <button class="go owner-only" onclick="openDeploy()">➕ Add Bot</button>
    <button class="go owner-only" onclick="bulk('start')">▶ Start all</button>
    <button class="warn owner-only" onclick="bulk('restart')">⟳ Restart all</button>
    <button class="danger owner-only" onclick="bulk('stop')">■ Stop all</button>
    <button class="danger" onclick="location.href='/logout'">⎋ Log out</button>
  </div>
</header>
<div class="topbar" id="slimTop" style="display:none"></div>
<main>
  <div id="viewDashboard">
    <div class="meta" id="meta"><span id="metaText">loading…</span> <span id="syncState" class="hint" style="opacity:.6"></span></div>
    <div class="hoststrip" id="hostStrip"></div>
    <div class="grid" id="grid"></div>
  </div>
  <div id="viewFleet" style="display:none"></div>
  <div id="viewActivity" style="display:none"></div>
  <div id="viewTelemetry" style="display:none"></div>
  <div id="viewAutomation" style="display:none"></div>
</main>
</div></div>

<div class="scrim" id="shareScrim" onclick="closeShares(event)">
  <div class="modal" style="width:min(640px,94vw)" onclick="event.stopPropagation()">
    <div class="mhead">👥 Share access</div>
    <div class="hint" style="margin:-.3rem 0 .6rem">Create a link that lets someone operate only the bots you choose, at a capability tier. <b>The link is the credential</b> — anyone with it has access until it expires or you revoke it.</div>
    <div class="modcard open" id="shTunCard">
      <div class="mhd"><span class="mtitle">Public sharing</span></div>
      <div class="mbody" style="gap:.5rem">
        <div class="hint">Your dashboard is private (localhost / SSH-tunnel) by default, so a link only works for people who can already reach it. Pick a way to give it a public HTTPS address that anyone can open — from a one-click Cloudflare quick tunnel (no account or domain) to your own domain.</div>
        <div id="shTunBody"><div class="hint">…</div></div>
      </div>
    </div>
    <div class="modcard open">
      <div class="mhd"><span class="mtitle">New link</span></div>
      <div class="mbody" style="gap:.6rem">
        <label style="color:var(--dim)">Label <input id="shLabel" placeholder="Friend's bots" maxlength="80"></label>
        <label class="rrow"><input type="checkbox" id="shAll" style="width:auto" onchange="shToggleAll()"> All my local bots (current + future)</label>
        <div id="shBotsWrap"><div style="color:var(--dim);font-size:.8rem;margin-bottom:.25rem">Bots</div><div id="shBots" class="shbots"><span class="hint">loading…</span></div></div>
        <div style="display:flex;gap:.9rem;align-items:center;flex-wrap:wrap;font-size:.82rem">
          <span class="hint">Capability</span>
          <label class="rrow"><input type="radio" name="shcap" value="view" checked style="width:auto"> View</label>
          <label class="rrow"><input type="radio" name="shcap" value="operate" style="width:auto"> Operate</label>
          <label class="rrow"><input type="radio" name="shcap" value="config" style="width:auto"> Config</label>
          <span class="hint" style="flex-basis:100%;margin:.1rem 0 0">View = read-only · Operate = + start/stop/commands · Config = + edit settings. Delete/rename/deploy stay owner-only.</span>
        </div>
        <div style="display:flex;gap:.9rem;align-items:center;flex-wrap:wrap;font-size:.82rem">
          <span class="hint">Expires</span>
          <select id="shTtl" style="width:auto"><option value="1">1 day</option><option value="7" selected>7 days</option><option value="30">30 days</option><option value="">Never</option></select>
        </div>
        <div class="mbar"><span class="msg" id="shMsg" style="color:var(--dim);flex:1"></span>
          <button class="go" onclick="createShare(this)">Create link</button></div>
        <div id="shResult" style="display:none"></div>
      </div>
    </div>
    <div class="modcard open" style="margin-top:.4rem">
      <div class="mhd"><span class="mtitle">Existing links</span></div>
      <div class="mbody">
        <div id="shList" style="display:flex;flex-direction:column;gap:.3rem;font-size:.82rem"><span class="hint">No links yet.</span></div>
        <div class="mbar"><span style="flex:1"></span><button class="danger" onclick="revokeAllShares(this)">Revoke all links</button></div>
      </div>
    </div>
    <div class="modcard" id="shAuditCard" style="margin-top:.4rem">
      <div class="mhd" onclick="document.getElementById('shAuditCard').classList.toggle('open')"><span class="caret">▶</span><span class="mtitle">Recent guest activity</span></div>
      <div class="mbody"><div id="shAudit" style="display:flex;flex-direction:column;gap:.2rem;font-size:.76rem;font-family:var(--mono);max-height:200px;overflow:auto"><span class="hint">No guest actions recorded.</span></div></div>
    </div>
  </div>
</div>
<div class="scrim" id="usersScrim" onclick="closeUsers(event)">
  <div class="modal" style="width:min(640px,94vw)" onclick="event.stopPropagation()">
    <div class="mhead">👤 Users &amp; access</div>
    <div class="hint" style="margin:-.3rem 0 .6rem">Give people their own login with a role and a set of bots. Roles: <b>View</b> (read-only) · <b>Operate</b> (+ start/stop/commands) · <b>Config</b> (+ edit settings) · <b>Admin</b> (full control, like a second owner). Your owner account is separate and always full-access.</div>
    <div class="modcard open">
      <div class="mhd"><span class="mtitle">Add a user</span></div>
      <div class="mbody" style="gap:.55rem">
        <label style="color:var(--dim)">Username <input id="usrName" placeholder="alice" maxlength="32" autocomplete="off"></label>
        <label style="color:var(--dim)">Password <input id="usrPass" type="password" placeholder="at least 6 characters" autocomplete="new-password"></label>
        <div id="usrRole_add"></div>
        <div id="usrScope_add"></div>
        <div class="mbar"><span class="msg" id="usrMsg" style="flex:1;color:var(--dim)"></span><button class="go" onclick="addUser(this)">Add user</button></div>
      </div>
    </div>
    <div class="modcard open">
      <div class="mhd"><span class="mtitle">Invite link</span></div>
      <div class="mbody" style="gap:.55rem">
        <div class="hint">Generate a one-time link with a preset role + bots. They open it, pick their own password, and they're in — no password handoff.</div>
        <label style="color:var(--dim)">Preset username <span class="hint">(optional — blank lets them choose)</span><input id="invName" placeholder="(optional)" maxlength="32" autocomplete="off"></label>
        <div id="usrRole_inv"></div>
        <div id="usrScope_inv"></div>
        <div style="display:flex;gap:.9rem;align-items:center;flex-wrap:wrap;font-size:.82rem">
          <span class="hint">Expires</span>
          <select id="invTtl" style="width:auto"><option value="1">1 day</option><option value="7" selected>7 days</option><option value="30">30 days</option><option value="">Never</option></select>
        </div>
        <div class="mbar"><span class="msg" id="invMsg" style="flex:1;color:var(--dim)"></span><button onclick="createInvite(this)">Create invite link</button></div>
        <div id="invResult" style="display:none"></div>
      </div>
    </div>
    <div class="modcard open" style="margin-top:.4rem">
      <div class="mhd"><span class="mtitle">People</span></div>
      <div class="mbody"><div id="usrList" style="display:flex;flex-direction:column;gap:.4rem;font-size:.82rem"><span class="hint">No users yet.</span></div></div>
    </div>
    <div class="modcard" id="invListCard" style="margin-top:.4rem">
      <div class="mhd" onclick="document.getElementById('invListCard').classList.toggle('open')"><span class="caret">▶</span><span class="mtitle">Pending invites</span></div>
      <div class="mbody"><div id="invList" style="display:flex;flex-direction:column;gap:.3rem;font-size:.8rem"><span class="hint">None.</span></div></div>
    </div>
  </div>
</div>
<div class="scrim" id="permsScrim" onclick="closePerms(event)">
  <div class="modal" style="width:min(620px,94vw)" onclick="event.stopPropagation()">
    <div class="mhead" id="permsTitle">Permissions</div>
    <div class="hint" id="permsNote" style="margin:-.3rem 0 .6rem"></div>
    <div id="permsBody"></div>
    <div class="mbar" style="margin-top:.6rem"><span class="msg" id="permsMsg" style="flex:1;color:var(--dim)"></span>
      <button onclick="resetPerms()">Reset to role default</button>
      <button class="go" onclick="savePerms()">Save permissions</button></div>
  </div>
</div>
<div class="scrim" id="connScrim" onclick="closeConnection(event)">
  <div class="modal" style="width:min(560px,94vw)" onclick="event.stopPropagation()">
    <div class="mhead">Connect / Reconnect</div>
    <div class="hint" style="margin:-.3rem 0 .6rem">Your bots and this dashboard run on the VPS and keep going if you close the browser, restart your PC, or drop offline. To get back in, just reopen the bookmark below — re-logging in only if your session has expired.</div>

    <label style="color:var(--dim);font-size:.8rem">Bookmark this dashboard
      <div style="display:flex;gap:.4rem;margin-top:.25rem">
        <input id="connUrl" readonly style="flex:1;font-family:var(--mono);font-size:.78rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:7px;padding:.4rem .5rem">
        <button onclick="copyText($('connUrl').value,this)">Copy</button>
      </div>
    </label>

    <div id="connTunnel" style="display:none">
      <div class="hint" style="margin:.9rem 0 .3rem">You're on a private (localhost) connection — that means an SSH tunnel. After a PC restart you re-open the tunnel first, then the bookmark. Make it one double-click:</div>
      <label style="color:var(--dim);font-size:.8rem">VPS IP / host
        <input id="connIp" oninput="renderConn()" style="width:100%;margin-top:.25rem;font-family:var(--mono);font-size:.78rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:7px;padding:.4rem .5rem"></label>
      <label style="color:var(--dim);font-size:.8rem;margin-top:.5rem;display:block">Tunnel command
        <div style="display:flex;gap:.4rem;margin-top:.25rem">
          <input id="connSsh" readonly style="flex:1;font-family:var(--mono);font-size:.74rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:7px;padding:.4rem .5rem">
          <button onclick="copyText($('connSsh').value,this)">Copy</button>
        </div></label>
      <div class="hint" style="margin:.9rem 0 .35rem">Or download a one-click reconnect shortcut (opens the tunnel + this dashboard):</div>
      <div style="display:flex;gap:.5rem;flex-wrap:wrap">
        <button onclick="dlReconnect('windows')">⬇ Windows (.bat)</button>
        <button onclick="dlReconnect('mac')">⬇ macOS (.command)</button>
        <button onclick="dlReconnect('linux')">⬇ Linux (.sh)</button>
      </div>
      <div class="hint" style="margin-top:.4rem;opacity:.75">Save it on your PC; double-click to reconnect. (macOS/Linux: <code>chmod +x</code> it first.)</div>
      <div id="connMulti" style="display:none">
        <div class="hint" style="margin:.9rem 0 .35rem">Or a launcher that tunnels into <b>every box</b> (controller + nodes) on its own port — a direct-access fallback for when the controller is down:</div>
        <div style="display:flex;gap:.5rem;flex-wrap:wrap">
          <button onclick="dlMulti('windows')">⬇ All boxes (.bat)</button>
          <button onclick="dlMulti('mac')">⬇ All boxes (.command)</button>
          <button onclick="dlMulti('linux')">⬇ All boxes (.sh)</button>
        </div>
      </div>
    </div>

    <div id="connDirect" style="display:none">
      <div class="hint" style="margin:.9rem 0 0">You're connecting directly over the network — just bookmark the URL above. Reconnecting after a PC restart or dropout is one step: open the bookmark, then log in.</div>
    </div>

    <div class="mbar" style="margin-top:1rem"><span class="msg" id="connMsg" style="color:var(--dim);flex:1"></span>
      <button onclick="closeConnection()">Close</button></div>
  </div>
</div>

<div class="scrim" id="boxScrim" onclick="closeBoxes(event)">
  <div class="modal" style="width:min(640px,94vw)" onclick="event.stopPropagation()">
    <div class="mhead">Boxes — your VPS fleet</div>
    <div class="hint" style="margin:-.3rem 0 .6rem">Connect other boxes running the manager. The controller opens an SSH tunnel to each one (they stay private — never exposed to the internet) so you can view and control them from here. Click <b>Open</b> on any box to drive its full dashboard in this tab.</div>

    <div id="boxBulk" style="display:none;gap:.4rem;flex-wrap:wrap;align-items:center;margin-bottom:.6rem">
      <button class="go" onclick="fleetAction('start',this)">▶ Start all</button>
      <button class="warn" onclick="fleetAction('restart',this)">⟳ Restart all</button>
      <button class="danger" onclick="fleetAction('stop',this)">■ Stop all</button>
      <button onclick="fleetUpdate(this)">🔄 Update all nodes</button>
      <span class="msg" id="boxBulkMsg" style="color:var(--dim);flex:1"></span>
    </div>

    <div id="boxList" style="display:flex;flex-direction:column;gap:.4rem;margin-bottom:.8rem"><span class="hint">loading…</span></div>

    <div class="modcard open" id="boxAddCard">
      <div class="mhd" onclick="document.getElementById('boxAddCard').classList.toggle('open')">
        <span class="caret">▶</span><span class="mtitle">Connect a box</span>
      </div>
      <div class="mbody" style="gap:.7rem">
        <div style="display:flex;gap:1rem;font-size:.82rem;flex-wrap:wrap">
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="boxmode" value="ssh" checked onchange="boxMode()" style="width:auto"> SSH box</label>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="boxmode" value="do" onchange="boxMode()" style="width:auto"> DigitalOcean</label>
        </div>

        <div id="boxSsh" style="display:flex;flex-direction:column;gap:.6rem">
          <label>Name <input id="boxName" placeholder="my-second-box"></label>
          <label>SSH target <input id="boxTarget" placeholder="ubuntu@15.204.210.247"></label>
          <div class="hint" style="margin-top:-.2rem">Just <code>user@host</code> — the controller uses its own SSH key. Add <code>:port</code> if SSH isn't on 22.</div>
          <div class="modcard" id="boxAdvCard">
            <div class="mhd" onclick="document.getElementById('boxAdvCard').classList.toggle('open')"><span class="caret">▶</span><span class="mtitle">Advanced (optional)</span></div>
            <div class="mbody" style="gap:.55rem">
              <label>SSH key path <span class="hint" style="font-weight:400">on the controller, if not the default</span><input id="boxKey" placeholder="~/.ssh/id_ed25519"></label>
              <label>Node manager port <input id="boxRemotePort" placeholder="8765"></label>
              <div class="hint">If that box's dashboard has a login, give its username/password so the controller can proxy into it:</div>
              <div style="display:flex;gap:.5rem">
                <input id="boxBasicUser" placeholder="node username">
                <input id="boxBasicPass" type="password" placeholder="node password">
              </div>
            </div>
          </div>
        </div>

        <div id="boxDo" style="display:none;flex-direction:column;gap:.6rem">
          <div id="doTokenBox">
            <label>DigitalOcean API token <span class="hint" style="font-weight:400">stored on the controller, obfuscated</span>
              <input id="doToken" type="password" placeholder="dop_v1_…"></label>
            <div class="mbar" style="margin-top:.4rem"><span class="msg" id="doTokenMsg" style="color:var(--dim);flex:1"></span>
              <button class="go" onclick="saveDoToken(this)">Save token</button></div>
          </div>
          <div id="doMain" style="display:none;flex-direction:column;gap:.7rem">
            <div class="modcard open" id="doProvCard">
              <div class="mhd" onclick="document.getElementById('doProvCard').classList.toggle('open')"><span class="caret">▶</span><span class="mtitle">Spin up a new droplet</span></div>
              <div class="mbody" style="gap:.55rem">
                <label>Name <input id="doNewName" placeholder="aquarius-node-3"></label>
                <div style="display:flex;gap:.5rem;flex-wrap:wrap">
                  <label style="flex:1;min-width:160px">Region <select id="doRegion" style="width:100%"></select></label>
                  <label style="flex:1;min-width:160px">Size <select id="doSize" style="width:100%"></select></label>
                </div>
                <div class="hint">Installs the manager in lightweight node mode and registers it automatically. 1GB is plenty for the manager + one capped bot.</div>
                <div class="mbar"><span class="msg" id="doProvMsg" style="color:var(--dim);flex:1"></span>
                  <button class="go" onclick="doProvision(this)">Create droplet</button></div>
                <pre id="doProvLog" class="log" style="display:none;min-height:60px;max-height:26vh"></pre>
              </div>
            </div>
            <div class="modcard" id="doConnCard">
              <div class="mhd" onclick="document.getElementById('doConnCard').classList.toggle('open')"><span class="caret">▶</span><span class="mtitle">Connect an existing droplet</span></div>
              <div class="mbody" style="gap:.5rem">
                <div class="hint">The droplet must already trust an SSH key the controller holds.</div>
                <div id="doDropletList" style="display:flex;flex-direction:column;gap:.3rem;font-size:.8rem"><span class="hint">loading droplets…</span></div>
                <div class="mbar"><span class="msg" id="doConnMsg" style="color:var(--dim);flex:1"></span>
                  <button onclick="loadDo(true)">Refresh</button>
                  <button onclick="forgetDoToken(this)">Change token</button></div>
              </div>
            </div>
          </div>
        </div>

        <div class="mbar" id="boxSshBar"><span class="msg" id="boxMsg" style="color:var(--dim);flex:1"></span>
          <button class="go" id="boxAddBtn" onclick="addBox(this)">Connect</button></div>
      </div>
    </div>

    <div class="mbar" style="margin-top:1rem"><span style="flex:1"></span><button onclick="closeBoxes()">Close</button></div>
  </div>
</div>

<div class="scrim" id="proxScrim" onclick="closeProxies(event)">
  <div class="modal" style="width:min(680px,94vw)" onclick="event.stopPropagation()">
    <div class="mhead">Proxies</div>
    <div class="hint" style="margin:-.3rem 0 .5rem">Instances with a proxy field (client.connection.proxy). Set host/port and optional user/password — edits write to config.json — restart to apply (use ⟳ to save &amp; restart). Clearing the user field removes the saved credentials.</div>

    <div class="modcard open" id="healthCard" style="margin-bottom:.4rem">
      <div class="mhd" onclick="document.getElementById('healthCard').classList.toggle('open')">
        <span class="caret">▶</span><span class="mtitle">Proxy health &amp; auto-fix</span>
      </div>
      <div class="mbody" style="gap:.6rem">
        <div class="hint">Scans each running bot's console for proxy errors (dead / removed IPs). Re-import fresh IPs from Webshare and reassign to the broken bots in one click. Tune the detection patterns in Settings → Monitoring if your proxies word errors differently.</div>
        <div id="healthList" style="display:flex;flex-direction:column;gap:.25rem;font-size:.78rem"><span class="hint">Click Scan to check bot consoles.</span></div>
        <div style="display:flex;gap:.9rem;align-items:center;flex-wrap:wrap;font-size:.8rem">
          <span class="hint">Fix scope</span>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="fixscope" value="errored" checked style="width:auto"> Errored only</label>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="fixscope" value="selected" style="width:auto"> Selected</label>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="fixscope" value="all" style="width:auto"> All</label>
        </div>
        <div style="display:flex;gap:.9rem;align-items:center;flex-wrap:wrap;font-size:.8rem">
          <span class="hint">Assign</span>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="fixmode" value="random" checked style="width:auto"> Random</label>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="fixmode" value="roundrobin" style="width:auto"> Round-robin</label>
          <span class="hint" style="flex-basis:100%;margin:0">Re-import uses the Webshare token + auth below (saved token reused if the field is blank). Restarts the fixed bots so the new IP takes effect.</span>
        </div>
        <div class="mbar"><span class="msg" id="healthMsg" style="color:var(--dim);flex:1"></span>
          <button onclick="scanHealth(this)">Scan</button>
          <button class="go" onclick="autoFix(this)">Re-import &amp; fix (Webshare)</button></div>
      </div>
    </div>

    <div class="modcard" id="bulkCard" style="margin-bottom:.4rem">
      <div class="mhd" onclick="document.getElementById('bulkCard').classList.toggle('open')">
        <span class="caret">▶</span><span class="mtitle">Bulk assign / rotate</span>
      </div>
      <div class="mbody" style="gap:.6rem">
        <label style="color:var(--dim)">Proxy list <span class="hint">one host:port per line</span>
          <textarea id="bulkList" spellcheck="false" placeholder="1.2.3.4:1080&#10;5.6.7.8:1080" style="min-height:84px;font-size:.76rem"></textarea></label>
        <div style="display:flex;gap:.9rem;align-items:center;flex-wrap:wrap;font-size:.8rem">
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="bulkmode" value="roundrobin" checked style="width:auto"> Round-robin</label>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="bulkmode" value="random" style="width:auto"> Random</label>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="bulkmode" value="same" style="width:auto"> Same to all</label>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400;margin-left:auto"><input type="checkbox" id="bulkRestart" style="width:auto"> Restart after</label>
        </div>
        <div class="hint">Targets <span id="bulkCount"></span></div>
        <div id="bulkTargets" style="display:flex;gap:.4rem;flex-wrap:wrap"></div>
        <div class="mbar"><span class="msg" id="bulkMsg" style="color:var(--dim);flex:1"></span>
          <button onclick="bulkSelectAll(true)">All</button>
          <button onclick="bulkSelectAll(false)">None</button>
          <button onclick="bulkSelectErrored(this)" title="select only bots with proxy errors">Errored</button>
          <button class="go" onclick="applyBulkProxies(this)">Apply</button></div>
      </div>
    </div>

    <div class="modcard" id="wsCard" style="margin-bottom:.4rem">
      <div class="mhd" onclick="document.getElementById('wsCard').classList.toggle('open')">
        <span class="caret">▶</span><span class="mtitle">Import from Webshare</span>
      </div>
      <div class="mbody" style="gap:.6rem">
        <label style="color:var(--dim)">API token <span class="hint" id="wsTokHint"></span>
          <input id="wsToken" type="password" spellcheck="false" autocomplete="off" placeholder="Webshare API key" style="font-family:var(--mono);font-size:.76rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:7px;padding:.4rem .5rem"></label>
        <div style="display:flex;gap:.9rem;align-items:center;flex-wrap:wrap;font-size:.8rem">
          <span class="hint">Auth</span>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="wsauth" value="userpass" checked style="width:auto"> User / pass</label>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="wsauth" value="ip" style="width:auto"> IP-authorized</label>
          <span class="hint" style="flex-basis:100%;margin:0">IP mode writes host:port only — whitelist this VPS's IP in your Webshare dashboard first.</span>
        </div>
        <div style="display:flex;gap:.9rem;align-items:center;flex-wrap:wrap;font-size:.8rem">
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="wsmode" value="roundrobin" checked style="width:auto"> Round-robin</label>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="wsmode" value="random" style="width:auto"> Random</label>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="wsmode" value="same" style="width:auto"> Same to all</label>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="checkbox" id="wsValid" checked style="width:auto"> Valid only</label>
        </div>
        <div style="display:flex;gap:.9rem;align-items:center;flex-wrap:wrap;font-size:.8rem">
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--dim);font-weight:400">Countries <input id="wsCountries" placeholder="US,CA (optional)" style="width:9rem;font-family:var(--mono);font-size:.72rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:7px;padding:.3rem .4rem"></label>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="checkbox" id="wsSave" style="width:auto"> Save token</label>
          <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400;margin-left:auto"><input type="checkbox" id="wsRestart" style="width:auto"> Restart after</label>
        </div>
        <div class="hint">Targets <span id="wsCount"></span> <span style="opacity:.7">— shared with the bulk panel</span></div>
        <div id="wsTargets" style="display:flex;gap:.4rem;flex-wrap:wrap"></div>
        <div class="mbar"><span class="msg" id="wsMsg" style="color:var(--dim);flex:1"></span>
          <button onclick="webshareCount(this)">Count</button>
          <button class="go" onclick="webshareImport(this)">Import &amp; assign</button></div>
      </div>
    </div>

    <div class="modcard open" id="proxListCard" style="margin-bottom:.4rem">
      <div class="mhd" onclick="document.getElementById('proxListCard').classList.toggle('open')">
        <span class="caret">▶</span><span class="mtitle">Per-bot proxies<span id="proxCount" class="hint" style="font-weight:400;margin-left:.4rem"></span></span>
      </div>
      <div class="mbody" style="gap:.5rem">
        <div id="proxList" style="display:flex;flex-direction:column;gap:.5rem">loading…</div>
      </div>
    </div>
    <div class="mbar"><span class="msg" id="proxMsg" style="color:var(--dim)"></span>
      <button onclick="loadProxies()">Refresh</button>
      <button onclick="closeProxies()">Close</button></div>
  </div>
</div>

<div class="scrim" id="deployScrim" onclick="closeDeploy(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="mhead">Add a bot</div>
    <div class="hint" style="margin:-.3rem 0 .4rem">Creates a new folder for the bot, downloads the chosen fork's launcher into it, and registers it. The launcher fetches Java + the jar on first start.</div>
    <label>Source
      <div id="depSrc" style="display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.25rem">
        <div class="chip sel" data-s="aquarius" onclick="pickSrc('aquarius',this)">AquariusProxy</div>
        <div class="chip" data-s="zenith" onclick="pickSrc('zenith',this)">ZenithProxy</div>
        <div class="chip" data-s="custom" onclick="pickSrc('custom',this)">Custom fork</div>
      </div>
    </label>
    <label id="depRepoWrap" style="display:none">Custom repo <span class="hint">owner/repo on GitHub — must publish a launcher-v3 release</span>
      <input id="dep_repo" placeholder="youruser/YourProxyFork" autocomplete="off"></label>
    <label>Name <span class="hint">spaces &amp; symbols become "-" (Linux-safe)</span>
      <input id="dep_name" placeholder="bot1" autocomplete="off" oninput="depPreview()"></label>
    <div class="hint" id="dep_path" style="margin:-.2rem 0 .4rem"></div>
    <div class="mrow">
      <label style="flex:1">Memory cap <span class="hint">e.g. 2G — optional</span>
        <input id="dep_mem" placeholder="2G" autocomplete="off"></label>
      <label style="flex:1">CPU cap % <span class="hint">100 = one core</span>
        <input id="dep_cpu" type="number" placeholder="200" autocomplete="off"></label>
    </div>
    <label style="flex-direction:row;align-items:center;gap:.4rem;color:var(--txt);font-weight:400;margin-top:.2rem">
      <input type="checkbox" id="dep_autostart" checked style="width:auto"> Relaunch on VPS reboot <span class="hint">(autostart — recommended)</span></label>
    <pre class="log" id="depLog" style="display:none;min-height:120px;max-height:32vh">…</pre>
    <div class="mbar"><span class="msg" id="depMsg" style="flex:1;color:var(--dim)"></span>
      <button onclick="closeDeploy()">Close</button>
      <button class="go" id="depBtn" onclick="startDeploy()">Add Bot</button></div>
  </div>
</div>

<div class="scrim" id="migrateScrim" onclick="closeMigrate(event)">
  <div class="modal" style="width:min(620px,94vw)" onclick="event.stopPropagation()">
    <div class="mhead">⇪ Migrate to AquariusProxy</div>
    <div class="hint" style="margin:-.3rem 0 .5rem">Convert <b id="migName">this bot</b> from ZenithProxy to <b>AquariusProxy</b> in place — <b>keeps its config and Minecraft account</b>. The bot is stopped, its files backed up, the launcher swapped, and it's restarted on AquariusProxy.</div>
    <ul class="hint" style="margin:.2rem 0 .5rem;padding-left:1.1rem;line-height:1.6">
      <li>Stops the bot, then backs up <span style="font-family:var(--mono);font-size:.9em">config.json · mc_auth_cache.json · launch_config.json · launch · the jar</span> to a <span style="font-family:var(--mono);font-size:.9em">premigrate-…</span> folder.</li>
      <li>Repoints the launcher to AquariusProxy and swaps the <span style="font-family:var(--mono);font-size:.9em">launch</span> binary, then starts it again.</li>
      <li><b>External plugin jars won't load</b> on AquariusProxy (many have baked-in equivalents). Your account &amp; settings carry over.</li>
      <li>If anything looks off afterward, hit <b>Roll back</b> to restore the backup.</li>
    </ul>
    <pre class="log" id="migLog" style="display:none;min-height:120px;max-height:34vh">…</pre>
    <div class="mbar"><span class="msg" id="migMsg" style="flex:1;color:var(--dim)"></span>
      <button onclick="closeMigrate()">Close</button>
      <button class="warn" id="migRollBtn" style="display:none" onclick="rollbackMigrate()">Roll back</button>
      <button class="go" id="migBtn" onclick="startMigrate()">Migrate</button></div>
  </div>
</div>

<div class="scrim" id="filesScrim" onclick="closeFiles(event)">
  <div class="modal" style="width:min(840px,96vw)" onclick="event.stopPropagation()">
    <div class="mhead">Files</div>
    <div class="hint" style="margin:-.3rem 0 .5rem">Jailed to the manager's allowed roots. Upload &amp; download from your computer, create, edit, rename, delete, and (with boxes registered) ↗ send files between boxes.</div>
    <div id="fbBrowse">
      <div class="fbbar">
        <select id="fbRoot" onchange="fbGotoRoot()" title="jump to a root"></select>
        <button onclick="fbUp()">⬆ Up</button>
        <button onclick="fbMkdir()">+ Folder</button>
        <button onclick="fbNewFile()">+ File</button>
        <button onclick="fbPickUpload(false)" title="upload file(s) from your computer">📤 Upload</button>
        <button onclick="fbPickUpload(true)" title="upload a whole folder from your computer">📤 Folder</button>
        <button onclick="fbReload()">⟳</button>
        <input id="fbUpFiles" type="file" multiple style="display:none" onchange="fbUpload(this.files);this.value='';">
        <input id="fbUpDir" type="file" webkitdirectory style="display:none" onchange="fbUpload(this.files);this.value='';">
      </div>
      <div class="fbpath" id="fbPath"></div>
      <div id="fbList" style="max-height:54vh;overflow:auto">loading…</div>
      <div class="mbar"><span class="msg" id="fbMsg" style="flex:1;color:var(--dim)"></span>
        <button onclick="closeFiles()">Close</button></div>
    </div>
    <div id="fbEdit" style="display:none">
      <div class="fbpath" id="fbEditPath"></div>
      <textarea id="fbContent" spellcheck="false" style="min-height:48vh"></textarea>
      <div class="mbar"><span class="msg" id="fbEditMsg" style="flex:1;color:var(--dim)"></span>
        <button onclick="fbBack()">← Back</button>
        <button class="go" onclick="fbSave()">Save</button></div>
    </div>
  </div>
</div>

<div class="scrim" id="vwScrim" onclick="closeViewer(event)">
  <div class="modal" id="vwModal" style="width:min(760px,96vw)" onclick="event.stopPropagation()">
    <div class="mhead" style="display:flex;align-items:center;gap:.55rem">
      <span>Live viewer — <span id="vwName"></span></span>
      <span id="vwMode" style="font-family:var(--mono);font-size:.6rem;color:var(--acc);border:1px solid var(--acc-dim);border-radius:5px;padding:.05rem .35rem"></span>
      <button id="vwRecenter" onclick="vwRecenter()" title="Recenter + follow the bot"
        style="margin-left:auto;font-size:.7rem;padding:.3rem .55rem;border:1px solid var(--line);background:var(--panel);color:var(--txt);border-radius:7px;cursor:pointer">⌖ follow</button>
    </div>
    <div class="tabs" style="padding:0;margin:.1rem 0 .6rem;display:flex;gap:.4rem">
      <div class="tab active" id="vwTabMap" onclick="vwSetTab('map')">🗺 Map</div>
      <div class="tab" id="vwTabPov" onclick="vwSetTab('pov')">◳ POV</div>
      <div class="tab" id="vwTabCtl" onclick="vwSetTab('control')">🎛 Control</div>
    </div>
    <style>
      .vw-ctl{max-height:70vh;overflow-y:auto;padding:.1rem .3rem .4rem}
      .vw-card{background:linear-gradient(180deg,#ffffff06,#ffffff02);border:1px solid var(--line);border-radius:12px;padding:.65rem .75rem;margin-bottom:.7rem}
      .vw-vitals{display:flex;flex-wrap:wrap;gap:.5rem 1rem;align-items:center}
      .vw-stat{display:flex;align-items:center;gap:.4rem;font-family:var(--mono);font-size:.78rem}
      .vw-stat .vw-i{font-size:.92rem}
      .vw-bar{width:104px;height:10px;border-radius:6px;background:#0007;overflow:hidden;border:1px solid var(--line)}
      .vw-bar i{display:block;height:100%;border-radius:6px;transition:width .3s ease}
      .vw-stat b{min-width:1.5em;text-align:right}
      .vw-chip{font-family:var(--mono);font-size:.67rem;color:var(--txt);background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:.16rem .58rem;white-space:nowrap}
      .vw-banner{font-size:.7rem;color:var(--warn);background:#3a2d0e;border:1px solid #6b520f;border-radius:9px;padding:.45rem .65rem;margin-bottom:.6rem}
      .vw-cols{display:grid;grid-template-columns:320px 1fr;gap:.7rem;align-items:start}
      @media(max-width:760px){.vw-cols{grid-template-columns:1fr}}
      .vw-sec{font-size:.6rem;letter-spacing:.09em;text-transform:uppercase;color:var(--dim);margin:0 0 .5rem;font-weight:700;display:flex;align-items:center;gap:.45rem}
      .vw-sec::before{content:'';width:3px;height:11px;border-radius:2px;background:var(--acc)}
      .vw-mini{position:relative;width:100%;aspect-ratio:1;background:#06090c;border:1px solid var(--line);border-radius:10px;overflow:hidden}
      .vw-equip{display:grid;grid-template-columns:repeat(6,1fr);gap:.4rem}
      .vw-eq{display:flex;flex-direction:column;align-items:center;gap:.22rem}
      .vw-eqlab{font-size:.52rem;color:var(--dim);font-family:var(--mono);text-transform:uppercase;letter-spacing:.04em}
      .vw-grid{display:grid;grid-template-columns:repeat(9,1fr);gap:4px}
      .vw-hot{margin-top:6px;padding-top:6px;border-top:1px dashed #ffffff14}
      .vw-slot{position:relative;aspect-ratio:1;border-radius:8px;background:#ffffff07;border:1px solid var(--line);overflow:hidden;transition:border-color .12s}
      .vw-slot:hover{border-color:var(--acc-dim)}
      .vw-eq .vw-slot{width:100%}
      .vw-slot.vw-ench{box-shadow:inset 0 0 0 1px #a371f7aa,0 0 8px #a371f74d;border-color:#a371f7aa;background:#a371f714}
      .vw-ic{position:absolute;inset:13%;width:74%;height:74%;image-rendering:pixelated;object-fit:contain;z-index:1}
      .vw-lbl{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font:600 .46rem/1.05 var(--mono);color:#8b97a3;text-align:center;padding:2px}
      .vw-cnt{position:absolute;right:2px;bottom:0;font:700 .62rem/1 var(--mono);color:#fff;text-shadow:0 1px 2px #000,0 0 3px #000;z-index:3}
      .vw-dur{position:absolute;left:3px;right:3px;bottom:2px;height:3px;background:#000b;border-radius:2px;overflow:hidden;z-index:3}
      .vw-dur i{display:block;height:100%}
      .vw-quick{display:flex;flex-wrap:wrap;gap:.4rem}
      .vw-qbtn{font-size:.72rem;padding:.38rem .7rem;border:1px solid var(--line);background:var(--panel);color:var(--txt);border-radius:8px;cursor:pointer;transition:all .12s}
      .vw-qbtn:hover{border-color:var(--acc-dim);color:var(--acc);background:var(--bg)}
      .vw-search{width:100%;box-sizing:border-box;font-size:.72rem;padding:.4rem .55rem;border:1px solid var(--line);background:var(--bg);color:var(--txt);border-radius:8px;font-family:var(--mono);margin-bottom:.4rem}
      .vw-modules{max-height:214px;overflow-y:auto;display:grid;grid-template-columns:1fr 1fr;gap:0 1rem}
      @media(max-width:760px){.vw-modules{grid-template-columns:1fr}}
      .vw-mod{display:flex;align-items:center;justify-content:space-between;gap:.5rem;padding:.27rem .1rem;font-size:.72rem;border-bottom:1px solid #ffffff0a}
      .vw-sw{width:32px;height:18px;border-radius:999px;background:#3a3f46;position:relative;cursor:pointer;flex:0 0 auto;transition:background .15s}
      .vw-sw.on{background:var(--acc)}
      .vw-sw i{position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:#fff;transition:left .15s}
      .vw-sw.on i{left:16px}
      .vw-palette{max-height:184px;overflow-y:auto;display:flex;flex-direction:column;gap:1px;margin-bottom:.45rem}
      .vw-cmd{display:flex;flex-direction:column;padding:.3rem .5rem;border-radius:7px;cursor:pointer;transition:background .12s}
      .vw-cmd:hover{background:#ffffff0e}
      .vw-cmd b{font-size:.75rem;color:var(--acc)}
      .vw-cmd span{font-size:.64rem;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .vw-cmdbar{display:flex;gap:.4rem;margin-bottom:.45rem}
      .vw-cmdbar input{flex:1;font-family:var(--mono);font-size:.74rem;padding:.42rem .55rem;border:1px solid var(--line);background:var(--bg);color:var(--txt);border-radius:8px}
      .vw-result{font-family:var(--mono);font-size:.7rem;color:var(--txt);background:#06090c;border:1px solid var(--line);border-radius:9px;padding:.5rem .6rem;min-height:2.4rem;max-height:170px;overflow-y:auto;white-space:pre-wrap;word-break:break-word}
      .vw-rcmd{color:var(--acc);margin-bottom:.25rem;font-weight:700}
      .vw-flstat{display:flex;flex-direction:column;gap:.45rem;margin-bottom:.6rem}
      .vw-fl-top{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem .9rem}
      .vw-fl-stat{font-family:var(--mono);font-size:.72rem;color:var(--dim)}
      .vw-fl-stat b{color:var(--txt);font-size:.82rem}
      .vw-fl-row{display:flex;align-items:center;gap:.5rem;font-family:var(--mono);font-size:.72rem;color:var(--dim)}
      .vw-fl-row b{color:var(--txt)}
      .vw-fl-ctl{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center}
      .vw-fl-ctl input{width:88px;font-family:var(--mono);font-size:.72rem;padding:.36rem .5rem;border:1px solid var(--line);background:var(--bg);color:var(--txt);border-radius:8px}
    </style>
    <div id="vwPovWrap" style="display:none;position:relative;width:100%;max-width:600px;margin:0 auto;aspect-ratio:1;background:#06090c;border:1px solid var(--line);border-radius:10px;overflow:hidden">
      <canvas id="vwPov" style="position:absolute;inset:0;width:100%;height:100%"></canvas>
      <div id="vwPovMsg" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--dim);font-family:var(--mono);font-size:.74rem;padding:1rem">loading 3D…</div>
      <div style="position:absolute;top:.4rem;right:.4rem;display:flex;gap:.3rem">
        <button id="vwCam1" onclick="vwSetCam('1st')" style="font-size:.62rem;padding:.2rem .45rem;border:1px solid var(--acc-dim);background:var(--panel);color:var(--acc);border-radius:6px;cursor:pointer">1st</button>
        <button id="vwCam3" onclick="vwSetCam('3rd')" style="font-size:.62rem;padding:.2rem .45rem;border:1px solid var(--line);background:var(--panel);color:var(--txt);border-radius:6px;cursor:pointer">3rd</button>
      </div>
      <div style="position:absolute;top:.4rem;left:.5rem;font-family:var(--mono);font-size:.6rem;color:#cdd9e2cc;text-shadow:0 1px 2px #000">fullbright</div>
      <div style="position:absolute;bottom:.4rem;right:.4rem;display:flex;gap:.3rem;align-items:center">
        <span style="font-family:var(--mono);font-size:.55rem;color:#cdd9e2aa;text-shadow:0 1px 2px #000">range</span>
        <button id="vwD32" onclick="vwSetDist(32)" style="font-size:.6rem;padding:.15rem .4rem;border:1px solid var(--line);background:var(--panel);color:var(--txt);border-radius:6px;cursor:pointer">32</button>
        <button id="vwD48" onclick="vwSetDist(48)" style="font-size:.6rem;padding:.15rem .4rem;border:1px solid var(--acc-dim);background:var(--panel);color:var(--acc);border-radius:6px;cursor:pointer">48</button>
        <button id="vwD64" onclick="vwSetDist(64)" style="font-size:.6rem;padding:.15rem .4rem;border:1px solid var(--line);background:var(--panel);color:var(--txt);border-radius:6px;cursor:pointer">64</button>
      </div>
    </div>
    <div id="vwWrap" style="position:relative;width:100%;max-width:600px;margin:0 auto;aspect-ratio:1;background:#06090c;border:1px solid var(--line);border-radius:10px;overflow:hidden;cursor:grab;touch-action:none">
      <canvas id="vwCanvas" width="600" height="600" style="position:absolute;inset:0;width:100%;height:100%;image-rendering:pixelated"></canvas>
      <div id="vwOff" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--dim);font-family:var(--mono);font-size:.78rem;padding:1rem;line-height:1.6">Viewer offline.<br>AquariusProxy: enable <code>server.viewer.enabled</code>.<br>ZenithProxy: install the <code>zenith-abm-bridge</code> plugin, then <code>abmBridge on</code>.<br>(port auto-detected)</div>
      <div style="position:absolute;top:.4rem;left:.5rem;font-family:var(--mono);font-size:.6rem;color:#cdd9e2cc;text-shadow:0 1px 2px #000">N ↑</div>
    </div>
    <div id="vwControlWrap" class="vw-ctl" style="display:none">
      <div id="vwCtlBanner"></div>
      <div style="display:flex;align-items:center;gap:.6rem;margin-bottom:.7rem;flex-wrap:wrap">
        <button class="vw-qbtn" onclick="vwOpenFullControl()"
          style="border-color:var(--acc-dim);color:var(--acc);font-weight:700">⛶ Open full control surface</button>
        <span style="font-size:.64rem;color:var(--dim)">live Mission Control — every module, the world map, vitals &amp; a command palette on one page</span>
      </div>
      <div class="vw-card"><div id="vwVitals" class="vw-vitals"></div></div>
      <div class="vw-card">
        <div class="vw-sec">Flight — ElytraPilot</div>
        <div id="vwFlightStat" class="vw-flstat"></div>
        <div class="vw-fl-ctl">
          <input id="vwFlX" placeholder="dest X" inputmode="numeric">
          <input id="vwFlZ" placeholder="dest Z" inputmode="numeric">
          <button class="vw-qbtn" onclick="vwFlyTo()">✈ Fly there</button>
          <button class="vw-qbtn" onclick="vwRunCommand('fly stop')">Stop</button>
          <button class="vw-qbtn" onclick="vwRunCommand('fly resupplyspares')">Resupply</button>
          <button class="vw-qbtn" onclick="vwRunCommand('fly restart')">Restart</button>
          <span style="font-size:.62rem;color:var(--dim)">tip: click the map to set a destination</span>
        </div>
      </div>
      <div class="vw-cols">
        <div>
          <div class="vw-card">
            <div class="vw-sec">Location</div>
            <div class="vw-mini">
              <canvas id="vwMiniCanvas" width="520" height="520" onclick="vwMiniClick(event)" style="position:absolute;inset:0;width:100%;height:100%;image-rendering:pixelated;cursor:crosshair"></canvas>
              <div style="position:absolute;top:.3rem;left:.4rem;font-family:var(--mono);font-size:.55rem;color:#cdd9e2cc;text-shadow:0 1px 2px #000">N ↑</div>
            </div>
          </div>
          <div class="vw-card">
            <div class="vw-sec">Quick actions</div>
            <div id="vwQuick" class="vw-quick"></div>
          </div>
        </div>
        <div>
          <div class="vw-card">
            <div class="vw-sec">Equipment</div>
            <div id="vwEquip" class="vw-equip"></div>
          </div>
          <div class="vw-card">
            <div class="vw-sec">Inventory</div>
            <div id="vwInv"></div>
          </div>
          <div class="vw-card">
            <div class="vw-sec">Modules</div>
            <input id="vwModFilter" class="vw-search" placeholder="filter modules…" oninput="vwRenderModules()">
            <div id="vwModules" class="vw-modules"></div>
          </div>
        </div>
      </div>
      <div class="vw-card">
        <div class="vw-sec">Command palette</div>
        <div class="vw-cmdbar">
          <input id="vwCmdInput" placeholder="type a command, or pick one below…" onkeydown="if(event.key==='Enter')vwRunInput()">
          <button class="vw-qbtn" onclick="vwRunInput()">Run</button>
        </div>
        <input id="vwCmdSearch" class="vw-search" placeholder="search commands…" oninput="vwRenderPalette()">
        <div id="vwPalette" class="vw-palette"></div>
        <div id="vwCmdResult" class="vw-result">—</div>
      </div>
    </div>
    <div id="vwHud" style="display:flex;flex-wrap:wrap;gap:.45rem 1rem;justify-content:center;margin-top:.7rem;font-family:var(--mono);font-size:.72rem;color:var(--dim)"></div>
    <div style="text-align:center;margin-top:.4rem;color:var(--dim);font-size:.64rem;font-family:var(--mono)">drag to pan · scroll to zoom · ⌖ to follow</div>
  </div>
</div>

<div class="scrim" id="settingsScrim" onclick="closeSettings(event)">
  <div class="modal" style="width:min(620px,94vw)" onclick="event.stopPropagation()">
    <div class="mhead">Settings</div>
    <div class="tabs" style="padding:0;margin-bottom:.6rem">
      <div class="tab active" id="stApBtn" onclick="setTab('ap')">Appearance</div>
      <div class="tab" id="stPreBtn" onclick="setTab('pre')">Console</div>
      <div class="tab" id="stMonBtn" onclick="setTab('mon')">Monitoring</div>
      <div class="tab" id="stSysBtn" onclick="setTab('sys')">System</div>
    </div>

    <div id="stAp">
      <div class="hint" style="margin-bottom:.4rem">Sidebar &amp; navigation</div>
      <div id="sidebarRow" style="display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.55rem">
        <div class="chip" data-sb="off" onclick="pickSidebar('off')">Off · classic header</div>
        <div class="chip" data-sb="rail" onclick="pickSidebar('rail')">Icon rail</div>
        <div class="chip" data-sb="full" onclick="pickSidebar('full')">Full</div>
        <div class="chip" data-sb="cmd" onclick="pickSidebar('cmd')">Command center</div>
      </div>
      <div id="sideOrientRow" style="display:flex;gap:1rem;margin-bottom:.95rem;font-size:.84rem">
        <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="sbside" value="left" style="width:auto" onchange="pickSide('left')"> Left</label>
        <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="sbside" value="right" style="width:auto" onchange="pickSide('right')"> Right</label>
      </div>
      <div class="hint" style="margin-bottom:.5rem">Theme preset</div>
      <div id="presetRow" style="display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.9rem"></div>
      <label>Accent colour <span class="hint">blank = preset default</span>
        <div style="display:flex;gap:.5rem;align-items:center">
          <input type="color" id="accentPick" style="width:48px;height:38px;padding:2px;background:#06090c;border:1px solid var(--line);border-radius:9px">
          <input id="accentHex" placeholder="#3ddc97" style="flex:1" autocomplete="off">
          <button onclick="document.getElementById('accentHex').value='';document.getElementById('accentPick').value='#3ddc97';SELACCENT='';previewTheme()">Clear</button>
        </div>
      </label>
      <div id="accentSwatches" style="display:flex;gap:.4rem;flex-wrap:wrap;margin:.45rem 0 .9rem"></div>

      <div class="hint" style="margin-bottom:.4rem">Density</div>
      <div id="densityRow" style="display:flex;gap:1rem;margin-bottom:.9rem;font-size:.84rem">
        <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="density" value="" style="width:auto" onchange="SELDENSITY='';previewTheme()"> Comfortable</label>
        <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="density" value="compact" style="width:auto" onchange="SELDENSITY='compact';previewTheme()"> Compact</label>
        <label style="flex-direction:row;align-items:center;gap:.35rem;color:var(--txt);font-weight:400"><input type="radio" name="density" value="spacious" style="width:auto" onchange="SELDENSITY='spacious';previewTheme()"> Spacious</label>
      </div>

      <label>Font <span class="hint">display + monospace pairing</span>
        <select id="fontSel" onchange="SELFONT=this.value;previewTheme()" style="font-family:var(--sans);font-size:.84rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:9px;padding:.5rem .6rem;width:100%;cursor:pointer"></select>
      </label>

      <label>Background image URL <span class="hint">blank = solid theme colour</span>
        <input id="bgImage" placeholder="https://…/wallpaper.jpg" autocomplete="off" oninput="SELBG=this.value.trim();previewTheme()"></label>
      <label style="margin-top:.5rem">Background dim <span class="hint" id="bgDimVal"></span>
        <input type="range" id="bgDim" min="0" max="95" value="60" style="width:100%" oninput="SELBGDIM=this.value/100;document.getElementById('bgDimVal').textContent=this.value+'%';previewTheme()"></label>

      <div class="mbar" style="margin-top:.8rem">
        <span class="msg" id="apMsg" style="color:var(--dim)"></span>
        <button class="go" onclick="saveAppearance()">Save appearance</button>
      </div>
    </div>

    <div id="stPre" style="display:none">
      <div class="hint" style="margin-bottom:.6rem">Quick-command buttons shown in each instance's Console tab. Clicking one sends its command to the live console. Label is what you see; command is what's typed.</div>
      <div id="preList" style="display:flex;flex-direction:column;gap:.4rem"></div>
      <button onclick="addPreset()" style="width:auto;margin-top:.6rem">+ Add preset</button>
      <div class="mbar"><span class="msg" id="preMsg" style="color:var(--dim);flex:1"></span>
        <button class="go" onclick="savePresets()">Save presets</button></div>
    </div>

    <div id="stMon" style="display:none">
      <div class="hint" style="margin-bottom:.6rem">Warn-color a host gauge / instance bar once usage crosses these (percent of capacity).</div>
      <div id="thrRows"></div>
      <div class="mbar"><span class="msg" id="monMsg" style="color:var(--dim);flex:1"></span>
        <button class="go" onclick="saveThresholds()">Save thresholds</button></div>
    </div>

    <div id="stSys" style="display:none">
      <div id="sysInfo" class="sysgrid">loading…</div>
      <div style="margin:.9rem 0;padding:.6rem .7rem;border:1px solid var(--line);border-radius:10px">
        <div style="font-size:.85rem;color:var(--txt);font-weight:600;margin-bottom:.35rem">Manager updates</div>
        <div class="hint" style="margin-bottom:.55rem">Update the manager in place (<code>git pull</code> + restart the web UI) — no reinstall. Enable auto-update to run it on a daily schedule.</div>
        <div class="row" style="align-items:center;gap:.7rem;flex-wrap:wrap">
          <button class="go" onclick="selfUpdate(this)">🔄 Update manager now</button>
          <span id="updAvail" style="display:none"></span>
          <label style="flex-direction:row;align-items:center;gap:.4rem;color:var(--txt);font-weight:400;margin:0"><input type="checkbox" id="autoUpd" onchange="toggleAutoupdate(this)" style="width:auto"> Auto-update daily</label>
          <span class="msg" id="updMsg" style="color:var(--dim);flex:1"></span>
        </div>
      </div>
      <div style="margin:.9rem 0;padding:.6rem .7rem;border:1px solid var(--line);border-radius:10px">
        <div style="font-size:.85rem;color:var(--txt);font-weight:600;margin-bottom:.35rem">Backup &amp; restore</div>
        <div class="hint" style="margin-bottom:.55rem">Download this box's configs (instances + connected boxes). The file contains secrets — keep it safe. Restoring overwrites the current configs (a timestamped copy is saved first) and may require logging in again.</div>
        <div class="row" style="align-items:center;gap:.7rem;flex-wrap:wrap">
          <button class="go" onclick="dlBackup()">⬇ Download backup</button>
          <input type="file" id="restoreFile" accept="application/json,.json" style="width:auto;font-size:.78rem">
          <button class="warn" onclick="doRestore(this)">⬆ Restore</button>
          <span class="msg" id="bkpMsg" style="color:var(--dim);flex:1"></span>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:.6rem;margin:.9rem 0;padding:.6rem .7rem;border:1px solid var(--line);border-radius:10px">
        <input type="checkbox" id="sysEnable" onchange="toggleSystem()" style="width:18px;height:18px">
        <label for="sysEnable" style="margin:0;color:var(--txt);font-size:.85rem">Enable system actions (reboot / OS update)</label>
      </div>
      <div id="sysDanger" style="opacity:.45;pointer-events:none">
        <div class="row" style="margin-bottom:.7rem">
          <button class="warn" onclick="sysAction('update')">⟳ Update OS (apt upgrade)</button>
          <button class="danger" onclick="sysAction('reboot')">⏻ Reboot VPS</button>
        </div>
        <pre class="log" id="sysJob" style="min-height:90px;max-height:30vh">(no system job run yet)</pre>
      </div>
      <div class="hint" style="margin-top:.6rem">Requires passwordless sudo for <code>reboot</code> and <code>apt-get</code>. See README. Your password is never stored.</div>
    </div>
  </div>
</div>

<div class="scrim" id="scanScrim" onclick="closeScan(event)">
  <div class="modal" style="width:min(680px,94vw)" onclick="event.stopPropagation()">
    <div class="mhead">Existing tmux sessions</div>
    <div class="hint" style="margin:-.3rem 0 .4rem">Sessions not yet managed. Adopting binds the manager to the live session — nothing restarts.</div>
    <div id="scanList" style="max-height:55vh;overflow:auto;display:flex;flex-direction:column;gap:.5rem"></div>
    <div class="mbar">
      <span class="msg" id="scanMsg"></span>
      <button onclick="loadScan()">Rescan</button>
      <button onclick="closeScan()">Close</button>
    </div>
  </div>
</div>

<div class="scrim" id="scrim" onclick="closeDrawer()"></div>
<aside class="drawer" id="drawer">
  <header>
    <div class="brand" id="drawerName" style="font-size:1rem"></div>
    <button class="close" onclick="closeDrawer()">×</button>
  </header>
  <div class="tabs">
    <div class="tab active" id="tabLogsBtn" onclick="showTab('logs')">Console</div>
    <div class="tab" id="tabCfgBtn" onclick="showTab('cfg')">Config</div>
    <div class="tab" id="tabLimBtn" onclick="showTab('lim')">Limits</div>
  </div>
  <div class="body">
    <div id="tabLogs">
      <div id="logWrap">
        <pre class="log" id="logBox" onscroll="onLogScroll()">…</pre>
        <button id="logPill" class="logpill" style="display:none" onclick="jumpLogBottom()">↓ Jump to latest</button>
      </div>
      <div id="presetBar" style="display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.6rem"></div>
      <div class="cmdbar">
        <span class="prompt">&gt;</span>
        <input id="cmdInput" placeholder="send a console command, e.g. killAura on" autocomplete="off" spellcheck="false"
          onkeydown="cmdKey(event)">
        <button class="go" onclick="sendCmd()">Send</button>
      </div>
    </div>
    <div id="tabLim" style="display:none">
      <div class="hint" id="limCap" style="margin-bottom:.8rem"></div>
      <div class="frow"><div class="flabel">Memory cap <span class="unit">hard</span></div>
        <div class="fctrl"><input type="text" id="lim_mem" placeholder="e.g. 2G (blank = none)"></div></div>
      <div class="frow"><div class="flabel">CPU cap <span class="unit">% / core</span></div>
        <div class="fctrl"><input type="text" id="lim_cpu" placeholder="e.g. 200 (blank = none)"></div></div>
      <div class="hint" style="margin-top:.8rem">Enforced via a transient systemd user scope (cgroups). Memory over the cap is OOM-killed inside the scope; CPU is throttled. Takes effect on next start/restart.</div>
      <div class="mbar" style="margin-top:.8rem"><span class="msg" id="limMsg" style="color:var(--dim);flex:1"></span>
        <button onclick="saveLimits(false)">Save</button>
        <button class="warn" onclick="saveLimits(true)">Save &amp; Restart</button></div>
    </div>
    <div id="tabCfg" style="display:none">
      <div style="display:flex;gap:.5rem;align-items:center;margin-bottom:.7rem">
        <input id="cfgFilter" placeholder="Filter settings…" oninput="renderForm()" autocomplete="off"
          style="flex:1;font-family:var(--mono);font-size:.78rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:9px;padding:.45rem .6rem">
        <button id="cfgViewBtn" onclick="toggleCfgView()">Raw JSON</button>
      </div>
      <div id="cfgForm"></div>
      <textarea id="cfgBox" spellcheck="false" style="display:none"></textarea>
    </div>
  </div>
  <div class="bar">
    <span class="msg" id="drawerMsg"></span>
    <button id="logRefresh" onclick="loadLogs()">Refresh</button>
    <button id="cfgSaveRestart" class="warn" style="display:none" onclick="saveAndRestart()">Save &amp; Restart</button>
    <button id="cfgSave" class="go" style="display:none" onclick="saveCfg()">Save</button>
  </div>
</aside>

<div class="palscrim" id="palScrim" onclick="closePalette(event)">
  <div class="palbox" onclick="event.stopPropagation()">
    <input id="palInput" placeholder="Search bots &amp; jump to a page…  (type a bot name)" autocomplete="off" spellcheck="false" oninput="palRender()" onkeydown="palKey(event)">
    <div class="palresults" id="palResults"></div>
    <div class="palhint"><span>↑↓ navigate</span><span>↵ open</span><span>esc close</span><span id="palScope" style="margin-left:auto"></span></div>
  </div>
</div>

<script>
let CUR=null, TAB='logs', logTimer=null, INSTMAP={}, LAST_REFRESH=0;
const $=id=>document.getElementById(id);

async function api(path,method='GET',body){
  const o={method,headers:{}};
  if(body){o.headers['Content-Type']='application/json';o.body=JSON.stringify(body);}
  const r=await fetch(path,o);
  return r.json();
}
function badge(s){return `<span class="badge ${s}">${s}</span>`;}
function connLabel(c){
  if(!c) return 'Offline';
  if(c.state==='in-queue') return 'In queue'+(c.queue!=null?(' · #'+c.queue):'');
  return ({offline:'Offline',online:'Online',updating:'Updating…',restarting:'Restarting…'})[c.state]||c.state;
}
async function toggleAuto(name,enabled){
  await api(`/api/instances/${encodeURIComponent(name)}/autostart`,'POST',{enabled});
  refresh();
}
function fmtBytes(n){ if(!n)return'0'; if(n>=1e9)return (n/1e9).toFixed(1)+' GB'; if(n>=1e6)return Math.round(n/1e6)+' MB'; return Math.max(1,Math.round(n/1e3))+' KB'; }
function thr(){ return (SETTINGS&&SETTINGS.thresholds)||{cpu_pct:85,mem_pct:85,disk_pct:90}; }
function lvl(pct,t){ return pct>=Math.min(100,t+10)?'crit':(pct>=t?'warn':''); }
let HOST=null;
async function loadHost(){ try{ HOST=await api('/api/system/info'); }catch(e){ return; } renderHost(); updateSidebarLive(); }
function gauge(label,pct,valstr,t){
  pct=Math.max(0,Math.min(100,Math.round(pct||0)));
  return `<div class="gauge ${lvl(pct,t)}"><div class="k"><span>${label}</span><span>${pct}%</span></div><div class="v">${valstr}</div><div class="b"><i style="width:${pct}%"></i></div></div>`;
}
function renderHost(){
  const el=$('hostStrip'); if(!el||!HOST)return;
  const t=thr(), cores=HOST.cpus||1, load0=HOST.load?HOST.load[0]:0;
  el.innerHTML=
    gauge('CPU load',100*load0/cores,(HOST.load?HOST.load[0].toFixed(2):'?')+' / '+cores+' cores',t.cpu_pct)+
    gauge('Memory',HOST.mem_total?100*HOST.mem_used/HOST.mem_total:0,fmtGB(HOST.mem_used)+' / '+fmtGB(HOST.mem_total),t.mem_pct)+
    gauge('Disk',HOST.disk_total?100*HOST.disk_used/HOST.disk_total:0,fmtGB(HOST.disk_used)+' / '+fmtGB(HOST.disk_total),t.disk_pct);
}
function statBars(s,limits){
  const t=thr(), cores=(HOST&&HOST.cpus)||1, memTotal=(HOST&&HOST.mem_total)||0;
  limits=limits||{};
  // CPU bar: relative to the instance's cpu cap if set, else to all cores
  const cpuCeil=limits.cpu||cores*100;
  const cpuShare=s.cpu_pct==null?0:Math.min(100,100*s.cpu_pct/cpuCeil);
  // RAM bar: relative to the memory cap if set, else to host total
  const memCeil=limits.memory_bytes||memTotal;
  const memPct=memCeil?Math.min(100,100*s.rss/memCeil):0;
  const cpuTxt=(s.cpu_pct==null?'…':s.cpu_pct+'%')+(limits.cpu?(' / '+limits.cpu+'%'):'');
  const memTxt=fmtBytes(s.rss)+(limits.memory?(' / '+limits.memory):'');
  return `<div class="cstats">
    <div class="cs ${lvl(cpuShare,t.cpu_pct)}"><div class="cl"><span>CPU</span><span>${cpuTxt}</span></div><div class="b"><i style="width:${cpuShare}%"></i></div></div>
    <div class="cs ${lvl(memPct,t.mem_pct)}"><div class="cl"><span>RAM</span><span>${memTxt}</span></div><div class="b"><i style="width:${memPct}%"></i></div></div>
  </div>`;
}
function renderThresholds(){
  const t=thr();
  $('thrRows').innerHTML=[['cpu_pct','CPU load'],['mem_pct','Memory'],['disk_pct','Disk']].map(([k,lbl])=>
    `<div class="frow"><div class="flabel">${lbl} <span class="unit">%</span></div><div class="fctrl"><input type="number" min="1" max="100" id="thr_${k}" value="${t[k]}" class="snum wide"></div></div>`).join('');
}
async function saveThresholds(){
  const thresholds={cpu_pct:+$('thr_cpu_pct').value,mem_pct:+$('thr_mem_pct').value,disk_pct:+$('thr_disk_pct').value};
  $('monMsg').style.color='var(--dim)'; $('monMsg').textContent='saving…';
  const d=await api('/api/settings','POST',{thresholds});
  if(d.error){ $('monMsg').style.color='var(--crash)'; $('monMsg').textContent='✗ '+d.error; return; }
  SETTINGS=d.settings; renderHost(); $('monMsg').textContent='✓ saved';
}

async function refresh(){
  loadHost();
  let d;
  try{ d=await api('/api/instances'); }
  catch(e){ syncAgo(); return; }   // keep last-known counts; the pill below shows staleness
  LAST_REFRESH=Date.now();
  const list=d.instances||[];
  INSTMAP=Object.fromEntries(list.map(i=>[i.name,i]));
  const run=list.filter(i=>i.status==='running').length;
  const cr=list.filter(i=>i.status==='crashed').length;
  $('metaText').textContent=`${list.length} instances · ${run} running`+(cr?` · ${cr} crashed`:'');
  syncAgo();
  const g=$('grid');
  if(!list.length){g.innerHTML='<div class="empty">No bots yet. Click “➕ Add Bot” to create one.</div>';return;}
  g.innerHTML=list.map(i=>`
    <div class="card ${i.status}">
      <div class="top">
        <div class="name">${esc(i.name)}</div>
        <div style="display:flex;align-items:center;gap:.4rem">
          <span class="star cap-config ${i.autostart?'on':''}" title="${i.autostart?'Autostart on — launches on boot':'Autostart off'}" onclick="toggleAuto('${jsq(i.name)}',${!i.autostart})">${i.autostart?'★':'☆'}</span>
          ${badge(i.status)}
        </div>
      </div>
      ${i.proxy?`<div class="ptag ${i.proxy.fork==='AquariusProxy'?'aqua':(i.proxy.fork==='ZenithProxy'?'zenith':'')}" title="${esc(i.proxy.version_full||i.proxy.fork)}"><span class="pdot"></span>${esc(i.proxy.fork)}${i.proxy.version?(' v'+esc(i.proxy.version)):''}${i.proxy.platform?(' '+esc(i.proxy.platform)):''}</div>`:''}
      <div class="cstate s-${i.conn?i.conn.state:'offline'}">${connLabel(i.conn)}</div>
      ${i.status==='running'&&i.stats?statBars(i.stats,i.limits):''}
      <div class="row">
        <button class="go cap-operate" onclick="act('${jsq(i.name)}','start',this)"><i class="ic">▶</i><span class="lbl">Start</span></button>
        <button class="warn cap-operate" onclick="act('${jsq(i.name)}','restart',this)"><i class="ic">⟳</i><span class="lbl">Restart</span></button>
        <button class="danger cap-operate" onclick="act('${jsq(i.name)}','stop',this)"><i class="ic">■</i><span class="lbl">Stop</span></button>
        <button class="mini" title="More" onclick="openDrawer('${jsq(i.name)}')">⋯</button>
        <button class="mini" title="Live viewer" onclick="openViewer('${jsq(i.name)}')">👁</button>
        ${i.proxy&&i.proxy.fork==='ZenithProxy'?`<button class="mini owner-only" title="Migrate to AquariusProxy (keeps config + account)" onclick="openMigrate('${jsq(i.name)}')">⇪ Aquarius</button>`:''}
        <button class="mini owner-only" title="Rename bot" onclick="renameBot('${jsq(i.name)}')">✎</button>
        <button class="mini danger owner-only" title="Delete instance" onclick="del('${jsq(i.name)}','${i.status}')">🗑</button>
      </div>
    </div>`).join('');
  updateSidebarLive();
}
async function manualRefresh(btn){
  if(!btn) return refresh();
  const o=btn.innerHTML; btn.disabled=true; btn.innerHTML='<span class="spin"></span> Refreshing…';
  try{ await refresh(); } finally { btn.disabled=false; btn.innerHTML=o; }
}
// Freshness pill next to the instance count, ticked every second from tick().
// Healthy auto-refresh (every 3s) keeps it on "live"; it only escalates once
// updates actually stop arriving: live → last synced Xs ago → interrupted.
function syncAgo(){
  const el=$('syncState'); if(!el) return;
  if(!LAST_REFRESH){ el.textContent=''; return; }            // nothing synced yet
  const s=Math.round((Date.now()-LAST_REFRESH)/1000);
  if(s<10){ el.textContent='● live'; el.style.color='var(--run)'; }
  else if(s<30){ el.textContent='· last synced '+s+'s ago'; el.style.color='var(--warn)'; }
  else { el.textContent='⚠ connection interrupted'; el.style.color='var(--crash)'; }
}

async function renameBot(name){
  const v=prompt('Rename "'+name+'" to:\n(letters & digits; spaces & symbols become "-". The folder is renamed too when the bot is stopped.)', name);
  if(v===null) return;
  const nn=v.trim(); if(!nn||nn===name) return;
  const d=await api('/api/instances/'+encodeURIComponent(name)+'/rename','POST',{new:nn});
  if(d&&d.error){ alert('✗ '+d.error); return; }
  if(CUR===name) CUR=d.name;          // keep an open drawer pointed at the renamed bot
  if(d&&d.note) alert('Renamed to "'+d.name+'".\n'+d.note);
  refresh();
}
async function act(name,action,btn){
  const card=btn.closest('.card');
  card.querySelectorAll('button').forEach(b=>b.disabled=true);
  const ic=btn.querySelector('.ic');               // swap just the icon for a spinner
  if(ic){ ic.innerHTML='<span class="spin"></span>'; } else { btn.innerHTML='<span class="spin"></span>'; }
  await api(`/api/instances/${encodeURIComponent(name)}/${action}`,'POST');
  await refresh();
}
async function bulk(action){
  if(action==='stop'&&!confirm('Stop ALL instances?'))return;
  document.querySelectorAll('.bulk button').forEach(b=>b.disabled=true);
  await api(`/api/${action}_all`,'POST');
  document.querySelectorAll('.bulk button').forEach(b=>b.disabled=false);
  refresh();
}

// ---- live viewer: bot-centered smooth scroll (interpolated @ rAF) + free pan/zoom ----
// State polls ~10 Hz; the canvas renders every frame, easing the bot pose toward the
// latest sample so motion is smooth at display fps even though the data is ~20 TPS.
// The map PNG is bot-centered on the bot (server side) and carries its world-center in
// headers, so the client pans it precisely. Free mode unlocks the camera (drag/zoom);
// since the bot only knows its own loaded chunks, panning past them shows empty space.
let VW=null, vwStateT=null, vwRAF=null, vwHandlersInit=false, vwData=null;
let vwES=null, vwStreamRetry=null;   // SSE push (~20 Hz) with polling fallback
// 2b2t nether ring-road radii (blocks) for the overlay; cardinals at x=0/z=0, diagonals x=±z.
const VW_RINGS=[200,500,1000,1500,2000,2500,5000,7500,10000,15000,20000,25000,50000,55000,62500,100000,125000,250000,500000,750000,1000000,1250000,1568852,1875000,2500000,3750000];
const VW_HOSTILE=new Set(['WITHER','WITHER_SKELETON','WITHER_SKULL','SKELETON','ZOMBIE','ZOMBIFIED_PIGLIN','PIGLIN','PIGLIN_BRUTE','HOGLIN','ZOGLIN','BLAZE','GHAST','MAGMA_CUBE','ENDERMAN','CREEPER','SPIDER','PHANTOM','VEX','PILLAGER','VINDICATOR','RAVAGER','EVOKER']);
let vwSamples=[], vwRender={x:0,y:0,z:0,yaw:0,pitch:0,has:false};
let vwMap={img:null,cx:0,cz:0,size:0,loading:false,fetchedAt:0};
let vwCam={x:0,z:0}, vwZoom=3, vwFollow=true, vwDrag=null;
let vwTab='map', vwCam3rd=false, vwGL=null, vwBox=null, vwChunkBusy=false, vwChunkAt=0, vwPovR=48;
function vwLerpAng(a,b,k){ let d=((b-a+540)%360)-180; return a+d*k; }
function vwUpdMode(){ const m=$('vwMode'); if(m) m.textContent = vwFollow?'FOLLOW':'FREE'; }
function vwOnlineSet(on){ const o=$('vwOff'); if(o) o.style.display = on?'none':'flex'; }
function openViewer(name){
  VW=name; $('vwName').textContent=name;
  vwSamples=[]; vwRender={x:0,y:0,z:0,yaw:0,pitch:0,has:false};
  vwMap={img:null,cx:0,cz:0,size:0,loading:false,fetchedAt:0};
  vwCam={x:0,z:0}; vwFollow=true; vwDrag=null; vwBox=null; vwChunkAt=0;
  if(vwGL){ vwGL.count=0; }   // drop the old mesh; context is reused on reopen
  vwInvData=null; vwModData=null; vwCmdData=null; vwCmdLoaded=false;   // fresh control data per bot
  vwZoom=$('vwCanvas').width/220;          // ~220 blocks across by default
  vwUpdMode(); vwOnlineSet(false); vwSetTab('map'); vwSetCam('1st');
  $('vwScrim').classList.add('open');
  vwInitHandlers(); vwTickState();
  clearInterval(vwStateT); vwStateT=setInterval(vwTickState,100);
  vwOpenStream();          // upgrade to ~20 Hz push; onopen stops the poll, onerror falls back to it
  if(!vwRAF) vwRAF=requestAnimationFrame(vwFrame);
}
function closeViewer(e){
  if(e && e.target!==$('vwScrim')) return;
  $('vwScrim').classList.remove('open');
  clearInterval(vwStateT); vwStateT=null;
  clearTimeout(vwStreamRetry); vwStreamRetry=null;
  vwCloseStream(); vwControlStop();
  if(vwRAF){ cancelAnimationFrame(vwRAF); vwRAF=null; }
  VW=null;
}
function vwCloseStream(){ if(vwES){ try{ vwES.close(); }catch(_){ } vwES=null; } }
function vwOpenStream(){
  if(typeof EventSource==='undefined' || !VW) return;        // ancient browser → polling only
  vwCloseStream();
  let es;
  try{ es=new EventSource('/api/instances/'+encodeURIComponent(VW)+'/viewer/stream'); }
  catch(e){ return; }
  vwES=es;
  es.onopen=()=>{ clearInterval(vwStateT); vwStateT=null; };  // push is live → stop the HTTP poll
  es.onmessage=(ev)=>{ if(!VW) return; let d=null; try{ d=JSON.parse(ev.data); }catch(_){ return; } vwApplyState(d); };
  es.onerror=()=>{
    if(!VW){ vwCloseStream(); return; }
    if(!vwStateT){ vwTickState(); vwStateT=setInterval(vwTickState,100); }   // keep data flowing via polling
    if(es.readyState===EventSource.CLOSED){     // fatal (e.g. 503 from relay); browser won't retry — we do
      vwCloseStream();
      clearTimeout(vwStreamRetry); vwStreamRetry=setTimeout(()=>{ if(VW) vwOpenStream(); }, 8000);
    }
  };
}
function vwRecenter(){ vwFollow=true; vwUpdMode(); }
async function vwTickState(){
  if(!VW) return;
  let d=null;
  try{ d=await (await fetch('/api/instances/'+encodeURIComponent(VW)+'/viewer/state')).json(); }catch(e){}
  vwApplyState(d);
}
function vwApplyState(d){
  if(!d || d.offline){ vwOnlineSet(false); vwData=null; $('vwHud').innerHTML='<span style="color:var(--crash)">● viewer offline</span>'; return; }
  vwOnlineSet(true); vwData=d;
  const s={x:+d.x, y:+d.y, z:+d.z, yaw:+d.yaw||0, pitch:+d.pitch||0};
  const prev=vwSamples[vwSamples.length-1];
  vwSamples.push(s); if(vwSamples.length>4) vwSamples.shift();
  const now=performance.now();   // live horizontal speed (b/s) from position deltas, smoothed
  if(prev&&vwLastSampleT){ const dt=(now-vwLastSampleT)/1000;
    if(dt>0.01&&dt<2){ vwSpeed=vwSpeed*0.6+(Math.hypot(s.x-prev.x,s.z-prev.z)/dt)*0.4; } }
  vwLastSampleT=now;
  if(!vwRender.has){ vwRender.x=s.x; vwRender.y=s.y; vwRender.z=s.z; vwRender.yaw=s.yaw; vwRender.pitch=s.pitch; vwRender.has=true; vwCam.x=s.x; vwCam.z=s.z; }
  if(vwTab==='control'){ vwRenderVitals(); vwRenderFlight(); }
  const band=d.band&&d.band!=='CLEAR'?' <span style="color:var(--warn)">⚠ '+esc(d.band)+'</span>':'';
  $('vwHud').innerHTML='<span>📍 '+Math.round(d.x)+', '+Math.round(d.y)+', '+Math.round(d.z)+'</span>'
    +'<span>'+esc(d.dimension||'?')+'</span>'
    +'<span>❤ '+(+d.health).toFixed(0)+'</span><span>🍗 '+d.food+'</span>'
    +'<span>✈ '+esc(d.flightPhase||'-')+band+'</span><span>↻ '+Math.round(d.yaw)+'°</span>';
}
async function vwFetchMap(size){
  vwMap.loading=true;
  try{
    const r=await fetch('/api/instances/'+encodeURIComponent(VW)+'/viewer/map?size='+size+'&ts='+Date.now());
    if(!r.ok) throw 0;
    const cx=+r.headers.get('X-Center-X'), cz=+r.headers.get('X-Center-Z'), sz=+r.headers.get('X-Size')||size;
    const bmp=await createImageBitmap(await r.blob());
    vwMap.img=bmp;
    vwMap.cx=isFinite(cx)?cx:vwRender.x; vwMap.cz=isFinite(cz)?cz:vwRender.z; vwMap.size=sz;
    vwMap.fetchedAt=performance.now();
  }catch(e){}
  finally{ vwMap.loading=false; }
}
function vwMaybeFetchMap(){
  const W=$('vwCanvas').width, viewB=W/vwZoom;
  const need=Math.min(512, Math.max(64, Math.ceil(viewB*1.3/16)*16));
  const m=vwMap;
  const moved = m.img && Math.hypot(vwRender.x-m.cx, vwRender.z-m.cz) > (m.size*0.5 - viewB*0.5 - 8);
  const tooSmall = m.img && m.size < Math.ceil(viewB/16)*16;
  const stale = (performance.now()-m.fetchedAt) > 4000;
  if(!m.loading && (!m.img || moved || tooSmall || stale)) vwFetchMap(need);
}
function vwFrame(){
  if(VW){
    const s=vwSamples[vwSamples.length-1];
    if(s){ vwRender.x+=(s.x-vwRender.x)*0.30; vwRender.y+=(s.y-vwRender.y)*0.30; vwRender.z+=(s.z-vwRender.z)*0.30;
           vwRender.yaw=vwLerpAng(vwRender.yaw,s.yaw,0.30); vwRender.pitch+=(s.pitch-vwRender.pitch)*0.30; }
    if(vwFollow){ vwCam.x=vwRender.x; vwCam.z=vwRender.z; }
    if(vwTab==='pov'){ if(vwGL) vwPovRender(); }
    else if(vwTab==='control'){ vwMaybeFetchMap(); vwDrawMini(); }
    else { vwMaybeFetchMap(); vwDraw(); }
  }
  vwRAF=requestAnimationFrame(vwFrame);
}
function vwDraw(){
  const c=$('vwCanvas'); if(!c) return;
  const x=c.getContext('2d'), W=c.width, H=c.height, z=vwZoom, camx=vwCam.x, camz=vwCam.z, m=vwMap;
  x.clearRect(0,0,W,H);
  if(m.img){
    x.imageSmoothingEnabled=false;
    const wx0=m.cx-m.size/2, wz0=m.cz-m.size/2;
    x.drawImage(m.img,(wx0-camx)*z+W/2,(wz0-camz)*z+H/2,m.size*z,m.size*z);
  }
  // ---- world overlays (same world->canvas transform) ----
  const P=(wx,wz)=>[(wx-camx)*z+W/2,(wz-camz)*z+H/2];
  const seg=(ax,az,bx,bz)=>{ const a=P(ax,az),b=P(bx,bz); x.beginPath(); x.moveTo(a[0],a[1]); x.lineTo(b[0],b[1]); x.stroke(); };
  const wMinX=camx-W/2/z, wMaxX=camx+W/2/z, wMinZ=camz-H/2/z, wMaxZ=camz+H/2/z;
  // highway network: cardinals (x=0, z=0), diagonals (x=±z), ring squares — clipped to the view
  x.lineWidth=1; x.strokeStyle='rgba(120,150,200,.30)';
  if(wMinX<=0&&wMaxX>=0) seg(0,wMinZ,0,wMaxZ);
  if(wMinZ<=0&&wMaxZ>=0) seg(wMinX,0,wMaxX,0);
  x.strokeStyle='rgba(120,150,200,.20)';
  { const x0=Math.max(wMinX,wMinZ),x1=Math.min(wMaxX,wMaxZ); if(x0<=x1) seg(x0,x0,x1,x1); }
  { const x0=Math.max(wMinX,-wMaxZ),x1=Math.min(wMaxX,-wMinZ); if(x0<=x1) seg(x0,-x0,x1,-x1); }
  x.strokeStyle='rgba(120,150,200,.22)';
  for(const r of VW_RINGS){
    for(const ex of [r,-r]){ if(ex<wMinX-1||ex>wMaxX+1) continue; const z0=Math.max(-r,wMinZ),z1=Math.min(r,wMaxZ); if(z0<=z1) seg(ex,z0,ex,z1); }
    for(const ez of [r,-r]){ if(ez<wMinZ-1||ez>wMaxZ+1) continue; const x0=Math.max(-r,wMinX),x1=Math.min(r,wMaxX); if(x0<=x1) seg(x0,ez,x1,ez); }
  }
  if(vwData){
    // reroute path (bot -> waypoints)
    if(vwData.reroute&&vwData.reroute.length&&vwRender.has){
      x.strokeStyle='#64d2ff'; x.lineWidth=2; x.beginPath();
      const b=P(vwRender.x,vwRender.z); x.moveTo(b[0],b[1]);
      for(const w of vwData.reroute){ const p=P(w[0],w[1]); x.lineTo(p[0],p[1]); }
      x.stroke();
      x.fillStyle='#64d2ff'; for(const w of vwData.reroute){ const p=P(w[0],w[1]); x.beginPath(); x.arc(p[0],p[1],3,0,7); x.fill(); }
    }
    // target
    if(vwData.target){ const p=P(vwData.target[0],vwData.target[1]); x.strokeStyle='#ff9f0a'; x.lineWidth=2;
      x.beginPath(); x.arc(p[0],p[1],7,0,7); x.stroke();
      x.beginPath(); x.moveTo(p[0]-10,p[1]); x.lineTo(p[0]+10,p[1]); x.moveTo(p[0],p[1]-10); x.lineTo(p[0],p[1]+10); x.stroke(); }
    // grief markers (red X)
    if(vwData.grief){ x.strokeStyle='#ff5b4d'; x.lineWidth=2;
      for(const g of vwData.grief){ const p=P(g[0],g[1]);
        x.beginPath(); x.moveTo(p[0]-5,p[1]-5); x.lineTo(p[0]+5,p[1]+5); x.moveTo(p[0]-5,p[1]+5); x.lineTo(p[0]+5,p[1]-5); x.stroke(); } }
    // entities (dots colored by kind)
    if(vwData.entities){
      for(const e of vwData.entities){ const p=P(e.x,e.z), t=(e.type||'').toUpperCase();
        let col='#9aa7b2';
        if(t==='PLAYER') col='#ffffff';
        else if(t==='ITEM'||t==='EXPERIENCE_ORB') col='#ffd60a';
        else if(VW_HOSTILE.has(t)) col='#ff5b4d';
        x.fillStyle=col; x.beginPath(); x.arc(p[0],p[1],3.2,0,7); x.fill();
        if(t==='PLAYER'){ x.strokeStyle='#ffffff'; x.lineWidth=1; x.beginPath(); x.arc(p[0],p[1],5.5,0,7); x.stroke(); } }
    }
  }
  if(!vwRender.has) return;
  const bx=(vwRender.x-camx)*z+W/2, by=(vwRender.z-camz)*z+H/2;
  x.strokeStyle='rgba(255,255,255,.30)'; x.lineWidth=2;
  x.beginPath(); x.moveTo(bx-11,by); x.lineTo(bx+11,by); x.moveTo(bx,by-11); x.lineTo(bx,by+11); x.stroke();
  const yaw=vwRender.yaw*Math.PI/180, dx=-Math.sin(yaw), dz=Math.cos(yaw), L=26;
  const ex=bx+dx*L, ey=by+dz*L, a=Math.atan2(dz,dx);
  x.strokeStyle='#30d158'; x.fillStyle='#30d158'; x.lineWidth=3;
  x.beginPath(); x.moveTo(bx,by); x.lineTo(ex,ey); x.stroke();
  x.beginPath(); x.moveTo(ex,ey);
  x.lineTo(ex-12*Math.cos(a-0.42), ey-12*Math.sin(a-0.42));
  x.lineTo(ex-12*Math.cos(a+0.42), ey-12*Math.sin(a+0.42));
  x.closePath(); x.fill();
  x.fillStyle='#fff'; x.beginPath(); x.arc(bx,by,3.5,0,7); x.fill();
}
function vwInitHandlers(){
  if(vwHandlersInit) return; vwHandlersInit=true;
  const wrap=$('vwWrap');
  wrap.addEventListener('wheel',(e)=>{
    e.preventDefault();
    const W=$('vwCanvas').width, f=e.deltaY<0?1.12:1/1.12;
    vwZoom=Math.max(W/512, Math.min(W/24, vwZoom*f));
  },{passive:false});
  wrap.addEventListener('pointerdown',(e)=>{
    wrap.setPointerCapture(e.pointerId);
    if(vwFollow){ vwFollow=false; vwCam.x=vwRender.x; vwCam.z=vwRender.z; vwUpdMode(); }
    vwDrag={sx:e.clientX, sy:e.clientY, cx:vwCam.x, cz:vwCam.z};
    wrap.style.cursor='grabbing';
  });
  wrap.addEventListener('pointermove',(e)=>{
    if(!vwDrag) return;
    const cv=$('vwCanvas'), sc=cv.width/cv.getBoundingClientRect().width;
    vwCam.x=vwDrag.cx-(e.clientX-vwDrag.sx)*sc/vwZoom;
    vwCam.z=vwDrag.cz-(e.clientY-vwDrag.sy)*sc/vwZoom;
  });
  const end=()=>{ vwDrag=null; $('vwWrap').style.cursor='grab'; };
  wrap.addEventListener('pointerup',end); wrap.addEventListener('pointercancel',end);
}
// ---- POV: three.js fullbright voxel renderer (1st / 3rd person) ----
function vwSetTab(t){
  vwTab=t;
  $('vwTabMap').classList.toggle('active', t==='map');
  $('vwTabPov').classList.toggle('active', t==='pov');
  $('vwTabCtl').classList.toggle('active', t==='control');
  $('vwWrap').style.display = t==='map'?'block':'none';
  $('vwPovWrap').style.display = t==='pov'?'block':'none';
  $('vwControlWrap').style.display = t==='control'?'block':'none';
  const md=$('vwModal'); if(md) md.style.width = (t==='control'?'min(1040px,96vw)':'min(760px,96vw)');
  if(t==='pov'){ vwInitPov(); vwControlStop(); }
  else if(t==='control'){ vwControlStart(); }
  else { vwControlStop(); }
}
function vwSetCam(m){
  vwCam3rd=(m==='3rd');
  const a=$('vwCam1'), b=$('vwCam3');
  if(a){ a.style.color=vwCam3rd?'var(--txt)':'var(--acc)'; a.style.borderColor=vwCam3rd?'var(--line)':'var(--acc-dim)'; }
  if(b){ b.style.color=vwCam3rd?'var(--acc)':'var(--txt)'; b.style.borderColor=vwCam3rd?'var(--acc-dim)':'var(--line)'; }
}
function vwSetDist(r){
  vwPovR=r;
  for(const v of [32,48,64]){ const b=$('vwD'+v); if(b){ const on=(v===r);
    b.style.color=on?'var(--acc)':'var(--txt)'; b.style.borderColor=on?'var(--acc-dim)':'var(--line)'; } }
  vwChunkAt=0; vwFetchChunks();   // re-fetch the voxel box at the new radius
}
// Self-contained WebGL voxel renderer — no three.js, no external fetch (fully offline).
// Column-major 4x4 matrix helpers (gl-matrix conventions, no deps).
function vwM4Mul(a,b){ const o=new Float32Array(16);
  for(let c=0;c<4;c++) for(let r=0;r<4;r++){ let s=0; for(let k=0;k<4;k++) s+=a[k*4+r]*b[c*4+k]; o[c*4+r]=s; }
  return o; }
function vwPerspective(fovy,aspect,near,far){ const f=1/Math.tan(fovy/2), nf=1/(near-far);
  return new Float32Array([ f/aspect,0,0,0, 0,f,0,0, 0,0,(far+near)*nf,-1, 0,0,2*far*near*nf,0 ]); }
function vwLookAt(e,c,up){
  let zx=e[0]-c[0],zy=e[1]-c[1],zz=e[2]-c[2]; const zl=Math.hypot(zx,zy,zz)||1; zx/=zl;zy/=zl;zz/=zl;
  let xx=up[1]*zz-up[2]*zy, xy=up[2]*zx-up[0]*zz, xz=up[0]*zy-up[1]*zx; const xl=Math.hypot(xx,xy,xz)||1; xx/=xl;xy/=xl;xz/=xl;
  const yx=zy*xz-zz*xy, yy=zz*xx-zx*xz, yz=zx*xy-zy*xx;
  return new Float32Array([ xx,yx,zx,0, xy,yy,zy,0, xz,yz,zz,0,
    -(xx*e[0]+xy*e[1]+xz*e[2]), -(yx*e[0]+yy*e[1]+yz*e[2]), -(zx*e[0]+zy*e[1]+zz*e[2]), 1 ]); }
function vwCompileShader(gl,type,src){
  const s=gl.createShader(type); gl.shaderSource(s,src); gl.compileShader(s);
  if(!gl.getShaderParameter(s,gl.COMPILE_STATUS)){ const e=gl.getShaderInfoLog(s); gl.deleteShader(s); throw new Error(e); }
  return s; }
function vwInitPov(){
  if(vwGL){ vwResizePov(); return; }
  try{
    const cv=$('vwPov');
    const gl=cv.getContext('webgl')||cv.getContext('experimental-webgl');
    if(!gl) throw new Error('WebGL not supported');
    const vs=vwCompileShader(gl, gl.VERTEX_SHADER,
      'attribute vec3 aPos;attribute vec3 aCol;uniform mat4 uMVP;uniform vec3 uCam;'
      +'varying vec3 vCol;varying float vDist;'
      +'void main(){vCol=aCol;vDist=length(aPos-uCam);gl_Position=uMVP*vec4(aPos,1.0);}');
    const fs=vwCompileShader(gl, gl.FRAGMENT_SHADER,
      'precision mediump float;varying vec3 vCol;varying float vDist;'
      +'uniform vec3 uFogCol;uniform vec2 uFogRange;'
      +'void main(){float f=clamp((vDist-uFogRange.x)/(uFogRange.y-uFogRange.x),0.0,1.0);'
      +'gl_FragColor=vec4(mix(vCol,uFogCol,f),1.0);}');
    const prog=gl.createProgram(); gl.attachShader(prog,vs); gl.attachShader(prog,fs); gl.linkProgram(prog);
    if(!gl.getProgramParameter(prog,gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    gl.useProgram(prog);
    gl.enable(gl.DEPTH_TEST); gl.clearColor(0x06/255,0x09/255,0x0c/255,1);
    vwGL={gl,prog,
      locPos:gl.getAttribLocation(prog,'aPos'), locCol:gl.getAttribLocation(prog,'aCol'),
      locMVP:gl.getUniformLocation(prog,'uMVP'), locCam:gl.getUniformLocation(prog,'uCam'),
      locFogCol:gl.getUniformLocation(prog,'uFogCol'), locFogRange:gl.getUniformLocation(prog,'uFogRange'),
      bufPos:gl.createBuffer(), bufCol:gl.createBuffer(), count:0};
    vwResizePov(); $('vwPovMsg').style.display='none';
    vwFetchChunks();
  }catch(e){ const m=$('vwPovMsg'); if(m){ m.style.display='flex'; m.textContent='3D unavailable ('+e+')'; } }
}
function vwResizePov(){
  if(!vwGL) return;
  const cv=$('vwPov'), dpr=Math.min(2, window.devicePixelRatio||1);
  const w=Math.max(1,Math.round((cv.clientWidth||600)*dpr)), h=Math.max(1,Math.round((cv.clientHeight||600)*dpr));
  if(cv.width!==w||cv.height!==h){ cv.width=w; cv.height=h; }
  vwGL.gl.viewport(0,0,cv.width,cv.height);
}
async function vwFetchChunks(){
  if(vwChunkBusy||!VW||!vwRender.has) return;
  vwChunkBusy=true;
  try{
    const r=await fetch('/api/instances/'+encodeURIComponent(VW)+'/viewer/chunks?r='+vwPovR+'&yb='+vwPovR+'&ya='+vwPovR);
    if(!r.ok) throw 0;
    let buf=await r.arrayBuffer();
    if((r.headers.get('X-Encoding')||'')==='deflate'){
      const ds=new DecompressionStream('deflate');
      buf=await new Response(new Blob([buf]).stream().pipeThrough(ds)).arrayBuffer();
    }
    vwBuildMesh(buf); vwChunkAt=performance.now();
  }catch(e){}
  finally{ vwChunkBusy=false; }
}
// n=face normal, s=directional shade, c=4 corner offsets, t=the two tangent axes (for AO sampling)
const VW_FACES=[
  {n:[0,1,0], s:1.00, t:[0,2], c:[[0,1,0],[0,1,1],[1,1,1],[1,1,0]]},
  {n:[0,-1,0],s:0.55, t:[0,2], c:[[0,0,1],[0,0,0],[1,0,0],[1,0,1]]},
  {n:[1,0,0], s:0.80, t:[1,2], c:[[1,0,0],[1,1,0],[1,1,1],[1,0,1]]},
  {n:[-1,0,0],s:0.80, t:[1,2], c:[[0,0,1],[0,1,1],[0,1,0],[0,0,0]]},
  {n:[0,0,1], s:0.70, t:[0,1], c:[[1,0,1],[1,1,1],[0,1,1],[0,0,1]]},
  {n:[0,0,-1],s:0.70, t:[0,1], c:[[0,0,0],[0,1,0],[1,1,0],[1,0,0]]}
];
const VW_AO=[0.45,0.66,0.84,1.0];   // per-vertex occlusion multipliers (inner corner -> open)
function vwBuildMesh(buf){
  if(!vwGL) return;
  const gl=vwGL.gl, dv=new DataView(buf);
  const ox=dv.getInt32(0), oy=dv.getInt32(4), oz=dv.getInt32(8);
  const sx=dv.getUint16(12), sy=dv.getUint16(14), sz=dv.getUint16(16);
  const pal=new Uint8Array(buf,18,192), vox=new Uint8Array(buf,210,sx*sy*sz);
  const at=(x,y,z)=> (x<0||y<0||z<0||x>=sx||y>=sy||z>=sz)?0:vox[(y*sz+z)*sx+x];
  const pos=[], col=[];
  for(let y=0;y<sy;y++) for(let z=0;z<sz;z++) for(let x=0;x<sx;x++){
    const id=vox[(y*sz+z)*sx+x]; if(!id) continue;
    const pr=pal[id*3], pg=pal[id*3+1], pb=pal[id*3+2], wx=ox+x, wy=oy+y, wz=oz+z;
    for(const f of VW_FACES){
      const fx=x+f.n[0], fy=y+f.n[1], fz=z+f.n[2];
      if(at(fx,fy,fz)) continue;                 // face hidden by a neighbour
      const c=f.c, ta=f.t[0], tb=f.t[1], base=f.s/255;
      const crf=pr*base, cgf=pg*base, cbf=pb*base;
      // ambient occlusion: darken each corner by its two edge + diagonal neighbours in the front layer
      const ao=[0,0,0,0];
      for(let k=0;k<4;k++){
        const o1=[0,0,0]; o1[ta]=c[k][ta]?1:-1;
        const o2=[0,0,0]; o2[tb]=c[k][tb]?1:-1;
        const e1=at(fx+o1[0],fy+o1[1],fz+o1[2])?1:0;
        const e2=at(fx+o2[0],fy+o2[1],fz+o2[2])?1:0;
        const ec=(e1&&e2)?1:(at(fx+o1[0]+o2[0],fy+o1[1]+o2[1],fz+o1[2]+o2[2])?1:0);
        ao[k]=VW_AO[(e1&&e2)?0:3-(e1+e2+ec)];
      }
      for(const i of [0,1,2,0,2,3]){
        const a=ao[i];
        pos.push(wx+c[i][0],wy+c[i][1],wz+c[i][2]);
        col.push(crf*a, cgf*a, cbf*a);
      }
    }
  }
  gl.bindBuffer(gl.ARRAY_BUFFER, vwGL.bufPos); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(pos), gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, vwGL.bufCol); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(col), gl.STATIC_DRAW);
  vwGL.count=pos.length/3;
  vwBox={cx:ox+sx/2, cz:oz+sz/2, sx:sx};
}
function vwPovRender(){
  if(!vwGL) return;
  const gl=vwGL.gl, cv=$('vwPov');
  const ex=vwRender.x+0.5, ey=vwRender.y+1.62, ez=vwRender.z+0.5;
  const yaw=vwRender.yaw*Math.PI/180, pit=vwRender.pitch*Math.PI/180;
  const lx=-Math.sin(yaw)*Math.cos(pit), ly=-Math.sin(pit), lz=Math.cos(yaw)*Math.cos(pit);
  let eye, ctr;
  if(vwCam3rd){ const D=5; eye=[ex-lx*D, ey-ly*D+1.4, ez-lz*D]; ctr=[ex,ey,ez]; }
  else { eye=[ex,ey,ez]; ctr=[ex+lx, ey+ly, ez+lz]; }
  const proj=vwPerspective(75*Math.PI/180, (cv.width/cv.height)||1, 0.1, 3000);
  const mvp=vwM4Mul(proj, vwLookAt(eye, ctr, [0,1,0]));
  gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
  if(vwGL.count){
    gl.useProgram(vwGL.prog);
    gl.uniformMatrix4fv(vwGL.locMVP, false, mvp);
    gl.uniform3f(vwGL.locCam, eye[0], eye[1], eye[2]);
    gl.uniform3f(vwGL.locFogCol, 0x06/255, 0x09/255, 0x0c/255);    // fade distant blocks into the backdrop
    const fr=(vwBox?vwBox.sx*0.5:vwPovR);                          // box half-width ~ render radius
    gl.uniform2f(vwGL.locFogRange, fr*0.6, fr*1.15);
    gl.bindBuffer(gl.ARRAY_BUFFER, vwGL.bufPos); gl.enableVertexAttribArray(vwGL.locPos); gl.vertexAttribPointer(vwGL.locPos,3,gl.FLOAT,false,0,0);
    gl.bindBuffer(gl.ARRAY_BUFFER, vwGL.bufCol); gl.enableVertexAttribArray(vwGL.locCol); gl.vertexAttribPointer(vwGL.locCol,3,gl.FLOAT,false,0,0);
    gl.drawArrays(gl.TRIANGLES, 0, vwGL.count);
  }
  if(!vwBox || Math.hypot(vwRender.x-vwBox.cx, vwRender.z-vwBox.cz)>vwBox.sx*0.30 || performance.now()-vwChunkAt>6000) vwFetchChunks();
}
// ---- Control tab: live vitals + inventory (real MC item icons), module toggles, command palette, mini-map ----
let vwInvData=null, vwModData=null, vwCmdData=null, vwInvT=null, vwModT=null, vwCmdLoaded=false, vwSpeed=0, vwLastSampleT=0;
const VW_ICON='https://raw.githubusercontent.com/InventivetalentDev/minecraft-assets/1.21.4/assets/minecraft/textures/';
function vwIconFallback(img,next){ if(img.dataset.f){ img.style.display='none'; } else { img.dataset.f='1'; img.src=next; } }
function vwIconHtml(name){
  if(!name) return '';
  const n=name.replace('minecraft:','');
  return '<img class="vw-ic" loading="lazy" src="'+VW_ICON+'item/'+n+'.png" onerror="vwIconFallback(this,\''+VW_ICON+'block/'+n+'.png\')" alt="">';
}
function vwShort(n){ return (n||'').replace('minecraft:','').replace(/_/g,' '); }
function vwSlot(it){
  if(!it) return '<div class="vw-slot"></div>';
  const n=it.name||''; let extra='';
  if(it.max){ const f=Math.max(0,Math.min(1,it.dur/it.max)); const c=f>0.5?'#3fb950':(f>0.25?'#d29922':'#f85149');
    extra+='<div class="vw-dur"><i style="width:'+(f*100).toFixed(0)+'%;background:'+c+'"></i></div>'; }
  if(it.count>1) extra+='<span class="vw-cnt">'+it.count+'</span>';
  const tip=esc(vwShort(n))+(it.max?(' • '+it.dur+'/'+it.max):'')+(it.ench?' • enchanted':'');
  return '<div class="vw-slot'+(it.ench?' vw-ench':'')+'" title="'+tip+'"><span class="vw-lbl">'+esc(vwShort(n))+'</span>'+vwIconHtml(n)+extra+'</div>';
}
function vwRenderVitals(){
  const d=vwData||{}, iv=vwInvData||{};
  const hp=+((d.health!=null)?d.health:iv.health)||0, food=+((d.food!=null)?d.food:iv.food)||0;
  const bar=(l,v,mx,c)=>'<div class="vw-stat"><span class="vw-i">'+l+'</span><div class="vw-bar"><i style="width:'+(Math.max(0,Math.min(1,v/mx))*100).toFixed(0)+'%;background:'+c+'"></i></div><b>'+Math.round(v)+'</b></div>';
  let h=bar('❤',hp,20,'#f85149')+bar('🍗',food,20,'#d29922');
  if(iv.totems!=null) h+='<div class="vw-chip">⛨ '+iv.totems+' totem'+(iv.totems===1?'':'s')+'</div>';
  if(d.dimension) h+='<div class="vw-chip">'+esc(d.dimension.replace('minecraft:',''))+'</div>';
  if(d.flightPhase&&d.flightPhase!=='IDLE') h+='<div class="vw-chip">✈ '+esc(d.flightPhase)+'</div>';
  $('vwVitals').innerHTML=h;
}
function vwRenderEquip(){
  const iv=vwInvData||{}, a=iv.armor||{};
  const slots=[['Helm',a.helmet],['Chest',a.chestplate],['Legs',a.leggings],['Boots',a.boots],['Main',iv.mainHand],['Off',iv.offHand]];
  $('vwEquip').innerHTML=slots.map(s=>'<div class="vw-eq">'+vwSlot(s[1])+'<span class="vw-eqlab">'+s[0]+'</span></div>').join('');
}
function vwRenderInv(){
  const iv=vwInvData||{}, g=a=>(a||[]).map(vwSlot).join('');
  $('vwInv').innerHTML='<div class="vw-grid">'+g(iv.main)+'</div><div class="vw-grid vw-hot">'+g(iv.hotbar)+'</div>';
}
function vwFindElytra(){
  const iv=vwInvData||{}; let worn=null, spares=0;
  const chest=iv.armor&&iv.armor.chestplate;
  if(chest&&chest.name&&chest.name.indexOf('elytra')>=0) worn=chest;
  for(const arr of [iv.hotbar,iv.main]) for(const it of (arr||[])) if(it&&it.name&&it.name.indexOf('elytra')>=0) spares++;
  return {worn,spares};
}
function vwRenderFlight(){
  const el=$('vwFlightStat'); if(!el) return;
  const d=vwData||{}, e=vwFindElytra();
  const phase=d.flightPhase||'IDLE', band=d.band, tgt=d.target?(d.target[0]+', '+d.target[1]):'—';
  let h='<div class="vw-fl-top"><span class="vw-chip">✈ '+esc(phase)+'</span>';
  if(band&&band!=='CLEAR') h+='<span class="vw-chip" style="color:var(--warn)">⚠ '+esc(band)+'</span>';
  h+='<span class="vw-fl-stat">speed <b>'+(vwSpeed||0).toFixed(0)+'</b> b/s</span>'
    +'<span class="vw-fl-stat">alt <b>'+Math.round(d.y||0)+'</b></span>'
    +'<span class="vw-fl-stat">target <b>'+esc(tgt)+'</b></span></div>';
  if(e.worn&&e.worn.max){ const f=Math.max(0,Math.min(1,e.worn.dur/e.worn.max)), c=f>0.5?'#3fb950':(f>0.25?'#d29922':'#f85149');
    h+='<div class="vw-fl-row"><span>elytra</span><div class="vw-bar" style="width:150px"><i style="width:'+(f*100).toFixed(0)+'%;background:'+c+'"></i></div><b>'+e.worn.dur+'/'+e.worn.max+'</b><span class="vw-chip">×'+e.spares+' spare'+(e.spares===1?'':'s')+'</span></div>'; }
  else { h+='<div class="vw-fl-row"><span style="color:var(--dim)">no elytra worn</span><span class="vw-chip">×'+e.spares+' spare'+(e.spares===1?'':'s')+'</span></div>'; }
  el.innerHTML=h;
}
function vwFlyTo(){ const x=(($('vwFlX')||{}).value||'').trim(), z=(($('vwFlZ')||{}).value||'').trim();
  if(x!==''&&z!=='') vwRunCommand('fly trip nether '+x+' '+z); }
function vwMiniClick(e){
  const cv=$('vwMiniCanvas'); if(!cv||!vwRender.has) return;
  const r=cv.getBoundingClientRect(), W=cv.width, z=W/110;
  const px=(e.clientX-r.left)/r.width*W, py=(e.clientY-r.top)/r.height*cv.height;
  const wx=Math.round(vwRender.x+(px-W/2)/z), wz=Math.round(vwRender.z+(py-cv.height/2)/z);
  if($('vwFlX')) $('vwFlX').value=wx; if($('vwFlZ')) $('vwFlZ').value=wz;
}
function vwRenderModules(){
  const mods=(vwModData&&vwModData.modules)||[];
  const f=(($('vwModFilter')||{}).value||'').toLowerCase();
  $('vwModules').innerHTML=mods.filter(m=>m.name.toLowerCase().includes(f)).map(m=>
    '<label class="vw-mod"><span>'+esc(m.name)+'</span><span class="vw-sw'+(m.enabled?' on':'')+'" onclick="vwModToggle(\''+jsq(m.name)+'\','+(!m.enabled)+')"><i></i></span></label>').join('')
    || '<div style="color:var(--dim);font-size:.7rem;padding:.3rem">no modules</div>';
}
function vwRenderPalette(){
  const cmds=vwCmdData||[];
  const f=(($('vwCmdSearch')||{}).value||'').toLowerCase();
  const list=cmds.filter(c=>c.name.toLowerCase().includes(f)||((c.description||'').toLowerCase().includes(f))).slice(0,100);
  $('vwPalette').innerHTML=list.map(c=>'<div class="vw-cmd" onclick="vwPickCmd(\''+jsq(c.name)+'\')"><b>'+esc(c.name)+'</b><span>'+esc((c.description||'').replace(/\s+/g,' ').trim().slice(0,90))+'</span></div>').join('');
}
function vwPickCmd(n){ const i=$('vwCmdInput'); if(i){ i.value=n+' '; i.focus(); } }
function vwRunInput(){ const v=(($('vwCmdInput')||{}).value||'').trim(); if(v) vwRunCommand(v); }
function vwRunCommand(cmd,quiet){
  if(!VW) return;
  fetch('/api/instances/'+encodeURIComponent(VW)+'/control/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:cmd})})
   .then(r=>r.json().catch(()=>({error:'HTTP '+r.status}))).then(d=>{
     if(!quiet){ const lines=(d&&d.lines)||[(d&&d.error)||'(no output)'];
       $('vwCmdResult').innerHTML='<div class="vw-rcmd">› '+esc(cmd)+'</div>'+lines.map(l=>'<div>'+esc(l)+'</div>').join(''); }
     setTimeout(vwTickInv,500);
   }).catch(e=>{ if(!quiet&&$('vwCmdResult')) $('vwCmdResult').textContent='error: '+e; });
}
function vwModToggle(name,on){ vwRunCommand(name.toLowerCase()+' '+(on?'on':'off'),true); setTimeout(vwTickMod,700); setTimeout(vwTickMod,1700); }
async function vwTickInv(){
  if(!VW||vwTab!=='control') return;
  try{ const d=await(await fetch('/api/instances/'+encodeURIComponent(VW)+'/viewer/inventory')).json();
    if(d&&!d.offline){ vwInvData=d; vwRenderVitals(); vwRenderEquip(); vwRenderInv(); vwRenderFlight(); } }catch(e){}
}
async function vwTickMod(){
  if(!VW||vwTab!=='control') return;
  try{ const d=await(await fetch('/api/instances/'+encodeURIComponent(VW)+'/control/state')).json();
    if(d&&!d.offline){ vwModData=d; vwRenderModules(); vwCtlBanner(d.control); } }catch(e){}
}
function vwCtlBanner(on){ const b=$('vwCtlBanner'); if(b) b.innerHTML = on ? '' :
  '<div class="vw-banner">Control is read-only. Enable <code>server.viewer.control</code> on the bot to run commands &amp; toggles.</div>'; }
async function vwLoadCommands(){
  if(vwCmdLoaded||!VW) return;
  try{ const d=await(await fetch('/api/instances/'+encodeURIComponent(VW)+'/control/commands')).json();
    if(Array.isArray(d)){ vwCmdData=d; vwCmdLoaded=true; vwRenderPalette(); } }catch(e){}
}
function vwControlStart(){
  const q=$('vwQuick'); if(q&&!q.dataset.init){ q.dataset.init='1';
    q.innerHTML=[['Connect','connect'],['Disconnect','disconnect'],['Status','info'],['Reconnect','reconnect']]
      .map(a=>'<button class="vw-qbtn" onclick="vwRunCommand(\''+a[1]+'\')">'+a[0]+'</button>').join(''); }
  vwRenderVitals(); vwRenderFlight(); vwTickInv(); vwTickMod(); vwLoadCommands();
  clearInterval(vwInvT); vwInvT=setInterval(vwTickInv,1500);
  clearInterval(vwModT); vwModT=setInterval(vwTickMod,2800);
}
function vwControlStop(){ clearInterval(vwInvT); vwInvT=null; clearInterval(vwModT); vwModT=null; }
function vwOpenFullControl(){
  const style=localStorage.getItem('abmControlStyle')||'v1';
  window.open('/control?inst='+encodeURIComponent(VW)+'&style='+style, '_blank');
}
function vwDrawMini(){
  const cv=$('vwMiniCanvas'); if(!cv) return;
  const x=cv.getContext('2d'), W=cv.width, H=cv.height, z=W/110;   // ~110 blocks across
  x.fillStyle='#06090c'; x.fillRect(0,0,W,H);
  if(!vwRender.has) return;
  const camx=vwRender.x, camz=vwRender.z, m=vwMap;
  if(m.img){ const x0=(m.cx-m.size/2-camx)*z+W/2, y0=(m.cz-m.size/2-camz)*z+H/2;
    x.imageSmoothingEnabled=false; x.drawImage(m.img, x0, y0, m.size*z, m.size*z); }
  const bx=W/2, by=H/2;
  x.strokeStyle='rgba(255,255,255,.35)'; x.lineWidth=2;
  x.beginPath(); x.moveTo(bx-9,by); x.lineTo(bx+9,by); x.moveTo(bx,by-9); x.lineTo(bx,by+9); x.stroke();
  const yaw=vwRender.yaw*Math.PI/180, dx=-Math.sin(yaw), dz=Math.cos(yaw), L=22, a=Math.atan2(dz,dx);
  const ex=bx+dx*L, ey=by+dz*L;
  x.strokeStyle='#30d158'; x.fillStyle='#30d158'; x.lineWidth=3;
  x.beginPath(); x.moveTo(bx,by); x.lineTo(ex,ey); x.stroke();
  x.beginPath(); x.moveTo(ex,ey); x.lineTo(ex-10*Math.cos(a-0.42),ey-10*Math.sin(a-0.42)); x.lineTo(ex-10*Math.cos(a+0.42),ey-10*Math.sin(a+0.42)); x.closePath(); x.fill();
  x.fillStyle='#fff'; x.beginPath(); x.arc(bx,by,3,0,7); x.fill();
}
function openDrawer(name){
  CUR=name; $('drawerName').textContent=name;
  $('scrim').classList.add('open'); $('drawer').classList.add('open');
  showTab('logs');
}
function closeDrawer(){
  $('scrim').classList.remove('open'); $('drawer').classList.remove('open');
  CUR=null; if(logTimer){clearInterval(logTimer);logTimer=null;}
}
function showTab(t){
  TAB=t;
  $('tabLogsBtn').classList.toggle('active',t==='logs');
  $('tabCfgBtn').classList.toggle('active',t==='cfg');
  $('tabLimBtn').classList.toggle('active',t==='lim');
  $('tabLogs').style.display=t==='logs'?'':'none';
  $('tabCfg').style.display=t==='cfg'?'':'none';
  $('tabLim').style.display=t==='lim'?'':'none';
  $('logRefresh').style.display=t==='logs'?'':'none';
  $('cfgSave').style.display=t==='cfg'?'':'none';
  $('cfgSaveRestart').style.display=t==='cfg'?'':'none';
  if(logTimer){clearInterval(logTimer);logTimer=null;}
  if(t==='logs'){renderPresetBar();logPinned=true;lastLogText=null;loadLogs();logTimer=setInterval(loadLogs,3000);}
  else if(t==='cfg'){loadCfg();}
  else if(t==='lim'){renderLimits();}
}
function renderPresetBar(){
  const bar=$('presetBar'); if(!bar)return;
  const presets=(SETTINGS&&SETTINGS.console_presets)||[];
  bar.innerHTML=presets.map((p,i)=>`<button class="chip" title="sends: ${esc(p.command)}" onclick="sendPreset(${i})">${esc(p.label)}</button>`).join('');
}
async function sendPreset(i){
  const presets=(SETTINGS&&SETTINGS.console_presets)||[];
  if(!presets[i]||!CUR)return;
  const d=await api(`/api/instances/${encodeURIComponent(CUR)}/command`,'POST',{command:presets[i].command});
  if(d.error){ $('drawerMsg').textContent='✗ '+d.error; return; }
  $('drawerMsg').textContent='✓ sent: '+presets[i].command;
  setTimeout(loadLogs,250); setTimeout(loadLogs,1200);
}
// follow-tail / scroll-pause state for the console
let logPinned=true, lastLogText=null, lastSeenLines=0;
function logLineCount(){ return lastLogText ? lastLogText.split('\n').length : 0; }
function onLogScroll(){
  const box=$('logBox'); if(!box) return;
  // pinned = parked at (or near) the bottom → keep following new lines
  logPinned = box.scrollHeight-box.scrollTop-box.clientHeight < 40;
  if(logPinned){ const p=$('logPill'); if(p) p.style.display='none'; lastSeenLines=logLineCount(); }
}
function jumpLogBottom(){
  const box=$('logBox'); if(!box) return;
  logPinned=true; box.scrollTop=box.scrollHeight;
  const p=$('logPill'); if(p) p.style.display='none'; lastSeenLines=logLineCount();
}
async function loadLogs(){
  if(!CUR)return;
  const d=await api(`/api/instances/${encodeURIComponent(CUR)}/logs?lines=400`);
  const box=$('logBox'); if(!box) return;
  const txt=d.logs||'(empty)';
  if(txt===lastLogText){ if(logPinned) box.scrollTop=box.scrollHeight; return; }
  const prevH=box.scrollHeight, prevTop=box.scrollTop;
  lastLogText=txt; box.textContent=txt;
  if(logPinned){
    box.scrollTop=box.scrollHeight; lastSeenLines=logLineCount();
    const p=$('logPill'); if(p) p.style.display='none';
  }else{
    // user scrolled up: hold their view steady (constant distance from the bottom)
    // as new lines append, and surface a pill instead of yanking them to the tail
    box.scrollTop = box.scrollHeight - prevH + prevTop;
    const unseen = Math.max(0, logLineCount()-lastSeenLines);
    const p=$('logPill'); if(p){ p.style.display='flex';
      p.textContent = unseen>0 ? ('↓ '+unseen+' new line'+(unseen===1?'':'s')) : '↓ Jump to latest'; }
  }
}
let CMDHIST=[], CMDIDX=-1;
async function sendCmd(){
  const inp=$('cmdInput'); const cmd=inp.value.trim();
  if(!cmd)return;
  inp.disabled=true;
  const d=await api(`/api/instances/${encodeURIComponent(CUR)}/command`,'POST',{command:cmd});
  inp.disabled=false; inp.focus();
  if(d.error){ $('drawerMsg').textContent='✗ '+d.error; return; }
  CMDHIST.push(cmd); CMDIDX=CMDHIST.length; inp.value='';
  $('drawerMsg').textContent='';
  // surface the result quickly (log also auto-polls)
  setTimeout(loadLogs,250); setTimeout(loadLogs,1200);
}
function cmdKey(e){
  if(e.key==='Enter'){ e.preventDefault(); sendCmd(); }
  else if(e.key==='ArrowUp'){ if(CMDHIST.length){ e.preventDefault(); CMDIDX=Math.max(0,CMDIDX-1); $('cmdInput').value=CMDHIST[CMDIDX]||''; } }
  else if(e.key==='ArrowDown'){ if(CMDHIST.length){ e.preventDefault(); CMDIDX=Math.min(CMDHIST.length,CMDIDX+1); $('cmdInput').value=CMDHIST[CMDIDX]||''; } }
}
let CFGOBJ=null, CFGRAW=false, CFGPARSE_OK=true, SCHEMA=null;

async function ensureSchema(){
  if(SCHEMA)return SCHEMA;
  const d=await api('/api/schema'); SCHEMA=d.schema||{}; return SCHEMA;
}
async function loadCfg(){
  if(!CUR)return;
  $('drawerMsg').textContent='loading…';
  await ensureSchema();
  const d=await api(`/api/instances/${encodeURIComponent(CUR)}/config`);
  $('cfgBox').value=d.config||'';
  $('drawerMsg').textContent=d.exists?d.path:'(file will be created on save) '+d.path;
  try{ CFGOBJ = d.config && d.config.trim() ? JSON.parse(d.config) : {}; CFGPARSE_OK=true; }
  catch(e){ CFGOBJ=null; CFGPARSE_OK=false; }
  if(!CFGPARSE_OK || CFGOBJ===null || typeof CFGOBJ!=='object' || Array.isArray(CFGOBJ)){
    CFGRAW=true; applyCfgView();
    if(!CFGPARSE_OK) $('drawerMsg').textContent='(not valid JSON — editing raw) '+(d.path||'');
  }else{ CFGRAW=false; applyCfgView(); renderForm(); }
}
function toggleCfgView(){
  if(!CFGRAW){ if(CFGOBJ!==null) $('cfgBox').value=JSON.stringify(CFGOBJ,null,2); }
  else{ try{ CFGOBJ=JSON.parse($('cfgBox').value||'{}'); CFGPARSE_OK=true; }
        catch(e){ $('drawerMsg').textContent='✗ raw JSON invalid, fix before switching'; return; } }
  CFGRAW=!CFGRAW; applyCfgView(); if(!CFGRAW) renderForm();
}
function applyCfgView(){
  $('cfgForm').style.display=CFGRAW?'none':'';
  $('cfgBox').style.display=CFGRAW?'':'none';
  $('cfgFilter').style.display=CFGRAW?'none':'';
  $('cfgViewBtn').textContent=CFGRAW?'Form view':'Raw JSON';
}
// dotted-path helpers (path is an array of keys)
function dget(obj,path){ let o=obj; for(const k of path){ if(o==null||typeof o!=='object')return undefined; o=o[k]; } return o; }
function dset(obj,path,val){ let o=obj; for(let i=0;i<path.length-1;i++){ if(typeof o[path[i]]!=='object'||o[path[i]]===null)o[path[i]]={}; o=o[path[i]]; } o[path[path.length-1]]=val; }
function dhas(obj,path){ return dget(obj,path)!==undefined; }
const isScalar=v=> v===null||['string','number','boolean'].includes(typeof v);

function renderForm(){
  if(CFGRAW||CFGOBJ===null) return;
  const filt=($('cfgFilter').value||'').trim().toLowerCase();
  const root=$('cfgForm'); root.innerHTML='';
  const usedModulePaths=new Set();
  let shown=0;
  // 1) schema-driven categories
  for(const [cat,mods] of Object.entries(SCHEMA)){
    const modCards=[];
    for(const [modKey,fields] of Object.entries(mods)){
      const modPath=modKey.split('.');
      usedModulePaths.add(modKey);
      const label=fields._label||modKey;
      const fieldEntries=Object.entries(fields).filter(([k])=>!k.startsWith('_'));
      // filter: match category, module label, or any field key
      const fieldMatches=fieldEntries.filter(([fk])=> !filt || cat.toLowerCase().includes(filt) || label.toLowerCase().includes(filt) || fk.toLowerCase().includes(filt));
      if(!fieldMatches.length) continue;
      const card=buildModuleCard(label,modPath,fieldMatches);
      modCards.push(card); shown+=fieldMatches.length;
    }
    if(!modCards.length) continue;
    const grp=document.createElement('div'); grp.className='cfggroup';
    const gh=document.createElement('div'); gh.className='gh'; gh.textContent=cat;
    grp.appendChild(gh);
    modCards.forEach(c=>grp.appendChild(c));
    root.appendChild(grp);
  }
  // 2) leftover keys present in file but not in schema -> "Other (from file)"
  const leftovers=collectLeftovers(CFGOBJ, usedModulePaths, filt);
  if(leftovers.length){
    const grp=document.createElement('div'); grp.className='cfggroup';
    const gh=document.createElement('div'); gh.className='gh'; gh.textContent='Other (from your config)';
    grp.appendChild(gh);
    const card=document.createElement('div'); card.className='modcard open';
    const body=document.createElement('div'); body.className='mbody';
    leftovers.forEach(({label,path,val})=> body.appendChild(buildAutoRow(label,path,val)) );
    card.appendChild(body); grp.appendChild(card); root.appendChild(grp);
    shown+=leftovers.length;
  }
  if(!shown) root.innerHTML='<div class="hint">No settings match "'+esc(filt)+'".</div>';
}

function buildModuleCard(label,modPath,fieldEntries){
  const card=document.createElement('div'); card.className='modcard';
  // is there an 'enabled' field? show its toggle in the header
  const enEntry=fieldEntries.find(([k])=>k==='enabled');
  const head=document.createElement('div'); head.className='mhd';
  const caret=document.createElement('span'); caret.className='caret'; caret.textContent='▶';
  const title=document.createElement('span'); title.className='mtitle'; title.textContent=label;
  head.appendChild(caret); head.appendChild(title);
  if(enEntry){
    const ep=modPath.concat('enabled');
    const on=!!dget(CFGOBJ,ep);
    const t=document.createElement('div'); t.className='tgl'+(on?' on':''); t.title='enabled';
    t.onclick=(e)=>{ e.stopPropagation(); const nv=!t.classList.contains('on'); t.classList.toggle('on',nv); dset(CFGOBJ,ep,nv); card.classList.toggle('active',nv); };
    head.appendChild(t);
    if(on)card.classList.add('active');
  }
  const body=document.createElement('div'); body.className='mbody';
  head.onclick=()=>card.classList.toggle('open');
  for(const [fk,spec] of fieldEntries){
    if(fk==='enabled') continue; // shown in header
    body.appendChild(buildField(modPath.concat(fk.split('.')), fk, spec));
  }
  card.appendChild(head); card.appendChild(body);
  return card;
}

function buildField(path,key,spec){
  const row=document.createElement('div'); row.className='frow';
  const lbl=document.createElement('div'); lbl.className='flabel';
  const nm=key.split('.').pop();
  lbl.innerHTML=esc(nm)+(spec.unit?` <span class="unit">${esc(spec.unit)}</span>`:'');
  row.appendChild(lbl);
  const cur=dget(CFGOBJ,path);
  const ctrl=document.createElement('div'); ctrl.className='fctrl';

  if(spec.type==='bool'){
    const on=cur===true;
    const t=document.createElement('div'); t.className='tgl'+(on?' on':'');
    t.onclick=()=>{ const nv=!t.classList.contains('on'); t.classList.toggle('on',nv); dset(CFGOBJ,path,nv); };
    ctrl.appendChild(t);
  } else if(spec.type==='enum'){
    const sel=document.createElement('select');
    spec.options.forEach(o=>{ const op=document.createElement('option'); op.value=o; op.textContent=o; if(cur===o)op.selected=true; sel.appendChild(op); });
    if(cur===undefined){ const op=document.createElement('option'); op.value=''; op.textContent='(default)'; op.selected=true; sel.insertBefore(op,sel.firstChild); }
    sel.onchange=()=>dset(CFGOBJ,path,sel.value);
    ctrl.appendChild(sel);
  } else if(spec.type==='int'||spec.type==='float'){
    const hasRange = spec.min!==undefined && spec.max!==undefined;
    const def = cur!==undefined?cur:(spec.min!==undefined?spec.min:0);
    if(hasRange){
      const sl=document.createElement('input'); sl.type='range'; sl.min=spec.min; sl.max=spec.max;
      sl.step=spec.step||(spec.type==='float'?0.1:1); sl.value=def; sl.className='slider';
      const num=document.createElement('input'); num.type='number'; num.value=def; num.className='snum';
      num.min=spec.min; num.max=spec.max; num.step=sl.step;
      const commit=v=>{ let n=spec.type==='float'?parseFloat(v):parseInt(v,10); if(Number.isNaN(n))return; n=Math.max(spec.min,Math.min(spec.max,n)); sl.value=n; num.value=n; dset(CFGOBJ,path,n); };
      sl.oninput=()=>commit(sl.value); num.oninput=()=>commit(num.value);
      ctrl.appendChild(sl); ctrl.appendChild(num);
    } else {
      const num=document.createElement('input'); num.type='number'; num.value=def; num.className='snum wide'; num.step=spec.step||(spec.type==='float'?0.1:1);
      num.oninput=()=>{ const n=spec.type==='float'?parseFloat(num.value):parseInt(num.value,10); if(!Number.isNaN(n))dset(CFGOBJ,path,n); };
      ctrl.appendChild(num);
    }
  } else if(spec.type==='list'){
    ctrl.appendChild(buildList(path, Array.isArray(cur)?cur:[]));
  } else { // string
    const inp=document.createElement('input'); inp.type=spec.secret?'password':'text';
    inp.value=cur!==undefined&&cur!==null?cur:''; inp.placeholder=spec.secret?'••••••':'';
    inp.oninput=()=>dset(CFGOBJ,path,inp.value);
    ctrl.appendChild(inp);
  }
  row.appendChild(ctrl);
  return row;
}

// auto-typed row for leftover/file-only values
function buildAutoRow(label,path,val){
  const spec = typeof val==='boolean'?{type:'bool'}
    : typeof val==='number'?{type:(Number.isInteger(val)?'int':'float')}
    : (Array.isArray(val)&&val.every(isScalar))?{type:'list'}
    : {type:'string'};
  if(val&&typeof val==='object'&&!Array.isArray(val)){
    // nested object leftover -> recurse into a mini card
    const card=document.createElement('div'); card.className='modcard';
    const head=document.createElement('div'); head.className='mhd';
    head.innerHTML='<span class="caret">▶</span><span class="mtitle">'+esc(label)+'</span>';
    const body=document.createElement('div'); body.className='mbody';
    head.onclick=()=>card.classList.toggle('open');
    for(const k of Object.keys(val)) body.appendChild(buildAutoRow(k, path.concat(k), val[k]));
    card.appendChild(head); card.appendChild(body); return card;
  }
  return buildField(path, label, spec);
}
function collectLeftovers(obj, usedModulePaths, filt, base){
  base=base||[]; const out=[];
  for(const k of Object.keys(obj)){
    const full=base.concat(k); const dotted=full.join('.');
    // skip if this exact path (or a parent) is covered by schema modules
    let covered=false;
    for(const mp of usedModulePaths){ if(mp===dotted||mp.startsWith(dotted+'.')||dotted.startsWith(mp+'.')||dotted===mp){ covered=true; break; } }
    if(covered) continue;
    if(filt && !dotted.toLowerCase().includes(filt)) continue;
    out.push({label:dotted, path:full, val:obj[k]});
  }
  return out;
}
function buildList(path,arr){
  const wrap=document.createElement('div'); wrap.className='arrlist';
  function draw(){
    wrap.innerHTML='';
    let cur=dget(CFGOBJ,path); if(!Array.isArray(cur)){ cur=[]; dset(CFGOBJ,path,cur); }
    cur.forEach((item,idx)=>{
      const ai=document.createElement('div'); ai.className='ai';
      const inp=document.createElement('input'); inp.type='text'; inp.value=item;
      inp.oninput=()=>{ const c=dget(CFGOBJ,path); const n=Number(inp.value);
        c[idx]=(typeof item==='number'&&inp.value!==''&&!Number.isNaN(n))?n:inp.value; };
      const rm=document.createElement('button'); rm.className='danger'; rm.textContent='✕';
      rm.onclick=()=>{ const c=dget(CFGOBJ,path); c.splice(idx,1); draw(); };
      ai.appendChild(inp); ai.appendChild(rm); wrap.appendChild(ai);
    });
    const add=document.createElement('button'); add.className='add'; add.textContent='+ add';
    add.onclick=()=>{ const c=dget(CFGOBJ,path); c.push(''); draw(); };
    wrap.appendChild(add);
  }
  draw(); return wrap;
}
async function saveCfg(){
  $('drawerMsg').textContent='saving…';
  let payload;
  if(CFGRAW){ payload=$('cfgBox').value; }
  else{ payload=JSON.stringify(CFGOBJ,null,2); }
  const d=await api(`/api/instances/${encodeURIComponent(CUR)}/config`,'POST',{config:payload});
  $('drawerMsg').textContent=d.error?('✗ '+d.error):('✓ saved '+d.path);
  if(!d.error){ $('cfgBox').value=payload; }
  return !d.error;
}
async function saveAndRestart(){
  const ok=await saveCfg();
  if(!ok) return;  // don't restart if the save failed
  $('drawerMsg').textContent='✓ saved — restarting…';
  const r=await api(`/api/instances/${encodeURIComponent(CUR)}/restart`,'POST');
  if(r.error){ $('drawerMsg').textContent='saved, but restart failed: '+r.error; }
  else{ $('drawerMsg').textContent='✓ saved & restarted ('+r.status+')'; }
  refresh();           // update card status in the grid
  setTimeout(loadLogs,600);  // surface fresh boot output if on Console tab
}

function renderLimits(){
  const lim=(INSTMAP[CUR]&&INSTMAP[CUR].limits)||{};
  $('lim_mem').value=lim.memory||'';
  $('lim_cpu').value=lim.cpu||'';
  const supported=!HOST||HOST.cgroup_limits!==false;
  $('limCap').innerHTML=supported
    ? 'Leave a field blank for no cap on that resource.'
    : '⚠ This host can\'t enforce limits yet (needs <code>loginctl enable-linger</code> / systemd user scopes — the installer sets this up). Caps are saved but won\'t apply.';
  $('limMsg').textContent='';
}
async function saveLimits(restart){
  const memory=$('lim_mem').value.trim(), cpu=$('lim_cpu').value.trim();
  $('limMsg').style.color='var(--dim)'; $('limMsg').textContent='saving…';
  const d=await api(`/api/instances/${encodeURIComponent(CUR)}/limits`,'POST',{memory,cpu});
  if(d.error){ $('limMsg').style.color='var(--crash)'; $('limMsg').textContent='✗ '+d.error; return; }
  let extra='';
  if(restart){
    const r=await api(`/api/instances/${encodeURIComponent(CUR)}/restart`,'POST');
    extra=r.error?(' — restart failed: '+r.error):(' — restarted ('+r.status+')');
  }
  $('limMsg').textContent='✓ saved'+extra;
  refresh();
}

async function del(name,status){
  let force=false;
  if(status==='running'||status==='crashed'){
    if(!confirm(`"${name}" is ${status}. Stop it and delete from the manager?\n(Files on disk are NOT removed.)`))return;
    force=true;
  }else{
    if(!confirm(`Delete "${name}" from the manager?\n(Files on disk are NOT removed.)`))return;
  }
  const d=await api(`/api/instances/${encodeURIComponent(name)}/delete`,'POST',{force});
  if(d.error)alert('Delete failed: '+d.error);
  refresh();
}

let SETTINGS=null, sysTimer=null;

function setVar(k,v){document.documentElement.style.setProperty(k,v);}
function applyTheme(s){
  const presets=s.presets||{};
  const p=presets[s.theme.preset]||presets.midnight;
  if(!p)return;
  setVar('--bg',p.bg); setVar('--panel',p.panel);
  // derive a slightly darker panel-2
  setVar('--panel-2',p.panel);
  const acc=(s.theme.accent&&s.theme.accent.trim())?s.theme.accent.trim():p.accent;
  setVar('--acc',acc); setVar('--run',acc);
  // light themes need darker text
  const light=p.bg && parseInt(p.bg.slice(1,3),16)>140;
  setVar('--txt', light?'#1a2026':'#dfe7ee');
  setVar('--dim', light?'#5a6b78':'#7b8a98');
  // custom background image (with a readability overlay tinted to the theme bg)
  const img=((s.theme.bg_image)||'').trim().replace(/["\\]/g,'');
  const b=document.body;
  if(img){
    let dim=(s.theme.bg_dim==null)?0.6:parseFloat(s.theme.bg_dim); if(isNaN(dim))dim=0.6;
    const ov=light?`rgba(244,241,234,${dim})`:`rgba(8,11,14,${dim})`;
    b.style.backgroundImage=`linear-gradient(${ov},${ov}), url("${img}")`;
    b.style.backgroundSize='cover'; b.style.backgroundPosition='center'; b.style.backgroundAttachment='fixed';
  } else { b.style.backgroundImage=''; }
  // density scales rem-based sizing via the root font-size
  document.documentElement.style.fontSize =
    (s.theme.density==='compact')?'14px':((s.theme.density==='spacious')?'17.5px':'');
  // font pairing → --sans / --mono (lazily fetch the Google Fonts stylesheet)
  const fonts=s.fonts||{};
  const fkey=(s.theme.font&&fonts[s.theme.font])?s.theme.font:'aquarius';
  const f=fonts[fkey];
  if(f){
    if(f.q){
      const id='gf-'+fkey;
      if(!document.getElementById(id)){
        const l=document.createElement('link'); l.id=id; l.rel='stylesheet';
        l.href='https://fonts.googleapis.com/css2?'+f.q+'&display=swap';
        document.head.appendChild(l);
      }
    }
    setVar('--sans',f.sans); setVar('--mono',f.mono);
  }
}
async function loadSettings(){
  SETTINGS=await api('/api/settings');
  applyTheme(SETTINGS);
  renderSidebar();
  return SETTINGS;
}

/* ===================== sidebar + page views (v1.5) ===================== */
const ABMVER='__ABM_VERSION__';
let CURVIEW='dashboard', TELBOT=null, telTimer=null, telSeries={};
let SELSIDEBAR='full', SELSIDE='left';

function navModel(){
  const list=Object.values((typeof INSTMAP!=='undefined'&&INSTMAP)||{});
  const cr=list.filter(i=>i.status==='crashed').length;
  return [
    {g:'Manage'},
    {ic:'▦',lbl:'Dashboard',view:'dashboard'},
    {ic:'❖',lbl:'Fleet',view:'fleet'},
    {ic:'⚡',lbl:'Activity',view:'activity',pip:cr||null,pipwarn:true},
    {ic:'⏱',lbl:'Automation',view:'automation'},
    {g:'Infrastructure'},
    {ic:'🖥',lbl:'Boxes',act:'openBoxes()'},
    {ic:'🌐',lbl:'Proxies',act:'openProxies()'},
    {ic:'➕',lbl:'Add Bot',act:'openDeploy()'},
    {ic:'📁',lbl:'Files',act:'openFiles()'},
    {ic:'👥',lbl:'Share',act:'openShares()'},
    {ic:'👤',lbl:'Users',act:'openUsers()'},
    {g:'System'},
    {ic:'🔗',lbl:'Connect',act:'openConnection()'},
    {ic:'⟲',lbl:'Scan',act:'openScan()'},
    {ic:'⚙',lbl:'Settings',act:'openSettings()'},
  ];
}
function navHtml(){
  return '<div class="nav">'+navModel().map(n=>{
    if(n.g) return `<div class="navg">${n.g}</div>`;
    const act=n.view?`showView('${n.view}')`:n.act;
    const active=(n.view&&n.view===CURVIEW)?' active':'';
    const pip=n.pip?`<span class="pip${n.pipwarn?' warn':''}">${n.pip}</span>`:'';
    return `<a class="${active.trim()}" data-view="${n.view||''}" onclick="${act}"><span class="ic">${n.ic}</span><span class="lbl">${esc(n.lbl)}</span>${pip}</a>`;
  }).join('')+'</div>';
}
function sbBrand(){ return '<div class="sbrand"><span class="dot"></span><span class="txt">Aquarius<small>BOT MANAGER v'+esc(ABMVER)+'</small></span></div>'; }
// Name of the box whose UI you're currently viewing: a proxied node page shows that
// node's label, the controller's own page shows its configured box name ("Controller"
// by default). Both are user-settable in the Boxes modal.
function curBoxLabel(){
  if(typeof window.ABM_CURRENT_NODE!=='undefined'&&window.ABM_CURRENT_NODE)
    return window.ABM_CURRENT_LABEL||window.ABM_CURRENT_NODE;
  return (SETTINGS&&SETTINGS.box_name)||'Controller';
}
function sbBox(){ return '<div class="boxchip" onclick="openBoxes()"><span class="bdot"></span><span class="txt">★ '+esc(curBoxLabel())+'</span><span class="car">▾</span></div>'; }
function sbFoot(){ return '<div class="sfoot"><div class="nav"><a onclick="location.href=\'/logout\'"><span class="ic">⏻</span><span class="lbl">Log out</span></a></div></div>'; }

function renderSidebar(ui){
  ui=ui||(SETTINGS&&SETTINGS.ui)||{sidebar:'full',sidebar_side:'left'};
  const style=ui.sidebar||'full', side=ui.sidebar_side||'left';
  const app=$('app'), sb=$('sidebar'), hdr=$('classicHeader'), top=$('slimTop');
  if(!app||!sb)return;
  app.classList.toggle('right', side==='right');
  if(style==='off'){
    app.classList.remove('has-side');
    sb.style.display='none'; sb.className='side'; sb.innerHTML='';
    if(hdr)hdr.style.display=''; if(top)top.style.display='none';
    return;
  }
  app.classList.add('has-side');
  if(hdr)hdr.style.display='none'; if(top)top.style.display='';
  sb.className='side'+(style==='rail'?' rail':'');
  let h=sbBrand();
  if(style!=='cmd') h+=sbBox();
  if(style==='cmd'){
    h+='<div class="spalette" onclick="openPalette()">🔍 Search bots… <span class="kbd">⌘K</span></div>';
    h+='<div class="salert" id="sbAlert" onclick="showView(\'activity\')">checking…</div>';
  }
  h+=navHtml();
  if(style==='cmd'){ h+='<div class="roster" id="sbRoster"></div>'; }
  else{
    if(style==='rail') h+='<div class="railtoggle" title="Expand" onclick="renderSidebar({sidebar:\'full\',sidebar_side:(SETTINGS.ui&&SETTINGS.ui.sidebar_side)||\'left\'})">»</div>';
    h+='<div class="sgrow"></div>';
    if(style==='full') h+='<div class="svitals" id="sbVitals"></div>';
  }
  h+=sbFoot();
  sb.style.display='flex'; sb.innerHTML=h;
  renderSlimTop();
  updateSidebarLive();
}
function renderSlimTop(){
  const top=$('slimTop'); if(!top||top.style.display==='none')return;
  const T={dashboard:['Dashboard','your bots on this box'],fleet:['Fleet','multi-box overview'],
    activity:['Activity & alerts','live snapshot'],telemetry:['Telemetry','bot detail'],
    automation:['Automation','scheduled actions & auto-recovery']};
  const t=T[CURVIEW]||['Dashboard',''];
  top.innerHTML=`<div><div class="pt">${esc(t[0])}</div><div class="sub">${esc(t[1])}</div></div><div class="sp"></div>`
    +'<button class="go" onclick="bulk(\'start\')">▶ Start all</button>'
    +'<button class="warn" onclick="bulk(\'restart\')">⟳ Restart all</button>'
    +'<button class="danger" onclick="bulk(\'stop\')">■ Stop all</button>'
    +'<button class="go" onclick="openDeploy()">➕ Add Bot</button>';
}
function refreshNavActive(){
  const sb=$('sidebar'); if(!sb)return;
  sb.querySelectorAll('.nav a[data-view]').forEach(a=>{
    const v=a.getAttribute('data-view'); a.classList.toggle('active', !!v&&v===CURVIEW);
  });
}
function updateSidebarLive(){
  const list=Object.values((typeof INSTMAP!=='undefined'&&INSTMAP)||{});
  const cr=list.filter(i=>i.status==='crashed').length;
  const run=list.filter(i=>i.status==='running').length;
  const v=$('sbVitals');
  if(v&&typeof HOST!=='undefined'&&HOST){
    const t=thr(), cores=HOST.cpus||1, load0=HOST.load?HOST.load[0]:0;
    const cpu=Math.min(100,Math.round(100*load0/cores));
    const mem=HOST.mem_total?Math.round(100*HOST.mem_used/HOST.mem_total):0;
    const disk=HOST.disk_total?Math.round(100*HOST.disk_used/HOST.disk_total):0;
    const row=(k,p,lim)=>`<div class="svit ${p>=lim?'warn':''}"><div class="k"><span>${k}</span><span>${p}%</span></div><div class="b"><i style="width:${p}%"></i></div></div>`;
    v.innerHTML=row('CPU',cpu,t.cpu_pct)+row('MEM',mem,t.mem_pct)+row('DISK',disk,t.disk_pct);
  }
  const r=$('sbRoster');
  if(r){
    r.innerHTML=`<div class="rhd"><span>Fleet • ${run}/${list.length} running</span><span>cpu</span></div>`+
      list.map(i=>{
        const cls=i.status==='running'?'run':(i.status==='crashed'?'crash':'');
        const rc=i.status==='running'?(((i.stats&&i.stats.cpu_pct!=null)?i.stats.cpu_pct:'·')+'%'):i.status;
        return `<div class="rrow ${cls}${i.name===TELBOT?' sel':''}" onclick="showView('telemetry','${jsq(i.name)}')"><span class="rd"></span><span class="rn">${esc(i.name)}</span><span class="rc">${esc(String(rc))}</span></div>`;
      }).join('');
  }
  const a=$('sbAlert');
  if(a){
    a.textContent=cr?('⚠ '+cr+' crashed — see Activity'):'✓ all systems normal';
    a.style.color=cr?'var(--warn)':'var(--dim)';
    a.style.borderColor=cr?'#5a3b1f':'var(--line)';
  }
  const av=document.querySelector('#sidebar .nav a[data-view="activity"]');
  if(av){ let pip=av.querySelector('.pip');
    if(cr){ if(!pip){ pip=document.createElement('span'); pip.className='pip warn'; av.appendChild(pip); } pip.textContent=cr; pip.style.display=''; }
    else if(pip){ pip.style.display='none'; } }
}

function showView(name,arg){
  CURVIEW=name;
  ['Dashboard','Fleet','Activity','Telemetry','Automation'].forEach(v=>{
    const el=$('view'+v); if(el) el.style.display=(v.toLowerCase()===name)?'':'none';
  });
  if(telTimer&&name!=='telemetry'){clearInterval(telTimer);telTimer=null;}
  if(name==='dashboard'){ refresh(); }
  else if(name==='fleet'){ loadFleetView(); }
  else if(name==='activity'){ loadActivityView(); }
  else if(name==='telemetry'){ openTelemetry(arg); }
  else if(name==='automation'){ renderAutomationView(); }
  renderSlimTop(); refreshNavActive();
  window.scrollTo(0,0);
}

async function loadFleetView(){
  const el=$('viewFleet');
  el.innerHTML='<div class="pagehd"><h1>Fleet</h1><span class="sub">loading…</span></div>';
  let d; try{ d=await api('/api/fleet/status'); }catch(e){ el.innerHTML='<div class="pagehd"><h1>Fleet</h1></div><div class="panel hint">failed to load fleet status</div>'; return; }
  const rows=(d&&d.fleet)||[];
  const boxes=rows.length, bots=rows.reduce((a,r)=>a+(r.bots||0),0), running=rows.reduce((a,r)=>a+(r.running||0),0);
  const offline=rows.filter(r=>!r.reachable).length;
  const g=(k,val,p)=>`<div class="gauge ${p>=85?'warn':''}"><div class="k"><span>${k}</span><span>${p}%</span></div><div class="v">${val}</div><div class="b"><i style="width:${p}%"></i></div></div>`;
  const cards=rows.map(r=>{
    const host=r.host||{};
    const cores=host.cpus||1, load0=host.load?host.load[0]:0;
    const cpu=Math.min(100,Math.round(100*load0/cores));
    const mem=host.mem_total?Math.round(100*host.mem_used/host.mem_total):0;
    const off=!r.reachable;
    const name=(r.controller?'★ ':'')+esc(r.label||r.name);
    const sub=r.controller?'controller':esc((r.ssh_user||'')+'@'+(r.ssh_host||''));
    const stat=off?('<span style="color:var(--crash)">⚠ offline'+(r.error?(' — '+esc(r.error)):'')+'</span>')
      :`${r.running||0}/${r.bots||0} bots running`;
    const gauges=(!off&&host.mem_total)?(g('CPU',(host.load?host.load[0].toFixed(2):'?')+' load',cpu)+'<div style="height:.5rem"></div>'+g('MEM',mem+'% used',mem)):'';
    const actions=r.controller?'<button class="go" onclick="showView(\'dashboard\')">Open dashboard</button>':
      (off?`<button class="go" onclick="reconnectBox('${jsq(r.name)}',this)">↻ Reconnect</button> <button onclick="openBoxes()">Manage</button>`
          :`<button class="go" onclick="openNode('${jsq(r.name)}')">Open</button> <button onclick="openBoxes()">Manage</button>`);
    return `<div class="panel${off?' boxoff':''}"><h3>${name}</h3><div class="path" style="margin-top:-.5rem">${sub}</div>
      <div style="font-weight:800;font-size:1.1rem;margin:.5rem 0 .7rem">${stat}</div>${gauges}
      <div class="row" style="margin-top:.8rem">${actions}</div></div>`;
  }).join('');
  // every bot on every reachable box, tagged with its host box
  const allbots=[];
  rows.forEach(r=>{
    const blabel=r.controller?(r.label||'Controller'):(r.label||r.name);
    (r.instances||[]).forEach(i=>allbots.push(Object.assign({_box:blabel,_boxname:r.name,_controller:r.controller},i)));
  });
  const trows=allbots.map(b=>{
    const col=b.status==='running'?'var(--run)':(b.status==='crashed'?'var(--crash)':'var(--stop)');
    const cpu=(b.stats&&b.stats.cpu_pct!=null)?b.stats.cpu_pct+'%':'—';
    const mem=(b.stats&&b.stats.rss)?fmtBytes(b.stats.rss):'—';
    const onclk=b._controller?`showView('telemetry','${jsq(b.name)}')`:`openNode('${jsq(b._boxname)}')`;
    return `<tr><td style="font-weight:700;cursor:pointer" onclick="${onclk}">${esc(b.name)}</td>
      <td style="font-family:var(--mono);font-size:.74rem;color:var(--dim)">${b._controller?'★ ':''}${esc(b._box)}</td>
      <td><span style="color:${col};font-family:var(--mono);font-size:.72rem">● ${esc((b.status||'').toUpperCase())}</span></td>
      <td>${cpu}</td><td>${mem}</td></tr>`;
  }).join('');
  const offNote=offline?`<div class="hint" style="margin-top:.6rem">${offline} box${offline===1?'':'es'} offline — their bots can't be listed until you reconnect (see the darkened card${offline===1?'':'s'} above).</div>`:'';
  el.innerHTML=`<div class="pagehd"><h1>Fleet</h1><span class="sub">${boxes} box${boxes===1?'':'es'} · ${bots} bots · ${running} running</span></div>
    <div class="sumstrip">
      <div class="s"><div class="k">Boxes</div><div class="v">${boxes}</div></div>
      <div class="s good"><div class="k">Bots running</div><div class="v">${running}</div></div>
      <div class="s"><div class="k">Total bots</div><div class="v">${bots}</div></div>
      <div class="s ${offline?'bad':''}"><div class="k">Offline boxes</div><div class="v">${offline}</div></div>
    </div>
    <div class="cols3">${cards}</div>
    <div class="panel" style="margin-top:1rem"><h3>All bots<span class="sub">every bot on every box · click a controller bot for telemetry, a node bot to open its box</span></h3>
      <table class="tbl"><thead><tr><th>Bot</th><th>Box</th><th>Status</th><th>CPU</th><th>MEM</th></tr></thead>
      <tbody>${trows||'<tr><td colspan="5" class="hint">no instances</td></tr>'}</tbody></table>${offNote}</div>`;
}
async function reconnectBox(name,btn){
  const o=btn.innerHTML; btn.disabled=true; btn.innerHTML='<span class="spin"></span> reconnecting…';
  let d; try{ d=await api('/api/nodes/reconnect','POST',{name}); }catch(e){ d={error:String(e)}; }
  btn.disabled=false; btn.innerHTML=o;
  if(d&&d.error&&!('reachable' in d)){ alert('✗ '+d.error); return; }
  if(d&&!d.reachable){ alert('Still offline'+(d.error?(': '+d.error):'.')); }
  loadFleetView();
}

async function loadActivityView(){
  const el=$('viewActivity');
  const list=Object.values((typeof INSTMAP!=='undefined'&&INSTMAP)||{});
  const t=thr(); let evs=[];
  list.filter(i=>i.status==='crashed').forEach(i=>evs.push({sev:'crit',bot:i.name,m:'crashed — needs a restart'}));
  list.filter(i=>i.status==='running'&&i.stats).forEach(i=>{
    const cores=(typeof HOST!=='undefined'&&HOST&&HOST.cpus)||1, memTotal=(typeof HOST!=='undefined'&&HOST&&HOST.mem_total)||0;
    if(i.stats.cpu_pct!=null && i.stats.cpu_pct>=t.cpu_pct*cores) evs.push({sev:'warn',bot:i.name,m:'high CPU — '+i.stats.cpu_pct+'%'});
    if(memTotal && i.stats.rss && (100*i.stats.rss/memTotal)>=t.mem_pct) evs.push({sev:'warn',bot:i.name,m:'high memory — '+fmtBytes(i.stats.rss)});
  });
  let ph=null; try{ ph=await api('/api/proxies/health','POST',{}); }catch(e){}
  const phrows=(ph&&(ph.errored||ph.results||ph.instances))||[];
  (Array.isArray(phrows)?phrows:[]).forEach(r=>{
    const nm=r.name||r.instance||r; if(nm) evs.push({sev:'warn',bot:nm,m:'proxy issue detected — '+(r.sample||r.reason||'check the console')});
  });
  const run=list.filter(i=>i.status==='running');
  run.forEach(i=>evs.push({sev:'ok',bot:i.name,m:'running'+((i.stats&&i.stats.cpu_pct!=null)?(' · '+i.stats.cpu_pct+'% cpu'):'')}));
  const crashed=list.filter(i=>i.status==='crashed').length;
  const warns=evs.filter(e=>e.sev==='warn').length;
  const ord={crit:0,warn:1,info:2,ok:3};
  evs.sort((a,b)=>ord[a.sev]-ord[b.sev]);
  const feed=evs.map(e=>`<div class="ev ${e.sev}"><div class="when">now</div><div class="edot"></div><div class="eb"><div class="m"><span class="tag">${esc(e.bot)}</span>${esc(e.m)}</div></div></div>`).join('')||'<div class="hint" style="padding:1rem">No activity.</div>';
  el.innerHTML=`<div class="pagehd"><h1>Activity &amp; alerts</h1><span class="sub">live snapshot of this box</span></div>
    <div class="sumstrip">
      <div class="s ${crashed?'bad':''}"><div class="k">Crashed</div><div class="v">${crashed}</div></div>
      <div class="s ${warns?'warnv':''}"><div class="k">Warnings</div><div class="v">${warns}</div></div>
      <div class="s good"><div class="k">Running</div><div class="v">${run.length}</div></div>
      <div class="s"><div class="k">Total bots</div><div class="v">${list.length}</div></div>
    </div>
    <div class="filters"><span class="hint">Derived live from current status, resource thresholds and proxy-console scan. A persistent event log is coming in v1.6.</span></div>
    <div class="panel"><div class="feed">${feed}</div></div>`;
}

function telTilesHtml(i){
  const st=i.stats||{};
  const memTotal=(typeof HOST!=='undefined'&&HOST&&HOST.mem_total)||0;
  const memPct=(memTotal&&st.rss)?Math.round(100*st.rss/memTotal):0;
  const T=[
    ['STATUS', i.status==='running'?'<span style="color:var(--run)">RUNNING</span>':(i.status==='crashed'?'<span style="color:var(--crash)">CRASHED</span>':'STOPPED')],
    ['CPU', (st.cpu_pct!=null?st.cpu_pct:'—')+' <small>%</small>'],
    ['MEMORY', (st.rss?fmtBytes(st.rss):'—')],
    ['MEM SHARE', memPct+' <small>% host</small>'],
    ['PROCS', (st.procs!=null?st.procs:'—')],
    ['PID', (st.pid!=null?st.pid:'—')],
  ];
  return T.map(t=>`<div class="tile"><div class="k">${t[0]}</div><div class="v">${t[1]}</div></div>`).join('');
}
function telSpark(vals,color){
  const w=560,h=120,n=vals.length;
  if(!n) return '';
  const mn=Math.min.apply(null,vals), mx=Math.max.apply(null,vals), rng=(mx-mn)||1;
  const pts=vals.map((v,idx)=>[n>1?idx/(n-1)*w:w/2, h-10-(v-mn)/rng*(h-22)]);
  const d='M'+pts.map(p=>p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' L');
  const last=pts[pts.length-1];
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path d="${d} L${w} ${h} L0 ${h} Z" fill="${color}" opacity="0.12"/><path d="${d}" fill="none" stroke="${color}" stroke-width="2"/><circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="3.2" fill="${color}"/></svg>`;
}
function telDrawChart(){
  const c=$('telChart'); if(!c)return;
  const s=telSeries[TELBOT]||[];
  const acc=getComputedStyle(document.documentElement).getPropertyValue('--acc').trim()||'#3ddc97';
  c.innerHTML=s.length?telSpark(s,acc):'<div class="hint" style="padding:1.6rem 0">collecting samples…</div>';
}
async function telTailNow(){
  if(!TELBOT)return;
  let d; try{ d=await api(`/api/instances/${encodeURIComponent(TELBOT)}/logs?lines=200`);}catch(e){return;}
  const box=$('telLog'); if(!box)return;
  const txt=d.logs||'(not running)';
  if(txt===box.__last)return;
  const atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<60;
  box.__last=txt; box.textContent=txt;
  if(atBottom)box.scrollTop=box.scrollHeight;
}
function openTelemetry(name){
  const list=Object.values((typeof INSTMAP!=='undefined'&&INSTMAP)||{});
  if(!name) name=TELBOT||((list.find(i=>i.status==='running')||list[0]||{}).name);
  TELBOT=name;
  const el=$('viewTelemetry'), i=(typeof INSTMAP!=='undefined'&&INSTMAP)?INSTMAP[TELBOT]:null;
  if(!i){ el.innerHTML='<div class="pagehd"><h1>Telemetry</h1></div><div class="panel hint">No instance to show yet.</div>'; return; }
  const opts=list.map(x=>`<option value="${esc(x.name)}"${x.name===TELBOT?' selected':''}>${esc(x.name)}</option>`).join('');
  el.innerHTML=`<div class="pagehd"><h1>${esc(TELBOT)}</h1><span class="sub">${esc(i.dir||'')}</span><span class="sp" style="flex:1"></span>
      <select onchange="showView('telemetry',this.value)" style="font-family:var(--sans);font-size:.8rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:8px;padding:.35rem .5rem">${opts}</select></div>
    <div class="tiles" id="telTiles">${telTilesHtml(i)}</div>
    <div class="cols">
      <div class="panel"><h3>CPU<span class="sub">live · since opened</span></h3><div id="telChart"></div></div>
      <div class="panel"><h3>Actions</h3>
        <div class="row"><button class="go" onclick="act('${jsq(TELBOT)}','start',this)">▶ Start</button>
          <button class="warn" onclick="act('${jsq(TELBOT)}','restart',this)">⟳ Restart</button>
          <button class="danger" onclick="act('${jsq(TELBOT)}','stop',this)">■ Stop</button></div>
        <div style="margin-top:.8rem"><button onclick="openDrawer('${jsq(TELBOT)}')">⋯ Full console &amp; config</button></div>
        <div class="hint" style="margin-top:.8rem">Game telemetry (totems / food / flight speed) streams in the bot's console — open the full console for the live tail + scroll-pause.</div>
      </div>
    </div>
    <div class="panel" style="margin-top:1rem"><h3>Console<span class="sub">live tail</span></h3>
      <pre class="log" id="telLog" style="height:240px;overflow:auto">…</pre></div>`;
  telSeries[TELBOT]=telSeries[TELBOT]||[];
  if(i.stats&&i.stats.cpu_pct!=null) telSeries[TELBOT].push(i.stats.cpu_pct);
  telDrawChart(); telTailNow();
  if(telTimer)clearInterval(telTimer);
  telTimer=setInterval(telTick,4000);
}
function telTick(){
  if(CURVIEW!=='telemetry'){ if(telTimer){clearInterval(telTimer);telTimer=null;} return; }
  const i=(typeof INSTMAP!=='undefined'&&INSTMAP)?INSTMAP[TELBOT]:null; if(!i)return;
  if(i.stats&&i.stats.cpu_pct!=null){ (telSeries[TELBOT]=telSeries[TELBOT]||[]).push(i.stats.cpu_pct); if(telSeries[TELBOT].length>40)telSeries[TELBOT].shift(); }
  const tt=$('telTiles'); if(tt)tt.innerHTML=telTilesHtml(i);
  telDrawChart(); telTailNow();
}
let SCHEDJOBS=[], SCHEDHOOK='', SCHEDRT={}, SCHEDBOXES=[], SCHEDBOTS={};
async function renderAutomationView(){
  const el=$('viewAutomation');
  el.innerHTML='<div class="pagehd"><h1>Automation</h1><span class="sub">loading…</span></div>';
  let d; try{ d=await api('/api/schedules'); }catch(e){ el.innerHTML='<div class="pagehd"><h1>Automation</h1></div><div class="panel hint">failed to load</div>'; return; }
  SCHEDJOBS=(d.schedules&&d.schedules.jobs)||[]; SCHEDHOOK=(d.schedules&&d.schedules.notify_webhook)||''; SCHEDRT=d.runtime||{};
  SCHEDBOXES=[{v:'',l:curBoxLabel()}]; SCHEDBOTS={'':[]};
  // this box's bots (running or not) — authoritative local instance list
  try{ const il=await api('/api/instances'); SCHEDBOTS['']=((il&&il.instances)||[]).map(i=>i.name).filter(Boolean).sort(); }catch(e){}
  // connected boxes + their bots (best-effort; one fleet sweep)
  try{
    const fs=await api('/api/fleet/status'); let nodes=0;
    ((fs&&fs.fleet)||[]).forEach(r=>{
      if(r.controller)return; nodes++;
      SCHEDBOXES.push({v:r.name,l:r.name});
      SCHEDBOTS[r.name]=Array.isArray(r.bot_names)?r.bot_names.filter(Boolean).slice().sort():[];
    });
    if(nodes)SCHEDBOXES.push({v:'*',l:'All boxes'});
  }catch(e){}
  schedDraw();
}
// options for the target-bot dropdown given the selected box
function schedBotOpts(box,sel){
  const opts=[`<option value="all"${sel==='all'?' selected':''}>All bots</option>`];
  if(box==='*')return opts.join('');   // across all boxes only "all" is meaningful
  (SCHEDBOTS[box]||[]).forEach(nm=>opts.push(`<option value="${esc(nm)}"${nm===sel?' selected':''}>${esc(nm)}</option>`));
  return opts.join('');
}
// repopulate the target-bot dropdown when the box changes
function schedBoxSync(){
  const box=$('njBox')?$('njBox').value:'', sel=$('njTarget'); if(!sel)return;
  const cur=sel.value;
  sel.innerHTML=schedBotOpts(box,cur);
  if(![...sel.options].some(o=>o.value===cur))sel.value='all';
  sel.disabled=(box==='*');
}
function schedRel(ts){ const ms=ts*1000-Date.now(); if(ms<0)return'due'; const m=Math.round(ms/60000); if(m<60)return'in '+m+'m'; const h=Math.floor(m/60); return 'in '+h+'h '+(m%60)+'m'; }
function schedDraw(){
  const el=$('viewAutomation');
  const rows=SCHEDJOBS.length?SCHEDJOBS.map(j=>{
    const rt=SCHEDRT[j.id]||{};
    const when=j.trigger==='on_crash'?('on crash · up to '+(j.max_tries||3)+'×'):esc(j.when||'');
    const tgt=(j.box==='*'?'all boxes':(j.box?esc(j.box):curBoxLabel()))+' · '+esc(j.target||'all');
    const act=esc(j.action)+(j.action==='command'?(': '+esc(j.command||'')):'');
    const nxt=j.trigger==='on_crash'?'watching':(rt.next_run?schedRel(rt.next_run):'—');
    return `<tr>
      <td><div class="tgl ${j.enabled?'on':''}" onclick="schedToggle('${j.id}')"></div></td>
      <td style="font-weight:600">${esc(j.name||act)}<div class="hint" style="font-family:var(--mono);font-size:.6rem">${when}</div></td>
      <td style="font-family:var(--mono);font-size:.73rem;color:var(--dim)">${tgt}</td>
      <td style="font-family:var(--mono);font-size:.73rem">${act}</td>
      <td style="font-family:var(--mono);font-size:.72rem;color:var(--acc)">${nxt}</td>
      <td style="font-family:var(--mono);font-size:.66rem;color:var(--dim);max-width:230px;overflow:hidden;text-overflow:ellipsis">${rt.last_result?esc(rt.last_result):'—'}</td>
      <td style="white-space:nowrap"><button onclick="schedRun('${j.id}')">Run</button> <button class="danger" onclick="schedDelete('${j.id}')">✕</button></td></tr>`;
  }).join(''):'<tr><td colspan="7" class="hint">No jobs yet — add one below.</td></tr>';
  const boxopts=SCHEDBOXES.map(b=>`<option value="${esc(b.v)}">${esc(b.l)}</option>`).join('');
  el.innerHTML=`<div class="pagehd"><h1>Automation</h1><span class="sub">${SCHEDJOBS.length} job(s) · ${SCHEDJOBS.filter(j=>j.enabled).length} active · runner ticks ~30s</span></div>
    <div class="panel" style="margin-bottom:1rem"><h3>Scheduled jobs<span class="sub">restart / command / watchdog · this box or any connected box</span></h3>
      <table class="tbl"><thead><tr><th>On</th><th>Job</th><th>Target</th><th>Action</th><th>Next</th><th>Last result</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="cols" style="grid-template-columns:1.4fr 1fr">
      <div class="panel"><h3>New job</h3>
        <div class="frow"><div class="flabel">Trigger</div><div class="fctrl"><select id="njTrig" onchange="schedFormSync()"><option value="time">Time schedule</option><option value="on_crash">On crash (watchdog)</option></select></div></div>
        <div class="frow" id="njWhenRow"><div class="flabel">When <span class="unit">cron / every / daily</span></div><div class="fctrl"><input type="text" id="njWhen" placeholder="0 4 * * *   ·   every:2h   ·   daily:04:00"></div></div>
        <div class="frow" id="njTriesRow" style="display:none"><div class="flabel">Max restarts · cooldown s</div><div class="fctrl"><input type="text" id="njTries" value="3" class="snum"> <input type="text" id="njCool" value="60" class="snum"></div></div>
        <div class="frow"><div class="flabel">Box</div><div class="fctrl"><select id="njBox" onchange="schedBoxSync()">${boxopts}</select></div></div>
        <div class="frow"><div class="flabel">Target bot <span class="unit">running or not</span></div><div class="fctrl"><select id="njTarget">${schedBotOpts('','all')}</select></div></div>
        <div class="frow"><div class="flabel">Action</div><div class="fctrl"><select id="njAction" onchange="schedFormSync()"><option value="restart">Restart</option><option value="start">Start</option><option value="stop">Stop</option><option value="command">Send command</option></select></div></div>
        <div class="frow" id="njCmdRow" style="display:none"><div class="flabel">Command</div><div class="fctrl"><input type="text" id="njCmd" placeholder="fly resupplyspares"></div></div>
        <div class="frow"><div class="flabel">Name <span class="unit">optional</span></div><div class="fctrl"><input type="text" id="njName" placeholder="Nightly restart"></div></div>
        <div class="frow"><div class="flabel">Notify on Discord</div><div class="fctrl"><div class="tgl" id="njNotify" onclick="this.classList.toggle('on')"></div></div></div>
        <div class="mbar" style="margin-top:.6rem"><span class="msg" id="njMsg" style="color:var(--dim);flex:1"></span><button class="go" onclick="schedAdd()">+ Add job</button></div></div>
      <div class="panel"><h3>Discord notifications</h3>
        <div class="hint" style="margin-bottom:.5rem">Webhook pinged when a job runs/fails or the watchdog restarts a bot (only jobs with <b>Notify</b> on).</div>
        <input type="text" id="njHook" placeholder="https://discord.com/api/webhooks/…" value="${esc(SCHEDHOOK)}" style="width:100%;font-family:var(--mono);font-size:.76rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:8px;padding:.5rem .6rem">
        <div class="mbar" style="margin-top:.6rem"><span class="msg" id="hookMsg" style="color:var(--dim);flex:1"></span><button onclick="schedSaveHook()">Save webhook</button></div>
        <div class="hint" style="margin-top:.9rem">Schedule examples: <code>every:30m</code>, <code>daily:04:00</code>, cron <code>0 */6 * * *</code> (every 6h). For resupply, use action <b>Send command</b> with <code>fly resupplyspares</code>.</div></div>
    </div>`;
  schedFormSync(); schedBoxSync();
}
function schedFormSync(){
  if(!$('njTrig'))return;
  $('njWhenRow').style.display=($('njTrig').value==='time')?'':'none';
  $('njTriesRow').style.display=($('njTrig').value==='on_crash')?'':'none';
  $('njCmdRow').style.display=($('njAction').value==='command')?'':'none';
}
async function schedPersist(msgEl){
  const d=await api('/api/settings','POST',{schedules:{notify_webhook:SCHEDHOOK, jobs:SCHEDJOBS}});
  if(d.error){ if(msgEl){msgEl.style.color='var(--crash)';msgEl.textContent='✗ '+d.error;} return false; }
  if(d.settings&&d.settings.schedules){ SCHEDJOBS=d.settings.schedules.jobs||SCHEDJOBS; SCHEDHOOK=d.settings.schedules.notify_webhook||''; }
  return true;
}
async function schedAdd(){
  const t=$('njTrig').value, action=$('njAction').value;
  const job={trigger:t, box:$('njBox').value, target:($('njTarget').value.trim()||'all'),
    action:action, command:$('njCmd')?$('njCmd').value.trim():'', name:$('njName').value.trim(),
    notify:$('njNotify').classList.contains('on'), enabled:true};
  if(t==='time'){ job.when=$('njWhen').value.trim(); if(!job.when){ $('njMsg').style.color='var(--crash)'; $('njMsg').textContent='enter a schedule'; return; } }
  else { job.max_tries=parseInt($('njTries').value)||3; job.cooldown=parseInt($('njCool').value)||60; }
  if(action==='command'&&!job.command){ $('njMsg').style.color='var(--crash)'; $('njMsg').textContent='enter a command'; return; }
  SCHEDJOBS.push(job); $('njMsg').style.color='var(--dim)'; $('njMsg').textContent='saving…';
  if(await schedPersist($('njMsg'))) renderAutomationView(); else SCHEDJOBS.pop();
}
async function schedToggle(id){ const j=SCHEDJOBS.find(x=>x.id===id); if(!j)return; j.enabled=!j.enabled; if(await schedPersist())schedDraw(); }
async function schedDelete(id){ if(!confirm('Delete this job?'))return; SCHEDJOBS=SCHEDJOBS.filter(x=>x.id!==id); if(await schedPersist())renderAutomationView(); }
async function schedRun(id){ const d=await api('/api/schedules/run','POST',{id}); if(d.error)alert('Run failed: '+d.error); else setTimeout(renderAutomationView,500); }
async function schedSaveHook(){ SCHEDHOOK=$('njHook').value.trim(); $('hookMsg').textContent='saving…'; if(await schedPersist($('hookMsg'))){ $('hookMsg').style.color='var(--dim)'; $('hookMsg').textContent='✓ saved'; } }

/* settings → appearance: sidebar picker */
function pickSidebar(v){ SELSIDEBAR=v; document.querySelectorAll('#sidebarRow .chip').forEach(c=>c.classList.toggle('sel',c.dataset.sb===v)); previewLayout(); }
function pickSide(v){ SELSIDE=v; previewLayout(); }
function previewLayout(){ renderSidebar({sidebar:SELSIDEBAR,sidebar_side:SELSIDE}); }

/* ⌘K command palette — search bots (this box + across connected boxes) + jump to pages */
let PAL=[], PALSEL=0, PALFLEET=null;
function palHasSidebar(){ return !!(SETTINGS&&SETTINGS.ui&&SETTINGS.ui.sidebar&&SETTINGS.ui.sidebar!=='off'); }
async function openPalette(){
  $('palScrim').classList.add('open');
  const i=$('palInput'); i.value=''; PALSEL=0; palRender();
  setTimeout(()=>i.focus(),30);
  try{ const d=await api('/api/fleet/status'); PALFLEET=(d&&d.fleet)||[]; palRender(); }catch(e){}
}
function closePalette(e){ if(e&&e.target!==$('palScrim'))return; $('palScrim').classList.remove('open'); }
function palItems(){
  const items=[], side=palHasSidebar();
  Object.values((typeof INSTMAP!=='undefined'&&INSTMAP)||{}).forEach(b=>{
    items.push({kind:'bot',lbl:b.name,box:curBoxLabel(),dot:b.status,
      go:()=>{ side?showView('telemetry',b.name):openDrawer(b.name); }});
  });
  (PALFLEET||[]).filter(r=>!r.controller&&r.reachable&&Array.isArray(r.bot_names)).forEach(r=>{
    r.bot_names.forEach(nm=>{ if(nm) items.push({kind:'xbot',lbl:nm,box:r.name,go:()=>openNode(r.name)}); });
  });
  const modal=[
    {ic:'🖥',lbl:'Boxes',go:()=>openBoxes()},{ic:'🌐',lbl:'Proxies',go:()=>openProxies()},
    {ic:'➕',lbl:'Add Bot',go:()=>openDeploy()},{ic:'📁',lbl:'Files',go:()=>openFiles()},
    {ic:'👥',lbl:'Share',go:()=>openShares()},{ic:'👤',lbl:'Users',go:()=>openUsers()},
    {ic:'🔗',lbl:'Connect',go:()=>openConnection()},{ic:'⟲',lbl:'Scan existing',go:()=>openScan()},
    {ic:'⚙',lbl:'Settings',go:()=>openSettings()},
  ];
  const views=[
    {ic:'▦',lbl:'Dashboard',go:()=>showView('dashboard')},{ic:'❖',lbl:'Fleet',go:()=>showView('fleet')},
    {ic:'⚡',lbl:'Activity & alerts',go:()=>showView('activity')},{ic:'⏱',lbl:'Automation',go:()=>showView('automation')},
  ];
  (side?views.concat(modal):modal).forEach(p=>items.push(Object.assign({kind:'page'},p)));
  const q=($('palInput').value||'').trim().toLowerCase();
  return q?items.filter(it=>it.lbl.toLowerCase().includes(q)||(it.box&&it.box.toLowerCase().includes(q))):items;
}
function palRender(){
  PAL=palItems(); if(PALSEL>=PAL.length)PALSEL=Math.max(0,PAL.length-1);
  const box=$('palResults');
  if(!PAL.length){ box.innerHTML='<div class="palhint" style="border:none">No matches.</div>'; }
  else box.innerHTML=PAL.map((it,idx)=>{
    let ic, tag;
    if(it.kind==='page'){ ic=it.ic; tag='<span class="pk">page</span>'; }
    else if(it.kind==='bot'){ const c=it.dot==='running'?'var(--run)':(it.dot==='crashed'?'var(--crash)':'var(--dim)');
      ic=`<span style="color:${c}">●</span>`; tag=`<span class="pk">bot · ${esc(curBoxLabel())}</span>`; }
    else { ic='<span style="color:var(--dim)">●</span>'; tag=`<span class="pk">bot · ${esc(it.box)}</span>`; }
    return `<div class="palitem ${idx===PALSEL?'sel':''}" onclick="palPick(${idx})" onmousemove="if(PALSEL!=${idx}){PALSEL=${idx};palHi();}"><span class="pic">${ic}</span><span class="pl">${esc(it.lbl)}</span>${tag}</div>`;
  }).join('');
  const sc=$('palScope'); if(sc) sc.textContent=PALFLEET?(PALFLEET.filter(r=>r.reachable).length+' box(es)'):curBoxLabel();
}
function palHi(){ document.querySelectorAll('#palResults .palitem').forEach((el,idx)=>el.classList.toggle('sel',idx===PALSEL)); const s=document.querySelector('#palResults .palitem.sel'); if(s)s.scrollIntoView({block:'nearest'}); }
function palPick(idx){ const it=PAL[idx]; if(!it)return; closePalette(); try{ it.go(); }catch(e){} }
function palKey(e){
  if(e.key==='Escape'){ closePalette(); }
  else if(e.key==='ArrowDown'){ e.preventDefault(); PALSEL=Math.min(PAL.length-1,PALSEL+1); palHi(); }
  else if(e.key==='ArrowUp'){ e.preventDefault(); PALSEL=Math.max(0,PALSEL-1); palHi(); }
  else if(e.key==='Enter'){ e.preventDefault(); palPick(PALSEL); }
}
document.addEventListener('keydown',function(e){
  if((e.metaKey||e.ctrlKey)&&(e.key==='k'||e.key==='K')){ e.preventDefault(); openPalette(); }
});

let PROXROWS=[], BULKSEL=new Set();
let CONN={port:8765,user:'ubuntu',public_ip:''};
function copyText(t,btn){
  const done=()=>{ if(btn){ const o=btn.textContent; btn.textContent='✓'; setTimeout(()=>btn.textContent=o,1200); } };
  if(navigator.clipboard&&navigator.clipboard.writeText){ navigator.clipboard.writeText(t).then(done,()=>{}); }
  else{ const a=document.createElement('textarea'); a.value=t; document.body.appendChild(a); a.select();
    try{document.execCommand('copy');done();}catch(e){} a.remove(); }
}
async function openConnection(){
  $('connScrim').classList.add('open'); $('connMsg').textContent='';
  $('connUrl').value=location.origin;
  try{ CONN=await api('/api/connection'); }catch(e){}
  const localish=['localhost','127.0.0.1','::1','[::1]'].includes(location.hostname);
  $('connTunnel').style.display=localish?'block':'none';
  $('connDirect').style.display=localish?'none':'block';
  if(localish){ $('connIp').value=CONN.public_ip||''; renderConn(); }
  try{ const nd=await api('/api/nodes'); if($('connMulti'))$('connMulti').style.display=(localish&&nd&&nd.nodes&&nd.nodes.length)?'block':'none'; }catch(e){}
}
function closeConnection(e){ if(e&&e.target!==$('connScrim'))return; $('connScrim').classList.remove('open'); }

// ---- Boxes (multi-VPS controller) ----
// Rename a box: the controller's name lives in settings.box_name; a node's is a
// display label over its (unchanged) registry key. Refreshes every place the name shows.
async function renameBox(isController, name, current){
  const v=prompt(isController?'Name for the controller box:':'Display name for this box:', current||'');
  if(v===null) return;
  let d;
  if(isController){ d=await api('/api/settings','POST',{box_name:v.trim()}); if(d&&d.settings) SETTINGS=d.settings; }
  else { d=await api('/api/nodes/label','POST',{name:name,label:v.trim()}); }
  if(d&&d.error){ alert('✗ '+d.error); return; }
  loadBoxes();
  try{ renderSidebar(SETTINGS&&SETTINGS.ui); }catch(e){}
  try{ showView(CURVIEW); }catch(e){}
}
async function openBoxes(){ $('boxScrim').classList.add('open'); $('boxMsg').textContent=''; await loadBoxes(); }
function closeBoxes(e){ if(e&&e.target!==$('boxScrim'))return; $('boxScrim').classList.remove('open'); }
function boxMode(){
  const m=(document.querySelector('input[name=boxmode]:checked')||{}).value||'ssh';
  $('boxSsh').style.display=(m==='ssh')?'flex':'none';
  $('boxSshBar').style.display=(m==='ssh')?'flex':'none';
  $('boxDo').style.display=(m==='do')?'flex':'none';
  if(m==='do')loadDo(false);
}
function hostBit(h){
  if(!h)return '';
  let s='';
  if(h.load&&h.load.length)s+=' · load '+(+h.load[0]).toFixed(2);
  if(h.mem_total)s+=' · mem '+Math.round(100*(h.mem_used||0)/h.mem_total)+'%';
  return s;
}
async function loadBoxes(){
  const el=$('boxList');
  let d; try{ d=await api('/api/fleet/status'); }catch(e){ el.innerHTML='<span class="hint">failed to load</span>'; return; }
  const rows=(d&&d.fleet)||[];
  const nodes=rows.filter(r=>!r.controller);
  $('boxBulk').style.display=nodes.length?'flex':'none';
  if(!nodes.length){ el.innerHTML='<span class="hint">No other boxes connected yet — the controller is your current dashboard. Add another below.</span>'; return; }
  el.innerHTML=rows.map(r=>{
    const up=r.reachable, col=up?'var(--ok,#3a6f5a)':'var(--crash,#c25)';
    const meta=up?((r.running||0)+'/'+(r.bots||0)+' bots running'+hostBit(r.host)):('offline'+(r.error?(' — '+esc(r.error)):''));
    const sub=r.controller?'controller':esc((r.ssh_user||'')+'@'+(r.ssh_host||''));
    const ren=`<button title="Rename this box" onclick="renameBox(${r.controller?'true':'false'},'${jsq(r.name)}','${jsq(r.label||r.name)}')">✎</button>`;
    const right=ren+(r.controller?' <span class="hint">controller</span>':
      (` <button onclick="openNode('${jsq(r.name)}')">Open</button> `+
       (r.do?`<button class="danger" onclick="destroyBox('${jsq(r.name)}',this)">Destroy</button> `:'')+
       `<button class="danger" onclick="removeBox('${jsq(r.name)}',this)">Remove</button>`));
    return `<div style="display:flex;align-items:center;gap:.6rem;padding:.45rem .6rem;border:1px solid var(--line);border-radius:9px">
      <span style="width:9px;height:9px;border-radius:50%;background:${col};flex:none"></span>
      <div style="flex:1;min-width:0">
        <div style="font-weight:600">${esc(r.label||r.name)} <span class="hint" style="font-weight:400;font-family:var(--mono)">${sub}</span></div>
        <div class="hint">${meta}</div>
      </div>${right}</div>`;
  }).join('');
}
async function openNode(name){
  try{ await api('/api/node/select','POST',{name}); }catch(e){}
  location.href='/';
}
async function fleetAction(action,btn){
  if(action==='stop'&&!confirm('Stop ALL bots on ALL boxes?'))return;
  btn.disabled=true; $('boxBulkMsg').style.color='var(--dim)'; $('boxBulkMsg').textContent=action+'ing all boxes…';
  let d; try{ d=await api('/api/fleet/action','POST',{action,targets:['all']}); }catch(e){ d={error:String(e)}; }
  btn.disabled=false;
  if(!d||d.error){ $('boxBulkMsg').style.color='var(--crash)'; $('boxBulkMsg').textContent='✗ '+((d&&d.error)||'failed'); return; }
  const ok=(d.results||[]).filter(r=>r.ok).length, n=(d.results||[]).length;
  $('boxBulkMsg').style.color='var(--dim)'; $('boxBulkMsg').textContent='✓ '+action+' sent to '+ok+'/'+n+' boxes';
  loadBoxes();
}
async function fleetUpdate(btn){
  if(!confirm('Run self-update (git pull + restart) on all connected nodes?'))return;
  btn.disabled=true; $('boxBulkMsg').style.color='var(--dim)'; $('boxBulkMsg').textContent='updating all nodes…';
  let d; try{ d=await api('/api/fleet/update','POST',{targets:['all']}); }catch(e){ d={error:String(e)}; }
  btn.disabled=false;
  if(!d||d.error){ $('boxBulkMsg').style.color='var(--crash)'; $('boxBulkMsg').textContent='✗ '+((d&&d.error)||'failed'); return; }
  const res=d.results||[], ok=res.filter(r=>r.ok).length, upd=res.filter(r=>r.ok&&r.updated).length;
  $('boxBulkMsg').style.color='var(--dim)'; $('boxBulkMsg').textContent='✓ '+ok+'/'+res.length+' ok, '+upd+' updated';
  loadBoxes();
}
async function addBox(btn){
  const mode=(document.querySelector('input[name=boxmode]:checked')||{}).value||'ssh';
  if(mode==='do'){ $('boxMsg').style.color='var(--dim)'; $('boxMsg').textContent='DigitalOcean support is coming in the next step.'; return; }
  const name=$('boxName').value.trim(), target=$('boxTarget').value.trim();
  if(!name||!target){ $('boxMsg').style.color='var(--crash)'; $('boxMsg').textContent='Name and SSH target are required.'; return; }
  const body={name, target,
    ssh_key:$('boxKey').value.trim()||undefined,
    remote_port:($('boxRemotePort').value.trim()||undefined),
    basic_user:$('boxBasicUser').value.trim()||undefined,
    basic_pass:$('boxBasicPass').value||undefined};
  const orig=btn.textContent; btn.disabled=true; btn.innerHTML='<span class="spin"></span> connecting…';
  $('boxMsg').style.color='var(--dim)'; $('boxMsg').textContent='opening SSH tunnel + testing…';
  let d; try{ d=await api('/api/nodes','POST',body); }catch(e){ d={error:String(e)}; }
  btn.disabled=false; btn.textContent=orig;
  if(!d||d.error){ $('boxMsg').style.color='var(--crash)'; $('boxMsg').textContent='✗ '+((d&&d.error)||'failed'); return; }
  const t=d.test||{};
  if(t.reachable){ $('boxMsg').style.color='var(--dim)'; $('boxMsg').textContent='✓ connected — '+(t.instances||0)+' bot(s) on '+d.node.name; }
  else { $('boxMsg').style.color='var(--warn)'; $('boxMsg').textContent='added, but tunnel/API not reachable yet: '+(t.error||'?')+' (it will keep retrying)'; }
  $('boxName').value=''; $('boxTarget').value=''; $('boxKey').value=''; $('boxRemotePort').value=''; $('boxBasicUser').value=''; $('boxBasicPass').value='';
  loadBoxes();
}
async function removeBox(name,btn){
  if(!confirm('Disconnect box "'+name+'"? (Its bots keep running on that VPS; only the tunnel/registration here is removed.)'))return;
  btn.disabled=true;
  let d; try{ d=await api('/api/nodes/remove','POST',{name}); }catch(e){ d={error:String(e)}; }
  if(!d||d.error){ btn.disabled=false; $('boxMsg').style.color='var(--crash)'; $('boxMsg').textContent='✗ '+((d&&d.error)||'failed'); return; }
  loadBoxes();
}
async function destroyBox(name,btn){
  if(!confirm('DESTROY the DigitalOcean droplet behind "'+name+'"?\n\nThis permanently deletes the VPS and every bot on it. This cannot be undone.'))return;
  if(prompt('Type the box name to confirm permanent destruction:')!==name){ return; }
  btn.disabled=true;
  let d; try{ d=await api('/api/nodes/do/destroy','POST',{name}); }catch(e){ d={error:String(e)}; }
  if(!d||d.error){ btn.disabled=false; alert('Destroy failed: '+((d&&d.error)||'?')); return; }
  loadBoxes();
}

// ---- DigitalOcean ----
let DO_PROV_TIMER=null;
async function loadDo(force){
  let d; try{ d=await api('/api/nodes/do'+(force?'?_='+Date.now():'')); }catch(e){ d={error:String(e)}; }
  if(!d||!d.token_saved){
    $('doTokenBox').style.display='block'; $('doMain').style.display='none';
    if(d&&d.error)$('doTokenMsg').textContent=d.error;
    return;
  }
  $('doTokenBox').style.display='none'; $('doMain').style.display='flex';
  if(d.error){ $('doConnMsg').style.color='var(--crash)'; $('doConnMsg').textContent=d.error; return; }
  // regions
  const rs=$('doRegion'); if(rs)rs.innerHTML=(d.regions||[]).map(r=>`<option value="${esc(r.slug)}">${esc(r.name)} (${esc(r.slug)})</option>`).join('');
  // sizes (default to 1GB)
  const ss=$('doSize'); if(ss){
    ss.innerHTML=(d.sizes||[]).map(s=>{
      const gb=Math.round((s.memory||0)/1024*10)/10;
      return `<option value="${esc(s.slug)}"${s.slug===d.default_size?' selected':''}>${esc(s.slug)} — ${gb}GB / ${s.vcpus} vCPU — $${s.price}/mo</option>`;
    }).join('');
  }
  // existing droplets
  const dl=$('doDropletList');
  const ds=(d.droplets||[]);
  dl.innerHTML=ds.length? ds.map(x=>
    `<div style="display:flex;align-items:center;gap:.5rem;padding:.35rem .5rem;border:1px solid var(--line);border-radius:8px">
      <div style="flex:1;min-width:0"><b>${esc(x.name)}</b> <span class="hint" style="font-family:var(--mono)">${esc(x.public_ip||'no-ip')} · ${esc(x.region||'')} · ${esc(x.status||'')}</span></div>
      <button onclick="doConnect(${x.id},'${esc(x.name)}',this)">Connect</button>
    </div>`).join('') : '<span class="hint">no droplets in this account</span>';
}
async function saveDoToken(btn){
  const token=$('doToken').value.trim();
  if(!token){ $('doTokenMsg').style.color='var(--crash)'; $('doTokenMsg').textContent='paste a token first'; return; }
  btn.disabled=true; $('doTokenMsg').style.color='var(--dim)'; $('doTokenMsg').textContent='saving…';
  let d; try{ d=await api('/api/nodes/do/token','POST',{token}); }catch(e){ d={error:String(e)}; }
  btn.disabled=false; $('doToken').value='';
  if(!d||d.error||!d.token_saved){ $('doTokenMsg').style.color='var(--crash)'; $('doTokenMsg').textContent='✗ '+((d&&d.error)||'failed'); return; }
  loadDo(true);
}
async function forgetDoToken(){
  if(!confirm('Forget the saved DigitalOcean token?'))return;
  try{ await api('/api/nodes/do/token','POST',{token:''}); }catch(e){}
  loadDo(true);
}
async function doConnect(id,name,btn){
  btn.disabled=true; $('doConnMsg').style.color='var(--dim)'; $('doConnMsg').textContent='connecting '+name+'…';
  let d; try{ d=await api('/api/nodes/do/connect','POST',{droplet_id:id,name}); }catch(e){ d={error:String(e)}; }
  btn.disabled=false;
  if(!d||d.error){ $('doConnMsg').style.color='var(--crash)'; $('doConnMsg').textContent='✗ '+((d&&d.error)||'failed'); return; }
  const t=d.test||{};
  $('doConnMsg').style.color=t.reachable?'var(--dim)':'var(--warn)';
  $('doConnMsg').textContent=t.reachable?('✓ connected — '+(t.instances||0)+' bot(s)'):('added, not reachable yet: '+(t.error||'?'));
  loadBoxes();
}
async function doProvision(btn){
  const name=$('doNewName').value.trim(), region=$('doRegion').value, size=$('doSize').value;
  if(!name||!region){ $('doProvMsg').style.color='var(--crash)'; $('doProvMsg').textContent='name and region required'; return; }
  if(!confirm('Create a new DigitalOcean droplet "'+name+'" ('+size+')? This starts billing for the VPS.'))return;
  btn.disabled=true; $('doProvMsg').style.color='var(--dim)'; $('doProvMsg').textContent='starting…';
  let d; try{ d=await api('/api/nodes/do/provision','POST',{name,region,size}); }catch(e){ d={error:String(e)}; }
  btn.disabled=false;
  if(!d||d.error){ $('doProvMsg').style.color='var(--crash)'; $('doProvMsg').textContent='✗ '+((d&&d.error)||'failed'); return; }
  $('doProvMsg').textContent='provisioning…'; $('doProvLog').style.display='block';
  if(DO_PROV_TIMER)clearInterval(DO_PROV_TIMER);
  DO_PROV_TIMER=setInterval(pollProvision,3000); pollProvision();
}
async function pollProvision(){
  let j; try{ j=await api('/api/nodes/do/job'); }catch(e){ return; }
  if(!j)return;
  $('doProvLog').textContent=j.output||'';
  if(j.status==='done'||j.status==='error'){
    clearInterval(DO_PROV_TIMER); DO_PROV_TIMER=null;
    $('doProvMsg').style.color=(j.status==='done')?'var(--dim)':'var(--crash)';
    $('doProvMsg').textContent=(j.status==='done')?'✓ done':'✗ failed';
    loadBoxes();
  }
}
function renderConn(){
  const ip=($('connIp').value.trim()||'YOUR_VPS_IP'), p=CONN.port||8765, u=CONN.user||'ubuntu';
  $('connSsh').value=`ssh -L ${p}:127.0.0.1:${p} ${u}@${ip}`;
}
function dlReconnect(os){
  const ip=encodeURIComponent($('connIp').value.trim());
  if(!ip){ $('connMsg').style.color='var(--crash)'; $('connMsg').textContent='enter the VPS IP first'; return; }
  $('connMsg').style.color='var(--dim)'; $('connMsg').textContent='downloading shortcut…';
  window.location=`/api/connection/script?os=${os}&ip=${ip}&port=${CONN.port||8765}&user=${encodeURIComponent(CONN.user||'')}`;
}
function dlMulti(os){
  const ip=encodeURIComponent($('connIp').value.trim());
  if(!ip){ $('connMsg').style.color='var(--crash)'; $('connMsg').textContent='enter the controller VPS IP first'; return; }
  $('connMsg').style.color='var(--dim)'; $('connMsg').textContent='downloading all-boxes launcher…';
  window.location=`/api/connection/multiscript?os=${os}&ip=${ip}&port=${CONN.port||8765}&user=${encodeURIComponent(CONN.user||'')}`;
}
function openProxies(){ $('proxScrim').classList.add('open'); loadProxies(); loadWebshareHint(); }
function closeProxies(e){ if(e&&e.target!==$('proxScrim'))return; $('proxScrim').classList.remove('open'); }
async function loadProxies(){
  $('proxMsg').textContent='';
  const d=await api('/api/proxies');
  PROXROWS=d.proxies||[];
  renderProxyList();
  renderBulkTargets();
}
function renderProxyList(){
  const rows=PROXROWS, box=$('proxList');
  const inp='font-family:var(--mono);font-size:.78rem;background:#06090c;color:#cdd9e2;border:1px solid var(--line);border-radius:7px;padding:.35rem .45rem';
  const found=rows.filter(r=>r.found).length;
  const cnt=$('proxCount'); if(cnt) cnt.textContent=found?('· '+found+' bot'+(found===1?'':'s')):'';
  if(!found){ box.innerHTML='<div class="hint">No instances have a proxy field.</div>'; return; }
  box.innerHTML=rows.map((r,idx)=> r.found ? `
    <div class="scand likely" style="gap:.4rem;flex-wrap:wrap;align-items:center">
      <div class="si" style="flex-basis:100%"><div class="sn">${esc(r.name)}${r.has_auth?` <span title="proxy credentials set${r.user?' ('+esc(r.user)+')':''}">🔒</span>`:''}</div></div>
      <input id="ph${idx}" value="${esc(String(r.host))}" placeholder="host" style="flex:2 1 8rem;${inp}">
      <input id="pp${idx}" value="${esc(String(r.port))}" placeholder="port" style="flex:1 1 4rem;${inp}">
      <input id="pu${idx}" value="${esc(String(r.user||''))}" placeholder="user (optional)" autocomplete="off" style="flex:1.4 1 6rem;${inp}">
      <input id="pw${idx}" type="password" placeholder="${r.has_auth?'password (set — blank keeps)':'password (optional)'}" autocomplete="off" style="flex:1.4 1 6rem;${inp}">
      <button class="go" onclick="saveProxy('${jsq(r.name)}',${idx},this,false)">Save</button>
      <button class="warn" title="Save &amp; restart" onclick="saveProxy('${jsq(r.name)}',${idx},this,true)">⟳</button>
    </div>` :
    `<div class="scand" style="opacity:.5"><div class="si"><div class="sn">${esc(r.name)}</div><div class="sr">${esc(r.reason||'no proxy')}</div></div></div>`
  ).join('');
}
async function saveProxy(name,idx,btn,restart){
  const host=$('ph'+idx).value.trim(), port=parseInt($('pp'+idx).value,10);
  const user=$('pu'+idx).value.trim(), pw=$('pw'+idx).value;
  if(!host||!Number.isInteger(port)){ $('proxMsg').style.color='var(--crash)'; $('proxMsg').textContent='✗ host and numeric port required'; return; }
  const body={host,port};
  if(user) body.user=user;                              // set username
  else if((PROXROWS[idx]||{}).has_auth){ body.user=''; body.password=''; }  // cleared user = drop auth
  if(pw) body.password=pw;                              // blank password = keep existing
  const orig=btn.textContent; btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
  const d=await api(`/api/instances/${encodeURIComponent(name)}/proxy`,'POST',body);
  let extra='';
  if(!d.error&&restart){
    const r=await api(`/api/instances/${encodeURIComponent(name)}/restart`,'POST');
    extra=r.error?(' — restart failed: '+r.error):(' — restarted ('+r.status+')'); refresh();
  }
  btn.disabled=false; btn.textContent=orig;
  $('proxMsg').style.color=d.error?'var(--crash)':'var(--dim)';
  $('proxMsg').textContent=d.error?('✗ '+d.error):('✓ '+name+' → '+host+':'+port+extra);
}
function renderBulkTargets(){
  const found=PROXROWS.filter(r=>r.found);
  BULKSEL=new Set(found.map(r=>r.name));   // default: all proxy-capable selected
  const chips=found.length
    ? found.map(r=>`<div class="chip sel" data-n="${esc(r.name)}" onclick="toggleBulk('${jsq(r.name)}')">${esc(r.name)}</div>`).join('')
    : '<span class="hint">No proxy-capable instances.</span>';
  $('bulkTargets').innerHTML=chips;
  $('wsTargets').innerHTML=chips;          // same selection drives both panels
  updateBulkCount();
}
function syncChip(name){
  const on=BULKSEL.has(name);
  document.querySelectorAll('.chip[data-n="'+CSS.escape(name)+'"]').forEach(c=>c.classList.toggle('sel',on));
}
function toggleBulk(name){
  if(BULKSEL.has(name)) BULKSEL.delete(name); else BULKSEL.add(name);
  syncChip(name); updateBulkCount();
}
function bulkSelectAll(on){
  PROXROWS.filter(r=>r.found).forEach(r=>{
    if(on)BULKSEL.add(r.name); else BULKSEL.delete(r.name); syncChip(r.name);
  });
  updateBulkCount();
}
function updateBulkCount(){
  const t='('+BULKSEL.size+' selected)';
  $('bulkCount').textContent=t; if($('wsCount'))$('wsCount').textContent=t;
}
async function webshareCount(btn){
  const token=$('wsToken').value.trim();
  $('wsMsg').style.color='var(--dim)'; $('wsMsg').textContent='checking…'; btn.disabled=true;
  const d=await api('/api/proxies/webshare','POST',{token,count_only:true,
    valid_only:$('wsValid').checked, countries:$('wsCountries').value.trim()});
  btn.disabled=false;
  if(d.error){ $('wsMsg').style.color='var(--crash)'; $('wsMsg').textContent='✗ '+d.error; return; }
  $('wsMsg').style.color='var(--dim)';
  $('wsMsg').textContent=`✓ ${d.count} proxies`+(d.countries&&d.countries.length?` (${d.countries.join(', ')})`:'');
}
async function webshareImport(btn){
  const targets=[...BULKSEL];
  if(!targets.length){ $('wsMsg').style.color='var(--crash)'; $('wsMsg').textContent='select at least one target'; return; }
  const body={ targets, token:$('wsToken').value.trim(),
    auth:document.querySelector('input[name=wsauth]:checked').value,
    mode:document.querySelector('input[name=wsmode]:checked').value,
    valid_only:$('wsValid').checked, countries:$('wsCountries').value.trim(),
    save_token:$('wsSave').checked, restart:$('wsRestart').checked };
  btn.disabled=true; $('wsMsg').style.color='var(--dim)';
  $('wsMsg').textContent=body.restart?'importing + restarting…':'importing…';
  const d=await api('/api/proxies/webshare','POST',body);
  btn.disabled=false;
  if(d.error){ $('wsMsg').style.color='var(--crash)'; $('wsMsg').textContent='✗ '+d.error; return; }
  const ok=d.assigned.filter(r=>r.ok).length, fail=d.assigned.length-ok;
  $('wsMsg').style.color=fail?'var(--warn)':'var(--dim)';
  $('wsMsg').textContent=`✓ fetched ${d.fetched}, assigned ${ok}`+(fail?` · ${fail} failed`:'')
    +(d.saved_token?' · token saved':'')+(body.restart?' · restarted':'');
  if(d.saved_token){ $('wsToken').value=''; loadWebshareHint(true); }
  loadProxies(); if(body.restart)refresh();
}
async function loadWebshareHint(saved){
  if(saved===undefined){ try{ const s=await api('/api/settings'); saved=s.webshare_saved; }catch(e){ return; } }
  $('wsTokHint').textContent=saved?'a token is saved — leave blank to reuse it':'';
  if(saved)$('wsToken').placeholder='(saved token — blank to reuse)';
}
async function applyBulkProxies(btn){
  const targets=[...BULKSEL];
  const proxies=$('bulkList').value.split('\n').map(s=>s.trim()).filter(Boolean);
  $('bulkMsg').style.color='var(--crash)';
  if(!targets.length){ $('bulkMsg').textContent='select at least one target'; return; }
  if(!proxies.length){ $('bulkMsg').textContent='paste at least one host:port'; return; }
  const mode=document.querySelector('input[name=bulkmode]:checked').value;
  const restart=$('bulkRestart').checked;
  btn.disabled=true; $('bulkMsg').style.color='var(--dim)'; $('bulkMsg').textContent=restart?'applying + restarting…':'applying…';
  const d=await api('/api/proxies/bulk','POST',{targets,proxies,mode,restart});
  btn.disabled=false;
  if(d.error){ $('bulkMsg').style.color='var(--crash)'; $('bulkMsg').textContent='✗ '+d.error; return; }
  const ok=d.results.filter(r=>r.ok).length, fail=d.results.length-ok;
  $('bulkMsg').style.color=fail?'var(--warn)':'var(--dim)';
  $('bulkMsg').textContent=`✓ ${ok} updated`+(fail?` · ${fail} failed`:'')+(restart?' · restarted':'');
  loadProxies(); if(restart)refresh();
}

// --- proxy health / auto-fix ---
let HEALTH=[];
async function scanHealth(btn){
  if(btn)btn.disabled=true;
  $('healthMsg').style.color='var(--dim)'; $('healthMsg').textContent='scanning consoles…';
  const d=await api('/api/proxies/health','POST',{});
  if(btn)btn.disabled=false;
  if(d.error){ $('healthMsg').style.color='var(--crash)'; $('healthMsg').textContent='✗ '+d.error; return null; }
  HEALTH=d.results||[]; renderHealth();
  const n=(d.errored||[]).length;
  $('healthMsg').style.color=n?'var(--warn)':'var(--dim)';
  $('healthMsg').textContent=n?('⚠ '+n+' bot'+(n>1?'s':'')+' with proxy errors'):'✓ no proxy errors detected';
  return d.errored||[];
}
function renderHealth(){
  const box=$('healthList');
  if(!HEALTH.length){ box.innerHTML='<span class="hint">No proxy-using bots, or not scanned yet.</span>'; return; }
  box.innerHTML=HEALTH.map(r=>{
    const tag=r.errored?'<span style="color:var(--crash)">● errored</span>'
      :(r.running?'<span style="color:#57b65a">● ok</span>':'<span class="hint">○ stopped</span>');
    const ev=r.errored?`<div class="hint" style="margin-left:1.1rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.evidence)}">${esc(r.evidence)}</div>`:'';
    return `<div><span style="display:inline-block;min-width:10rem">${esc(r.name)}</span> <span class="hint">${esc(String(r.host)+':'+r.port)}</span> ${tag}${r.errored?' <span class="hint">('+r.hits+')</span>':''}${ev}</div>`;
  }).join('');
}
async function autoFix(btn){
  const scope=document.querySelector('input[name=fixscope]:checked').value;
  const mode=document.querySelector('input[name=fixmode]:checked').value;
  let targets;
  if(scope==='errored'){
    const er=await scanHealth(null);
    if(er===null)return;
    if(!er.length){ $('healthMsg').style.color='var(--dim)'; $('healthMsg').textContent='✓ nothing to fix — no errored bots'; return; }
    targets=['errored'];
  } else if(scope==='all'){ targets=['all']; }
  else { targets=[...BULKSEL]; if(!targets.length){ $('healthMsg').style.color='var(--crash)'; $('healthMsg').textContent='select target bots first (chips below)'; return; } }
  btn.disabled=true; $('healthMsg').style.color='var(--dim)';
  $('healthMsg').textContent='re-importing from Webshare + reassigning + restarting…';
  const body={ targets, token:$('wsToken').value.trim(),
    auth:document.querySelector('input[name=wsauth]:checked').value,
    mode, valid_only:true, save_token:$('wsSave').checked, restart:true };
  const d=await api('/api/proxies/webshare','POST',body);
  btn.disabled=false;
  if(d.error){ $('healthMsg').style.color='var(--crash)'; $('healthMsg').textContent='✗ '+d.error; return; }
  const ok=d.assigned.filter(r=>r.ok).length, fail=d.assigned.length-ok;
  $('healthMsg').style.color=fail?'var(--warn)':'var(--dim)';
  $('healthMsg').textContent='✓ fetched '+d.fetched+', reassigned '+ok+(fail?(' · '+fail+' failed'):'')+' · restarted';
  loadProxies(); refresh(); setTimeout(()=>scanHealth(null),2000);
}
async function bulkSelectErrored(btn){
  const er=await scanHealth(null);
  if(er===null)return;
  BULKSEL=new Set(er);
  PROXROWS.filter(r=>r.found).forEach(r=>syncChip(r.name));
  updateBulkCount();
  $('bulkMsg').style.color='var(--dim)';
  $('bulkMsg').textContent=er.length?('selected '+er.length+' errored bot'+(er.length>1?'s':'')):'no errored bots';
}

let depSrc='aquarius', depTimer=null;
// Mirror of the server's sanitize_name(): make a Linux-safe folder/instance name.
function lxSafe(s){ return (s||'').trim().replace(/[^A-Za-z0-9._-]+/g,'-').replace(/-{2,}/g,'-').replace(/^[-._]+|[-._]+$/g,''); }
function openDeploy(){
  depSrc='aquarius';
  document.querySelectorAll('#depSrc .chip').forEach(c=>c.classList.toggle('sel',c.dataset.s==='aquarius'));
  $('depRepoWrap').style.display='none';
  ['dep_name','dep_repo','dep_mem','dep_cpu'].forEach(id=>$(id).value='');
  $('dep_path').textContent='';
  $('depLog').style.display='none'; $('depLog').textContent='';
  $('depMsg').textContent=''; $('depBtn').disabled=false;
  $('deployScrim').classList.add('open'); setTimeout(()=>$('dep_name').focus(),50);
}
function closeDeploy(e){ if(e&&e.target!==$('deployScrim'))return; if(depTimer){clearInterval(depTimer);depTimer=null;} $('deployScrim').classList.remove('open'); }
function pickSrc(s,el){ depSrc=s; document.querySelectorAll('#depSrc .chip').forEach(c=>c.classList.toggle('sel',c.dataset.s===s)); $('depRepoWrap').style.display=s==='custom'?'':'none'; }
function depPreview(){
  const base=((SETTINGS&&SETTINGS.base_dir)||'').replace(/[\/\\]+$/,''), n=lxSafe($('dep_name').value);
  $('dep_path').textContent = n ? ('Installs to: '+(base?base+'/':'')+n) : '';
}
async function startDeploy(){
  const name=lxSafe($('dep_name').value);
  if(!name){ $('depMsg').style.color='var(--crash)'; $('depMsg').textContent='enter a name (letters & digits)'; return; }
  const body={name,source:depSrc,owner_repo:$('dep_repo').value.trim(),
              limits:{memory:$('dep_mem').value.trim(),cpu:$('dep_cpu').value.trim()},
              autostart:$('dep_autostart').checked};
  $('depMsg').style.color='var(--dim)'; $('depMsg').textContent='starting…'; $('depBtn').disabled=true;
  const d=await api('/api/deploy','POST',body);
  if(d.error){ $('depMsg').style.color='var(--crash)'; $('depMsg').textContent='✗ '+d.error; $('depBtn').disabled=false; return; }
  $('depLog').style.display=''; $('depMsg').textContent='adding…';
  if(depTimer)clearInterval(depTimer);
  depTimer=setInterval(pollDeploy,700); pollDeploy();
}
async function pollDeploy(){
  const j=await api('/api/deploy/job');
  $('depLog').textContent=j.output||'…';
  $('depLog').scrollTop=$('depLog').scrollHeight;
  if(j.status==='done'||j.status==='error'){
    clearInterval(depTimer); depTimer=null; $('depBtn').disabled=false;
    $('depMsg').style.color=j.status==='done'?'var(--dim)':'var(--crash)';
    $('depMsg').textContent=j.status==='done'?'✓ bot added — start it from the dashboard':'✗ could not add bot';
    refresh();
  }
}
/* ---- migrate ZenithProxy -> AquariusProxy (owner) ---- */
let migName=null, migTimer=null;
function openMigrate(name){
  migName=name; $('migName').textContent=name;
  $('migLog').style.display='none'; $('migLog').textContent='';
  $('migMsg').textContent=''; $('migBtn').disabled=false; $('migBtn').style.display='';
  $('migRollBtn').style.display='none';
  $('migrateScrim').classList.add('open');
}
function closeMigrate(e){ if(e&&e.target!==$('migrateScrim'))return; if(migTimer){clearInterval(migTimer);migTimer=null;} $('migrateScrim').classList.remove('open'); }
async function startMigrate(){
  if(!migName)return;
  $('migMsg').style.color='var(--dim)'; $('migMsg').textContent='starting…'; $('migBtn').disabled=true; $('migRollBtn').style.display='none';
  const d=await api('/api/instances/'+encodeURIComponent(migName)+'/migrate','POST',{});
  if(d.error){ $('migMsg').style.color='var(--crash)'; $('migMsg').textContent='✗ '+d.error; $('migBtn').disabled=false; return; }
  $('migLog').style.display=''; $('migMsg').textContent='migrating…';
  if(migTimer)clearInterval(migTimer); migTimer=setInterval(pollMigrate,700); pollMigrate();
}
async function rollbackMigrate(){
  if(!migName)return; if(!confirm('Roll back '+migName+' to ZenithProxy from the latest backup?'))return;
  $('migMsg').style.color='var(--dim)'; $('migMsg').textContent='rolling back…'; $('migRollBtn').disabled=true; $('migBtn').disabled=true;
  const d=await api('/api/instances/'+encodeURIComponent(migName)+'/migrate/rollback','POST',{});
  if(d.error){ $('migMsg').style.color='var(--crash)'; $('migMsg').textContent='✗ '+d.error; $('migRollBtn').disabled=false; return; }
  $('migLog').style.display=''; $('migMsg').textContent='rolling back…';
  if(migTimer)clearInterval(migTimer); migTimer=setInterval(pollMigrate,700); pollMigrate();
}
async function pollMigrate(){
  const j=await api('/api/migrate/job');
  $('migLog').textContent=j.output||'…';
  $('migLog').scrollTop=$('migLog').scrollHeight;
  if(j.status==='done'||j.status==='error'){
    clearInterval(migTimer); migTimer=null; $('migBtn').disabled=false; $('migRollBtn').disabled=false;
    $('migMsg').style.color=j.status==='done'?'var(--dim)':'var(--crash)';
    $('migMsg').textContent=j.status==='done'?'✓ done — watch the bot console':'✗ failed — see the log';
    $('migBtn').style.display='none'; $('migRollBtn').style.display='';   // offer rollback after any run
    refresh();
  }
}
let FBCWD=null, FBPARENT=null, FBEDIT=null;
function openFiles(){ $('filesScrim').classList.add('open'); fbBack(); fbNav(''); }
function closeFiles(e){ if(e&&e.target!==$('filesScrim'))return; $('filesScrim').classList.remove('open'); }
function fbBack(){ $('fbEdit').style.display='none'; $('fbBrowse').style.display=''; }
async function fbNav(path){
  $('fbMsg').style.color='var(--dim)'; $('fbMsg').textContent='';
  const d=await api('/api/files?path='+encodeURIComponent(path||''));
  if(d.error){ $('fbMsg').style.color='var(--crash)'; $('fbMsg').textContent='✗ '+d.error; return; }
  FBCWD=d.path; FBPARENT=d.parent; fbRender(d);
}
function fbReload(){ fbNav(FBCWD||''); }
function fbGotoRoot(){ fbNav($('fbRoot').value); }
function fbUp(){ if(FBPARENT) fbNav(FBPARENT); }
function fbRender(d){
  const sel=$('fbRoot');
  sel.innerHTML=(d.roots||[]).map(r=>`<option value="${esc(r)}">${esc(r)}</option>`).join('');
  const cont=(d.roots||[]).filter(r=>d.path===r||d.path.startsWith(r)).sort((a,b)=>b.length-a.length)[0];
  if(cont) sel.value=cont;
  $('fbPath').textContent=d.path;
  if(!d.entries.length){ $('fbList').innerHTML='<div class="hint">empty folder</div>'; return; }
  // cross-box "send to another box" is only meaningful in controller mode (boxes registered)
  const xbox=typeof window.ABM_CURRENT_NODE!=='undefined';
  $('fbList').innerHTML=d.entries.map(e=>{
    const open=e.type==='dir'?`fbNav('${jsq(e.path)}')`:`fbOpen('${jsq(e.path)}')`;
    return `<div class="frow2">
      <span class="ficon">${e.type==='dir'?'📁':'📄'}</span>
      <span class="fn" onclick="${open}">${esc(e.name)}</span>
      <span class="fmeta">${e.type==='dir'?'':fmtBytes(e.size)}</span>
      <button title="${e.type==='dir'?'download as .zip':'download'}" onclick="fbDownload('${jsq(e.path)}')">⬇</button>
      ${xbox?`<button title="send to another box" onclick="fbSendToBox('${jsq(e.path)}','${jsq(e.name)}')">↗</button>`:''}
      <button title="rename" onclick="fbRename('${jsq(e.path)}','${jsq(e.name)}')">✎</button>
      <button class="danger" title="delete" onclick="fbDelete('${jsq(e.path)}',${e.type==='dir'})">🗑</button>
    </div>`;
  }).join('');
}
async function fbOpen(path){
  $('fbMsg').textContent='opening…';
  const d=await api('/api/files/read?path='+encodeURIComponent(path));
  if(d.error){ $('fbMsg').style.color='var(--crash)'; $('fbMsg').textContent='✗ '+d.error; return; }
  FBEDIT=d.path; $('fbEditPath').textContent=d.path; $('fbContent').value=d.content;
  $('fbEditMsg').style.color='var(--dim)'; $('fbEditMsg').textContent=fmtBytes(d.size);
  $('fbBrowse').style.display='none'; $('fbEdit').style.display='';
}
async function fbSave(){
  $('fbEditMsg').style.color='var(--dim)'; $('fbEditMsg').textContent='saving…';
  const d=await api('/api/files/write','POST',{path:FBEDIT,content:$('fbContent').value});
  $('fbEditMsg').style.color=d.error?'var(--crash)':'var(--dim)';
  $('fbEditMsg').textContent=d.error?('✗ '+d.error):('✓ saved '+fmtBytes(d.size));
}
async function fbMkdir(){
  const name=prompt('New folder name:'); if(!name)return;
  const d=await api('/api/files/mkdir','POST',{dir:FBCWD,name});
  if(d.error){ $('fbMsg').style.color='var(--crash)'; $('fbMsg').textContent='✗ '+d.error; return; }
  fbReload();
}
async function fbNewFile(){
  const name=prompt('New file name:'); if(!name)return;
  const d=await api('/api/files/newfile','POST',{dir:FBCWD,name});
  if(d.error){ $('fbMsg').style.color='var(--crash)'; $('fbMsg').textContent='✗ '+d.error; return; }
  fbOpen(d.path);
}
async function fbRename(path,cur){
  const name=prompt('Rename to:',cur); if(!name||name===cur)return;
  const d=await api('/api/files/rename','POST',{path,name});
  if(d.error){ $('fbMsg').style.color='var(--crash)'; $('fbMsg').textContent='✗ '+d.error; return; }
  fbReload();
}
async function fbDelete(path,isdir){
  if(!confirm('Delete '+(isdir?'folder + contents':'file')+'?\n'+path))return;
  const d=await api('/api/files/delete','POST',{path,recursive:isdir});
  if(d.error){ $('fbMsg').style.color='var(--crash)'; $('fbMsg').textContent='✗ '+d.error; return; }
  fbReload();
}
function fbPickUpload(isDir){ $(isDir?'fbUpDir':'fbUpFiles').click(); }
async function fbUpload(fileList){
  const files=[...(fileList||[])]; if(!files.length)return;
  if(!FBCWD){ $('fbMsg').style.color='var(--crash)'; $('fbMsg').textContent='✗ pick a folder first'; return; }
  let ok=0, fail=0, lastErr='';
  for(let i=0;i<files.length;i++){
    const f=files[i], rel=f.webkitRelativePath||f.name;
    $('fbMsg').style.color='var(--dim)';
    $('fbMsg').textContent=`uploading ${i+1}/${files.length}: ${rel} …`;
    try{
      const r=await fetch('/api/files/upload?dir='+encodeURIComponent(FBCWD)+'&name='+encodeURIComponent(rel),{method:'POST',body:f});
      const d=await r.json().catch(()=>({error:'bad response ('+r.status+')'}));
      if(d&&d.error){ fail++; lastErr=d.error; } else { ok++; }
    }catch(e){ fail++; lastErr=String(e); }
  }
  $('fbMsg').style.color=fail?'var(--crash)':'var(--dim)';
  $('fbMsg').textContent='✓ uploaded '+ok+' file(s)'+(fail?(' · ✗ '+fail+' failed ('+lastErr+')'):'');
  fbReload();
}
function fbDownload(path){
  // GET with the session cookie; Content-Disposition makes the browser save it
  // (a folder comes back as a .zip). Works for remote boxes via the reverse proxy.
  const a=document.createElement('a');
  a.href='/api/files/download?path='+encodeURIComponent(path);
  a.rel='noopener'; document.body.appendChild(a); a.click(); a.remove();
}
async function fbSendToBox(path,name){
  // copy this file/folder to another box via the controller (scp over SSH).
  const cur=(typeof window.ABM_CURRENT_NODE!=='undefined')?(window.ABM_CURRENT_NODE||''):'';
  let boxes=[{id:'',label:'this box (controller)'}];
  try{ const d=await api('/api/nodes'); (d.nodes||[]).forEach(n=>boxes.push({id:n.name,label:n.label||n.name})); }catch(e){}
  const dests=boxes.filter(b=>b.id!==cur);
  if(!dests.length){ alert('No other boxes are registered to send to.'); return; }
  const pick=prompt('Send "'+name+'" to which box?\n\n'+dests.map((b,i)=>(i+1)+') '+b.label).join('\n')+'\n\nEnter a number:');
  if(!pick)return;
  const idx=parseInt(pick,10)-1;
  if(isNaN(idx)||idx<0||idx>=dests.length){ alert('Invalid choice.'); return; }
  const dst=dests[idx];
  let def='';
  try{ const r=await api('/api/box/roots?box='+encodeURIComponent(dst.id)); def=(r.roots&&r.roots[0])||''; }catch(e){}
  const dstdir=prompt('Destination folder on '+dst.label+':',def);
  if(!dstdir)return;
  $('fbMsg').style.color='var(--dim)'; $('fbMsg').textContent='transferring "'+name+'" → '+dst.label+' … (this can take a moment)';
  const res=await api('/api/transfer','POST',{src_box:cur,src_path:path,dst_box:dst.id,dst_dir:dstdir});
  $('fbMsg').style.color=res.error?'var(--crash)':'var(--dim)';
  $('fbMsg').textContent=res.error?('✗ '+res.error):('✓ sent to '+dst.label+(res.via?(' ('+res.via+')'):''));
}
function openSettings(){
  $('settingsScrim').classList.add('open');
  setTab('ap');
  renderPresets();
}
function closeSettings(e){
  if(e&&e.target!==$('settingsScrim'))return;
  $('settingsScrim').classList.remove('open');
  if(sysTimer){clearInterval(sysTimer);sysTimer=null;}
  applyTheme(SETTINGS); renderSidebar(); // revert any unsaved live preview
}
function setTab(t){
  $('stApBtn').classList.toggle('active',t==='ap');
  $('stPreBtn').classList.toggle('active',t==='pre');
  $('stMonBtn').classList.toggle('active',t==='mon');
  $('stSysBtn').classList.toggle('active',t==='sys');
  $('stAp').style.display=t==='ap'?'':'none';
  $('stPre').style.display=t==='pre'?'':'none';
  $('stMon').style.display=t==='mon'?'':'none';
  $('stSys').style.display=t==='sys'?'':'none';
  if(sysTimer){clearInterval(sysTimer);sysTimer=null;}
  if(t==='pre'){renderPresetEditor();}
  if(t==='mon'){renderThresholds();}
  if(t==='sys'){loadSysInfo();loadSysJob();renderSysToggle();sysTimer=setInterval(()=>{loadSysInfo();loadSysJob();},4000);}
}
function presetRow(label,command){
  const row=document.createElement('div'); row.style.cssText='display:flex;gap:.4rem;align-items:center';
  const l=document.createElement('input'); l.className='pl'; l.placeholder='Label'; l.value=label||''; l.style.flex='1';
  const c=document.createElement('input'); c.className='pc'; c.placeholder='command'; c.value=command||''; c.style.cssText='flex:2;font-family:var(--mono)';
  const rm=document.createElement('button'); rm.className='danger'; rm.textContent='✕'; rm.style.width='auto'; rm.onclick=()=>row.remove();
  row.appendChild(l); row.appendChild(c); row.appendChild(rm);
  return row;
}
function renderPresetEditor(){
  const presets=(SETTINGS&&SETTINGS.console_presets)||[];
  const box=$('preList'); box.innerHTML='';
  (presets.length?presets:[{label:'',command:''}]).forEach(p=>box.appendChild(presetRow(p.label,p.command)));
  $('preMsg').textContent='';
}
function addPreset(){ $('preList').appendChild(presetRow('','')); }
async function savePresets(){
  const presets=[...$('preList').children].map(r=>({
    label:r.querySelector('.pl').value.trim(), command:r.querySelector('.pc').value.trim()
  })).filter(p=>p.label&&p.command);
  $('preMsg').style.color='var(--dim)'; $('preMsg').textContent='saving…';
  const d=await api('/api/settings','POST',{console_presets:presets});
  if(d.error){ $('preMsg').style.color='var(--crash)'; $('preMsg').textContent='✗ '+d.error; return; }
  SETTINGS=d.settings; renderPresetBar();
  $('preMsg').style.color='var(--dim)'; $('preMsg').textContent='✓ saved ('+presets.length+')';
}
let SELPRESET=null, SELACCENT='', SELBG='', SELBGDIM=0.6, SELDENSITY='', SELFONT='aquarius';
const ACCENT_SWATCHES=['#3ddc97','#5cc8ff','#ff7a45','#b388ff','#ff6f9c','#e8b53a','#39b8d6','#5fd17a','#ff5d5d','#e6e6e6'];
function renderPresets(){
  const t=SETTINGS.theme;
  SELPRESET=t.preset; SELACCENT=t.accent||''; SELBG=t.bg_image||'';
  SELBGDIM=(t.bg_dim==null?0.6:t.bg_dim); SELDENSITY=t.density||'';
  SELFONT=t.font||'aquarius';
  const fonts=SETTINGS.fonts||{};
  if(!fonts[SELFONT])SELFONT='aquarius';
  if($('fontSel'))$('fontSel').innerHTML=Object.keys(fonts).map(k=>`<option value="${k}"${k===SELFONT?' selected':''}>${esc(fonts[k].label||k)}</option>`).join('');
  const presets=SETTINGS.presets;
  $('presetRow').innerHTML=Object.keys(presets).map(k=>`
    <div class="chip ${k===SELPRESET?'sel':''}" data-k="${k}" onclick="pickPreset('${k}')">
      <span class="sw" style="background:${presets[k].accent}"></span>${k}</div>`).join('');
  $('accentHex').value=SELACCENT;
  $('accentPick').value=SELACCENT||presets[SELPRESET].accent;
  $('accentHex').oninput=()=>{SELACCENT=$('accentHex').value.trim();$('accentPick').value=SELACCENT||presets[SELPRESET].accent;previewTheme();};
  $('accentPick').oninput=()=>{SELACCENT=$('accentPick').value;$('accentHex').value=SELACCENT;previewTheme();};
  $('accentSwatches').innerHTML=ACCENT_SWATCHES.map(c=>`<span title="${c}" onclick="pickAccent('${c}')" style="width:22px;height:22px;border-radius:6px;cursor:pointer;background:${c};border:1px solid var(--line)"></span>`).join('');
  $('bgImage').value=SELBG;
  $('bgDim').value=Math.round(SELBGDIM*100); $('bgDimVal').textContent=Math.round(SELBGDIM*100)+'%';
  document.querySelectorAll('#densityRow input[name=density]').forEach(r=>{r.checked=(r.value===SELDENSITY);});
  const ui=SETTINGS.ui||{sidebar:'full',sidebar_side:'left'};
  SELSIDEBAR=ui.sidebar||'full'; SELSIDE=ui.sidebar_side||'left';
  document.querySelectorAll('#sidebarRow .chip').forEach(c=>c.classList.toggle('sel',c.dataset.sb===SELSIDEBAR));
  document.querySelectorAll('#sideOrientRow input[name=sbside]').forEach(r=>{r.checked=(r.value===SELSIDE);});
}
function pickAccent(c){ SELACCENT=c; $('accentHex').value=c; $('accentPick').value=c; previewTheme(); }
function pickPreset(k){
  SELPRESET=k;
  document.querySelectorAll('#presetRow .chip').forEach(c=>c.classList.toggle('sel',c.dataset.k===k));
  previewTheme();
}
function previewTheme(){ applyTheme({presets:SETTINGS.presets,fonts:SETTINGS.fonts,theme:{preset:SELPRESET,accent:SELACCENT,bg_image:SELBG,bg_dim:SELBGDIM,density:SELDENSITY,font:SELFONT}}); }
async function saveAppearance(){
  $('apMsg').textContent='saving…';
  const d=await api('/api/settings','POST',{theme:{preset:SELPRESET,accent:SELACCENT,bg_image:SELBG,bg_dim:SELBGDIM,density:SELDENSITY,font:SELFONT},ui:{sidebar:SELSIDEBAR,sidebar_side:SELSIDE}});
  if(d.error){$('apMsg').style.color='var(--crash)';$('apMsg').textContent='✗ '+d.error;return;}
  SETTINGS=d.settings; applyTheme(SETTINGS); renderSidebar();
  $('apMsg').style.color='var(--dim)';$('apMsg').textContent='✓ saved';
}

function fmtGB(n){return n?(n/1e9).toFixed(1)+' GB':'?';}
function fmtUp(s){if(!s)return'?';const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return `${d}d ${h}h ${m}m`;}
async function loadSysInfo(){
  const i=await api('/api/system/info');
  const memPct=i.mem_total?Math.round(100*i.mem_used/i.mem_total):0;
  const diskPct=i.disk_total?Math.round(100*i.disk_used/i.disk_total):0;
  const load=i.load?i.load.map(x=>x.toFixed(2)).join(' '):'?';
  $('sysInfo').innerHTML=`
    <div class="s"><div class="k">OS</div><div class="v" style="font-size:.8rem">${esc(i.os||'?')}</div></div>
    <div class="s"><div class="k">CPU cores</div><div class="v">${i.cpus??'?'}</div></div>
    <div class="s"><div class="k">Load avg</div><div class="v" style="font-size:.82rem">${load}</div></div>
    <div class="s"><div class="k">Memory</div><div class="v">${fmtGB(i.mem_used)} / ${fmtGB(i.mem_total)}</div><div class="b"><i style="width:${memPct}%"></i></div></div>
    <div class="s"><div class="k">Disk</div><div class="v">${fmtGB(i.disk_used)} / ${fmtGB(i.disk_total)}</div><div class="b"><i style="width:${diskPct}%"></i></div></div>
    <div class="s"><div class="k">Uptime</div><div class="v" style="font-size:.85rem">${fmtUp(i.uptime_sec)}</div></div>`;
}
function renderSysToggle(){
  const on=!!(SETTINGS&&SETTINGS.system_actions_enabled);
  $('sysEnable').checked=on;
  $('sysDanger').style.opacity=on?'1':'.45';
  $('sysDanger').style.pointerEvents=on?'auto':'none';
  const au=SETTINGS&&SETTINGS.autoupdate;
  if($('autoUpd')){ $('autoUpd').checked=!!(au&&au.enabled); $('autoUpd').disabled=!!(au&&au.state==='unavailable'); }
  refreshUpdateBadge();
}
async function refreshUpdateBadge(){
  const el=$('updAvail'); if(!el)return;
  let d; try{ d=await api('/api/update/check'); }catch(e){ return; }
  if(!d||!d.available){ el.style.display='none'; return; }
  el.style.cssText='display:inline-block;background:var(--accent,#3a6f5a);color:#fff;border-radius:8px;padding:.15rem .55rem;font-size:.75rem;font-weight:600';
  el.textContent='⬆ Update available · '+d.behind+' behind'+((d.latest&&d.latest!=='?')?(' ('+d.latest+')'):'');
  el.title='Current '+d.current+' → latest '+d.latest;
}
async function selfUpdate(btn){
  if(!confirm('Update the manager now (git pull + restart the web UI)? The dashboard will blink for a moment.'))return;
  const orig=btn.textContent; btn.disabled=true; btn.innerHTML='<span class="spin"></span> updating…';
  $('updMsg').style.color='var(--dim)'; $('updMsg').textContent='git pull + restart…';
  // Don't use api() here: once the update fires, the box restarts and the request
  // either drops ("Failed to fetch") or returns the proxy's HTML "box unreachable"
  // page (can't .json()). Both are the EXPECTED success path, not an error.
  let d=null;
  try{
    const r=await fetch('/api/selfupdate',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const t=await r.text(); try{ d=JSON.parse(t); }catch(_){ d=null; }
  }catch(e){ d=null; }
  btn.disabled=false; btn.textContent=orig;
  if(d&&d.error){ $('updMsg').style.color='var(--crash)'; $('updMsg').textContent='✗ '+d.error; return; }
  $('updMsg').style.color='var(--dim)';
  if(!d){ $('updMsg').textContent='✓ update triggered · the box is restarting — reload in a few seconds'; }
  else if(d.updated){ $('updMsg').textContent='✓ '+d.old+' → '+d.new+(d.restarted?' · restarting…':' · restart manually'); }
  else { $('updMsg').textContent='✓ already up to date ('+d.new+')'; }
}
async function toggleAutoupdate(cb){
  const enable=cb.checked; cb.disabled=true;
  $('updMsg').style.color='var(--dim)'; $('updMsg').textContent=enable?'enabling daily auto-update…':'disabling auto-update…';
  let d; try{ d=await api('/api/autoupdate','POST',{enable}); }catch(e){ d={error:String(e)}; }
  cb.disabled=false;
  if(!d||d.error){ cb.checked=!enable; $('updMsg').style.color='var(--crash)'; $('updMsg').textContent='✗ '+((d&&d.error)||'failed'); return; }
  if(SETTINGS)SETTINGS.autoupdate={enabled:d.enabled,state:d.state};
  cb.checked=!!d.enabled;
  $('updMsg').style.color='var(--dim)';
  $('updMsg').textContent=d.enabled?('✓ auto-update on ('+(d.schedule||'daily')+')'):'✓ auto-update off';
}
function dlBackup(){ window.location='/api/backup'; }
async function doRestore(btn){
  const f=$('restoreFile').files[0];
  if(!f){ $('bkpMsg').style.color='var(--crash)'; $('bkpMsg').textContent='choose a backup file first'; return; }
  if(!confirm('Restore configs from this backup? It overwrites the current instances + connected boxes (a timestamped copy is saved first). You may need to log in again.'))return;
  btn.disabled=true; $('bkpMsg').style.color='var(--dim)'; $('bkpMsg').textContent='restoring…';
  let bundle; try{ bundle=JSON.parse(await f.text()); }catch(e){ btn.disabled=false; $('bkpMsg').style.color='var(--crash)'; $('bkpMsg').textContent='not a valid backup file'; return; }
  let d; try{ d=await api('/api/restore','POST',bundle); }catch(e){ d={error:String(e)}; }
  btn.disabled=false;
  if(!d||d.error){ $('bkpMsg').style.color='var(--crash)'; $('bkpMsg').textContent='✗ '+((d&&d.error)||'failed'); return; }
  $('bkpMsg').style.color='var(--dim)'; $('bkpMsg').textContent='✓ restored '+((d.restored||[]).join(', '))+' — reloading…';
  setTimeout(()=>location.reload(),1200);
}
async function toggleSystem(){
  const on=$('sysEnable').checked;
  const d=await api('/api/settings','POST',{system_actions_enabled:on});
  if(d.settings)SETTINGS=d.settings;
  renderSysToggle();
}
async function loadSysJob(){
  const j=await api('/api/system/job');
  if(j&&j.status&&j.status!=='idle'){
    $('sysJob').textContent=`[${j.name} — ${j.status}]\n`+(j.output||'');
    const box=$('sysJob'); box.scrollTop=box.scrollHeight;
  }
}
async function sysAction(action){
  if(action==='reboot'&&!confirm('Reboot the VPS now? All proxies will drop and the manager will go offline until the host is back.'))return;
  if(action==='update'&&!confirm('Run apt-get update && upgrade now? This can take a few minutes.'))return;
  const d=await api('/api/system/'+action,'POST');
  if(d.error){$('sysJob').textContent='✗ '+d.error;return;}
  $('sysJob').textContent='['+action+'] '+(d.note||'started');
  if(action==='update')setTimeout(loadSysJob,800);
}

function openScan(){
  $('scanScrim').classList.add('open');
  loadScan();
}
function closeScan(e){
  if(e&&e.target!==$('scanScrim'))return;
  $('scanScrim').classList.remove('open');
}
async function loadScan(){
  $('scanMsg').textContent='scanning…';
  $('scanList').innerHTML='';
  let d;
  try{ d=await api('/api/scan'); }catch(err){ $('scanMsg').textContent='scan failed'; return; }
  const rows=d.sessions||[];
  $('scanMsg').textContent='';
  if(!rows.length){ $('scanList').innerHTML='<div class="hint">No unmanaged tmux sessions found.</div>'; return; }
  $('scanList').innerHTML=rows.map((r,idx)=>`
    <div class="scand ${r.likely_proxy?'likely':''}">
      <div class="si">
        <div class="sn">${esc(r.session)} ${r.likely_proxy?'<span class="tag">proxy?</span>':''}</div>
        <div class="sp" title="${esc(r.path)}">${esc(r.path||'(no path)')} <span style="color:#586675">[${esc(r.command)}]</span></div>
        <div class="sr">${esc(r.reason)} · launch: ${esc(r.suggested_launch)}</div>
      </div>
      <button class="go" id="adopt${idx}" onclick='adopt(${JSON.stringify(r.session)},this)'>Adopt</button>
    </div>`).join('');
}
async function adopt(session,btn){
  btn.disabled=true; btn.innerHTML='<span class="spin"></span>';
  const d=await api('/api/adopt','POST',{session});
  if(d.error){ btn.disabled=false; btn.textContent='Adopt'; $('scanMsg').textContent='✗ '+d.error; return; }
  await loadScan();
  refresh();
}

function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function jsq(s){return (s||'').replace(/\\/g,'\\\\').replace(/'/g,"\\'");}
function tick(){$('clock').textContent=new Date().toLocaleTimeString();syncAgo();}
setInterval(tick,1000);tick();

/* ---- share access (owner) ---- */
function openShares(){ $('shareScrim').classList.add('open'); $('shResult').style.display='none'; $('shMsg').textContent=''; shToggleAll(); loadShareBots(); loadShares(); loadAudit(); loadTunnel(); }
let _tunPoll=null, _instPoll=null;
async function tunGet(){ return await (await fetch('/api/share/tunnel')).json(); }
async function tunPost(payload){
  const r=await fetch('/api/share/tunnel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const d=await r.json(); d._ok=r.ok; return d;
}
/* full render of the Public-sharing card from a GET payload */
async function loadTunnel(){
  let d; try{ d=await tunGet(); }catch(e){ $('shTunBody').innerHTML='<div class="hint">Could not load sharing state.</div>'; return; }
  renderTunnel(d);
}
/* the card shown below the dropdown for the chosen provider */
function provCard(d){
  const provs=d.providers||[], active=d.provider;
  const ap=provs.find(p=>p.id===active)||{};
  const needsSetup = ap.installable && !ap.installed;
  const canEnable = (!ap.installable) || ap.installed;
  let h='<div class="provcard">';
  h+='<div class="pblurb">'+esc(ap.blurb||'')+'</div>';
  if(ap.installing){
    h+='<div class="setuprow"><span class="msg" id="shSetupMsg" style="flex:1;color:var(--dim)"><span class="spin"></span> setting up '+esc(ap.name)+'… downloading (this can take a minute)</span></div>';
  } else if(needsSetup){
    h+='<div class="setuprow"><span class="msg" id="shSetupMsg" style="flex:1;color:var(--dim)">ABM will install this for you — no extra steps, no root.</span><button id="shSetupBtn" onclick="installProvider(\''+esc(active)+'\',this)">⬇ Set up '+esc(ap.name)+'</button></div>';
  }
  if(ap.needs && ap.needs.length){
    const cur=ap.config||{};
    h+='<div class="provcfg">';
    for(const n of ap.needs){
      if(n.secret){
        const set=cur[n.key+'_set'];
        h+='<label class="pf">'+esc(n.label)+(set?' <span class="hint">(saved — leave blank to keep)</span>':'')+'<input id="pf_'+esc(n.key)+'" type="password" autocomplete="off" placeholder="'+(set?'••••••••':esc(n.placeholder||''))+'"></label>';
      }else{
        h+='<label class="pf">'+esc(n.label)+'<input id="pf_'+esc(n.key)+'" value="'+esc(cur[n.key]||'')+'" placeholder="'+esc(n.placeholder||'')+'"></label>';
      }
      if(n.help) h+='<div class="hint" style="margin:-.2rem 0 .2rem">'+esc(n.help)+'</div>';
    }
    h+='<div class="mbar"><span class="msg" id="shProvMsg" style="flex:1;color:var(--dim)"></span><button onclick="saveProvider(\''+esc(active)+'\',this)">Save settings</button></div></div>';
  }
  h+='<div id="shTunLogin" style="display:none"></div>';
  h+='<div class="mbar" style="align-items:center;margin-top:.5rem"><span class="msg" id="shTunStatus" style="flex:1;color:var(--dim)"></span><button id="shTunBtn" onclick="toggleTunnel(this)"'+(canEnable?'':' style="display:none"')+'></button></div>';
  h+='<div id="shTunUrl" style="display:none"></div>';
  h+='</div>';
  return h;
}
function renderTunnel(d){
  const body=$('shTunBody');
  if(!d.password_set){ if(_tunPoll){clearInterval(_tunPoll);_tunPoll=null;} body.innerHTML='<div class="hint" style="color:var(--warn)">Set a dashboard password first (Settings → Account) — public sharing needs a login in front of it.</div>'; return; }
  const provs=d.providers||[], active=d.provider;
  let h='<label class="pf" style="margin-bottom:.3rem">Method<select id="shProvSel" onchange="selectProvider(this.value)">';
  for(const p of provs) h+='<option value="'+esc(p.id)+'"'+(p.id===active?' selected':'')+'>'+esc(p.name)+'</option>';
  h+='</select></label>';
  h+=provCard(d);
  body.innerHTML=h;
  paintTunStatus(d);
  const ap=provs.find(p=>p.id===active);
  if(ap && ap.installing) watchInstall(active);   // keep polling until the download finishes
}
/* repaint just the status line + login link + enable button (poller-safe — won't wipe config inputs) */
function paintTunStatus(d){
  const st=$('shTunStatus'), btn=$('shTunBtn'), urlb=$('shTunUrl'), login=$('shTunLogin');
  if(!st) return;
  if(btn) btn.disabled=false;
  if(login){
    if(d.needs_login && d.auth_url){
      login.style.display='block';
      login.innerHTML='<div class="shurl" style="margin:.4rem 0"><b>One step left</b> — sign in to Tailscale: <a href="'+esc(d.auth_url)+'" target="_blank" rel="noopener">open the sign-in link</a>. It goes live automatically once you\'re signed in.</div>';
    } else if(d.needs_funnel && d.funnel_url){
      login.style.display='block';
      login.innerHTML='<div class="shurl" style="margin:.4rem 0"><b>One step left</b> — Funnel isn\'t enabled on your tailnet yet. <a href="'+esc(d.funnel_url)+'" target="_blank" rel="noopener">Enable Funnel</a> (one-time, in your Tailscale admin console), then it goes live automatically.</div>';
    } else login.style.display='none';
  }
  if(d.enabled && d.running && d.url){
    st.innerHTML='<span style="color:var(--acc)">● Public</span> — links use this address:';
    if(btn){ btn.style.display=''; btn.textContent='Turn off'; } urlb.style.display='block';
    urlb.innerHTML='<div style="display:flex;gap:.4rem;margin-top:.2rem"><input readonly value="'+esc(d.url)+'" style="flex:1;font-family:var(--mono);font-size:.74rem"><button onclick="navigator.clipboard&&navigator.clipboard.writeText(\''+jsq(d.url)+'\')">Copy</button></div>';
    if(_tunPoll){clearInterval(_tunPoll);_tunPoll=null;}
  } else if(d.enabled && !d.running){
    if(d.installing) st.innerHTML='<span class="spin"></span> setting up… downloading (this can take a minute)';
    else if(d.needs_login) st.innerHTML='<span class="spin"></span> waiting for Tailscale sign-in…';
    else if(d.needs_funnel) st.innerHTML='<span class="spin"></span> waiting for Funnel to be enabled…';
    else st.innerHTML=d.error?('<span style="color:var(--danger)">'+esc(d.error)+'</span>'):'<span class="spin"></span> starting… (a few seconds)';
    if(btn){ btn.style.display=''; btn.textContent='Turn off'; } urlb.style.display='none';
    if((!d.error || d.needs_login || d.needs_funnel || d.installing)){ if(!_tunPoll) _tunPoll=setInterval(pollTunStatus,1800); }
    else if(_tunPoll){ clearInterval(_tunPoll); _tunPoll=null; }
  } else {
    st.textContent='Off — links only work on your own (tunnel/localhost) connection.';
    if(btn) btn.textContent='Enable public sharing'; urlb.style.display='none';
    if(_tunPoll){clearInterval(_tunPoll);_tunPoll=null;}
  }
}
async function pollTunStatus(){ try{ paintTunStatus(await tunGet()); }catch(e){} }
async function installProvider(pid,btn){
  const m=$('shSetupMsg'); if(btn){ btn.disabled=true; btn.textContent='Setting up…'; }
  if(m) m.innerHTML='<span class="spin"></span> starting download…';
  let d; try{ d=await tunPost({action:'install',provider:pid}); }catch(e){ d={_ok:false}; }
  if(!d._ok){ if(m) m.innerHTML='<span style="color:var(--danger)">'+esc(d.error||'setup failed')+'</span>'; if(btn){ btn.disabled=false; btn.textContent='Retry set up'; } return; }
  renderTunnel(d);          // install runs in the background now; poll until it finishes
  watchInstall(pid);
}
/* poll while a provider's background install runs; re-render when it lands (or errors) */
function watchInstall(pid){
  if(_instPoll) clearInterval(_instPoll);
  _instPoll=setInterval(async ()=>{
    let d; try{ d=await tunGet(); }catch(e){ return; }
    const ap=(d.providers||[]).find(p=>p.id===pid);
    if(!ap || !ap.installing){ clearInterval(_instPoll); _instPoll=null; if(d.provider===pid) renderTunnel(d); }
  }, 2000);
}
async function selectProvider(pid){
  const d=await tunPost({action:'select',provider:pid});
  if(!d._ok){ $('shTunStatus')&&($('shTunStatus').innerHTML='<span style="color:var(--danger)">'+esc(d.error||'failed')+'</span>'); return; }
  renderTunnel(d);
  // the user deliberately chose this provider — set it up now if it needs installing
  const ap=(d.providers||[]).find(p=>p.id===pid);
  if(ap && ap.installable && !ap.installed) installProvider(pid, $('shSetupBtn'));
}
async function saveProvider(pid,btn){
  const prov=(await tunGet()).providers.find(p=>p.id===pid);
  const cfg={};
  (prov.needs||[]).forEach(n=>{ const el=$('pf_'+n.key); if(el) cfg[n.key]=el.value; });
  btn.disabled=true; const m=$('shProvMsg'); if(m) m.innerHTML='<span class="spin"></span> saving…';
  const d=await tunPost({action:'config',provider:pid,config:cfg});
  btn.disabled=false;
  if(!d._ok){ if(m) m.innerHTML='<span style="color:var(--danger)">'+esc(d.error||'failed')+'</span>'; return; }
  if(m) m.textContent='Saved.';
  renderTunnel(d);
}
async function toggleTunnel(btn){
  const on=btn.textContent.indexOf('off')>=0;
  btn.disabled=true; $('shTunStatus').innerHTML='<span class="spin"></span> '+(on?'stopping…':'starting…');
  const d=await tunPost({action:'enable',enable:!on});
  if(!d._ok){ $('shTunStatus').innerHTML='<span style="color:var(--danger)">'+esc(d.error||'failed')+'</span>'; btn.disabled=false; return; }
  renderTunnel(d);
}
async function loadAudit(){
  try{ const d=await (await fetch('/api/shares/audit')).json(); const a=d.audit||[];
    $('shAudit').innerHTML=a.length?a.map(e=>'<div>'+new Date(e.ts*1000).toLocaleString()+' · <b>'+esc(e.target||'')+'</b> · '+esc((e.path||'').replace('/api/instances/'+(e.target||''),''))+' <span class="hint">['+esc(e.tier||'')+']</span></div>').join(''):'<span class="hint">No guest actions recorded.</span>';
  }catch(e){ $('shAudit').innerHTML='<span class="hint">Could not load.</span>'; }
}
function closeShares(e){ if(!e||e.target.id==='shareScrim'){ $('shareScrim').classList.remove('open'); if(_tunPoll){clearInterval(_tunPoll);_tunPoll=null;} if(_instPoll){clearInterval(_instPoll);_instPoll=null;} } }
function shToggleAll(){ const on=$('shAll').checked; $('shBotsWrap').style.opacity=on?.4:1; $('shBotsWrap').style.pointerEvents=on?'none':'auto'; }
async function loadShareBots(){
  const w=$('shBots');
  try{ const d=await (await fetch('/api/instances')).json();
    w.innerHTML=(d.instances||[]).map(i=>'<label class="shbot"><input type="checkbox" value="'+esc(i.name)+'"'+(i.node?' data-node="'+esc(i.node)+'"':'')+' style="width:auto"> '+esc(i.name)+(i.node?' <span class="hint">@'+esc(i.node)+'</span>':'')+'</label>').join('')||'<span class="hint">No bots.</span>';
  }catch(e){ w.innerHTML='<span class="hint">Could not load bots.</span>'; }
}
async function createShare(btn){
  const all=$('shAll').checked;
  const targets=[...$('shBots').querySelectorAll('input:checked')].map(c=>({node:c.dataset.node||null,name:c.value}));
  if(!all && !targets.length){ $('shMsg').textContent='Pick at least one bot, or “All”.'; return; }
  const cap=document.querySelector('input[name=shcap]:checked').value;
  const ttl=$('shTtl').value;
  btn.disabled=true; $('shMsg').textContent='Creating…';
  try{
    const r=await fetch('/api/shares',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({label:$('shLabel').value,all:all,targets:targets,capability:cap,ttl_days:ttl?Number(ttl):null})});
    const d=await r.json();
    if(!r.ok||!d.ok){ $('shMsg').textContent=d.error||'failed'; return; }
    $('shMsg').textContent='';
    $('shResult').style.display='block';
    const priv=/\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0)(:|\/)/.test(d.url||'');
    const warn=priv?'<div class="hint" style="color:var(--warn);margin-top:.3rem">⚠ This link points at your private address, so only you can open it. Enable <b>Public sharing</b> above, then create the link again.</div>':'';
    $('shResult').innerHTML='<div class="shurl"><div style="color:var(--acc);font-size:.78rem;font-weight:600">Link created — copy it now, it won’t be shown again:</div>'+
      '<div style="display:flex;gap:.4rem;margin-top:.35rem"><input id="shUrl" readonly value="'+esc(d.url)+'" style="flex:1;font-family:var(--mono);font-size:.76rem"><button onclick="shCopy()">Copy</button></div>'+warn+'</div>';
    $('shLabel').value=''; loadShares();
  }catch(e){ $('shMsg').textContent='error'; }
  finally{ btn.disabled=false; }
}
function shCopy(){ const i=$('shUrl'); if(!i)return; i.select(); try{ navigator.clipboard.writeText(i.value); }catch(e){ try{document.execCommand('copy');}catch(_){} } }
async function loadShares(){
  try{ const d=await (await fetch('/api/shares')).json(); const list=d.shares||[];
    $('shList').innerHTML=list.length?list.map(s=>{
      const scope=s.all?'all local bots':((s.targets||[]).map(t=>t.name+(t.node?'@'+t.node:'')).join(', ')||'—');
      const exp=s.expires?new Date(s.expires*1000).toLocaleDateString():'never';
      const stc=({active:'var(--acc)',expired:'var(--warn)',revoked:'var(--danger)'})[s.status]||'var(--dim)';
      return '<div class="shrow"><div style="flex:1;min-width:0"><b>'+esc(s.label)+'</b> <span class="badge" style="border-color:'+stc+';color:'+stc+'">'+s.status+'</span>'+
        '<div class="hint">'+esc(scope)+' · '+esc(s.capability)+' · expires '+exp+'</div></div>'+
        (s.status==='active'?'<button class="danger" onclick="revokeShare(\''+esc(s.id)+'\',this)">Revoke</button>':'')+'</div>';
    }).join(''):'<span class="hint">No links yet.</span>';
  }catch(e){ $('shList').innerHTML='<span class="hint">Could not load.</span>'; }
}
async function revokeShare(id,btn){ btn.disabled=true; try{ await fetch('/api/shares/revoke',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})}); }catch(e){} loadShares(); }
async function revokeAllShares(btn){ if(!confirm('Revoke ALL share links? Anyone using one loses access immediately.'))return; btn.disabled=true; try{ await fetch('/api/shares/revoke_all',{method:'POST'}); }catch(e){} btn.disabled=false; loadShares(); }

/* ---- users & access (owner/admin) ---- */
const ROLES=[['view','View'],['operate','Operate'],['config','Config'],['admin','Admin']];
let _usrBots=[];
function roleRadiosHtml(grp){
  return '<div style="display:flex;gap:.8rem;align-items:center;flex-wrap:wrap;font-size:.82rem"><span class="hint">Role</span>'+
    ROLES.map(r=>'<label class="rrow"><input type="radio" name="'+grp+'" value="'+r[0]+'"'+(r[0]==='view'?' checked':'')+' style="width:auto" onchange="syncRole(\''+grp+'\')"> '+r[1]+'</label>').join('')+
    '<span class="hint" style="flex-basis:100%;margin:.1rem 0 0">Admin = full control (a second owner). View/Operate/Config are scoped to the bots you pick.</span></div>';
}
function scopeHtml(idp){
  return '<div id="'+idp+'Wrap"><label class="rrow"><input type="checkbox" id="'+idp+'All" style="width:auto" onchange="syncScope(\''+idp+'\')"> All bots (current + future)</label>'+
    '<div id="'+idp+'BotsWrap"><div style="color:var(--dim);font-size:.8rem;margin:.25rem 0">Bots</div><div id="'+idp+'Bots" class="shbots"><span class="hint">loading…</span></div></div></div>';
}
function getRole(grp){ const el=document.querySelector('input[name='+grp+']:checked'); return el?el.value:'view'; }
function syncRole(grp){
  const idp = grp==='roleA'?'usA':'usI';
  const admin = getRole(grp)==='admin';
  const all=$(idp+'All'); if(all){ if(admin) all.checked=true; all.disabled=admin; $(idp+'Wrap').style.opacity=admin?.55:1; syncScope(idp); }
}
function syncScope(idp){ const on=$(idp+'All').checked; const w=$(idp+'BotsWrap'); if(w){ w.style.opacity=on?.4:1; w.style.pointerEvents=on?'none':'auto'; } }
function gatherScope(idp){
  return {all:$(idp+'All').checked, targets:[...$(idp+'Bots').querySelectorAll('input:checked')].map(c=>({node:c.dataset.node||null,name:c.value}))};
}
function botCheckboxes(){ return _usrBots.map(i=>'<label class="shbot"><input type="checkbox" value="'+esc(i.name)+'"'+(i.node?' data-node="'+esc(i.node)+'"':'')+' style="width:auto"> '+esc(i.name)+(i.node?' <span class="hint">@'+esc(i.node)+'</span>':'')+'</label>').join('')||'<span class="hint">No bots.</span>'; }
async function openUsers(){
  $('usersScrim').classList.add('open'); $('usrMsg').textContent=''; $('invMsg').textContent=''; $('invResult').style.display='none';
  $('usrRole_add').innerHTML=roleRadiosHtml('roleA'); $('usrScope_add').innerHTML=scopeHtml('usA');
  $('usrRole_inv').innerHTML=roleRadiosHtml('roleI'); $('usrScope_inv').innerHTML=scopeHtml('usI');
  await loadUserBots(); syncRole('roleA'); syncRole('roleI'); loadUsers();
}
function closeUsers(e){ if(!e||e.target.id==='usersScrim') $('usersScrim').classList.remove('open'); }
async function loadUserBots(){
  try{ const d=await (await fetch('/api/instances')).json(); _usrBots=d.instances||[]; }catch(e){ _usrBots=[]; }
  ['usA','usI'].forEach(idp=>{ const el=$(idp+'Bots'); if(el) el.innerHTML=botCheckboxes(); });
}
async function addUser(btn){
  const username=$('usrName').value.trim(), password=$('usrPass').value, role=getRole('roleA'), sc=gatherScope('usA');
  if(!username){ $('usrMsg').textContent='enter a username'; return; }
  if(!password||password.length<6){ $('usrMsg').textContent='password must be at least 6 characters'; return; }
  if(role!=='admin' && !sc.all && !sc.targets.length){ $('usrMsg').textContent='pick at least one bot, or All'; return; }
  btn.disabled=true; $('usrMsg').textContent='Adding…';
  const d=await api('/api/users','POST',{username,password,role,all:sc.all,targets:sc.targets});
  btn.disabled=false;
  if(d.error){ $('usrMsg').innerHTML='<span style="color:var(--crash)">'+esc(d.error)+'</span>'; return; }
  $('usrMsg').textContent='Added '+username+'.'; $('usrName').value=''; $('usrPass').value=''; loadUsers();
}
async function createInvite(btn){
  const role=getRole('roleI'), sc=gatherScope('usI'), username=$('invName').value.trim(), ttl=$('invTtl').value;
  if(role!=='admin' && !sc.all && !sc.targets.length){ $('invMsg').textContent='pick at least one bot, or All'; return; }
  btn.disabled=true; $('invMsg').textContent='Creating…';
  const d=await api('/api/invites','POST',{role,all:sc.all,targets:sc.targets,username:username||null,ttl_days:ttl?Number(ttl):null});
  btn.disabled=false;
  if(d.error){ $('invMsg').innerHTML='<span style="color:var(--crash)">'+esc(d.error)+'</span>'; return; }
  $('invMsg').textContent='';
  const r=$('invResult'); r.style.display='block';
  const priv=/\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0)(:|\/)/.test(d.url||'');
  const warn=priv?'<div class="hint" style="color:var(--warn);margin-top:.3rem">⚠ This link points at your private address, so the invitee can\'t open it. Turn on <b>Public sharing</b> (the Share panel) first, then create the invite again.</div>':'';
  r.innerHTML='<div class="shurl"><div style="font-size:.74rem;color:var(--dim);margin-bottom:.3rem">One-time invite link — copy it now, it won\'t be shown again:</div><div style="display:flex;gap:.4rem"><input readonly value="'+esc(d.url)+'" style="flex:1;font-family:var(--mono);font-size:.74rem"><button onclick="copyText(\''+jsq(d.url)+'\',this)">Copy</button></div>'+warn+'</div>';
  loadUsers();
}
function scopeSummary(u){ return u.all?'all bots':((u.targets||[]).map(t=>t.name).join(', ')||'no bots'); }
let _usersData={users:[],modules:[]};
async function loadUsers(){
  let d; try{ d=await (await fetch('/api/users')).json(); }catch(e){ return; }
  _usersData=d;
  const us=d.users||[];
  $('usrList').innerHTML = us.length? us.map(u=>{
    const opts=ROLES.map(r=>'<option value="'+r[0]+'"'+(u.role===r[0]?' selected':'')+'>'+r[1]+'</option>').join('');
    const last=u.last_login?('last in '+new Date(u.last_login*1000).toLocaleDateString()):'never signed in';
    const dis=u.disabled?'<span style="color:var(--crash)">disabled</span> · ':'';
    const restricted=(u.role!=='admin' && u.perms)?' · <span style="color:var(--acc)">custom perms</span>':'';
    const permsBtn=(u.role==='operate'||u.role==='config')?'<button onclick="openPerms(\''+u.id+'\')">Permissions</button>':'';
    return '<div class="shrow" style="flex-wrap:wrap;gap:.4rem"><b style="min-width:6rem">'+esc(u.username)+'</b>'+
      '<select onchange="changeRole(\''+u.id+'\',this.value)" style="width:auto">'+opts+'</select>'+
      '<span class="hint" style="flex:1;min-width:8rem">'+esc(scopeSummary(u))+' · '+dis+last+restricted+'</span>'+
      permsBtn+
      '<button onclick="resetPw(\''+u.id+'\',\''+jsq(u.username)+'\')">Reset pw</button>'+
      '<button onclick="toggleDisable(\''+u.id+'\','+(u.disabled?'false':'true')+')">'+(u.disabled?'Enable':'Disable')+'</button>'+
      '<button class="danger" onclick="delUser(\''+u.id+'\',\''+jsq(u.username)+'\')">Delete</button></div>';
  }).join('') : '<span class="hint">No users yet. Add one above, or send an invite link.</span>';
  const invs=d.invites||[];
  $('invList').innerHTML = invs.length? invs.map(i=>{
    const exp=i.expires?('expires '+new Date(i.expires*1000).toLocaleDateString()):'never expires';
    return '<div class="shrow"><span style="flex:1">'+esc(i.role)+' · '+esc(scopeSummary(i))+(i.username?(' · for '+esc(i.username)):'')+' <span class="hint">('+exp+')</span></span><button class="danger" onclick="revokeInvite(\''+i.id+'\')">Revoke</button></div>';
  }).join('') : '<span class="hint">None.</span>';
}
async function changeRole(id,role){ const d=await api('/api/users/'+id,'POST',{role}); if(d&&d.error)alert(d.error); loadUsers(); }
async function toggleDisable(id,dis){ const d=await api('/api/users/'+id,'POST',{disabled:dis}); if(d&&d.error)alert(d.error); loadUsers(); }
async function delUser(id,name){ if(!confirm('Delete user "'+name+'"? They lose access immediately.'))return; const d=await api('/api/users/'+id+'/delete','POST',{}); if(d&&d.error)alert(d.error); loadUsers(); }
async function resetPw(id,name){ const pw=prompt('New password for "'+name+'" (min 6 chars):'); if(!pw)return; const d=await api('/api/users/'+id+'/password','POST',{password:pw}); if(d&&d.error)alert(d.error); else alert('Password reset — their other sessions were signed out.'); }
async function revokeInvite(id){ const d=await api('/api/invites/'+id+'/revoke','POST',{}); if(d&&d.error)alert(d.error); loadUsers(); }

/* ---- per-user permissions editor ---- */
const CAT_LABELS={control:'Control & automation',combat:'Combat',survival:'Survival',connection:'Connection & queue',privacy:'Privacy & safety',automation:'Automation',diagnostics:'Diagnostics'};
let _permsUid=null;
function openPerms(uid){
  const u=(_usersData.users||[]).find(x=>x.id===uid); if(!u) return;
  _permsUid=uid;
  const mods=_usersData.modules||[]; const eff=u.effective||{modules:{}};
  const cfgTier=(u.role==='config');
  $('permsTitle').textContent='Permissions — '+u.username+' ('+u.role+')';
  $('permsNote').innerHTML='Tick exactly what this user can do. Unticked = blocked (enforced on the server, not just hidden). '+
    (cfgTier?'':'<b>Config editing needs the Config role</b> — promote them to grant it.');
  $('permsMsg').textContent='';
  let h='';
  // global grants
  h+='<div class="provcfg" style="gap:.5rem"><div class="lcSub2">Access</div>'+
     pToggle('pConsole','Free-form console (type any command)', eff.console)+
     pToggle('pLifecycle','Start / stop / restart bots', eff.lifecycle)+
     '<label class="rrow" style="margin-top:.2rem"><input type="checkbox" id="pUseAll" style="width:auto" onchange="permMasterSync()"> Use ALL modules (incl. ones added later)</label>'+
     '<label class="rrow"><input type="checkbox" id="pCfgAll"'+(cfgTier?'':' disabled')+' style="width:auto" onchange="permMasterSync()"> Configure ALL modules</label>'+
     '</div>';
  // per-module matrix grouped by category
  const cats={}; mods.forEach(m=>{ (cats[m.cat]=cats[m.cat]||[]).push(m); });
  h+='<div id="pModWrap" style="margin-top:.5rem;max-height:46vh;overflow:auto">';
  Object.keys(cats).forEach(cat=>{
    h+='<div class="lcSub2" style="margin-top:.5rem">'+esc(CAT_LABELS[cat]||cat)+'</div>';
    h+='<div style="display:flex;flex-direction:column;gap:.15rem">';
    h+='<div class="permhdr"><span style="flex:1"></span><span class="permcol">use</span><span class="permcol">config</span></div>';
    cats[cat].forEach(m=>{
      const e=eff.modules[m.id]||{};
      h+='<div class="permrow"><span style="flex:1">'+esc(m.name)+'</span>'+
         '<span class="permcol"><input type="checkbox" class="pUse" data-id="'+m.id+'"'+(e.use?' checked':'')+'></span>'+
         '<span class="permcol"><input type="checkbox" class="pCfg" data-id="'+m.id+'"'+(e.config?' checked':'')+(cfgTier?'':' disabled')+'></span></div>';
    });
    h+='</div>';
  });
  h+='</div>';
  $('permsBody').innerHTML=h;
  // initialise master checkboxes from effective use_all/config_all then sync disabled state
  $('pUseAll').checked=!!eff.use_all; $('pCfgAll').checked=!!eff.config_all;
  permMasterSync();
  $('permsScrim').classList.add('open');
}
function pToggle(id,label,on){ return '<label class="rrow"><input type="checkbox" id="'+id+'"'+(on?' checked':'')+' style="width:auto"> '+esc(label)+'</label>'; }
function permMasterSync(){
  const ua=$('pUseAll').checked, ca=$('pCfgAll').checked;
  [].forEach.call(document.querySelectorAll('.pUse'),function(c){ c.disabled=ua||ca; if(ua||ca)c.checked=true; });
  [].forEach.call(document.querySelectorAll('.pCfg'),function(c){ if(c.dataset.lock)return; c.disabled=ca|| $('pCfgAll').disabled; if(ca)c.checked=true; });
}
function closePerms(e){ if(!e||e.target.id==='permsScrim') $('permsScrim').classList.remove('open'); }
function gatherPerms(){
  const modules={};
  [].forEach.call(document.querySelectorAll('.pUse'),function(c){ modules[c.dataset.id]=modules[c.dataset.id]||{}; modules[c.dataset.id].use=c.checked; });
  [].forEach.call(document.querySelectorAll('.pCfg'),function(c){ modules[c.dataset.id]=modules[c.dataset.id]||{}; modules[c.dataset.id].config=c.checked; });
  return {use_all:$('pUseAll').checked, config_all:$('pCfgAll').checked,
          console:$('pConsole').checked, lifecycle:$('pLifecycle').checked, modules};
}
async function savePerms(){
  if(!_permsUid)return; $('permsMsg').textContent='Saving…';
  const d=await api('/api/users/'+_permsUid,'POST',{perms:gatherPerms()});
  if(d&&d.error){ $('permsMsg').innerHTML='<span style="color:var(--crash)">'+esc(d.error)+'</span>'; return; }
  $('permsScrim').classList.remove('open'); loadUsers();
}
async function resetPerms(){
  if(!_permsUid)return; if(!confirm('Reset to the role default (full access within the role)?'))return;
  const d=await api('/api/users/'+_permsUid,'POST',{perms:null});
  if(d&&d.error){ alert(d.error); return; }
  $('permsScrim').classList.remove('open'); loadUsers();
}

/* ---- principal gating (owner vs scoped guest) ---- */
async function applyPrincipal(){
  let d; try{ d=await (await fetch('/api/authstatus')).json(); }catch(e){ return; }
  const p=d.principal||'owner';
  // a named non-admin user is gated exactly like a guest link (same scope/capability tiers);
  // an admin user is owner-equivalent (full UI). Anonymous guests unchanged.
  const scoped = (p==='guest') || (p==='user' && !d.is_admin);
  const b=$('guestBadge');
  if(scoped){
    const cap=d.capability||'view';
    document.body.classList.add('guest','guest-'+cap);
    if(b){ b.style.display='inline-flex'; b.textContent=(p==='user')?(d.username+' · '+(d.role||cap)):('Guest · '+cap); }
    // belt-and-suspenders: hide any owner-only control the layout built without the class
    document.querySelectorAll('button[onclick]').forEach(el=>{
      if(/open(Settings|Connection|Boxes|Files|Proxies|Scan|Deploy|Shares|Users)\(|bulk\(/.test(el.getAttribute('onclick')||'')) el.classList.add('owner-only');
    });
  } else if(p==='user' && d.is_admin && b){
    b.style.display='inline-flex'; b.textContent=d.username+' · admin';
  }
}

loadSettings();
applyPrincipal();
refresh();
setInterval(refresh,3000);
// refresh the instant the tab becomes visible / regains focus, so you never
// stare at stale cards after switching back to the dashboard
document.addEventListener('visibilitychange',()=>{ if(!document.hidden) refresh(); });
window.addEventListener('focus',()=>refresh());
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
