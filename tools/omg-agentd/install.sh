#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

id -u omg-agentd >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/omg-agentd --shell /usr/sbin/nologin omg-agentd

apt-get update
apt-get install -y python3 python3-venv python3-pip

install -d -o omg-agentd -g omg-agentd /opt/omg-agentd
install -d -o omg-agentd -g omg-agentd /etc/omg-agentd
install -d -o omg-agentd -g omg-agentd /var/lib/omg-agentd/workspaces

install -o omg-agentd -g omg-agentd -m 0755 "${SCRIPT_DIR}/omg_agentd.py" /opt/omg-agentd/omg_agentd.py
install -o root -g root -m 0644 "${SCRIPT_DIR}/config.example.json" /etc/omg-agentd/config.json

python3 -m venv /opt/omg-agentd/.venv
/opt/omg-agentd/.venv/bin/pip install --upgrade pip
/opt/omg-agentd/.venv/bin/pip install -r "${SCRIPT_DIR}/requirements.txt"

install -o root -g root -m 0644 "${SCRIPT_DIR}/systemd/omg-agentd.service" /etc/systemd/system/omg-agentd.service

systemctl daemon-reload
systemctl enable --now omg-agentd

echo "Installed omg-agentd."
echo "Edit /etc/omg-agentd/config.json with vm_id/token/gateway_ws_url and restart service:"
echo "  sudo systemctl restart omg-agentd"
