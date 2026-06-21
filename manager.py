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
import random
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

__version__ = "1.3.1"

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
    s.setdefault("system_actions_enabled", False)
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
        # restart the system service so the new manager.py is loaded; needs sudo (same as
        # other system actions). The CLI/timer is a separate process, so this is safe.
        r = subprocess.run(["sudo", "-n", "systemctl", "restart", service],
                           capture_output=True, text=True)
        out["restarted"] = (r.returncode == 0)
        if r.returncode != 0:
            out["restart_error"] = (r.stderr.strip() or r.stdout.strip()
                                    or "sudo systemctl restart failed")
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

_FS_MAX_READ = 1024 * 1024   # 1 MB editable-file ceiling


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
    autostart defaults True so a VPS reboot relaunches the bot via the boot unit."""
    name = (name or "").strip()
    if not VALID_NAME.match(name):
        raise ValueError("name may contain only letters, digits, '.', '_' and '-'")
    cfg = load_config(cfg_path)
    if name in cfg["by_name"]:
        raise ValueError(f"instance '{name}' already exists")
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
}
HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

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
        },
        "system_actions_enabled": bool(s.get("system_actions_enabled", False)),
        "console_presets": presets if isinstance(presets, list) else DEFAULT_CONSOLE_PRESETS,
        "thresholds": {**DEFAULT_THRESHOLDS, **(s.get("thresholds") or {})},
        "base_dir": _base_dir(cfg),
        "presets": THEME_PRESETS,
        "webshare_saved": bool(s.get("webshare", {}).get("token")),
        "autoupdate": autoupdate_status(),
        # non-blocking cached snapshot; the UI refreshes it via /api/update/check
        "update": _UPDATE_CHECK["data"] or {"state": "checking"},
    }


def save_settings(cfg, theme=None, system_actions_enabled=None, console_presets=None,
                  thresholds=None):
    s = cfg["raw"].setdefault("settings", {})
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
    save_config(cfg)
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


def _new_session(gen=0):
    tok = secrets.token_urlsafe(32)
    _SESSIONS[tok] = {"exp": time.time() + SESSION_TTL, "gen": gen}
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


def fleet_aggregate(cfg, timeout=8):
    """The controller + every node, summarized for the Fleet view. Never raises
    per-row — unreachable boxes are reported as such."""
    rows = []
    try:
        insts = cfg["instances"]
        rows.append({"name": CONTROLLER_ROW, "controller": True, "reachable": True,
                     "bots": len(insts),
                     "running": sum(1 for i in insts if instance_status(i) == "running"),
                     "host": _sysinfo()})
    except Exception as e:
        rows.append({"name": CONTROLLER_ROW, "controller": True, "reachable": False,
                     "error": str(e), "bots": 0, "running": 0})
    for n in load_nodes()["nodes"]:
        row = node_public_view(n, TUNNELS)
        row["controller"] = False
        try:
            inst = node_request(n, "GET", "/api/instances", timeout=timeout).get("instances", [])
            row["reachable"] = True
            row["bots"] = len(inst)
            row["running"] = sum(1 for i in inst if i.get("status") == "running")
            try:
                row["host"] = node_request(n, "GET", "/api/system/info", timeout=timeout)
            except Exception:
                row["host"] = None
        except Exception as e:
            row.update(reachable=False, error=str(e), bots=0, running=0)
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
            try:
                self._procs[name] = subprocess.Popen(
                    self._ssh_cmd(node), stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            except Exception:
                self._procs.pop(name, None)

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
    <option value="">★ This box (controller)</option>
  </select>
  <span id="abmNodeWhich" style="opacity:.5;font-weight:400"></span>
  <span style="flex:1"></span>
  <a href="#" onclick="abmSelectNode('');return false" style="color:#7fb0d8;text-decoration:none;font-weight:400">controller home</a>
</div>
<script>
window.ABM_CURRENT_NODE="__ABM_NODE__";
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
    var html='<option value="">★ This box (controller)</option>';
    (d.nodes||[]).forEach(function(n){
      var off=(n.tunnel&&n.tunnel.alive)?'':' (offline)';
      html+='<option value="'+abmEsc(n.name)+'"'+(n.name===cur?' selected':'')+'>'+abmEsc(n.name)+off+'</option>';
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

    def _auth_ok(self, cfg):
        if not self._auth_required(cfg):
            return True
        tok = self._cookie_token()
        if tok and _session_valid(tok, session_epoch(cfg)):
            return True
        # legacy basic-auth fallback (e.g. behind a tunnel without a set password)
        if ABM_USER and ABM_PASS:
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    u, p = base64.b64decode(h[6:]).decode().split(":", 1)
                    if u == ABM_USER and p == ABM_PASS:
                        return True
                except Exception:
                    pass
        return False

    def _client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    def _set_session_cookie(self, token, clear=False):
        if clear:
            self.send_header("Set-Cookie",
                             "abm_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        else:
            self.send_header("Set-Cookie",
                             f"abm_session={token}; Path=/; HttpOnly; SameSite=Strict; "
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
        return (path in ("/logout", "/api/authstatus")
                or path.startswith("/api/node/")     # /api/node/select
                or path.startswith("/api/nodes")     # /api/nodes, .../remove, .../do*
                or path.startswith("/api/fleet/"))

    def _inject_switcher_str(self, text, current):
        if "<body>" not in text or "abmNodeBar" in text:
            return text
        bar = SWITCHER_BAR.replace("__ABM_NODE__", html.escape(current or "", quote=True))
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
                    rbody.decode("utf-8", "replace"), node["name"]).encode("utf-8")
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
        body = self._inject_switcher_str(page, node["name"]).encode("utf-8")
        self.send_response(502)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---- routing ----
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)
        cfg = self._cfg()

        # whether auth is on, expose it so the login page can decide what to show
        if path == "/api/authstatus":
            return self._json({"required": self._auth_required(cfg),
                               "authed": self._auth_ok(cfg),
                               "needs_setup": self._needs_setup(cfg)})

        # First run: the setup wizard owns the UI until a login is created
        # (or the user explicitly skips to run open on localhost).
        if self._needs_setup(cfg):
            if path in ("/", "/index.html", "/login", "/setup"):
                return self._html(SETUP_PAGE)
            return self._json({"error": "setup required"}, 401)

        if not self._auth_ok(cfg):
            # unauthenticated: only the login page and its check are reachable
            if path in ("/", "/index.html", "/login"):
                return self._html(LOGIN_PAGE)
            return self._json({"error": "unauthorized"}, 401)

        # controller: a selected node proxies the whole UI through its SSH tunnel
        node = self._selected_node()
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

        if path == "/api/system/job":
            return self._json(SYS_JOB.snapshot())

        if path == "/api/deploy/job":
            return self._json(DEPLOY_JOB.snapshot())

        if path == "/api/instances":
            cfg = self._cfg()
            out = []
            for i in cfg["instances"]:
                st = instance_status(i)
                row = {
                    "name": i["name"],
                    "dir": i["dir"],
                    "launch_cmd": i["launch_cmd"],
                    "status": st,
                    "autostart": bool(i.get("autostart")),
                    "limits": limits_view(i),
                }
                if st == "running":
                    try:
                        row["stats"] = instance_stats(i)
                    except Exception:
                        row["stats"] = None
                out.append(row)
            return self._json({"instances": out})

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
            ok = verify_password(cfg, user, pw) if auth_configured(cfg) else (
                bool(ABM_USER) and user == ABM_USER and pw == ABM_PASS)
            if not ok:
                _record_fail(ip)
                return self._json({"error": "invalid credentials"}, 401)
            return self._json({"ok": True}, cookie=_new_session(session_epoch(cfg)))

        if not self._auth_ok(cfg):
            return self._json({"error": "unauthorized"}, 401)

        # controller: proxy mutating requests to the selected node too (skip switcher controls)
        node = self._selected_node()
        if node is not None and not self._is_switcher_path(path):
            return self._proxy_to_node(node)

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
                                    thresholds=p.get("thresholds"))
                return self._json({"ok": True, "settings": out})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except json.JSONDecodeError as e:
                return self._json({"error": f"invalid request: {e}"}, 400)

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
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        if TUNNELS is not None:
            TUNNELS.stop()


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
        en = True if args.enable_system else (False if args.disable_system else None)
        try:
            out = save_settings(cfg, theme=theme or None, system_actions_enabled=en)
            print(f"theme preset:   {out['theme']['preset']}")
            print(f"theme accent:   {out['theme']['accent'] or '(preset default)'}")
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
.row{display:flex;gap:.4rem;flex-wrap:wrap}
.row button{flex:1;min-width:64px}
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
pre.log{font-family:var(--mono);font-size:.74rem;line-height:1.5;white-space:pre-wrap;word-break:break-word;
  background:#06090c;border:1px solid var(--line);border-radius:10px;padding:.9rem;margin:0;color:#b9c7d2}
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
  width:min(520px,92vw);background:var(--panel);border:1px solid var(--line);
  border-radius:16px;padding:1.3rem 1.4rem;display:flex;flex-direction:column;gap:.7rem;
  box-shadow:0 30px 80px #000a}
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
</style>
</head>
<body>
<header>
  <div class="brand"><span class="dot"></span>Aquarius Bot Manager <small class="ver">v__ABM_VERSION__</small> <small id="clock"></small></div>
  <div class="bulk">
    <button onclick="openSettings()">⚙ Settings</button>
    <button onclick="openConnection()">🔗 Connect</button>
    <button onclick="openBoxes()">🖥 Boxes</button>
    <button onclick="openFiles()">📁 Files</button>
    <button onclick="openProxies()">🌐 Proxies</button>
    <button onclick="openScan()">⟲ Scan existing</button>
    <button class="go" onclick="openDeploy()">🚀 Deploy</button>
    <button onclick="openAdd()">+ New instance</button>
    <button class="go" onclick="bulk('start')">▶ Start all</button>
    <button class="warn" onclick="bulk('restart')">⟳ Restart all</button>
    <button class="danger" onclick="bulk('stop')">■ Stop all</button>
    <button class="danger" onclick="location.href='/logout'">⎋ Log out</button>
  </div>
</header>
<main>
  <div class="meta" id="meta">loading…</div>
  <div class="hoststrip" id="hostStrip"></div>
  <div class="grid" id="grid"></div>
</main>

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

    <div id="proxList" style="max-height:52vh;overflow:auto;display:flex;flex-direction:column;gap:.5rem">loading…</div>
    <div class="mbar"><span class="msg" id="proxMsg" style="color:var(--dim)"></span>
      <button onclick="loadProxies()">Refresh</button>
      <button onclick="closeProxies()">Close</button></div>
  </div>
</div>

<div class="scrim" id="deployScrim" onclick="closeDeploy(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="mhead">Deploy a proxy</div>
    <div class="hint" style="margin:-.3rem 0 .4rem">Downloads the chosen fork's launcher and registers a new instance. The launcher fetches Java + the jar on first start.</div>
    <label>Source
      <div id="depSrc" style="display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.25rem">
        <div class="chip sel" data-s="aquarius" onclick="pickSrc('aquarius',this)">AquariusProxy</div>
        <div class="chip" data-s="zenith" onclick="pickSrc('zenith',this)">ZenithProxy</div>
        <div class="chip" data-s="custom" onclick="pickSrc('custom',this)">Custom fork</div>
      </div>
    </label>
    <label id="depRepoWrap" style="display:none">Custom repo <span class="hint">owner/repo on GitHub — must publish a launcher-v3 release</span>
      <input id="dep_repo" placeholder="youruser/YourProxyFork" autocomplete="off"></label>
    <label>Name <span class="hint">letters, digits, . _ -</span>
      <input id="dep_name" placeholder="bot1" autocomplete="off" oninput="depAutoDir()"></label>
    <label>Directory <span class="hint">where to install (auto-filled from base dir)</span>
      <input id="dep_dir" placeholder="/home/ubuntu/zenith/bot1" autocomplete="off" oninput="depDirEdited=true"></label>
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
      <button class="go" id="depBtn" onclick="startDeploy()">Deploy</button></div>
  </div>
</div>

<div class="scrim" id="filesScrim" onclick="closeFiles(event)">
  <div class="modal" style="width:min(840px,96vw)" onclick="event.stopPropagation()">
    <div class="mhead">Files</div>
    <div class="hint" style="margin:-.3rem 0 .5rem">Jailed to the manager's allowed roots. Create, edit, rename and delete files &amp; folders.</div>
    <div id="fbBrowse">
      <div class="fbbar">
        <select id="fbRoot" onchange="fbGotoRoot()" title="jump to a root"></select>
        <button onclick="fbUp()">⬆ Up</button>
        <button onclick="fbMkdir()">+ Folder</button>
        <button onclick="fbNewFile()">+ File</button>
        <button onclick="fbReload()">⟳</button>
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
      <div class="hint" style="margin-bottom:.5rem">Theme preset</div>
      <div id="presetRow" style="display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.9rem"></div>
      <label>Accent override <span class="hint">blank = preset default</span>
        <div style="display:flex;gap:.5rem;align-items:center">
          <input type="color" id="accentPick" style="width:48px;height:38px;padding:2px;background:#06090c;border:1px solid var(--line);border-radius:9px">
          <input id="accentHex" placeholder="#3ddc97" style="flex:1" autocomplete="off">
          <button onclick="document.getElementById('accentHex').value='';document.getElementById('accentPick').value='#3ddc97';previewTheme()">Clear</button>
        </div>
      </label>
      <div class="mbar">
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

<div class="scrim" id="addScrim" onclick="closeAdd(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="mhead">New instance</div>
    <label>Name <span class="hint">letters, digits, . _ -</span>
      <input id="f_name" placeholder="bot3" autocomplete="off"></label>
    <label>Directory
      <input id="f_dir" placeholder="/home/youruser/zenith/bot3" autocomplete="off"></label>
    <label>Launch command <span class="hint">default ./launch.sh</span>
      <input id="f_launch" placeholder="./launch.sh" autocomplete="off"></label>
    <label>Config file <span class="hint">default config.json</span>
      <input id="f_cfg" placeholder="config.json" autocomplete="off"></label>
    <div class="mrow">
      <label style="flex:2">Stop keys <span class="hint">comma sep, default C-c</span>
        <input id="f_keys" placeholder="C-c" autocomplete="off"></label>
      <label style="flex:1">Stop timeout
        <input id="f_to" type="number" placeholder="15" autocomplete="off"></label>
    </div>
    <div class="mrow">
      <label style="flex:1">Memory cap <span class="hint">e.g. 2G — blank = none</span>
        <input id="f_mem" placeholder="2G" autocomplete="off"></label>
      <label style="flex:1">CPU cap % <span class="hint">100 = one core</span>
        <input id="f_cpu" type="number" placeholder="200" autocomplete="off"></label>
    </div>
    <div class="mbar">
      <span class="msg" id="addMsg"></span>
      <button onclick="closeAdd()">Cancel</button>
      <button class="go" id="addBtn" onclick="submitAdd()">Add</button>
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
      <pre class="log" id="logBox">…</pre>
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

<script>
let CUR=null, TAB='logs', logTimer=null, INSTMAP={};
const $=id=>document.getElementById(id);

async function api(path,method='GET',body){
  const o={method,headers:{}};
  if(body){o.headers['Content-Type']='application/json';o.body=JSON.stringify(body);}
  const r=await fetch(path,o);
  return r.json();
}
function badge(s){return `<span class="badge ${s}">${s}</span>`;}
async function toggleAuto(name,enabled){
  await api(`/api/instances/${encodeURIComponent(name)}/autostart`,'POST',{enabled});
  refresh();
}
function fmtBytes(n){ if(!n)return'0'; if(n>=1e9)return (n/1e9).toFixed(1)+' GB'; if(n>=1e6)return Math.round(n/1e6)+' MB'; return Math.max(1,Math.round(n/1e3))+' KB'; }
function thr(){ return (SETTINGS&&SETTINGS.thresholds)||{cpu_pct:85,mem_pct:85,disk_pct:90}; }
function lvl(pct,t){ return pct>=Math.min(100,t+10)?'crit':(pct>=t?'warn':''); }
let HOST=null;
async function loadHost(){ try{ HOST=await api('/api/system/info'); }catch(e){ return; } renderHost(); }
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
  try{ d=await api('/api/instances'); }catch(e){ $('meta').textContent='connection lost'; return; }
  const list=d.instances||[];
  INSTMAP=Object.fromEntries(list.map(i=>[i.name,i]));
  const run=list.filter(i=>i.status==='running').length;
  const cr=list.filter(i=>i.status==='crashed').length;
  $('meta').textContent=`${list.length} instances · ${run} running`+(cr?` · ${cr} crashed`:'');
  const g=$('grid');
  if(!list.length){g.innerHTML='<div class="empty">No instances configured. Add them to instances.json.</div>';return;}
  g.innerHTML=list.map(i=>`
    <div class="card ${i.status}">
      <div class="top">
        <div class="name">${esc(i.name)}</div>
        <div style="display:flex;align-items:center;gap:.4rem">
          <span class="star ${i.autostart?'on':''}" title="${i.autostart?'Autostart on — launches on boot':'Autostart off'}" onclick="toggleAuto('${jsq(i.name)}',${!i.autostart})">${i.autostart?'★':'☆'}</span>
          ${badge(i.status)}
        </div>
      </div>
      <div class="path">${esc(i.dir)}</div>
      <div class="cmd">$ ${esc(i.launch_cmd)}</div>
      ${i.status==='running'&&i.stats?statBars(i.stats,i.limits):''}
      <div class="row">
        <button class="go" onclick="act('${jsq(i.name)}','start',this)">▶ Start</button>
        <button class="warn" onclick="act('${jsq(i.name)}','restart',this)">⟳ Restart</button>
        <button class="danger" onclick="act('${jsq(i.name)}','stop',this)">■ Stop</button>
        <button onclick="openDrawer('${jsq(i.name)}')">⋯</button>
        <button class="danger" title="Delete instance" onclick="del('${jsq(i.name)}','${i.status}')">🗑</button>
      </div>
    </div>`).join('');
}

async function act(name,action,btn){
  const card=btn.closest('.card');
  card.querySelectorAll('button').forEach(b=>b.disabled=true);
  btn.innerHTML='<span class="spin"></span>'+btn.textContent.replace(/^\S+\s/,'');
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
  if(t==='logs'){renderPresetBar();loadLogs();logTimer=setInterval(loadLogs,3000);}
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
async function loadLogs(){
  if(!CUR)return;
  const d=await api(`/api/instances/${encodeURIComponent(CUR)}/logs?lines=400`);
  const box=$('logBox');
  const atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<40;
  box.textContent=d.logs||'(empty)';
  if(atBottom)box.scrollTop=box.scrollHeight;
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
}
async function loadSettings(){
  SETTINGS=await api('/api/settings');
  applyTheme(SETTINGS);
  return SETTINGS;
}

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
  if(!nodes.length){ el.innerHTML='<span class="hint">No other boxes connected yet — this box (the controller) is your current dashboard. Add another below.</span>'; return; }
  el.innerHTML=rows.map(r=>{
    const up=r.reachable, col=up?'var(--ok,#3a6f5a)':'var(--crash,#c25)';
    const meta=up?((r.running||0)+'/'+(r.bots||0)+' bots running'+hostBit(r.host)):('offline'+(r.error?(' — '+esc(r.error)):''));
    const sub=r.controller?'this box':esc((r.ssh_user||'')+'@'+(r.ssh_host||''));
    const right=r.controller?'<span class="hint">controller</span>':
      (`<button onclick="openNode('${esc(r.name)}')">Open</button> `+
       (r.do?`<button class="danger" onclick="destroyBox('${esc(r.name)}',this)">Destroy</button> `:'')+
       `<button class="danger" onclick="removeBox('${esc(r.name)}',this)">Remove</button>`);
    return `<div style="display:flex;align-items:center;gap:.6rem;padding:.45rem .6rem;border:1px solid var(--line);border-radius:9px">
      <span style="width:9px;height:9px;border-radius:50%;background:${col};flex:none"></span>
      <div style="flex:1;min-width:0">
        <div style="font-weight:600">${esc(r.name)} <span class="hint" style="font-weight:400;font-family:var(--mono)">${sub}</span></div>
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
  if(!rows.filter(r=>r.found).length){ box.innerHTML='<div class="hint">No instances have a proxy field.</div>'; return; }
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

let depSrc='aquarius', depTimer=null, depDirEdited=false;
function openDeploy(){
  depDirEdited=false; depSrc='aquarius';
  document.querySelectorAll('#depSrc .chip').forEach(c=>c.classList.toggle('sel',c.dataset.s==='aquarius'));
  $('depRepoWrap').style.display='none';
  ['dep_name','dep_dir','dep_repo','dep_mem','dep_cpu'].forEach(id=>$(id).value='');
  $('depLog').style.display='none'; $('depLog').textContent='';
  $('depMsg').textContent=''; $('depBtn').disabled=false;
  $('deployScrim').classList.add('open'); setTimeout(()=>$('dep_name').focus(),50);
}
function closeDeploy(e){ if(e&&e.target!==$('deployScrim'))return; if(depTimer){clearInterval(depTimer);depTimer=null;} $('deployScrim').classList.remove('open'); }
function pickSrc(s,el){ depSrc=s; document.querySelectorAll('#depSrc .chip').forEach(c=>c.classList.toggle('sel',c.dataset.s===s)); $('depRepoWrap').style.display=s==='custom'?'':'none'; }
function depAutoDir(){
  if(depDirEdited)return;
  const base=(SETTINGS&&SETTINGS.base_dir)||'', n=$('dep_name').value.trim();
  $('dep_dir').value=(n&&base)?(base.replace(/[\/\\]+$/,'')+'/'+n):'';
}
async function startDeploy(){
  const name=$('dep_name').value.trim();
  if(!name){ $('depMsg').style.color='var(--crash)'; $('depMsg').textContent='name required'; return; }
  const body={name,dir:$('dep_dir').value.trim(),source:depSrc,owner_repo:$('dep_repo').value.trim(),
              limits:{memory:$('dep_mem').value.trim(),cpu:$('dep_cpu').value.trim()},
              autostart:$('dep_autostart').checked};
  $('depMsg').style.color='var(--dim)'; $('depMsg').textContent='starting…'; $('depBtn').disabled=true;
  const d=await api('/api/deploy','POST',body);
  if(d.error){ $('depMsg').style.color='var(--crash)'; $('depMsg').textContent='✗ '+d.error; $('depBtn').disabled=false; return; }
  $('depLog').style.display=''; $('depMsg').textContent='deploying…';
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
    $('depMsg').textContent=j.status==='done'?'✓ deployed — start it from the dashboard':'✗ deploy failed';
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
  $('fbList').innerHTML=d.entries.map(e=>{
    const open=e.type==='dir'?`fbNav('${jsq(e.path)}')`:`fbOpen('${jsq(e.path)}')`;
    return `<div class="frow2">
      <span class="ficon">${e.type==='dir'?'📁':'📄'}</span>
      <span class="fn" onclick="${open}">${esc(e.name)}</span>
      <span class="fmeta">${e.type==='dir'?'':fmtBytes(e.size)}</span>
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
function openSettings(){
  $('settingsScrim').classList.add('open');
  setTab('ap');
  renderPresets();
}
function closeSettings(e){
  if(e&&e.target!==$('settingsScrim'))return;
  $('settingsScrim').classList.remove('open');
  if(sysTimer){clearInterval(sysTimer);sysTimer=null;}
  applyTheme(SETTINGS); // revert any unsaved live preview
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
let SELPRESET=null, SELACCENT='';
function renderPresets(){
  SELPRESET=SETTINGS.theme.preset; SELACCENT=SETTINGS.theme.accent||'';
  const presets=SETTINGS.presets;
  $('presetRow').innerHTML=Object.keys(presets).map(k=>`
    <div class="chip ${k===SELPRESET?'sel':''}" data-k="${k}" onclick="pickPreset('${k}')">
      <span class="sw" style="background:${presets[k].accent}"></span>${k}</div>`).join('');
  $('accentHex').value=SELACCENT;
  $('accentPick').value=SELACCENT||presets[SELPRESET].accent;
  $('accentHex').oninput=()=>{SELACCENT=$('accentHex').value.trim();$('accentPick').value=SELACCENT||presets[SELPRESET].accent;previewTheme();};
  $('accentPick').oninput=()=>{SELACCENT=$('accentPick').value;$('accentHex').value=SELACCENT;previewTheme();};
}
function pickPreset(k){
  SELPRESET=k;
  document.querySelectorAll('#presetRow .chip').forEach(c=>c.classList.toggle('sel',c.dataset.k===k));
  previewTheme();
}
function previewTheme(){ applyTheme({presets:SETTINGS.presets,theme:{preset:SELPRESET,accent:SELACCENT}}); }
async function saveAppearance(){
  $('apMsg').textContent='saving…';
  const d=await api('/api/settings','POST',{theme:{preset:SELPRESET,accent:SELACCENT}});
  if(d.error){$('apMsg').textContent='✗ '+d.error;return;}
  SETTINGS=d.settings; applyTheme(SETTINGS);
  $('apMsg').textContent='✓ saved';
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
  let d; try{ d=await api('/api/selfupdate','POST',{}); }catch(e){ d={error:String(e)}; }
  btn.disabled=false; btn.textContent=orig;
  if(!d||d.error){ $('updMsg').style.color='var(--crash)'; $('updMsg').textContent='✗ '+((d&&d.error)||'failed'); return; }
  $('updMsg').style.color='var(--dim)';
  if(d.updated){ $('updMsg').textContent='✓ '+d.old+' → '+d.new+(d.restarted?' · restarting…':' · restart manually'); }
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

function openAdd(){
  ['f_name','f_dir','f_launch','f_cfg','f_keys','f_to','f_mem','f_cpu'].forEach(id=>$(id).value='');
  $('addMsg').textContent='';
  $('addScrim').classList.add('open');
  setTimeout(()=>$('f_name').focus(),50);
}
function closeAdd(e){
  if(e&&e.target!==$('addScrim'))return;
  $('addScrim').classList.remove('open');
}
async function submitAdd(){
  const body={
    name:$('f_name').value.trim(),
    dir:$('f_dir').value.trim(),
    launch_cmd:$('f_launch').value.trim()||null,
    config_file:$('f_cfg').value.trim()||null,
    stop_keys:$('f_keys').value.trim()||null,
    stop_timeout:$('f_to').value.trim()||null,
    limits:{memory:$('f_mem').value.trim(),cpu:$('f_cpu').value.trim()},
  };
  $('addMsg').textContent='';
  $('addBtn').disabled=true;
  const d=await api('/api/instances/add','POST',body);
  $('addBtn').disabled=false;
  if(d.error){$('addMsg').textContent='✗ '+d.error;return;}
  $('addScrim').classList.remove('open');
  refresh();
}

function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function jsq(s){return (s||'').replace(/\\/g,'\\\\').replace(/'/g,"\\'");}
function tick(){$('clock').textContent=new Date().toLocaleTimeString();}
setInterval(tick,1000);tick();
loadSettings();
refresh();
setInterval(refresh,4000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
