#!/usr/bin/env bash
set -e

SERVICE_NAME="agy-sidecar"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8000

echo "==> Setting up $SERVICE_NAME..."

# Verify agy binary
if ! command -v agy &>/dev/null && [ ! -f "$HOME/.local/bin/agy" ]; then
    echo "Warning: 'agy' CLI binary not found in PATH or ~/.local/bin/agy."
    echo "Make sure Google Antigravity CLI is installed and authenticated."
fi

# Ensure Python requirements
python3 -m pip install --break-system-packages fastapi uvicorn pydantic 2>/dev/null || python3 -m pip install fastapi uvicorn pydantic

# Create systemd user service
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat <<EOF > "$UNIT_DIR/$SERVICE_NAME.service"
[Unit]
Description=Antigravity agy Sidecar API Service
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$(which python3) -m uvicorn server:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=$HOME

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME.service"
systemctl --user restart "$SERVICE_NAME.service"

echo "==> Verifying service on port $PORT..."
sleep 2
if curl -s "http://localhost:$PORT/health" | grep -q "status"; then
    echo "==> Success! $SERVICE_NAME is running on port $PORT"
    echo "    Host Base URL:   http://localhost:$PORT/v1"
    echo "    Docker Base URL: http://host.docker.internal:$PORT/v1"
    echo "    API Key:         sk-antigravity-local"
else
    echo "Service started. Check logs: journalctl --user -u $SERVICE_NAME -n 20"
fi
