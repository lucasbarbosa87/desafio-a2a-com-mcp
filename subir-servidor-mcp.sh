#!/usr/bin/env sh
# Sobe o servidor MCP em http://127.0.0.1:${MCP_PORT:-7301}/mcp. O log de requests vai para o stderr.
set -eu
cd "$(dirname "$0")/servidor-mcp"
: "${REQUEST_STATE_SECRET:?exporte REQUEST_STATE_SECRET antes (veja o README)}"
exec ../.venv/bin/python servidor.py
