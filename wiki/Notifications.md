# Notifications

Push alerts without Discord. **[ntfy](https://ntfy.sh)** is the default — no account, no token,
subscribe to a topic and go. **[Apprise](https://github.com/caronc/apprise)** is an optional
add-on for Telegram/Pushover/Slack/etc. Discord still works if you already have a webhook — it's
just no longer the required path.

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

Each bot's *own* notifications (a player entering visual range, the bot connecting/disconnecting
from the MC server) are a separate, per-bot setting — see [Per-bot events](#per-bot-events) below.

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

Visual range and MC connect/disconnect/login-failure alerts come straight from AquariusProxy
itself (it sees these directly; the manager doesn't). Configure them per bot in the instance
drawer's **Config** tab, under **Notifications → Ntfy Notifications** — same server/topic fields,
plus enable/priority/tags for `visualRange`, `offline`, `online`, and `proxyEvent`. Point every
bot at the same topic as the manager's own alerts (the default) to get everything in one place, or
give a bot its own topic if you want to isolate it.

## Self-hosting ntfy

Point either config at your own server instead of `ntfy.sh` — set **Server** to your instance's
base URL (e.g. `https://ntfy.example.com`). See the
[ntfy self-hosting docs](https://ntfy.sh/docs/install/). The public `ntfy.sh` instance rate-limits
free publishing (60 messages, refilling 1/5s) — self-hosting removes that if you run a busy fleet.

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
