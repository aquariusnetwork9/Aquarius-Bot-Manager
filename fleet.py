#!/usr/bin/env python3
"""
Aquarius Bot Manager — Fleet controller.

Manages a FLEET of single-host Aquarius Bot Manager "agents" spread across
DigitalOcean droplets (the one-bot(-or-more)-per-droplet model). Each droplet
runs the normal manager (see manager.py) as an agent, bound to its VPC private
IP and protected by HTTP Basic auth. This controller:

  - provisions droplets via the DigitalOcean API (cloud-init installs the agent),
  - tracks them in fleet.json,
  - aggregates each agent's existing HTTP API into one view,
  - can run bulk lifecycle/deploy actions across the fleet.

The controller itself should run on a droplet IN THE SAME VPC so it can reach
agents on their private IPs. Pure Python stdlib — no extra dependencies.

Security: this process holds a DigitalOcean API token (can create/destroy
infrastructure) and Basic-auth credentials to every agent. Treat the host and
fleet.json as highly sensitive. The DO token is stored base64-OBFUSCATED, which
is NOT encryption. See the wiki Security page.
"""

import argparse
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "0.1.0"

DO_API = "https://api.digitalocean.com/v2"
DEFAULT_FLEET = os.environ.get("ABM_FLEET_CONFIG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fleet.json")
# the agent installer the cloud-init pulls (override for a fork)
DEFAULT_INSTALL_URL = ("https://raw.githubusercontent.com/aquariusnetwork9/"
                       "Aquarius-Bot-Manager/main/install.sh")
DEFAULT_TAG = "aquarius-fleet"
AGENT_PORT = 8765


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _enc(t):
    """Obfuscate-at-rest (base64). NOT encryption — just not eyeball-plaintext."""
    t = (t or "").strip()
    return "b64:" + base64.b64encode(t.encode()).decode() if t else ""


def _dec(s):
    s = s or ""
    if s.startswith("b64:"):
        try:
            return base64.b64decode(s[4:]).decode()
        except Exception:
            return ""
    return s


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# fleet registry  (fleet.json)
# ---------------------------------------------------------------------------

def load_fleet(path=DEFAULT_FLEET):
    if not os.path.exists(path):
        return {"_path": path, "droplets": [], "settings": {}}
    with open(path) as f:
        raw = json.load(f)
    raw.setdefault("droplets", [])
    raw.setdefault("settings", {})
    raw["_path"] = path
    return raw


def save_fleet(fleet):
    path = fleet["_path"]
    out = {k: v for k, v in fleet.items() if not k.startswith("_")}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=2)
    os.replace(tmp, path)
    try:                          # secrets at rest — restrict perms (best-effort; no-op on Windows)
        os.chmod(path, 0o600)
    except OSError:
        pass


def do_token(fleet, token=None):
    """Resolve the DigitalOcean token: arg > env > saved (decoded)."""
    saved = fleet.get("settings", {}).get("do_token") or ""
    return (token or os.environ.get("DO_API_TOKEN")
            or os.environ.get("DIGITALOCEAN_TOKEN") or _dec(saved) or "").strip()


def save_do_token(fleet, token):
    fleet.setdefault("settings", {})["do_token"] = _enc(token)
    save_fleet(fleet)


def fleet_creds(fleet):
    """The Basic-auth credentials the controller uses to reach every agent.
    Generated once and stored; the same pair is baked into each agent."""
    s = fleet.setdefault("settings", {})
    if not s.get("agent_user") or not s.get("agent_pass"):
        s["agent_user"] = s.get("agent_user") or "fleet"
        s["agent_pass"] = s.get("agent_pass") or secrets.token_urlsafe(24)
        save_fleet(fleet)
    return s["agent_user"], s["agent_pass"]


def _find_droplet(fleet, ident):
    """Match a droplet by name or DO id (string or int)."""
    sid = str(ident)
    for d in fleet["droplets"]:
        if d.get("name") == ident or str(d.get("id")) == sid:
            return d
    return None


# ---------------------------------------------------------------------------
# DigitalOcean API client
# ---------------------------------------------------------------------------

def _do_request(token, method, path, body=None):
    if not token:
        raise ValueError("no DigitalOcean API token (set DO_API_TOKEN, pass --token, or save one)")
    url = path if path.startswith("http") else DO_API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "aquarius-bot-manager-fleet",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode()).get("message", "")
        except Exception:
            pass
        if e.code == 401:
            raise ValueError("DigitalOcean rejected the token (401) — check DO_API_TOKEN scope")
        raise ValueError(f"DigitalOcean API {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise ValueError(f"could not reach DigitalOcean API: {e.reason}")


def _do_paged(token, path, key):
    """GET a paginated collection, following links.pages.next."""
    out, url = [], path + ("&" if "?" in path else "?") + "per_page=200"
    while url:
        data = _do_request(token, "GET", url)
        out.extend(data.get(key, []))
        url = (data.get("links", {}).get("pages", {}) or {}).get("next")
    return out


def do_list_droplets(token, tag=None):
    path = "/droplets" + (f"?tag_name={urllib.parse.quote(tag)}" if tag else "")
    return _do_paged(token, path, "droplets")


def do_get_droplet(token, droplet_id):
    return _do_request(token, "GET", f"/droplets/{droplet_id}").get("droplet")


def do_create_droplet(token, name, region, size, image, ssh_keys, user_data,
                      tags, vpc_uuid=None):
    body = {"name": name, "region": region, "size": size, "image": image,
            "ssh_keys": ssh_keys or [], "user_data": user_data,
            "tags": tags or [], "ipv6": False, "monitoring": True}
    if vpc_uuid:
        body["vpc_uuid"] = vpc_uuid
    return _do_request(token, "POST", "/droplets", body).get("droplet")


def do_destroy_droplet(token, droplet_id):
    _do_request(token, "DELETE", f"/droplets/{droplet_id}")
    return True


def do_list_ssh_keys(token):
    return _do_paged(token, "/account/keys", "ssh_keys")


def do_list_regions(token):
    return [r for r in _do_paged(token, "/regions", "regions") if r.get("available")]


def do_list_sizes(token):
    return [s for s in _do_paged(token, "/sizes", "sizes") if s.get("available")]


def do_list_vpcs(token):
    return _do_paged(token, "/vpcs", "vpcs")


def droplet_ips(droplet):
    """(public_ip, private_ip) from a DO droplet object."""
    pub = priv = ""
    for n in (droplet.get("networks", {}) or {}).get("v4", []) or []:
        if n.get("type") == "public":
            pub = n.get("ip_address", "")
        elif n.get("type") == "private":
            priv = n.get("ip_address", "")
    return pub, priv


# ---------------------------------------------------------------------------
# agent client  (each droplet runs the normal manager HTTP API)
# ---------------------------------------------------------------------------

def _agent_addr(d):
    host = d.get("private_ip") or d.get("public_ip")
    return host, d.get("port", AGENT_PORT)


def agent_request(fleet, d, method, path, body=None, timeout=10):
    host, port = _agent_addr(d)
    if not host:
        raise ValueError("droplet has no known IP yet")
    user, pw = fleet_creds(fleet)
    url = f"http://{host}:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "User-Agent": "aquarius-bot-manager-fleet",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def agent_reachable(fleet, d, timeout=5):
    try:
        agent_request(fleet, d, "GET", "/api/system/info", timeout=timeout)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

def fleet_status(fleet):
    """Pull each droplet's agent API into one list. Never raises per-droplet —
    unreachable agents are reported as such."""
    rows = []
    for d in fleet["droplets"]:
        host, port = _agent_addr(d)
        row = {"name": d.get("name"), "id": d.get("id"), "region": d.get("region"),
               "public_ip": d.get("public_ip"), "private_ip": d.get("private_ip"),
               "port": port}
        try:
            info = agent_request(fleet, d, "GET", "/api/system/info")
            inst = agent_request(fleet, d, "GET", "/api/instances").get("instances", [])
            row["reachable"] = True
            row["instances"] = inst
            row["bots"] = len(inst)
            row["running"] = sum(1 for i in inst if i.get("status") == "running")
            row["host"] = {"cpu": info.get("load") or info.get("cpu"),
                           "mem": info.get("mem") or info.get("memory")}
        except Exception as e:
            row["reachable"] = False
            row["error"] = str(e)
            row["instances"] = []
        rows.append(row)
    return rows


def fleet_action(fleet, action, targets=None):
    """Run start_all/stop_all/restart_all on each target droplet's agent."""
    if action not in ("start", "stop", "restart"):
        raise ValueError("action must be start, stop or restart")
    results = []
    for d in _targets(fleet, targets):
        try:
            r = agent_request(fleet, d, "POST", f"/api/{action}_all")
            results.append({"name": d["name"], "ok": True, "results": r.get("results")})
        except Exception as e:
            results.append({"name": d["name"], "ok": False, "error": str(e)})
    return results


def _targets(fleet, targets):
    if not targets or list(targets) == ["all"]:
        return list(fleet["droplets"])
    out = []
    for t in targets:
        d = _find_droplet(fleet, t)
        if not d:
            raise ValueError(f"no such droplet in fleet: {t}")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# cloud-init generation  (the file deployed to each droplet)
# ---------------------------------------------------------------------------

def generate_cloud_init(fleet, install_url=None):
    """Build the cloud-init user-data that turns a fresh droplet into an agent:
    install the manager in agent mode, bound to the VPC private IP, with the
    fleet's Basic-auth credentials. Bots are deployed afterward by the
    controller over the agent API."""
    user, pw = fleet_creds(fleet)
    install_url = install_url or fleet.get("settings", {}).get("install_url") or DEFAULT_INSTALL_URL
    # ABM_ACCESS=agent makes install.sh bind the private IP + bake ABM_USER/PASS
    # into the systemd unit (no Caddy, no tunnel). install.sh autodetects the
    # DO private IP from the metadata service.
    return f"""#cloud-config
package_update: true
packages:
  - python3
  - tmux
  - unzip
  - wget
  - git
  - curl
runcmd:
  - 'curl -fsSL {install_url} | ABM_RUN_USER=root ABM_ACCESS=agent ABM_USER={user} ABM_PASS={pw} bash'
"""


# ---------------------------------------------------------------------------
# provisioning
# ---------------------------------------------------------------------------

def provision(fleet, count, token=None, name_prefix=None, region=None, size=None,
              image=None, ssh_key_ids=None, vpc_uuid=None, tag=None,
              bot_source=None, wait=True, deploy_timeout=300, log=print):
    """Create `count` agent droplets, register them, optionally wait for the
    agent to come up and deploy a bot on each. Returns the new droplet records."""
    tok = do_token(fleet, token)
    s = fleet.setdefault("settings", {})
    region = region or s.get("region")
    size = size or s.get("size")
    image = image or s.get("image", "ubuntu-22-04-x64")
    tag = tag or s.get("tag", DEFAULT_TAG)
    vpc_uuid = vpc_uuid or s.get("vpc_uuid")
    ssh_key_ids = ssh_key_ids or s.get("ssh_key_ids") or []
    name_prefix = name_prefix or s.get("name_prefix", "abm-bot")
    bot_source = bot_source if bot_source is not None else s.get("bot_source", "aquarius")
    if not region or not size:
        raise ValueError("region and size are required (set them with `fleet config` or flags)")
    if not ssh_key_ids:
        raise ValueError("at least one SSH key id is required (see `fleet sshkeys`) so you can reach the droplet")

    user_data = generate_cloud_init(fleet)
    # next free numeric suffix
    existing = {d.get("name") for d in fleet["droplets"]}
    n, made = 1, []
    for _ in range(count):
        while f"{name_prefix}-{n}" in existing:
            n += 1
        name = f"{name_prefix}-{n}"
        existing.add(name)
        log(f"creating droplet {name} ({size} @ {region})…")
        dro = do_create_droplet(tok, name, region, size, image, ssh_key_ids,
                                user_data, [tag], vpc_uuid)
        rec = {"id": dro["id"], "name": name, "region": region, "size": size,
               "tag": tag, "port": AGENT_PORT, "public_ip": "", "private_ip": "",
               "created_at": dro.get("created_at")}
        fleet["droplets"].append(rec)
        made.append(rec)
        save_fleet(fleet)

    if not wait:
        return made

    for rec in made:
        _wait_active(tok, rec, log=log)
        save_fleet(fleet)
        if bot_source:
            _wait_and_deploy(fleet, rec, bot_source, deploy_timeout, log=log)
    return made


def _wait_active(token, rec, timeout=180, log=print):
    """Poll DO until the droplet is active and has IPs; fill them into rec."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = do_get_droplet(token, rec["id"])
        if d and d.get("status") == "active":
            pub, priv = droplet_ips(d)
            rec["public_ip"], rec["private_ip"] = pub, priv
            log(f"  {rec['name']} active  public={pub or '-'} private={priv or '-'}")
            if priv or pub:
                return rec
        time.sleep(5)
    log(f"  {rec['name']} did not become active in {timeout}s (still provisioning?)")
    return rec


def _wait_and_deploy(fleet, rec, source, timeout, log=print):
    """Wait for the agent API, then deploy one bot via its existing API."""
    deadline = time.time() + timeout
    log(f"  waiting for {rec['name']} agent to come up (installing on first boot)…")
    while time.time() < deadline:
        if agent_reachable(fleet, rec):
            break
        time.sleep(8)
    else:
        log(f"  {rec['name']} agent not reachable within {timeout}s — deploy the bot later "
            f"with `fleet deploy {rec['name']}`")
        return
    try:
        agent_request(fleet, rec, "POST", "/api/deploy",
                      {"name": "bot1", "source": source, "autostart": True}, timeout=30)
        log(f"  {rec['name']} deploying bot1 ({source})")
    except Exception as e:
        log(f"  {rec['name']} bot deploy failed: {e}")


def register_existing(fleet, token=None, tag=None):
    """Adopt droplets that already carry the fleet tag in DigitalOcean
    (e.g. created out of band) into fleet.json."""
    tok = do_token(fleet, token)
    tag = tag or fleet.get("settings", {}).get("tag", DEFAULT_TAG)
    known = {str(d.get("id")) for d in fleet["droplets"]}
    added = []
    for d in do_list_droplets(tok, tag=tag):
        if str(d["id"]) in known:
            continue
        pub, priv = droplet_ips(d)
        rec = {"id": d["id"], "name": d["name"],
               "region": (d.get("region") or {}).get("slug"),
               "size": (d.get("size") or {}).get("slug"), "tag": tag,
               "port": AGENT_PORT, "public_ip": pub, "private_ip": priv}
        fleet["droplets"].append(rec)
        added.append(rec)
    if added:
        save_fleet(fleet)
    return added


def destroy(fleet, ident, token=None, keep_record=False):
    tok = do_token(fleet, token)
    d = _find_droplet(fleet, ident)
    if not d:
        raise ValueError(f"no such droplet in fleet: {ident}")
    if d.get("id"):
        do_destroy_droplet(tok, d["id"])
    if not keep_record:
        fleet["droplets"] = [x for x in fleet["droplets"] if x is not d]
        save_fleet(fleet)
    return d


def refresh_ips(fleet, token=None):
    """Re-pull IP/status for every droplet from DO (they can change)."""
    tok = do_token(fleet, token)
    for d in fleet["droplets"]:
        if not d.get("id"):
            continue
        try:
            dro = do_get_droplet(tok, d["id"])
            if dro:
                pub, priv = droplet_ips(dro)
                d["public_ip"], d["private_ip"] = pub, priv
                d["status"] = dro.get("status")
        except Exception:
            pass
    save_fleet(fleet)
    return fleet["droplets"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_table(rows, cols):
    if not rows:
        return "(none)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    line = "  ".join(c.ljust(widths[c]) for c in cols)
    out = [line]
    for r in rows:
        out.append("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fleet", description="Aquarius Bot Manager fleet controller")
    ap.add_argument("--version", action="version", version=f"fleet {__version__}")
    ap.add_argument("--config", default=DEFAULT_FLEET, help="fleet.json path")
    ap.add_argument("--token", default=None, help="DigitalOcean API token (else DO_API_TOKEN / saved)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="aggregate every droplet's agent into one view")
    sub.add_parser("list", help="list droplets in the fleet registry")
    sub.add_parser("refresh", help="re-pull droplet IPs/status from DigitalOcean")
    sub.add_parser("regions", help="list available DigitalOcean regions")
    sub.add_parser("sizes", help="list available droplet sizes (+ monthly price)")
    sub.add_parser("sshkeys", help="list your DigitalOcean SSH keys (ids for provisioning)")
    sub.add_parser("vpcs", help="list your VPCs (ids for provisioning)")
    sub.add_parser("cloud-init", help="print the generated agent cloud-init user-data")

    p = sub.add_parser("save-token", help="save the DigitalOcean token (obfuscated) to fleet.json")
    p.add_argument("value", help="the API token")

    p = sub.add_parser("config", help="set fleet defaults (region/size/image/vpc/ssh keys/...)")
    for k in ("region", "size", "image", "vpc-uuid", "tag", "name-prefix", "bot-source", "install-url"):
        p.add_argument(f"--{k}", default=None)
    p.add_argument("--ssh-keys", default=None, help="comma-separated SSH key ids")

    p = sub.add_parser("provision", help="create N agent droplets (cloud-init installs them)")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--region", default=None)
    p.add_argument("--size", default=None)
    p.add_argument("--image", default=None)
    p.add_argument("--ssh-keys", default=None, help="comma-separated SSH key ids (else saved default)")
    p.add_argument("--vpc-uuid", default=None)
    p.add_argument("--tag", default=None)
    p.add_argument("--bot-source", default=None, help="aquarius|zenith|custom, or '' to skip bot deploy")
    p.add_argument("--no-wait", action="store_true", help="return after create; don't wait/deploy")

    p = sub.add_parser("register", help="adopt already-tagged droplets into the registry")
    p.add_argument("--tag", default=None)

    p = sub.add_parser("deploy", help="deploy a bot onto a droplet via its agent API")
    p.add_argument("droplet")
    p.add_argument("--name", default="bot1")
    p.add_argument("--source", default="aquarius")

    p = sub.add_parser("action", help="start/stop/restart all bots across droplets")
    p.add_argument("what", choices=["start", "stop", "restart"])
    p.add_argument("--targets", default="all", help="droplet names/ids, or 'all'")

    p = sub.add_parser("destroy", help="destroy a droplet (DigitalOcean) and drop it from the fleet")
    p.add_argument("droplet")
    p.add_argument("--yes", action="store_true", help="skip confirmation")

    args = ap.parse_args(argv)
    fleet = load_fleet(args.config)
    tok = lambda: do_token(fleet, args.token)

    try:
        if args.cmd == "save-token":
            save_do_token(fleet, args.value)
            print("saved DigitalOcean token (obfuscated) to", fleet["_path"])
        elif args.cmd == "config":
            s = fleet.setdefault("settings", {})
            for attr, key in (("region", "region"), ("size", "size"), ("image", "image"),
                              ("vpc_uuid", "vpc_uuid"), ("tag", "tag"),
                              ("name_prefix", "name_prefix"), ("bot_source", "bot_source"),
                              ("install_url", "install_url")):
                v = getattr(args, attr.replace("_", "_"))
                if v is not None:
                    s[key] = v
            if args.ssh_keys is not None:
                s["ssh_key_ids"] = [int(x) if x.strip().isdigit() else x.strip()
                                    for x in args.ssh_keys.split(",") if x.strip()]
            save_fleet(fleet)
            print(json.dumps(fleet["settings"], indent=2))
        elif args.cmd == "list":
            print(_fmt_table(fleet["droplets"],
                             ["name", "id", "region", "public_ip", "private_ip", "port"]))
        elif args.cmd == "refresh":
            refresh_ips(fleet, tok())
            print(_fmt_table(fleet["droplets"],
                             ["name", "id", "status", "public_ip", "private_ip"]))
        elif args.cmd == "status":
            rows = fleet_status(fleet)
            for r in rows:
                if r["reachable"]:
                    print(f"● {r['name']:<14} {r.get('private_ip') or r.get('public_ip'):<16} "
                          f"bots={r['bots']} running={r['running']}")
                else:
                    print(f"○ {r['name']:<14} {r.get('private_ip') or r.get('public_ip') or '-':<16} "
                          f"UNREACHABLE ({r.get('error','')})")
        elif args.cmd == "regions":
            print(_fmt_table([{"slug": r["slug"], "name": r["name"]}
                              for r in do_list_regions(tok())], ["slug", "name"]))
        elif args.cmd == "sizes":
            print(_fmt_table([{"slug": s["slug"], "mem_mb": s["memory"], "vcpus": s["vcpus"],
                               "usd_mo": s["price_monthly"]} for s in do_list_sizes(tok())],
                             ["slug", "mem_mb", "vcpus", "usd_mo"]))
        elif args.cmd == "sshkeys":
            print(_fmt_table([{"id": k["id"], "name": k["name"]}
                              for k in do_list_ssh_keys(tok())], ["id", "name"]))
        elif args.cmd == "vpcs":
            print(_fmt_table([{"id": v["id"], "name": v["name"], "region": v["region"]}
                              for v in do_list_vpcs(tok())], ["id", "name", "region"]))
        elif args.cmd == "cloud-init":
            print(generate_cloud_init(fleet))
        elif args.cmd == "provision":
            ssh = ([int(x) if x.strip().isdigit() else x.strip()
                    for x in args.ssh_keys.split(",") if x.strip()] if args.ssh_keys else None)
            bs = args.bot_source
            made = provision(fleet, args.count, token=args.token, region=args.region,
                             size=args.size, image=args.image, ssh_key_ids=ssh,
                             vpc_uuid=args.vpc_uuid, tag=args.tag,
                             bot_source=(bs if bs is not None else None) if bs == "" else bs,
                             wait=not args.no_wait)
            print(f"provisioned {len(made)} droplet(s):",
                  ", ".join(d["name"] for d in made))
        elif args.cmd == "register":
            added = register_existing(fleet, tok(), tag=args.tag)
            print(f"registered {len(added)} droplet(s):", ", ".join(d["name"] for d in added) or "(none)")
        elif args.cmd == "deploy":
            d = _find_droplet(fleet, args.droplet) or die(f"no such droplet: {args.droplet}")
            r = agent_request(fleet, d, "POST", "/api/deploy",
                              {"name": args.name, "source": args.source, "autostart": True}, timeout=30)
            print(f"{d['name']}: deploy started" if not r.get("error") else f"error: {r['error']}")
        elif args.cmd == "action":
            tg = [t for t in args.targets.replace(",", " ").split() if t]
            for r in fleet_action(fleet, args.what, tg):
                print(f"{r['name']}: {'ok' if r['ok'] else 'error: ' + r.get('error','')}")
        elif args.cmd == "destroy":
            d = _find_droplet(fleet, args.droplet) or die(f"no such droplet: {args.droplet}")
            if not args.yes:
                print(f"This DESTROYS droplet '{d['name']}' (id {d.get('id')}) on DigitalOcean. "
                      f"Re-run with --yes to confirm.")
                return
            destroy(fleet, args.droplet, token=args.token)
            print(f"destroyed {d['name']}")
    except ValueError as e:
        die(str(e))


if __name__ == "__main__":
    main()
