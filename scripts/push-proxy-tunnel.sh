#!/usr/bin/env bash
# push-proxy-tunnel.sh — Mac pushes a reverse SOCKS5 tunnel to a Colab worker
# Usage: push-proxy-tunnel.sh <worker-ts-ip>
# This lets the worker use Mac's residential IP as SOCKS5 proxy without
# needing inbound Tailscale ACL to Mac port 22.
#
# Architecture:
#   Mac → SSH to worker → allocate remote port 1056 on worker → 
#   worker curl via socks5://127.0.0.1:1056 → Mac → ISP exit

set -euo pipefail

WORKER_IP="${1:-100.72.164.37}"
PROXY_PORT="${2:-1056}"
SSH_KEY="${HOME}/.ssh/id_ed25519"

echo "🔗 Pushing SOCKS5 proxy tunnel to worker ${WORKER_IP}:${PROXY_PORT}..."

# Kill any existing tunnel to this worker
pkill -f "ssh.*${WORKER_IP}.*${PROXY_PORT}" 2>/dev/null || true
sleep 1

# Mac opens SSH to worker, creating remote dynamic SOCKS5 on worker's port 1056
# All traffic through that port exits via Mac's SSH local port forwarding
ssh \
  -i "${SSH_KEY}" \
  -o StrictHostKeyChecking=no \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=5 \
  -o ExitOnForwardFailure=yes \
  -R "${PROXY_PORT}:localhost:1056" \
  -N \
  root@"${WORKER_IP}" &

SSH_PID=$!
sleep 3

if kill -0 "$SSH_PID" 2>/dev/null; then
  echo "✅ Reverse tunnel PID=$SSH_PID: Mac→${WORKER_IP} port ${PROXY_PORT} ACTIVE"
  echo "   Worker can now: curl --socks5-hostname 127.0.0.1:${PROXY_PORT} https://api.ipify.org"
  # Keep PID for reference
  echo "$SSH_PID" > "/tmp/xio_tunnel_${WORKER_IP//./_}.pid"
else
  echo "❌ Tunnel failed to start"
  exit 1
fi
