# Custom domain — one address for your whole fleet

Goal: everyone (you and your users) types **one address** — e.g. `bots.example.com` — to reach the manager, instead of opening an SSH tunnel every time. This page wires a **domain on Cloudflare** to a **Cloudflare Tunnel**, so no ports are opened, the VPS IP stays hidden, and Cloudflare terminates TLS with a valid cert.

> This is a deployment recipe built entirely from features that already ship in ABM — the **Multi-VPS controller** ([[Multi-VPS Controller]]) and the **Public sharing** provider menu ([[Security#public-sharing]]). No new install of anything on your side beyond what ABM fetches for you.

---

## The shape of it

```
   you + your users ──▶ https://bots.example.com  (Cloudflare edge, TLS)
                                   │ Cloudflare Tunnel (cloudflared, outbound only)
                          Controller VPS  manager on 127.0.0.1:8765
                                   │  controller-managed ssh -N -L, one per node
              ┌────────────────────┼────────────────────┐
        node A (127.0.0.1:8765)  node B …            node C …
```

- **One controller VPS** owns the domain. The manager keeps its default **`127.0.0.1`** bind; `cloudflared` runs beside it and is the only thing reachable from the internet — through Cloudflare's edge, so **no inbound ports are opened** on the box.
- **Every other VPS is a node** ([[Multi-VPS Controller]]) — also `127.0.0.1`-only, reached by the controller over an SSH-key tunnel. Users never touch the nodes directly; they switch into them **inside the same tab** from the box switcher.

Net result: `https://bots.example.com` is the single login page for the whole fleet. No per-session `ssh -L`.

---

## Prerequisites

- The controller VPS already runs ABM (a normal install — [[Installation]]). It can stay in the default **SSH-tunnel** mode; you don't need the Caddy/HTTPS install mode for this path.
- **A dashboard password is set** (Settings → Account, or `abm setpassword`). Public sharing **refuses to turn on without a login** — an exposed open dashboard would hand full control to anyone with the URL. See [[Security]].
- Your domain's DNS is managed on **Cloudflare** (nameservers point at Cloudflare). Because DNS and the tunnel live in the same place, the tunnel creates the DNS record for you — you don't add one by hand.

---

## Step 1 — create the tunnel in Cloudflare

In the **Cloudflare Zero Trust** dashboard (one-time, free):

1. **Networks → Tunnels → Create a tunnel** → type **Cloudflared** → name it (e.g. `abm`).
2. On the **install** screen, copy the **tunnel token** — the long string at the end of the shown `cloudflared … run --token <TOKEN>` command. (You don't run that command yourself; ABM does.)
3. Add a **Public Hostname**:
   - **Subdomain / Domain:** the address you want, e.g. `bots.example.com` (pick the domain from the dropdown; leave subdomain blank for the apex, or set one like `bots`).
   - **Service:** **HTTP** → **`127.0.0.1:8765`** (the manager's local port; change `8765` if you run a custom `ABM_PORT`).
4. Save. Cloudflare **auto-creates the DNS record** (a proxied CNAME to the tunnel) — nothing to add in the DNS tab.

---

## Step 2 — turn on Public sharing on the controller

Reach the controller's dashboard **one last time over the SSH tunnel** (the line the installer printed), then:

1. Header → **👥 Share** → **Public sharing** card → provider dropdown → **Cloudflare Tunnel (your domain)**.
2. Paste the **Tunnel token** (from Step 1) and the **Public hostname** (`bots.example.com`).
3. **Enable public sharing.** ABM downloads `cloudflared` for you, launches `cloudflared tunnel run --token …`, and the address goes live.

The tunnel runs **detached and is adopted across manager restarts / self-updates**, so the URL survives reboots and `abm selfupdate` — it only changes if you change it. Switching providers later cleanly stops this one.

You can now close the SSH tunnel for good and open **`https://bots.example.com`**.

> The token is stored **base64-obfuscated** in `instances.json` and never sent back to the browser (same handling as the Webshare token — obfuscation, not encryption). Rotate it in Cloudflare if the box is ever compromised. See [[Security#4-whats-stored-on-disk-instancesjson]].

---

## Step 3 — bring your other VPSs in as nodes

On each **other** VPS, install in node mode (stays private, no Caddy):

```bash
curl -fsSL https://raw.githubusercontent.com/aquariusnetwork9/Aquarius-Bot-Manager/main/install.sh | ABM_ACCESS=node bash
```

Then, from the **controller** dashboard: header → **🖥 Boxes** → **Connect a box** → paste `user@host` (add `:port` if SSH isn't on 22). Headless equivalent:

```bash
abm node add box2 ubuntu@1.2.3.4      # register + open the tunnel + test
```

Now the **box switcher** (sidebar chip / brand pill) lists the controller and every node with a live online dot — pick one to reverse-proxy its **full dashboard into the same tab**. Full details: [[Multi-VPS Controller]]. Your users reach every box through `https://bots.example.com` alone.

---

## Step 4 — give your users the address

With public sharing on, every link ABM generates now points at `https://bots.example.com` instead of `localhost`:

- **Named accounts** (owner/admin → **👤 Users**) — real per-person logins with roles (view / operate / config / admin) scoped to specific bots, or a **one-time invite link** the invitee opens to set their own password. See [[Security#named-user-accounts]].
- **Shareable guest links** (**👥 Share**) — a single scoped URL, no account needed, with expiry + instant revoke. See [[Security#3a-shareable-link-guest-access-v320]].

Hand out the invite/guest links (or just the address + a login you created). Because sharing is on, they resolve to your public hostname and actually open for the recipient.

---

## Optional: put Cloudflare Access in front

Since the tunnel already runs through Cloudflare Zero Trust, you can add an **Access application** on `bots.example.com` (Zero Trust → **Access → Applications**) to require Cloudflare-side auth — email OTP, Google/GitHub SSO, or an allowlist — **before** anyone even reaches ABM's login. This is an extra gate on top of the dashboard password, not a replacement: ABM's own login is still what enforces roles and per-bot scopes.

---

## Alternative: Caddy + Let's Encrypt (open 443 instead of a tunnel)

If you'd rather expose the box directly (public IP, ports 80/443 reachable) and skip Cloudflare Tunnel, the installer's HTTPS mode fronts the manager with Caddy and fetches a trusted cert automatically:

```bash
curl -fsSL https://raw.githubusercontent.com/aquariusnetwork9/Aquarius-Bot-Manager/main/install.sh \
  | sudo ABM_ACCESS=https ABM_DOMAIN=bots.example.com bash
```

Point an **A record** for `bots.example.com` at the VPS IP first (on Cloudflare, set it **DNS-only / grey-cloud** so Let's Encrypt's HTTP-01 challenge reaches Caddy). Everything else on this page (nodes, users, links) is identical — only how TLS is terminated differs. See [[Installation]] and [[Security#2-network-exposure]].

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "Set a dashboard password first" when enabling sharing | No login is set. Set one (Settings → Account or `abm setpassword`) — sharing won't expose an open dashboard. |
| `bots.example.com` shows a Cloudflare 1033 / "tunnel not found" | `cloudflared` isn't running or the token is wrong. Re-check the token in the Share panel; confirm the tunnel shows **Healthy** in Zero Trust → Tunnels. |
| Loads but 502 / "bad gateway" | The Public Hostname's Service isn't `http://127.0.0.1:<port>`, or the manager isn't on that port. Match it to your `ABM_PORT` (default `8765`). |
| URL changed after a reboot | You used the **Quick Tunnel** (`*.trycloudflare.com`) provider, which re-rolls. Use **Cloudflare Tunnel (your domain)** for a stable hostname. |
| A node shows offline in the switcher | The controller's SSH tunnel to it dropped. Hit **↻ Reconnect** on its row (Boxes panel); check the SSH target/key. See [[Multi-VPS Controller]]. |

---

**Read before exposing anything:** owner/admin dashboard access is **equivalent to a shell on the box**. [[Security]] covers this in full — read it before handing the address to anyone.
