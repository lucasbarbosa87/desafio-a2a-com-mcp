"""O lado de dentro do agente: um host MCP sobre a ClientSession do SDK oficial.

Decisoes que importam para a ponte:

- Nenhum callback de elicitation. Os `tools/call` saem com
  `allow_input_required=True`, e o `InputRequiredResult` volta cru para quem
  chamou. Quem decide o que fazer com a pergunta e a Task A2A, nao este cliente.
- Sem handshake e sem sessao de protocolo: a sessao adota a revisao 2026-07-28
  localmente (`adopt`, sem trafego), e o SDK carimba em todo request o `_meta`
  com protocolVersion, clientInfo e clientCapabilities, mais os headers
  `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` espelhados do corpo.
- A capability declarada e exatamente `{"elicitation": {"form": {}}}`. A
  ClientSession do SDK, quando recebe um callback de elicitation, declara form e
  url; este agente so sabe responder form, entao sobrescreve a construcao das
  capabilities em vez de registrar um callback que nunca seria usado.
- O `traceparent` vai no `_meta` de cada request, com o trace-id da Task e um
  span-id novo por request.
- O `requestState` e tratado como bytes opacos: recebido, guardado e reenviado
  sem ser aberto.
"""

from __future__ import annotations

import secrets
from contextlib import AsyncExitStack
from typing import Any

import mcp_types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

PROTOCOLO = "2026-07-28"
INFO_DO_CLIENTE = types.Implementation(name="agente-central-de-salas", version="1.0.0")
CAPABILITIES = types.ClientCapabilities(elicitation=types.ElicitationCapability(form=types.FormElicitationCapability()))


class _SessaoSoFormMode(ClientSession):
    def _build_capabilities(self, version: str) -> types.ClientCapabilities:  # noqa: ARG002
        return CAPABILITIES


def traceparent_filho(trace_id: str) -> str:
    """Mesmo trace-id, span-id novo: cada request MCP e um span filho da Task."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


class ClienteMCP:
    def __init__(self, url: str) -> None:
        self._url = url
        self._pilha: AsyncExitStack | None = None
        self._sessao: ClientSession | None = None

    async def abrir(self) -> None:
        pilha = AsyncExitStack()
        leitura, escrita = await pilha.enter_async_context(streamable_http_client(self._url))
        sessao = await pilha.enter_async_context(_SessaoSoFormMode(leitura, escrita, client_info=INFO_DO_CLIENTE))
        sessao.adopt(
            types.DiscoverResult(
                supported_versions=[PROTOCOLO],
                capabilities=types.ServerCapabilities(),
                result_type="complete",
                ttl_ms=0,
                cache_scope="private",
            )
        )
        self._pilha, self._sessao = pilha, sessao

    async def fechar(self) -> None:
        if self._pilha is not None:
            await self._pilha.aclose()
        self._pilha = self._sessao = None

    @property
    def sessao(self) -> ClientSession:
        if self._sessao is None:
            raise RuntimeError("cliente MCP nao foi aberto")
        return self._sessao

    async def listar_tools(self, trace_id: str) -> list[types.Tool]:
        params = types.PaginatedRequestParams(_meta={"traceparent": traceparent_filho(trace_id)})
        return (await self.sessao.list_tools(params=params)).tools

    async def ler_resource(self, uri: str, trace_id: str) -> str:
        resultado = await self.sessao.read_resource(uri, meta={"traceparent": traceparent_filho(trace_id)})
        return "".join(getattr(c, "text", "") for c in resultado.contents)

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        trace_id: str,
        *,
        input_responses: types.InputResponses | None = None,
        request_state: str | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        """Um `tools/call`. Cada chamada e um request novo, com id JSON-RPC novo.

        Devolve o `InputRequiredResult` sem tocar nele quando o servidor pede input.
        """
        return await self.sessao.call_tool(
            nome,
            argumentos,
            input_responses=input_responses,
            request_state=request_state,
            meta={"traceparent": traceparent_filho(trace_id)},
            allow_input_required=True,
        )
