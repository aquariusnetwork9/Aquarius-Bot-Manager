# Notifications

Push alerts without Discord. **[ntfy](https://ntfy.sh)** is the default — no account, no token,
subscribe to a topic and go. **[Apprise](https://github.com/caronc/apprise)** is an optional
add-on for Telegram/Pushover/Slack/etc. Discord still works if you already have a webhook — it's
just no longer the required path.

Two layers, both ntfy-based, and independent of each other:

- **Fleet-wide topic** (this page, below) — one shared channel for the manager's own alerts
  (jobs/watchdog/bot online-offline) and, optionally, every bot's raw events. Set up once, everyone
  who scans the topic gets everything.
- **🔔 My Notifications** (a dashboard page, not a Settings tab) — each person picks *exactly*
  which bot + which event they personally want, delivered to **one personal topic** they scan
  once, ever. See [My Notifications](#my-notifications) below. Purely additive — the fleet-wide
  topic keeps working unchanged whether or not anyone uses this.

Configured in the dashboard: **⚙ Settings → Notifications**.

---

## Quick start

1. Install the [ntfy app](https://ntfy.sh/#subscribe) (or just open `https://ntfy.sh/<topic>` in a
   browser/mobile home-screen shortcut).
2. Open **Settings → Notifications** in the dashboard. A topic like `a1b2c3-alerts` is generated
   for you the first time you open the tab — it's random so a stranger can't guess it and
   subscribe to your alerts. Copy it, or scan the QR code shown next to it.
3. Subscribe to that topic in the ntfy app.
4. Flip **Enabled** on and hit **Save**. Use **Send test** to confirm delivery.

That's it — no further config needed for the manager's own alerts (scheduled jobs, watchdog
restarts, a bot's process going offline/coming back).

Each bot's *own* notifications (visual range, combat/safety, proxy security, queue, account
alerts, connect/disconnect, maintenance) are a separate, per-bot setting — see
[Per-bot events](#per-bot-events) below. Want only *some* of them, personally, without sharing one
big topic with everyone? See [My Notifications](#my-notifications).

---

## Fleet events (from this manager)

| Event | Default priority | Notes |
|---|---|---|
| Job ran | 3 | any scheduled job with **Notify** on |
| Watchdog restarted a bot | 4 | on-crash watchdog jobs with **Notify** on |
| Bot process offline | 4 | tmux session died — independent of whether a watchdog job exists for that bot |
| Bot process online | 2 | recovered from offline |

Priority is ntfy's 1 (min, silent) – 5 (urgent, bypasses phone DND) scale. Enable/priority/tags
per event are editable in the Notifications tab. Events left at priority 1 with **Batching**
enabled get collapsed into one digest per window instead of pinging individually.

## Per-bot events

These come straight from AquariusProxy itself (it sees them directly; the manager doesn't) —
requires AquariusProxy 5.12.3+ for the original 4, 5.13.2+ for the other 18. Configure them per
bot in the instance drawer's **Config** tab, under **Notifications → Ntfy Notifications** — same
server/topic fields as the manager's own, plus enable/priority/tags **per category**:

| Category | Default | Events |
|---|---|---|
| *(ungrouped, always on by default)* | on | `visualRange` (player enters/leaves/logs out nearby), `offline`/`online` (MC connect/disconnect), `proxyEvent` (login failed) |
| **Combat/safety** | off | player attacked you, death, health-auto-disconnect triggered, totem popped, out of totems, out of food |
| **Proxy security** | off | a non-whitelisted or blacklisted player tried to connect and was blocked |
| **Queue** | off | fully online (with queue wait time), priority-queue status gained/lost, queue warning threshold crossed |
| **Account** | off | Microsoft device-code login needs approval (action-required) |
| **Proxy access** | off | a real client or spectator connected/disconnected from *your* proxy (not the target server) |
| **Maintenance** | off | update available, a plugin failed to load |

The original 4 default **on** (matching earlier releases); the 6 new categories default **off** —
a deliberately conservative expansion, since 22 individual toggles would otherwise be a lot to
review on upgrade. Turn on whichever categories you actually want; each category is one
enabled/priority/tags row, not one row per event. Point every bot at the same topic as the
manager's own alerts (the default) to get everything in one place, or give a bot its own topic to
isolate it — or use **My Notifications**, below, instead of hand-managing topics at all.

## My Notifications

**🔔 per-person routing** — a dashboard page (not a Settings tab, reachable from the main nav)
where **each person** — you, a named user, or someone on a guest share link — picks exactly what
*they* personally want, per bot, delivered to **one personal ntfy topic** generated for them the
first time they open the page. Scan its QR once; every bot/event you tick afterward just starts
showing up on that same phone subscription — no re-scanning per bot.

**How it differs from the fleet-wide topic above:** that's one shared channel, everyone hears
everything they subscribed to. This is server-side filtering — the manager relays each bot's
events, checks who opted into *that specific* bot + event, and only forwards a copy to them. Two
people managing the same fleet can have completely different alert sets on completely different
phones, with zero ntfy topic-juggling on their end.

**Setup:** open the page, tick boxes per bot (grouped the same way as the categories above),
**Save**. The first time you tick *any* per-bot proxy event, the manager automatically points that
bot's own `notifications.ntfy.server` at itself (a local relay) — your original topic/server
setting for that bot (if any) keeps receiving everything unchanged too, this is additive, not a
replacement.

**Access control:**
- If an admin removes your access to a bot (existing per-user/per-share targeting, see
  [[Security]]), your personal notifications for that bot **stop automatically** on the very next
  event — no separate cleanup step, it's re-checked live every time.
- Admins get a direct **🔕 Mute** button next to each user/share row (Users/Shares panels) to
  silence someone's personal alerts without touching their dashboard access at all.

**Privacy:** the QR/link on this page is generated **client-side** (no third-party image API sees
your topic). Everyone's personal topic is a random, unguessable string — same security model as
the fleet topic.

## Self-hosting ntfy

Point any of the configs above at your own server instead of `ntfy.sh` — set **Server** to your
instance's base URL (e.g. `https://ntfy.example.com`). This is a single shared setting: the fleet
topic, every bot's own topic, and every person's **My Notifications** personal topic all move
together when you change it. The public `ntfy.sh` rate-limits free publishing (60 request burst,
refilling ~1/5s) — self-hosting removes that, and is worth doing once you have more than a couple
of people/bots generating events.

**Install** (Debian/Ubuntu, official repo — see [docs.ntfy.sh/install](https://docs.ntfy.sh/install/)
for the current exact commands):
```bash
sudo mkdir -p /etc/apt/keyrings
curl -fsSL -o /etc/apt/keyrings/ntfy.gpg https://archive.ntfy.sh/apt/keyring.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/ntfy.gpg] https://archive.ntfy.sh/apt stable main" \
  | sudo tee /etc/apt/sources.list.d/ntfy.list
sudo apt update && sudo apt install ntfy
sudo systemctl enable --now ntfy
```
Runs as its own systemd unit + dedicated `ntfy` user — independent of the manager, so an ABM
restart/self-update never takes notifications down (and vice versa).

**`/etc/ntfy/server.yml`** — the settings worth changing from default for a private, single-tenant
instance:
```yaml
listen-http: "127.0.0.1:2586"       # loopback only - a tunnel fronts it, never bind 0.0.0.0 directly
behind-proxy: true                   # trust forwarded client IPs once behind a tunnel/reverse proxy
auth-default-access: "read-write"    # no accounts - unguessable topic names are the access boundary,
                                      # same model as ntfy.sh itself
visitor-request-limit-burst: 10000   # was 60 by default - the actual fix for 429s on a busy fleet
visitor-request-limit-replenish: "1s"  # was 5s by default
```

**Reachability without a domain, or from behind a home router/no-port-forward box:** don't open a
port — front it with a **Cloudflare Tunnel**, exactly the mechanism [[Custom Domain]] already
documents for exposing the dashboard itself. If you already have a domain routed through Cloudflare
(and already run a named tunnel for the dashboard, per [[Custom Domain]]), the cheapest path is
**reusing that same tunnel**: add a second **Public Hostname** (e.g. `ntfy.example.com` →
`http://127.0.0.1:2586`) to the existing tunnel in Cloudflare Zero Trust — no new tunnel, no new
`cloudflared` process, and it takes effect within seconds (named tunnels pull their ingress config
from Cloudflare, no restart needed on the box). No domain at all? **Tailscale Funnel** is the
better no-domain option here specifically (not the dashboard's Cloudflare *Quick* Tunnel) — a ntfy
subscription is pinned to one URL for months, and a Quick Tunnel's URL re-rolling on every restart
would silently break every phone's subscription.

**Gotcha — Cloudflare error 1010 ("banned browser signature"):** if you front a self-hosted ntfy
with a Cloudflare Tunnel, requests sent with no/a default scripting-library User-Agent (notably
Python's `urllib` default) get **hard-blocked** by Cloudflare regardless of your ntfy server's own
config — confirmed live, `curl` and any request with a real custom User-Agent pass fine, bare
`urllib` does not. ABM's own sends already set an explicit `User-Agent` (fixed as of the version
that shipped this section) and AquariusProxy's Java client already sets its own — but if you're
scripting anything else against a Cloudflare-fronted ntfy server, make sure it sets a real
User-Agent header or you'll see silent 403s with no clue why.

## Apprise (advanced)

Optional — only needed for Telegram/Pushover/Slack/etc, ntfy needs none of it. The Notifications
tab shows a **📦 Install Apprise** button if the package isn't present on this box (`pip install
--user apprise`); once installed, paste one [Apprise URL](https://github.com/caronc/apprise#supported-notifications)
per line (e.g. `tgram://token/chat_id`, `pover://user@token`) and save. Apprise is never a hard
dependency of the manager — it's a plain Python file with **no third-party packages required** to
run; this is the one opt-in exception, gated entirely behind that install button.

## Discord (legacy)

Still supported — paste a webhook URL under **Legacy: Discord webhook** in the Notifications tab.
It fires alongside ntfy/Apprise for the same fleet events. New installs don't need this at all.
