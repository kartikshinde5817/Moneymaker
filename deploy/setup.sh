#!/usr/bin/env bash
# Provisions a fresh Oracle Cloud Always Free instance to run Money Maker as
# a systemd service: firewall, Python, the app itself, and the service file.
#
# Run this ON THE SERVER, after SSHing in, from inside the uploaded repo:
#   cd ~/moneymaker
#   chmod +x deploy/setup.sh
#   ./deploy/setup.sh
#
# Idempotent - safe to re-run after a `git pull` or file upload to pick up
# changes and restart the service.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="$(whoami)"
SERVICE_NAME="moneymaker"

echo "==> Money Maker setup in $APP_DIR as user $SERVICE_USER"

# --- 1. OS packages ---------------------------------------------------------
echo "==> Installing system packages (needs sudo once)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip ufw

# --- 2. Oracle's instance-level firewall (iptables) -------------------------
# Oracle's Ubuntu images ship with iptables rules that drop everything not
# already allowed, on top of the Console's separate Security List - both
# layers have to permit port 8000, or nothing reaches the app from outside.
echo "==> Opening port 8000 in the instance firewall (iptables)"
if ! sudo iptables -C INPUT -p tcp --dport 8000 -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT
    sudo netfilter-persistent save 2>/dev/null || sudo iptables-save | sudo tee /etc/iptables/rules.v4 >/dev/null || true
fi

# --- 3. the app itself -------------------------------------------------------
cd "$APP_DIR"

# HOST=127.0.0.1 (the correct default for a laptop) only accepts connections
# from the machine itself. On a server this means the firewall can be wide
# open and nothing external still gets through. Force it to 0.0.0.0 here.
if [ -f .env ] && grep -q '^HOST=127\.0\.0\.1' .env; then
    echo "==> Changing HOST=127.0.0.1 to HOST=0.0.0.0 in .env (server must accept external connections)"
    sed -i 's/^HOST=127\.0\.0\.1/HOST=0.0.0.0/' .env
fi

echo "==> Running python3 run.py --check to build the venv and verify config"
python3 run.py --check

# --- 4. systemd service -------------------------------------------------------
echo "==> Installing the systemd service"
sudo sed \
    -e "s#/home/ubuntu/moneymaker#${APP_DIR}#g" \
    -e "s#^User=ubuntu#User=${SERVICE_USER}#" \
    "$APP_DIR/deploy/moneymaker.service" \
    | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

sleep 3
echo
echo "==> Service status"
sudo systemctl --no-pager status "$SERVICE_NAME" || true

IP="$(curl -s -4 ifconfig.me || echo '<your-instance-public-ip>')"
cat <<EOF

==================================================================
  Done.

  Dashboard:  http://${IP}:8000

  Remaining manual step, in the OCI Console (not this script):
    Networking -> Virtual Cloud Networks -> (your VCN) -> Security Lists
    -> add an Ingress Rule: source 0.0.0.0/0, TCP, destination port 8000.
    Without this, the firewall rules above are not enough - the Console's
    Security List is a second, separate layer in front of the instance.

  Then in .env, set:
    PUBLIC_BASE_URL=http://${IP}:8000
  and restart:
    sudo systemctl restart ${SERVICE_NAME}

  Useful commands:
    sudo systemctl status ${SERVICE_NAME}
    sudo journalctl -u ${SERVICE_NAME} -f      # live logs
    sudo systemctl restart ${SERVICE_NAME}     # after editing .env
==================================================================
EOF
