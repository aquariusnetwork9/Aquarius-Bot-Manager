# Named multi-user accounts with RBAC (ABM)

> Builds on the shipped guest-access model (`shareable-link-guest-access*.md`). That gave us a
> **scoped, capability-tiered principal** (`view < operate < config`) re-validated on every request.
> This adds **named user accounts** (each with their own login) on top of the same machinery.
> Owner decisions (2026-06-26): **role model = fixed tiers + an Admin role**; **scope = owner's
> choice per user (fleet-wide OR specific bots)**; **provisioning = owner-provisioned + invite links**
> (no self-register).

## The core insight

A named non-admin user is *authorization-identical to a guest link* — same `scope = {targets, all,
capability}`, same route gating. The only differences are: it has a **username + persistent account**,
you log in with a password instead of bearing a link, and it shows your name. An **admin** user is
**owner-equivalent**. So the change is small: generalize "guest" → "scoped principal" and make `admin`
fall through the existing owner guards.

## Data model (`settings`)

```
settings.users = [{
  id, username,                 # username unique (case-insensitive)
  salt, hash,                   # PBKDF2 (same as owner)
  role: "view"|"operate"|"config"|"admin",
  all: bool,                    # fleet-wide (all bots); admin is implicitly all
  targets: [{node,name}],       # specific bots when not `all`
  pwgen: int,                   # bump on password change => logs out that user's other sessions
  disabled: bool,               # soft lockout (re-validated live)
  created, last_login
}]
settings.invites = [{
  id, token_hash,               # sha256; full link revealed once at creation
  label, role, all, targets,    # preset grant the invite confers
  username,                     # optional preset (locked) username, else invitee chooses
  created, expires, revoked,
  used_by: <user id>|null,      # consumed once
  epoch                         # = shares_epoch reuse? no — invites use their own active check
}]
```

No new global epoch: like guest grants, **the user is re-resolved from cfg on every request**
(`_principal`), so role/scope edits, `disabled`, and delete take effect instantly. `pwgen` in the
session vs. the user record gives "change password ⇒ log out my other sessions". The existing
`session_epoch` (owner password change / `logout-all`) still nukes *everyone*.

## Principal generalization

- `_new_session(gen, scope=None, user=None)` — `user` mints `{principal:"user", uid, pwgen}`.
- `_principal` resolves a `user` session: look up by uid; drop if missing/disabled/pwgen-mismatch;
  else return `{type:"user", uid, username, role, scope:{targets,all,capability}}` where
  `capability = role` (admin → "config" cap, but it's owner-equiv anyway).
- `_is_owner(princ)` → true for `type=="owner"` **or** (`type=="user"` and `role=="admin"`).
  ⇒ admin users pass every `_guard_owner` / `_cap_ok` owner branch automatically.
- `_guest_gate` and friends already key off `princ["scope"]`; flip the early-return from
  "not guest ⇒ pass" to "**is owner ⇒ pass**", so any non-owner scoped principal (anonymous guest
  **or** named non-admin user) gets the same scope/cap/target gating. Audit uses
  `grant_id or uid`.

## Endpoints (owner/admin only unless noted)

- `POST /api/login` — already username+password. Extend: if not the owner, check `settings.users`
  ⇒ mint a user session. Rate-limited as today.
- `GET /api/users` — list (no hashes). `POST /api/users` — create (username, password OR
  invite-only, role, all/targets). `POST /api/users/<id>` — edit role/scope/disable.
  `POST /api/users/<id>/password` — owner/admin reset. `POST /api/users/<id>/delete`.
- `POST /api/invites` — create (role, all/targets, optional username, ttl) ⇒ one-time URL
  `share_base_url()/i/<token>`. `GET /api/invites` — list pending. `POST /api/invites/<id>/revoke`.
- `GET /i/<token>` — public redemption page (small standalone HTML, like the setup wizard):
  choose username (locked if preset) + password ×2. `POST /api/invite/redeem` {token, username,
  password} — validate active + unique ⇒ create user with the invite's role/scope ⇒ consume ⇒ log in
  (Lax cookie) ⇒ 302 `/`. Rate-limited.
- **True-owner-only:** changing the *owner* account password stays restricted to `type=="owner"`
  (an admin user can't lock out the original owner). Everything else admin == owner.

Owner-account safety: the owner login lives in `settings.auth`, never in `settings.users`, so the
Users UI can't touch it.

## UI (owner/admin)

- **👤 Users** modal (header + sidebar + quick-menu, owner-only class) modeled on the Share modal:
  user list (name, role chip, scope summary, status, last login) with edit/disable/delete; an
  add-user form (username, set-password **or** "send invite link", role radios, scope = All / bot
  picker reusing the share bot picker); an Invites section (create ⇒ one-time link reveal + Copy;
  pending list + revoke).
- `applyPrincipal()`: for `principal==="user"` with a non-admin role, add the **same**
  `body.guest`+`body.guest-<role>` classes (reuse every existing gating rule) and a badge
  "<name> · <role>"; for admin, full UI + an "Admin · <name>" badge. Anonymous guests unchanged.
- `/i/<token>` page: standalone, mirrors LOGIN/SETUP styling.

## Testing

- Unit: user/invite helpers (create/find/verify/unique/disable/pwgen), role→cap map, `_is_owner`
  admin, scope gating reuse.
- HTTP on box1: owner creates user (operate, 1 bot) ⇒ that user logs in ⇒ can operate the granted
  bot, 404 on others, 403 on owner-only; admin user ⇒ full access; invite create ⇒ redeem ⇒ login;
  disable ⇒ instant lockout; password reset ⇒ old session dropped; delete ⇒ instant.
- Dashboard JS `node --check`.

## Ship
v3.5.0 — commit to main, `abm selfupdate` both boxes, wiki (Security §3a sibling + Changelog).
`manager.py`-only; bots untouched.
