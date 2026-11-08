"""Log de cada request MCP no stderr, na borda HTTP.

Fica do lado de fora do SDK de proposito: assim ate os requests que o SDK recusa
antes de chegar a um handler (por exemplo, `_meta` incompleto) aparecem no log.
Uma linha na chegada, com metodo, id, nome, traceparent e capabilities, e uma linha na saida,
com o status HTTP e o tipo do resultado.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

TRACEPARENT = "traceparent"
CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO = "io.modelcontextprotocol/clientInfo"


def _log(texto: str) -> None:
    print(f"{datetime.now().isoformat(timespec='milliseconds')} {texto}", file=sys.stderr, flush=True)


def _resumo_saida(status: int, corpo: bytes) -> str:
    try:
        resposta = json.loads(corpo)
    except ValueError:
        return f"http={status}"
    if not isinstance(resposta, dict):
        return f"http={status}"
    if "error" in resposta:
        return f"http={status} error={resposta['error'].get('code')}"
    resultado = resposta.get("result") or {}
    partes = [f"http={status}", f"resultType={resultado.get('resultType')}"]
    if resultado.get("isError"):
        partes.append("isError=true")
    return " ".join(partes)


class LogDeRequests:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        pedacos: list[bytes] = []
        while True:
            mensagem = await receive()
            pedacos.append(mensagem.get("body", b""))
            if not mensagem.get("more_body"):
                break
        corpo = b"".join(pedacos)
        rotulo, curto = self._rotulo(corpo)
        _log(f"<- {rotulo}")

        entregue = False

        async def receive_de_novo() -> dict:
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        status = 0
        saida: list[bytes] = []

        async def send_com_log(mensagem: dict) -> None:
            nonlocal status
            if mensagem["type"] == "http.response.start":
                status = mensagem["status"]
            elif mensagem["type"] == "http.response.body":
                saida.append(mensagem.get("body", b""))
                if not mensagem.get("more_body"):
                    _log(f"-> {curto} {_resumo_saida(status, b''.join(saida))}")
            await send(mensagem)

        await self.app(scope, receive_de_novo, send_com_log)

    @staticmethod
    def _rotulo(corpo: bytes) -> tuple[str, str]:
        try:
            msg = json.loads(corpo)
        except ValueError:
            return "corpo-invalido", "corpo-invalido"
        if not isinstance(msg, dict):
            return "corpo-nao-objeto", "corpo-nao-objeto"
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        partes = [f"method={msg.get('method')}", f"id={msg.get('id')}"]
        curto = " ".join(partes)
        nome = params.get("name") or params.get("uri")
        if nome:
            partes.append(f"name={nome}")
        if "requestState" in params:
            partes.append("retry=sim")
        cliente = meta.get(CLIENT_INFO)
        if isinstance(cliente, dict) and cliente.get("name"):
            partes.append(f"client={cliente['name']}")
        partes.append(f"traceparent={meta.get(TRACEPARENT, '-')}")
        if CAPABILITIES in meta:
            partes.append(f"caps={json.dumps(meta[CAPABILITIES], separators=(',', ':'))}")
        return " ".join(partes), curto
