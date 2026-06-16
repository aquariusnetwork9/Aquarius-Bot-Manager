#!/usr/bin/env bash
# Aquarius Bot Manager — fresh-VPS installer.
#
#   curl -fsSL https://raw.githubusercontent.com/aquariusnetwork9/Aquarius-Bot-Manager/main/install.sh | sudo bash
#
# Override defaults with env vars:
#   sudo ABM_RUN_USER=ubuntu ABM_PORT=8765 ABM_BASE_DIR=/home/ubuntu/zenith bash install.sh
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

echo "==> Installing Aquarius Bot Manager  (user=$RUN_USER dir=$INSTALL_DIR base=$BASE_DIR port=$PORT)"

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
systemctl enable --now aquarius-bot-manager.service
systemctl enable aquarius-bot-manager-boot.service

cat <<DONE

==> Done. Aquarius Bot Manager is running on 127.0.0.1:$PORT (localhost only).

Next steps:
  1) Set a web login:
       sudo -u $RUN_USER abm setpassword
  2) Reach the UI from your computer over an SSH tunnel:
       ssh -L $PORT:127.0.0.1:$PORT $RUN_USER@<this-vps-ip>
     then open  http://localhost:$PORT
  3) In the UI, click  🚀 Deploy  to add AquariusProxy / ZenithProxy / a custom fork.

  (Optional) allow reboot / OS-update from the UI by granting tight passwordless sudo:
     echo '$RUN_USER ALL=(root) NOPASSWD: /usr/sbin/reboot, /usr/bin/apt-get' | sudo tee /etc/sudoers.d/aquarius-bot-manager
DONE
