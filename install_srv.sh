#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/kidtime}"
DATA_DIR="${KIDTIME_DATA_DIR:-/var/lib/kidtime}"
SERVICE_USER="${SERVICE_USER:-kidtime}"
PORT="${KIDTIME_PORT:-8001}"

if [[ -z "${KIDTIME_SHARED_KEY_HEX:-}" ]]; then
  echo "KIDTIME_SHARED_KEY_HEX is required. Generate one with: python3 -c 'import secrets; print(secrets.token_hex(32))'" >&2
  exit 1
fi

if [[ ! "$KIDTIME_SHARED_KEY_HEX" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "KIDTIME_SHARED_KEY_HEX must be a 64-character hex string." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y python3 python3-venv

if [[ -z "${KIDTIME_ADMIN_TOKEN:-}" ]]; then
  KIDTIME_ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  echo "Generated KIDTIME_ADMIN_TOKEN=$KIDTIME_ADMIN_TOKEN"
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  sudo useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

sudo mkdir -p "$APP_DIR" "$DATA_DIR"
sudo cp kidtimeSrv.py requirements-srv.txt "$APP_DIR/"
sudo python3 -m venv "$APP_DIR/.venv"
sudo "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements-srv.txt"

sudo tee /etc/kidtime.env >/dev/null <<EOF
KIDTIME_SHARED_KEY_HEX=$KIDTIME_SHARED_KEY_HEX
KIDTIME_DATA_DIR=$DATA_DIR
KIDTIME_PORT=$PORT
KIDTIME_HOST=0.0.0.0
KIDTIME_ADMIN_TOKEN=$KIDTIME_ADMIN_TOKEN
EOF
sudo chmod 600 /etc/kidtime.env

sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$DATA_DIR"

sudo tee /etc/systemd/system/kidtime.service >/dev/null <<EOF
[Unit]
Description=KidTime upload server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/kidtime.env
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/kidtimeSrv.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now kidtime.service
sudo systemctl status kidtime.service --no-pager
