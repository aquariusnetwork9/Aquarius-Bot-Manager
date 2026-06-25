# Shareable-Link Guest Access — Implementation Plan & Handoff

> **Status:** Design complete, not yet implemented. This document is a handoff for
> the next session to implement. No production code has been changed.
> **Scope:** `manager.py` only (single-file backend + embedded SPA). The proxy repos
> (ZenithProxy / AquariusProxy / ProxyBridge) are **not** touched.
> **Branch:** `claude/aquarius-shared-access-auth-4yw2jb`
> **Source refs below are against** `manager.py` @ commit `31fade85`.

---

## 1. Goal & Context

The owner wants to let other people use the dashboard / control plane to operate **only the
bots they've been granted access to**, granted by a **single shareable URL** — no password
handout, no guest account creation. Opening the link authenticates the visitor and limits them
to a specific set of bots at a specific capability level.

**Today the manager is strictly single-user.** Auth is one boolean gate (`_auth_ok`) at the top
of `do_GET`/`do_POST`; once past it, every route is reachable and the user sees/controls
everything. There are no roles, no per-bot scoping, no second class of user. This feature adds a
**guest principal** backed by share grants, scopes each grant to specific bots + a capability
tier, and enforces that scope on every instance endpoint, on the multi-node proxy path, and in
the UI.

### Owner decisions (already confirmed — do not re-ask)
- **Tiered capability:** `view` / `operate` / `config` (not binary).
- **Fleet-wide scope:** a link may grant bots on **remote nodes**, not just the controller box.
- **Optional expiry + instant revoke:** owner may set a per-link expiry and revoke any link anytime.

### How the system works today (verified against source)
- Single-file stdlib server (`http.server` + `ThreadingHTTPServer`), no external deps. Vanilla-JS
  SPA embedded as HTML strings in `manager.py`. A `/control` Mission Control surface lives in `/control/*`.
- **Auth/session block** (`manager.py:2206-2293`): `_SESSIONS = {}` maps `token -> {"exp","gen"}`
  (`:2213`, `:2268-2271`). `_new_session(gen=0)` (`:2268`), `_session_valid(tok, gen)` (`:2274`),
  `session_epoch`/`bump_session_epoch` (`:2224-2233`), PBKDF2-200k `_hash_password` (`:2219`),
  `set_password`/`verify_password` (`:2236-2265`, `hmac.compare_digest`), rate limiter
  `_rate_limited`/`_record_fail` (`:2284-2293`, 5 fails / 5 min / IP).
- **Handler auth**: `_cookie_token` (`:3554`), `_auth_required` (`:3562`), `_needs_setup` (`:3566`),
  `_auth_ok` → bool (`:3574`), `_set_session_cookie` (`:3595`, hardcoded `HttpOnly; SameSite=Strict`),
  `_json(..., cookie=...)` (`:3605`).
- **Routing**: `do_GET` (`:3986`), `do_POST` (`:4253`). `/api/authstatus` (`:3993`) returns
  `{required, authed, needs_setup}`. The single gate `if not self._auth_ok(cfg)` at `:4005`
  (GET) / `:4302` (POST) — after it, every route is reachable by any authed session.
- **Instances**: list builder `:4210-4231`; instance-scoped routes match the instance name from a
  path regex group throughout both methods.
- **Multi-node**: `_selected_node` (`:3668`, reads `abm_node` cookie), `_proxy_to_node` (`:3696`,
  forwards the whole request to a node over its loopback SSH tunnel and injects the node's **owner**
  Basic-auth via `node_creds`), `_is_switcher_path` (`:3680`), fleet aggregation `_fleet…` (`:2867`),
  `/api/fleet/status`.
- **Control surface**: `_serve_control_page` (`:3774`, takes `?inst=<name>`), `_serve_control_asset`
  (`:3796`), `_viewer_relay` (`:3820`), `_control_relay` (`:3879`, the live `command` POST is at `:3899`).
- **Data/config**: `load_config`/`save_config` (`:65/:91`); settings under `data["settings"]`.
- **Frontend**: `LOGIN_PAGE` JS at `:5644`; `PAGE` starts `:5674`; owner header controls `:6016-6031`
  (Settings/Files/Proxies/Add Bot/Start-all/etc.), modals via `openSettings/openFiles/openProxies/openDeploy`.

---

## 2. Data Model — `settings.shares`

Add `cfg["raw"]["settings"]["shares"]` (list) and `settings.shares_epoch` (int). Default both in the
config loader's `setdefault` block (`~:82-87`) so existing `instances.json` files load unchanged.

```jsonc
{
  "id":         "<8-hex>",                 // secrets.token_hex(4) — stable handle for revoke
  "token_hash": "<sha256 hex>",            // sha256(plaintext); 256-bit token => no salt needed
  "label":      "Friend's bots",
  "targets":    [{"node": null,   "name": "botA"},   // node=null => controller (local) box
                 {"node": "vps2", "name": "botX"}],  // node-qualified for fleet-wide scope
  "all":        false,                     // true => all current+future LOCAL bots (targets ignored)
  "capability": "view" | "operate" | "config",
  "created":    1719300000,
  "expires":    1719900000,                // or null (never); UI defaults to a value
  "revoked":    false,
  "epoch":      0                          // snapshot of shares_epoch at creation
}
```

### Capability tiers (ordered `view < operate < config`)
- **view** — read only: logs, config read, `viewer/*`, `control/state`, `control/commands` list.
- **operate** — `view` + start/stop/restart, send console command, `control/command`.
- **config** — `operate` + edit `config.json`, proxy, limits, autostart.
- **Never for guests at any tier:** delete, rename, deploy, adopt, add (these are fleet management,
  owner-only).

### Token hashing
Tokens are `secrets.token_urlsafe(32)` (256-bit). Brute force is infeasible, so unsalted
`hashlib.sha256(token.encode()).hexdigest()` is sufficient and fast on the redemption hot path
(PBKDF2's 200k rounds would be wasteful per request). Compare with `hmac.compare_digest`.

### One-time reveal
Plaintext token is generated server-side; the full share URL is returned in the **create** response
exactly once; only `token_hash` is persisted. The list endpoint never returns the token.

### New helpers (place after the session helpers, `~:2293`)
`_share_token_hash`, `new_share`, `find_share_by_token` (constant-time scan, skip
revoked/expired/wrong-epoch), `find_share_by_id`, `revoke_share`, `bump_shares_epoch` (mirror of
`bump_session_epoch` `:2229`), `share_public_view` (strip `token_hash`, add computed `status`).

---

## 3. Link Format & Redemption

**URL shape:** `/s/<token>` (dedicated short path; keeps the token out of the SPA address bar after
load and is easy to special-case before the auth gate).

**Flow** — add a branch in `do_GET` **before** the `_auth_ok` gate (`:4005`) and before the
node-proxy block:
1. Match `^/s/(?P<token>[A-Za-z0-9_-]+)$`.
2. Rate-limit by IP via the existing `_rate_limited`/`_record_fail` (`:2284-2293`) — same bucket as
   login. 429 if tripped.
3. `grant = find_share_by_token(cfg, token)`; on `None` → `_record_fail(ip)` and serve a small
   "invalid or expired link" HTML page (not the login page).
4. Valid → mint a **scoped** session and `302` to `/`.

**Scoped session.** Extend `_new_session(gen=0, scope=None)` (`:2268`). Owner sessions pass
`scope=None` (unchanged). Guest sessions store
`scope = {"grant_id", "targets", "all", "capability", "shares_epoch"}` and `"principal":"guest"`.

**Cookie SameSite.** `_set_session_cookie` (`:3595`) hardcodes `SameSite=Strict`, which can drop the
cookie on a cross-site link click. Add a `samesite="Strict"` param and pass `"Lax"` **only** on the
`/s/` redemption response so links clicked from Discord/email work. Owner login keeps `Strict`. `Lax`
still blocks CSRF on the mutating POSTs (none are top-level navigations). Implement the 302 inline
(`send_response(302)`, `Location: /`, `_set_session_cookie(tok, samesite="Lax")`).

---

## 4. Authorization Enforcement (the crux)

### 4a. Principal resolver
Add `_principal(self, cfg)` next to `_auth_ok` (`:3574`), returning a dict or `None`:
- Auth not required (open mode) → `{"type":"owner","scope":None}`.
- Valid owner cookie / legacy Basic-Auth → `{"type":"owner","scope":None}`.
- Valid guest cookie → **re-validate the grant from `cfg` every request** by `grant_id` (revoked?
  expired? `shares_epoch` still matches `settings.shares_epoch`?). Stale → drop session, return
  `None`. Fresh → `{"type":"guest","scope":{...}}` using the **current** grant fields (so editing a
  grant takes effect immediately and revoke kills live sessions next request).
- Sessions lacking a `principal` key (in-flight owner sessions) default to owner — back-compat.

`_auth_ok` becomes `return self._principal(cfg) is not None` (existing call sites unchanged).

### 4b. Guard helpers
- `_require_target(princ, node, name)` — owner → ok; guest → target in scope (or `all` for local);
  else **404** (`"no such instance"`) to prevent fleet enumeration.
- `_require_cap(princ, level)` — owner → ok; guest → `tier(capability) >= tier(level)`; else **403**.
- `_require_owner(princ)` — owner only; else 403.

### 4c. Dispatch wiring
Replace the two bare `if not self._auth_ok(cfg)` gates (`:4005`, `:4302`) with
`princ = self._principal(cfg); if princ is None: <existing 401/login behavior>`. Thread `princ`
through routes.

### 4d. Fleet routing for guests (the largest piece)
The controller already proxies to a node via `_proxy_to_node(node)` (`:3696`), today selected by the
`abm_node` cookie (`_selected_node`, `:3668`) and authenticated to the node with **owner** creds from
`nodes.json` (`node_creds`). Guests must **not** pick nodes or receive node creds. Instead:
- Guest instance-scoped requests are routed by the **grant's target mapping**, not the cookie:
  resolve the requested `<name>` to its `node` from `scope["targets"]`; if `node` is set, verify
  scope/capability **on the controller first**, then call
  `_proxy_to_node(find_node(load_nodes(), node))` (owner creds injected as today). Local targets are
  served locally.
- **Block** the cookie-driven proxy path for guests: in the node-proxy branches (`:4017` GET,
  `:4307` POST) return 403 if `princ["type"]=="guest"` — guests never drive box-switching or
  node/fleet endpoints. Box switcher UI stays owner-only.
- Guest `/api/instances` is built by aggregating only the grant's targets: local targets from the
  normal builder (`:4210`), remote targets fetched per-node via the existing fleet aggregation
  (`_fleet…` `:2867`, `/api/fleet/status`), filtered to allowed `(node,name)` pairs. **This is the
  bulk of the fleet work** — budget for it.

### 4e. Per-route classification
**OWNER-ONLY** (`_require_owner`): `/api/scan`, `/api/proxies`, all `/api/files*`, `/api/settings`,
`/api/update/check`, `/api/selfupdate`, `/api/autoupdate`, all `/api/nodes*` + `/api/node/select` +
`/api/fleet/*`, `/api/box/roots`, `/api/schedules*`, `/api/system/*`, `/api/connection*`,
`/api/backup` + `/api/restore`, `/api/transfer`, `/api/deploy*`, `/api/adopt`,
`/api/instances/add`, `/api/instances/<n>/{delete,rename}`, `/api/proxies/{bulk,webshare,health}`,
and the new `/api/shares*`.

**INSTANCE-SCOPED, capability-aware** (`_require_target` first, then `_require_cap`):
- **view**: GET `/api/instances/<n>/logs` (`:4233`), `/config` (`:4242`), viewer relay (`:4042`),
  control relay state/commands (`:4045`), viewer stream (`:4015`).
- **operate**: POST `/api/instances/<n>/{start,stop,restart}` (`:4670`), `/command` (`:4781`),
  control `/command` (`:4311`).
- **config**: POST `/api/instances/<n>/config` write (`:4242`), `/proxy` (`:4764`), `/limits`
  (`:4795`), `/autostart` (`:4807`).
- Add defense-in-depth `name`-extract + `_require_target` at the top of `_viewer_relay` /
  `_control_relay` and gate `?inst=` in `_serve_control_page` (`:3774`); guest-allow the `/control`
  page + static `/control/<asset>` for in-scope bots.

**GUEST-ALLOWED, filtered**: `/api/instances` (4d), `/control` page for in-scope bots.
**No-auth (unchanged)**: `/api/authstatus`, `/api/login`, `/api/setup*`, login/setup pages, new `/s/<token>`.

---

## 5. UI Changes (all in the embedded `PAGE`/JS in `manager.py`)

### 5a. Owner share-management panel
Owner-only header button near `:6020` (`🔗 Share` → `openShares()`), modeled on the existing
Files/Proxies modals. New owner-only endpoints:
- `GET  /api/shares` → `{shares:[share_public_view(g)…]}` (never the token).
- `POST /api/shares` → `{label, targets|all, capability, ttl_days}` → `new_share` →
  `{ok, share, url}` where `url = "<scheme>://<Host header>/s/<plaintext>"` (one-time reveal).
- `POST /api/shares/revoke` → `{id}`; `POST /api/shares/revoke_all` → `bump_shares_epoch`.

Modal: create form (multi-select of bots incl. their node, or "All local bots"; capability radio
view/operate/config; expiry select 1d/7d/30d/never with a "set one" nudge) → on submit show the URL
in a read-only field + Copy button + "shown once" warning + an HTTPS advisory. Below: table of
existing shares (label, scope, capability, status, created, expires) each with Revoke, plus "Revoke
all links".

### 5b. Guest-facing UI gating
Gate in JS off `/api/authstatus` (5c) — the HTML is one blob, so no fork. If `principal === "guest"`:
hide owner-only header buttons (Settings/Connect/Boxes/Files/Proxies/Scan/Add Bot/
Start-Stop-Restart-all at `:6020-6029`), keep Refresh + Log out, show a "Guest — limited access"
badge. The bot grid already renders only what `/api/instances` returns (server-filtered, so no client
trust needed). Apply a `body.guest-<cap>` class so CSS hides controls above the tier: `view` hides
per-card start/stop/restart, console input, and config Save; `operate` hides only config editing. All
hiding is **cosmetic** — the server enforces 403/404 regardless.

### 5c. Extend `/api/authstatus` (`:3993`)
Add `principal` (`owner`/`guest`/`anon`), and for guests `capability`, `targets`, `all`. Owners get
`principal:"owner"` and no scope fields; the existing login-page JS (`:5648`) only reads `required`,
so it's unaffected. Dashboard boot adds `applyPrincipal(d)` running the 5b gating.

---

## 6. Security Considerations

- **The link is the credential.** 256-bit tokens; store only `sha256`; never log plaintext; compare
  with `hmac.compare_digest`.
- **Expiry + revoke** both enforced at redemption *and* on every request (resolver re-reads
  `instances.json`), so expired/revoked links die immediately even for already-minted sessions.
  `bump_shares_epoch` = revoke-all; owner password change / `bump_session_epoch` also kills guests
  (they carry `gen`).
- **Rate-limit `/s/`** via the existing login limiter.
- **HTTPS advisory** in the share modal: the token travels over whatever transport the manager is
  exposed on; recommend an HTTPS access mode before sharing externally (advisory text only — server
  is plain `http.server`).
- **`SameSite=Lax`** on the redemption cookie only (still CSRF-safe for the mutating POSTs); owner
  stays `Strict`. `HttpOnly` retained for both.
- **No escalation via nodes**: guests blocked from the cookie proxy path + all node/fleet endpoints;
  cross-node access is controller-mediated with owner creds the guest never sees.
- **Enumeration resistance**: out-of-scope instance → 404, identical to a missing bot.
- **Audit logging**: append guest mutations `{ts, grant_id, ip, method, path, target, action}` to an
  in-memory ring buffer (best-effort; `log_message` is silenced at `:3550`); optionally surface recent
  guest actions in the share panel.

---

## 7. Migration / Backward Compatibility

- Loader gains `s.setdefault("shares", [])` and `s.setdefault("shares_epoch", 0)`; old configs load
  unchanged.
- `_new_session(gen=0, scope=None)` keeps its signature; existing callers (`:4273`, `:4300`) pass no scope.
- `_auth_ok` still returns bool; every current call site untouched.
- Open mode (no password) → owner principal, full access as today.
- `authstatus` only **adds** fields. Empty `settings.shares` → none of the new paths run for owners →
  zero behavior change for single-user installs.

---

## 8. Implementation Order

1. **Data + helpers** — `shares`/`shares_epoch` defaults; share helpers after `:2293`.
2. **Session scope** — `_new_session` scope; `_set_session_cookie` `samesite` param.
3. **Principal layer** — `_principal`, `_require_target`/`_require_cap`/`_require_owner`; `_auth_ok`
   wraps `_principal`.
4. **Redemption route** — `/s/<token>` (rate-limited, scoped session, Lax cookie, 302).
5. **Dispatch gating** — replace the two `_auth_ok` gates; guest-block node-proxy; fleet routing;
   `/api/instances` filter; relay/control scope checks.
6. **Owner share endpoints** — `/api/shares*`.
7. **`authstatus` extension**.
8. **Owner share modal + button**.
9. **Guest UI** — `applyPrincipal()` + `body.guest-*` CSS.
10. **Audit logging**.

---

## 9. Verification (manual test matrix)

Run a local `manager.py` with a password set and ≥2 bots (and one remote node for fleet tests). Verify:

1. **Owner unchanged** — login, full dashboard, all tabs/actions, box-switching work as before.
2. **Open mode unchanged** — no password set → full access, no guest paths triggered.
3. **Create link** — owner makes a `view` link for botA; URL returned once; list never shows token.
4. **Redeem** — open `/s/<token>` in incognito (and from a cross-origin click): lands on dashboard,
   sees only botA, badge shows "Guest".
5. **view tier** — logs/viewer/map load; start/stop/command/config-save hidden AND return 403; botB
   absent and `/api/instances/botB/logs` returns 404.
6. **operate tier** — start/stop/restart + console command work on allowed bots; config save still
   403; delete/rename/deploy still 403.
7. **config tier** — config/proxy/limits/autostart edits work on allowed bots; delete/rename still 403.
8. **Fleet** — a link granting a bot on a remote node: guest sees and (per tier) controls it, routed
   through the controller; guest cannot list/select nodes or see other nodes' bots; node creds never
   exposed.
9. **Expiry** — expired link rejected at `/s/`; a live guest session with a now-expired grant dropped
   next request.
10. **Revoke** — per-grant revoke and "revoke all" kill live guest sessions on the next request.
11. **Rate-limit** — repeated bad `/s/<token>` hits get 429 after 5 tries.

---

## 10. Critical Files

- **`manager.py`** — the single source file: all backend auth/session/routing **and** the embedded
  SPA HTML/JS. Every code change in this plan lands here.
- **`instances.json`** — runtime store; new `settings.shares` + `settings.shares_epoch` live here
  (written by code, not hand-edited).
