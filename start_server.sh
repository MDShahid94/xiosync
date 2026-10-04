#!/bin/zsh
# XIOSYNC Server startup script — loads .env then starts uvicorn
# Install as LaunchAgent: launchctl load ~/Library/LaunchAgents/dev.xiosync.server.plist
# Or run manually: ./start_server.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$SCRIPT_DIR"

# Load environment from .env (set -a exports all variables)
set -a
source "$SCRIPT_DIR/.env"
set +a

exec uv run uvicorn xiosync.api.app:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 1 \
  --log-level info
