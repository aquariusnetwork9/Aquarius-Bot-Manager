#!/usr/bin/env bash
# Aquarius Bot Manager — fresh-VPS installer.
#
#   curl -fsSL https://raw.githubusercontent.com/aquariusnetwork9/Aquarius-Bot-Manager/main/install.sh | sudo bash
#
# That one command installs everything and ends by printing the single line you
# paste on your own computer to reach the UI. You set the login in the browser
# on first visit — there is no separate CLI password step.
#
# Non-interactive / scripted overrides (env vars):
#   ABM_ACCESS=tunnel            keep on localhost, reach it over an SSH tunnel (default)
#   ABM_ACCESS=https             expose over HTTPS via Caddy (set ABM_DOMAIN for a real cert)
#   ABM_DOMAIN=bots.example.com  domain pointed at this VPS (HTTPS mode; omit for a self-signed cert)
#   ABM_RUN_USER=ubuntu  ABM_PORT=8765  ABM_BASE_DIR=/home/ubuntu/zenith
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (use sudo)."; exit 1
fi

RUN_USER="${ABM_RUN_USER:-${SUDO_USER:-ubuntu}}"
if ! id "$RUN_USER" >/dev/null 2>&1; then
  echo "User '$RUN_USER' does not exist. Re-run with ABM_RUN_USER=<existing user>."; exit 1
fi
INSTALL_DIR="/opt/aquarius-bot-manager"
REPO="${ABM_REPO:-https://github.com/aquariusnetwork9/Aquarius-Bot-Manager}"
PORT="${ABM_PORT:-8765}"
USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
BASE_DIR="${ABM_BASE_DIR:-$USER_HOME/zenith}"

# ---- choose how the UI is reached ------------------------------------------
# tunnel = stay on 127.0.0.1 (most secure, default); https = public via Caddy.
ACCESS="${ABM_ACCESS:-}"
if [ -z "$ACCESS" ]; then
  if [ -r /dev/tty ]; then
    echo
    echo "How do you want to reach the web UI from your own computer?"
    echo "  1) SSH tunnel   — keep it private on localhost (recommended, no exposure)"
    echo "  2) Public HTTPS — open it on https://<this-vps> via Caddy (needs a domain for a trusted cert)"
    printf "Choose [1/2] (default 1): " > /dev/tty
    read -r choice < /dev/tty || choice=""
    case "$choice" in 2|https|HTTPS) ACCESS="https" ;; *) ACCESS="tunnel" ;; esac
    if [ "$ACCESS" = "https" ] && [ -z "${ABM_DOMAIN:-}" ]; then
      printf "Domain pointed at this VPS (blank = self-signed cert with a browser warning): " > /dev/tty
      read -r ABM_DOMAIN < /dev/tty || ABM_DOMAIN=""
    fi
  else
    ACCESS="tunnel"   # non-interactive default: never auto-expose
  fi
fi

echo "==> Installing Aquarius Bot Manager  (user=$RUN_USER dir=$INSTALL_DIR base=$BASE_DIR port=$PORT access=$ACCESS)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 tmux unzip wget git curl

# fetch or update the manager
if [ -d "$INSTALL_DIR/.git" ]; then
  echo "==> Updating existing install"
  git -C "$INSTALL_DIR" pull --ff-only || true
else
  rm -rf "$INSTALL_DIR"
  git clone --depth 1 "$REPO" "$INSTALL_DIR"
fi
chmod +x "$INSTALL_DIR/abm" 2>/dev/null || true
ln -sf "$INSTALL_DIR/abm" /usr/local/bin/abm

# base dir for proxy instances
mkdir -p "$BASE_DIR"

# starter config so the web UI can start before any instances exist
if [ ! -f "$INSTALL_DIR/instances.json" ]; then
  cat > "$INSTALL_DIR/instances.json" <<JSON
{
  "instances": [],
  "settings": { "base_dir": "$BASE_DIR" }
}
JSON
fi
chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR" "$BASE_DIR"

# enable lingering so per-instance cgroup limits (systemd --user scopes) work headless
loginctl enable-linger "$RUN_USER" 2>/dev/null || \
  echo "WARN: could not enable linger; per-instance resource limits may not enforce."

# install systemd units (patch the run user + bind port from the bundled templates)
for unit in aquarius-bot-manager.service aquarius-bot-manager-boot.service; do
  sed -e "s/^User=.*/User=$RUN_USER/" \
      -e "s#serve --host 127.0.0.1 --port 8765#serve --host 127.0.0.1 --port $PORT#" \
      "$INSTALL_DIR/$unit" > "/etc/systemd/system/$unit"
done

systemctl daemon-reload
# enable = start automatically on every VPS boot/reboot (no SSH needed); --now also starts it right away
systemctl enable --now aquarius-bot-manager.service
systemctl enable aquarius-bot-manager-boot.service

# confirm the manager is wired to boot, so the operator knows it's hands-off
if systemctl is-enabled --quiet aquarius-bot-manager.service; then
  echo "==> Manager enabled to start automatically on every boot (systemd). It is also running now."
else
  echo "WARN: could not confirm boot-autostart (systemctl is-enabled failed). Check: systemctl status aquarius-bot-manager"
fi

# best-effort public IP for the printed instructions
PUBIP="$(curl -fsSL --max-time 5 https://api.ipify.org 2>/dev/null || true)"
[ -z "$PUBIP" ] && PUBIP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$PUBIP" ] && PUBIP="<this-vps-ip>"

# ---- HTTPS path: front the manager with Caddy --------------------------------
if [ "$ACCESS" = "https" ]; then
  echo "==> Setting up HTTPS via Caddy"
  if ! command -v caddy >/dev/null 2>&1; then
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -y
    apt-get install -y caddy
  fi
  if [ -n "${ABM_DOMAIN:-}" ]; then
    cat > /etc/caddy/Caddyfile <<CADDY
${ABM_DOMAIN} {
    reverse_proxy 127.0.0.1:${PORT}
}
CADDY
    UI_URL="https://${ABM_DOMAIN}"
    CERT_NOTE=""
  else
    cat > /etc/caddy/Caddyfile <<CADDY
:443 {
    tls internal
    reverse_proxy 127.0.0.1:${PORT}
}
CADDY
    UI_URL="https://${PUBIP}"
    CERT_NOTE="  (self-signed cert — your browser will warn once; click through. Set ABM_DOMAIN for a trusted cert.)"
  fi
  systemctl enable --now caddy 2>/dev/null || systemctl restart caddy
  systemctl reload caddy 2>/dev/null || systemctl restart caddy

  cat <<DONE

==> Done. Aquarius Bot Manager is live.

Open it in your browser:
    ${UI_URL}
${CERT_NOTE}
First visit walks you through creating your login. Then click 🚀 Deploy to add proxies.
A login is required in this mode — don't skip it, since the port is public.
DONE
else
  cat <<DONE

==> Done. Aquarius Bot Manager is running privately on 127.0.0.1:${PORT}.

Open it in 2 steps:
  1) On YOUR computer, paste this (keep the terminal open):
       ssh -L ${PORT}:127.0.0.1:${PORT} ${RUN_USER}@${PUBIP}
  2) Open http://localhost:${PORT}

First visit walks you through creating your login. Then click 🚀 Deploy to add proxies.
Bots keep running on the VPS even if you close the browser or restart your PC.
To reconnect later in one click, grab a reconnect shortcut from the dashboard's
🔗 Connect panel (it opens the tunnel + dashboard for you).
DONE
fi

cat <<DONE

  (Optional) allow reboot / OS-update from the UI by granting tight passwordless sudo:
     echo '${RUN_USER} ALL=(root) NOPASSWD: /usr/sbin/reboot, /usr/bin/apt-get' | sudo tee /etc/sudoers.d/aquarius-bot-manager
DONE
