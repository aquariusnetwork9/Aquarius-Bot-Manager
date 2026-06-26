# Guest Access — v3.1 Reconciliation Addendum

> Companion to **`shareable-link-guest-access.md`**. That plan was written against
> `manager.py @ 31fade85` (**ABM v3.0.0**). Mainline is now **`95df695` (v3.1.0)**, which added the
> live config API (`/control/config`). This addendum reconciles the plan with the current code so it
> can be implemented as-is. **The design is sound and unchanged in substance** — only the items below.

## A. Anchor drift (small, localized)

The auth/session core is **byte-identical** to the plan (my v3.1 edits were all in the control/relay
area ~3900+):

| Symbol | Plan | Now |
|---|---|---|
| `_SESSIONS`, `_new_session`, `_session_valid`, `bump_session_epoch`, `_hash_password`, `verify_password`, `_rate_limited`, `_record_fail` | 2213–2293 | **identical** |
| `_cookie_token`/`_auth_required`/`_needs_setup`/`_auth_ok`/`_set_session_cookie` | 3554/3562/3566/3574/3595 | **identical** |
| `_selected_node`/`_is_switcher_path`/`_proxy_to_node` | 3668/3680/3696 | **identical** |
| `_serve_control_page`/`_serve_control_asset`/`_viewer_relay`/`_control_relay` | 3774/3796/3820/3879 | **identical** |
| `do_GET` auth gate `if not self._auth_ok` | :4005 | **:4007** (+2) |
| `do_POST` auth gate | :4302 | **:4304** (+2) |
| node-proxy GET / POST | :4017 / :4307 | **:4020 / :4310** |

Everything in the do_GET/do_POST body shifted **+2**. **Action:** implement by **symbol/regex anchor**,
not literal `:line`. Verified current instance-route lines: `/logs` 4235, `/config` GET 4244 / POST
4821, `/start|stop|restart` 4672, `/command` 4783, `/proxy` 4766, `/limits` 4797, `/autostart` 4809,
`/delete` 4641, `/rename` 4657, `/api/instances/add` 4620.

## B. Fold in `/control/config` (the one real delta)

v3.1 added two routes the plan's §4e table doesn't list. They map cleanly onto the tiers:

| Endpoint | Path now | Tier |
|---|---|---|
| `GET /control/config` (read live config, secrets redacted bot-side) | relayed via `_control_relay`, `sub=config`, GET | **view** |
| `POST /control/config` (write one field) | do_POST `path.endswith("/control/config")` → `_control_relay` `sub=config` POST (:4313) | **config** |

`_control_relay` now serves **four** subs, so the capability check inside it must be
**(sub, method)-aware**, not per-relay. It already computes the read/write split at `:3899`
(`is_post = sub == "command" or (sub == "config" and POST)`) — reuse that shape for the cap map:

```
state | commands            -> view
config + GET                 -> view
command                      -> operate
config + POST                -> config
```

So §4e's "control relay state/commands → view, control `/command` → operate" becomes the 4-row map
above, and the `_require_cap` call lands **inside `_control_relay`** keyed on `(sub, self.command)`
(plus `_require_target` on the resolved instance name at the top of both relays, as §4e already says).

This is a **net positive** for the model: the `config` tier now has a real, first-class write surface
(live config editing) instead of only the file-based `/api/instances/<n>/config` save. Both are
`config`-tier. And the bot already redacts/denies secrets server-side — defense-in-depth under the
manager's tiering.

## C. Two UI refinements (extend §5b)

1. **⚙ Live configuration panel** (control-live.js, v3.1) writes via `POST /control/config`. For
   `view`/`operate` guests the server returns 403 (correct), but the panel should be **visibly
   read-only** for them. Extend the `body.guest-view` / `body.guest-operate` gating to disable the
   `.lcPanel` inputs/toggles (cosmetic; server still enforces). `body.guest-config` leaves it live.
2. The control surface's **command runner + module toggles** (control-live.js) issue
   `/control/command`. Those are **operate**-tier — hide them under `body.guest-view` along with the
   other operate controls.

## D. Fleet routing must cover the v3.x viewer/control endpoints for guests

§4d routes guest instance requests by the **grant's target node** (not the `abm_node` cookie) and
proxies remote targets through the controller. Make sure that resolution also wraps the endpoints
added since the plan:

- `_viewer_relay` (`/viewer/{state,map,chunks,inventory}`) and `_control_relay`
  (`/control/{state,commands,command,config}`) — for a guest target on a **remote node**, these must
  proxy to that node (today they call `viewer_port_for` on the **local** bot). Resolve node from
  `scope.targets` first; if remote, `_proxy_to_node(find_node(...))`.
- **`/viewer/stream`** (SSE, `_viewer_stream(path,q,node)`, :4017) deliberately **bypasses**
  `_proxy_to_node`. It currently picks its upstream from `_selected_node`. For a guest it must use the
  **grant-resolved node**, never the cookie. Add a guest branch that passes the resolved node.

(For the common case — all granted bots are local to the controller — none of this fires; it only
matters for fleet-scoped links.)

## E. One decision for the owner — `operate` = full console command execution

`operate` grants `POST /control/command` and `/api/instances/<n>/command`, i.e. the guest can run
**any console command the bot supports** (the control-surface command runner is free-form; module
on/off toggles and "Fly there" also go through it). That's broad: a determined `operate` guest could
invoke commands that change behavior well beyond "start/stop". The plan intends this (command-send =
operate), and **restricting it would break the operate-tier control surface** (toggles/flight all use
commands).

**Options:**
- **(a) Keep as designed** — `operate` = full command access on granted bots. Simple; the control
  surface works fully at operate tier. *(recommended; matches the plan)*
- **(b) Allow-list** — guests may only run a curated command set; free-form runner hidden. Safer, but
  more code and a degraded operate UX.
- **(c) Free-form → config tier** — keep module toggles/quick-actions at operate, but gate the
  free-form command box to `config`.

Default if unspecified: **(a)**.

## F. Audit (extend §6)

Include the v3.1 surfaces in the guest audit ring buffer: log `POST /control/config`
`{path, value}` and `POST /control/command` `{command}` with `{ts, grant_id, ip, target}`.

## G. Net effect on the plan

- §2/§3 (data model, redemption, sessions): **unchanged**.
- §4a–4c (principal, guards): **unchanged**.
- §4d (fleet routing): **unchanged in approach**; just ensure it wraps the viewer/control relays +
  SSE (item D).
- §4e (route table): **add the two `/control/config` rows** (item B); make `_control_relay`'s cap
  check (sub, method)-aware.
- §5b (guest UI): **add** the ⚙ Live-config panel + command-runner gating (item C).
- §6 (audit): **add** `/control/config` + `/control/command` (item F).
- §8 implementation order: unchanged; do item B as part of step 5, item C as part of step 9.

No blockers. Pending the §E decision, this is ready to build.
