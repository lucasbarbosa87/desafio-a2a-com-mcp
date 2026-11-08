#!/usr/bin/env sh
# Sobe o agente A2A em http://127.0.0.1:${AGENTE_PORT:-7300}/a2a, falando com o MCP em ${MCP_URL:-http://127.0.0.1:7301/mcp}.
set -eu
cd "$(dirname "$0")/agente"
exec ../.venv/bin/python agente.py
