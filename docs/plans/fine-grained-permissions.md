# Fine-grained permissions (owner controls what each user can do)

> Extends the v3.5.0 named-user RBAC (`named-multi-user-rbac.md`). Owner decisions (2026-06-26):
> **model = custom roles + per-user overrides (Both)**; **control axes = module visibility/use,
> module config-edit, free-form console, bot lifecycle**; **free-form console hidden + server-enforced
> unless granted**. Build per-user perms first (foundation), custom roles as a follow-up — both share
> the same permission-set machinery.

## Permission set (shared by per-user overrides and, later, custom roles)

```
perms = {
  use_all:    bool,   # may use (see + toggle on/off) every module
  config_all: bool,   # may edit every module's live config (implies use)
  modules:    { "<moduleId>": {"use": bool, "config": bool} },   # per-module when not *_all
  console:    bool,   # the free-form command box + parameterized actions (map-click fly, etc.)
  lifecycle:  bool,   # start / stop / restart the bot
}
```
Stored optionally on a user as `u["perms"]`. **Absent ⇒ full-within-tier** (current behavior — no
regression). Perms only ever *restrict* within the role tier; they never grant below it.

### Effective resolution (`resolve_perms`)
- **admin / owner** ⇒ everything (perms ignored).
- **view tier** ⇒ read-only: no use/config/console/lifecycle regardless of perms.
- **operate tier** ⇒ default use_all + console + lifecycle; perms restrict (drop console/lifecycle,
  limit modules). No config.
- **config tier** ⇒ operate + config_all; perms restrict which modules' config.
- `module_use(id)`  = tier≥operate AND (use_all OR config_all OR modules[id].use OR modules[id].config)
- `module_config(id)` = tier≥config AND (config_all OR modules[id].config)
- `console` = tier≥operate AND perms.console
- `lifecycle` = tier≥operate AND perms.lifecycle

## Module registry (manager.py)
`CONTROL_MODULES = [(id, name, raw, cat), …]` — 48 entries ported from `control/abm-control-data.js`
(the single source of truth). Derived: `_MOD_BY_ID`, `_RAW2MOD` (raw.lower()→id, + aliases
liveviewer→livemap, pearlmanager→pearl). Used to map a `/control/command` toggle (`<rawlower> on|off`)
and a `/control/config` path (`client.extra.<lcfirst(raw)>.…`) back to a module id for authorization.

## Enforcement (manager-side; guests use the existing tier model unchanged)
For `principal.type == "user"` (non-admin):
- **`/control/command` (POST, in `_control_relay`):** read the body command `c`. Allow iff
  `effective.console` **OR** `c` matches `^<rawlower>\s+(on|off)$` for a module with `module_use`.
  Else 403. (Deny-by-default: parameterized/free-form/`fly`/`highway` commands need console.)
- **`/control/config` (POST, in `_control_relay`):** the path must be `client.extra.<X>` mapping to a
  module with `module_config`; any other root (authentication/discord/database/server) ⇒ 403 for users
  (secrets stay owner/admin-only; the bot also redacts). Else relay.
- **lifecycle (`/start|/stop|/restart`, in `_guest_gate`, path-based):** require `effective.lifecycle`.
- Reads (`/control/state|commands`, `/control/config` GET, viewer, logs) stay view-tier; the module
  *list* is filtered in the control UI (low-risk to show names).

## UI
- **Users modal → per-user "Permissions" editor** (button on each user row; opens a panel): console
  toggle, lifecycle toggle, and a module **use/config matrix grouped by category** with "all" shortcuts.
  Saved via `POST /api/users/<id> {perms}`. New users default to full-within-tier; owner restricts.
- **control-live.js:** `fetchPrincipal()` also captures `perms` for `principal==='user'`; hide module
  nav rows/cards without `use`, make the ⚙ config panel read-only without `config`, hide the command
  runner + map-click + parameterized action buttons without `console`. (Server still enforces.)
- `GET /api/users` returns each user's `perms` + a `modules` catalog (id/name/cat) for the editor;
  `/api/authstatus` returns the caller's effective `perms` for control-live.

## Test
Unit: resolve_perms across tiers + module_use/config. HTTP box1: operate user with only AutoMiner →
`automine`… wait, toggle `aquariusminer on` allowed, `autoeat on` 403, free-form `fly` 403, config of
allowed module ok / disallowed 403, lifecycle gate, console grant flips it; admin bypass; guest links
unchanged. JS `node --check`.

## Ship
v3.6.0 (per-user perms). Then v3.7.0 = custom roles (reusable `settings.roles[]` with a perms set,
assignable as a user's role; resolve = role perms ∘ per-user overrides).
