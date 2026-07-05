#!/usr/bin/env bash
set -Eeuo pipefail

# Run this on the Mac. It exposes the Mac HTTP proxy to the server through SSH.
# Example:
#   REMOTE=aidev@192.168.31.143 ./scripts/open_server_proxy_tunnel.sh
# Then use http://127.0.0.1:17892 as HTTP_PROXY/HTTPS_PROXY on the server.

REMOTE="${REMOTE:-aidev@192.168.31.143}"
LOCAL_PROXY_HOST="${LOCAL_PROXY_HOST:-127.0.0.1}"
LOCAL_PROXY_PORT="${LOCAL_PROXY_PORT:-7892}"
REMOTE_PROXY_PORT="${REMOTE_PROXY_PORT:-17892}"

echo "Opening reverse proxy tunnel:"
echo "  server 127.0.0.1:${REMOTE_PROXY_PORT} -> ${LOCAL_PROXY_HOST}:${LOCAL_PROXY_PORT} on this Mac"
echo "  remote: ${REMOTE}"
echo
echo "Keep this process running while the server downloads model files."

exec ssh \
  -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -R "${REMOTE_PROXY_PORT}:${LOCAL_PROXY_HOST}:${LOCAL_PROXY_PORT}" \
  "${REMOTE}"
