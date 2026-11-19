#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="agy-sidecar"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8000}"
API_KEY="${AGY_API_KEY:-sk-antigravity-local}"

echo "==> Setting up $SERVICE_NAME from $DIR..."

if ! command -v python3 &>/dev/null; then
    echo "Error: python3 is required but not installed." >&2
    exit 1
fi

# Locate official agy binary
AGY_BIN="${AGY_BINARY:-}"
if [ -z "$AGY_BIN" ]; then
    AGY_BIN="$(command -v agy 2>/dev/null || true)"
fi
if [ -z "$AGY_BIN" ] && [ -x "$HOME/.local/bin/agy" ]; then
    AGY_BIN="$HOME/.local/bin/agy"
fi
if [ -z "$AGY_BIN" ] && [ -x "/usr/local/bin/agy" ]; then
    AGY_BIN="/usr/local/bin/agy"
fi

EXTRA_PATH="$HOME/.local/bin"
if [ -n "$AGY_BIN" ] && [ -x "$AGY_BIN" ]; then
    AGY_DIR="$(dirname "$AGY_BIN")"
    if [ "$AGY_DIR" != "$HOME/.local/bin" ]; then
        EXTRA_PATH="$AGY_DIR:$EXTRA_PATH"
    fi
    AGY_VER="$("$AGY_BIN" --version 2>/dev/null | head -n 1 || echo "unknown")"
    echo "==> Found agy CLI: $AGY_BIN ($AGY_VER)"
else
    echo "Warning: 'agy' CLI binary not found in PATH or ~/.local/bin/agy."
    echo "         Install and authenticate Google Antigravity CLI before using inference endpoints."
fi

# Set up Python environment (prefer isolated .venv to avoid PEP 668 system pip issues)
PYTHON_BIN=""
if python3 -m venv "$DIR/.venv" 2>/dev/null; then
    echo "==> Installing Python dependencies into .venv..."
    "$DIR/.venv/bin/python3" -m pip install --quiet --upgrade pip 2>/dev/null || true
    "$DIR/.venv/bin/python3" -m pip install --quiet -r "$DIR/requirements.txt"
    PYTHON_BIN="$DIR/.venv/bin/python3"
else
    PYTHON_BIN="$(command -v python3)"
    if ! "$PYTHON_BIN" -c "import fastapi, uvicorn, pydantic" 2>/dev/null; then
        echo "==> Installing Python dependencies via pip..."
        "$PYTHON_BIN" -m pip install --user --break-system-packages -r "$DIR/requirements.txt" 2>/dev/null \
            || "$PYTHON_BIN" -m pip install --break-system-packages -r "$DIR/requirements.txt" 2>/dev/null \
            || "$PYTHON_BIN" -m pip install --user -r "$DIR/requirements.txt" 2>/dev/null \
            || "$PYTHON_BIN" -m pip install -r "$DIR/requirements.txt"
    fi
fi

# Create systemd user service
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

{
    cat <<EOF
[Unit]
Description=Antigravity agy Sidecar API Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$PYTHON_BIN -m uvicorn server:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5
Environment=PATH=$EXTRA_PATH:/usr/local/bin:/usr/bin:/bin
Environment=HOME=$HOME
EOF
    [ -n "$AGY_BIN" ] && echo "Environment=AGY_BINARY=$AGY_BIN"
    [ -n "${AGY_API_KEY:-}" ] && echo "Environment=AGY_API_KEY=$AGY_API_KEY"
    [ -n "${AGY_MODELS_CACHE_TTL:-}" ] && echo "Environment=AGY_MODELS_CACHE_TTL=$AGY_MODELS_CACHE_TTL"
    echo "Environment=PI_MODELS_JSON=${PI_MODELS_JSON:-$HOME/.pi/agent/models.json}"
    echo "Environment=PI_MODELS_SYNC=${PI_MODELS_SYNC:-1}"
    echo "Environment=PI_MODELS_DENY=${PI_MODELS_DENY:-kimi,moonshot,qwen}"
    echo "Environment=PI_BASE_URL=http://localhost:$PORT/v1"
    [ -n "${AGY_DEFAULT_MODEL:-}" ] && echo "Environment=AGY_DEFAULT_MODEL=$AGY_DEFAULT_MODEL"
    cat <<EOF

[Install]
WantedBy=default.target
EOF
} > "$UNIT_DIR/$SERVICE_NAME.service"

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME.service"
systemctl --user restart "$SERVICE_NAME.service"

echo "==> Verifying service on port $PORT..."
if command -v curl &>/dev/null; then
    if curl -sf --retry 10 --retry-delay 1 --retry-connrefused "http://localhost:$PORT/health" | grep -q '"status"'; then
        echo "==> Success! $SERVICE_NAME is running on port $PORT"
        echo "    Host Base URL:   http://localhost:$PORT/v1"
        echo "    Docker Base URL: http://host.docker.internal:$PORT/v1"
        echo "    API Key:         $API_KEY"
        if [ -n "$AGY_BIN" ]; then
            echo "==> Syncing live models into pi config..."
            "$PYTHON_BIN" - <<PY || true
import json, urllib.request
req = urllib.request.Request(
    "http://localhost:$PORT/v1/models/sync",
    data=b"{}",
    headers={"Authorization": "Bearer $API_KEY", "Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=60) as resp:
    body = json.loads(resp.read().decode())
print(f"    {body.get('models')} models -> {body.get('pi_models_json')} (provider '{body.get('provider')}', written={body.get('written')})")
PY
        fi
    else
        echo "Service started, but health check did not respond yet. Check: journalctl --user -u $SERVICE_NAME -n 25"
    fi
else
    echo "Service started. Check logs: journalctl --user -u $SERVICE_NAME -n 25"
fi
