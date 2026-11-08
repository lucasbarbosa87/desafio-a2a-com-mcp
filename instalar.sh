#!/usr/bin/env sh
# Cria .venv na raiz e instala o servidor MCP e o agente com as versoes travadas.
set -eu
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -m venv .venv
.venv/bin/python -m pip install --quiet -c requirements.lock ./servidor-mcp ./agente
echo "pronto: .venv criado com as dependencias de servidor-mcp/ e agente/"
