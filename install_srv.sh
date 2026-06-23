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

if command -v dnf >/dev/null 2>&1; then
  PKG_MGR="dnf"
elif command -v yum >/dev/null 2>&1; then
  PKG_MGR="yum"
else
  echo "Neither dnf nor yum was found. This installer targets Alibaba Cloud Linux / RHEL-like systems." >&2
  exit 1
fi

sudo "$PKG_MGR" install -y python3 python3-pip || true
sudo "$PKG_MGR" install -y python311 python311-pip || true
sudo "$PKG_MGR" install -y python3.11 python3.11-pip || true
sudo "$PKG_MGR" install -y python39 python39-pip || true
sudo "$PKG_MGR" install -y python3.9 python3.9-pip || true
sudo "$PKG_MGR" install -y python38 python38-pip || true
sudo "$PKG_MGR" install -y python3.8 python3.8-pip || true

PYTHON_BIN=""
for candidate in python3.11 python311 python3.10 python310 python3.9 python39 python3.8 python38 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'; then
      PYTHON_BIN="$(command -v "$candidate")"
      echo "Using Python $version at $PYTHON_BIN"
      break
    fi
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.8+ is required. Install python39/python311 from Alibaba Cloud Linux repositories, then rerun." >&2
  exit 1
fi

if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
  sudo "$PYTHON_BIN" -m pip install --upgrade virtualenv
  VENV_CMD=("$PYTHON_BIN" -m virtualenv)
else
  VENV_CMD=("$PYTHON_BIN" -m venv)
fi

if [[ -z "${KIDTIME_ADMIN_TOKEN:-}" ]]; then
  KIDTIME_ADMIN_TOKEN="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  echo "Generated KIDTIME_ADMIN_TOKEN=$KIDTIME_ADMIN_TOKEN"
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  sudo useradd --system --home-dir "$APP_DIR" --shell /sbin/nologin "$SERVICE_USER"
fi

sudo mkdir -p "$APP_DIR" "$DATA_DIR"
sudo cp kidtimeSrv.py requirements-srv.txt "$APP_DIR/"
sudo "${VENV_CMD[@]}" "$APP_DIR/.venv"
PIP_ENV=()
if [[ -n "${PIP_INDEX_URL:-}" ]]; then
  PIP_ENV+=("PIP_INDEX_URL=$PIP_INDEX_URL")
fi
sudo env "${PIP_ENV[@]}" "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
sudo env "${PIP_ENV[@]}" "$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements-srv.txt"

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

if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld; then
  echo "firewalld is active. Open the port if clients connect directly:"
  echo "  sudo firewall-cmd --permanent --add-port=${PORT}/tcp"
  echo "  sudo firewall-cmd --reload"
fi
