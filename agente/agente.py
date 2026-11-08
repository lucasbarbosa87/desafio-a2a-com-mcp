"""Agente Central de Salas: servidor A2A por fora, host MCP por dentro.

    MCP_URL=http://127.0.0.1:7301/mcp python agente.py

A ponte fica em `ExecutorDeReservas`:

- `_concluir_ou_pausar` e onde o `input_required` do MCP vira
  `TASK_STATE_INPUT_REQUIRED`: a pergunta vira a linha `alternativas: ...` e o
  `requestState` fica guardado em `self._pausas`, indexado pelo id da Task.
- `_retomar` e onde o `requestState` volta para o servidor MCP: a continuacao A2A
  vira um `tools/call` novo (id JSON-RPC novo), com os mesmos argumentos, o
  `inputResponses` na chave que o servidor escolheu e o `requestState` ecoado
  byte a byte.

O agente nao conhece regra de sala. Conflito, politica e alternativas vem do
servidor MCP; o agente so traduz entre os dois protocolos.
"""

from __future__ import annotations

import os

# Spans de OpenTelemetry estao fora do escopo (o que se pede e propagar o traceparent).
# A instrumentacao embutida do a2a-sdk e lida na importacao, por isso vem antes dos imports.
os.environ.setdefault("OTEL_INSTRUMENTATION_A2A_SDK_ENABLED", "false")

import json
import re
import secrets
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import mcp_types as mt
import uvicorn
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.context import ServerCallContext
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    Part,
    Task,
    TaskState,
    TaskStatus,
)
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.requests import Request

from cliente_mcp import ClienteMCP
from pedido import RECUSAR, FormatoInvalidoError, ler_escolha, ler_pedido

TOOL_DE_RESERVA = "reservar_sala"
URI_DA_POLITICA = "politica://uso"
VERSAO_A2A = "A2A-Version"
TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")


def _log(texto: str) -> None:
    print(f"{datetime.now().isoformat(timespec='milliseconds')} {texto}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Pausa:
    """Tudo que o agente precisa para repetir o `tools/call` original.

    `request_state` e opaco: o agente guarda e devolve, nunca abre.
    """

    tool: str
    argumentos: dict[str, Any]
    chave: str
    campo: str
    opcoes: tuple[str, ...]
    request_state: str
    politica: str

    def linha_de_alternativas(self) -> str:
        return "alternativas: " + ", ".join(self.opcoes)


class FalhaDaTask(Exception):
    """Termina a Task em FAILED com esta mensagem."""


def _texto_do_resultado(resultado: mt.CallToolResult) -> str:
    return " ".join(c.text for c in resultado.content if isinstance(c, mt.TextContent))


def _pergunta(resultado: mt.InputRequiredResult) -> tuple[str, str, tuple[str, ...]]:
    """Le a elicitation do `input_required`: a chave, o campo do schema plano e as opcoes."""
    pedidos = resultado.input_requests or {}
    if len(pedidos) != 1:
        raise FalhaDaTask(f"o servidor MCP pediu {len(pedidos)} inputs; este agente atende exatamente um")
    chave, pedido = next(iter(pedidos.items()))
    if not isinstance(pedido, mt.ElicitRequest) or not isinstance(pedido.params, mt.ElicitRequestFormParams):
        raise FalhaDaTask("o servidor MCP pediu um input que nao e elicitation em form mode")
    propriedades = pedido.params.requested_schema.get("properties") or {}
    if len(propriedades) != 1:
        raise FalhaDaTask("a elicitation nao tem um schema plano de um campo")
    campo, definicao = next(iter(propriedades.items()))
    opcoes = definicao.get("enum") or ([definicao["const"]] if "const" in definicao else [])
    if not opcoes:
        raise FalhaDaTask("a elicitation nao restringe as opcoes")
    return chave, campo, tuple(str(o) for o in opcoes)


class ExecutorDeReservas(AgentExecutor):
    def __init__(self, mcp: ClienteMCP) -> None:
        self._mcp = mcp
        # O estado pausado e por Task: id da Task -> Pausa. Fica so na memoria do agente.
        self._pausas: dict[str, Pausa] = {}
        # Trace-id de cada Task, para propagar o mesmo trace em todos os requests MCP dela.
        self._traces: dict[str, str] = {}

    # ------------------------------------------------------------- entrada A2A

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        # A continuacao traz so o taskId; o contextId vem da Task que ja existe.
        atual = context.current_task
        context_id = atual.context_id if atual is not None else context.context_id or ""
        updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context_id)
        trace_id = self._trace_id(context, task_id)
        texto = context.get_user_input().strip()
        try:
            if context.current_task is None:
                await event_queue.enqueue_event(
                    Task(
                        id=task_id,
                        context_id=context_id,
                        status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                        history=[context.message] if context.message else [],
                    )
                )
                _log(f"task={task_id} SUBMITTED trace={trace_id}")
                await self._novo_pedido(texto, updater, trace_id)
            else:
                await self._continuacao(texto, updater, trace_id)
        except FalhaDaTask as exc:
            self._pausas.pop(task_id, None)
            _log(f"task={task_id} FAILED {exc}")
            await self._terminar(updater, TaskState.TASK_STATE_FAILED, str(exc))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        self._pausas.pop(task_id, None)
        updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context.context_id or "")
        await updater.cancel()

    async def _terminar(self, updater: TaskUpdater, estado: TaskState, texto: str) -> None:
        """Leva a Task a um estado terminal com a mensagem final visivel no history.

        O a2a-sdk so arquiva `status.message` no `history` quando um status novo o
        substitui, e depois de um estado terminal nao ha status novo. Por isso a
        mensagem final sai primeiro num WORKING e em seguida no estado terminal:
        ela entra uma vez no history e continua em `status.message`, como em
        `exemplos/wire/10-a2a-send-message-continuacao.json`.
        """
        mensagem = updater.new_agent_message(parts=[Part(text=texto)])
        await updater.update_status(TaskState.TASK_STATE_WORKING, message=mensagem)
        await updater.update_status(estado, message=mensagem)
        self._pausas.pop(updater.task_id, None)
        self._traces.pop(updater.task_id, None)

    def _trace_id(self, context: RequestContext, task_id: str) -> str:
        cabecalhos = context.call_context.state.get("headers") or {}
        casou = TRACEPARENT.match(str(cabecalhos.get("traceparent", "")).strip().lower())
        if casou:
            self._traces[task_id] = casou.group(1)
        return self._traces.setdefault(task_id, secrets.token_hex(16))

    # ------------------------------------------------------------- pedido novo

    async def _novo_pedido(self, texto: str, updater: TaskUpdater, trace_id: str) -> None:
        try:
            campos = ler_pedido(texto)
        except FormatoInvalidoError as exc:
            raise FalhaDaTask(f"Pedido invalido: {exc}") from None
        await updater.start_work()
        _log(f"task={updater.task_id} WORKING")

        try:
            # Descoberta em runtime: o agente so sabe o nome da capacidade que quer;
            # os argumentos saem do inputSchema que o servidor publica.
            tools = {t.name: t for t in await self._mcp.listar_tools(trace_id)}
            if TOOL_DE_RESERVA not in tools:
                raise FalhaDaTask(f"o servidor MCP nao oferece a tool {TOOL_DE_RESERVA}")
            esquema = tools[TOOL_DE_RESERVA].input_schema
            exigidos = esquema.get("required") or list((esquema.get("properties") or {}))
            faltando = [c for c in exigidos if c not in campos]
            if faltando:
                raise FalhaDaTask(f"Pedido invalido: a tool exige {faltando}")
            argumentos = {c: campos[c] for c in (esquema.get("properties") or {}) if c in campos}

            politica = self._versao_da_politica(await self._mcp.ler_resource(URI_DA_POLITICA, trace_id))
            resultado = await self._mcp.chamar_tool(TOOL_DE_RESERVA, argumentos, trace_id)
        except MCPError as exc:
            raise FalhaDaTask(f"Erro do servidor MCP ({exc.error.code}): {exc.error.message}") from None
        await self._concluir_ou_pausar(resultado, updater, TOOL_DE_RESERVA, argumentos, politica, recusou=False)

    @staticmethod
    def _versao_da_politica(texto: str) -> str:
        primeira = texto.splitlines()[0] if texto else ""
        chave, _, valor = primeira.partition(":")
        if chave.strip() != "versao" or not valor.strip():
            raise FalhaDaTask("o resource da politica nao declara a versao na primeira linha")
        return valor.strip()

    # ------------------------------------------------------------- a ponte

    async def _concluir_ou_pausar(
        self,
        resultado: mt.CallToolResult | mt.InputRequiredResult,
        updater: TaskUpdater,
        tool: str,
        argumentos: dict[str, Any],
        politica: str,
        *,
        recusou: bool,
    ) -> None:
        task_id = updater.task_id

        if isinstance(resultado, mt.InputRequiredResult):
            # MCP input_required -> A2A TASK_STATE_INPUT_REQUIRED.
            chave, campo, opcoes = _pergunta(resultado)
            if not resultado.request_state:
                raise FalhaDaTask("o servidor MCP pediu input sem requestState")
            pausa = Pausa(tool, argumentos, chave, campo, opcoes, resultado.request_state, politica)
            self._pausas[task_id] = pausa
            _log(f"task={task_id} INPUT_REQUIRED {pausa.linha_de_alternativas()}")
            await updater.requires_input(updater.new_agent_message(parts=[Part(text=pausa.linha_de_alternativas())]))
            return

        self._pausas.pop(task_id, None)
        if resultado.is_error:
            raise FalhaDaTask(_texto_do_resultado(resultado))

        dados = resultado.structured_content or {}
        if dados.get("reservado") is not True:
            motivo = dados.get("motivo") or "sem motivo"
            if recusou:
                _log(f"task={task_id} CANCELED motivo={motivo}")
                await self._terminar(updater, TaskState.TASK_STATE_CANCELED, f"Reserva nao realizada: {motivo}.")
                return
            raise FalhaDaTask(f"Reserva nao realizada: {motivo}")

        reserva = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": politica,
        }
        await updater.add_artifact(parts=[Part(text=json.dumps(reserva))], name="reserva", last_chunk=True)
        _log(f"task={task_id} COMPLETED reserva={reserva['reserva']} sala={reserva['sala']}")
        await self._terminar(
            updater, TaskState.TASK_STATE_COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}."
        )

    async def _continuacao(self, texto: str, updater: TaskUpdater, trace_id: str) -> None:
        task_id = updater.task_id
        pausa = self._pausas.get(task_id)
        if pausa is None:
            raise FalhaDaTask("esta Task nao esta esperando resposta")

        escolha = ler_escolha(texto)
        if escolha != RECUSAR and escolha not in pausa.opcoes:
            # Fora do enum: a Task continua pausada e a pergunta se repete igual.
            _log(f"task={task_id} INPUT_REQUIRED escolha invalida {texto!r}")
            await updater.requires_input(updater.new_agent_message(parts=[Part(text=pausa.linha_de_alternativas())]))
            return

        await updater.start_work()
        _log(f"task={task_id} WORKING escolha={escolha}")
        await self._retomar(pausa, escolha, updater, trace_id)

    async def _retomar(self, pausa: Pausa, escolha: str, updater: TaskUpdater, trace_id: str) -> None:
        # O requestState volta para o servidor MCP aqui, num tools/call novo.
        if escolha == RECUSAR:
            resposta = mt.ElicitResult(action="decline")
        else:
            resposta = mt.ElicitResult(action="accept", content={pausa.campo: escolha})
        try:
            resultado = await self._mcp.chamar_tool(
                pausa.tool,
                pausa.argumentos,
                trace_id,
                input_responses={pausa.chave: resposta},
                request_state=pausa.request_state,
            )
        except MCPError as exc:
            raise FalhaDaTask(f"Erro do servidor MCP ({exc.error.code}): {exc.error.message}") from None
        await self._concluir_ou_pausar(
            resultado, updater, pausa.tool, pausa.argumentos, pausa.politica, recusou=escolha == RECUSAR
        )


# ----------------------------------------------------------------- card e app


class ContextoV1PorPadrao(DefaultServerCallContextBuilder):
    """Request sem `A2A-Version` e tratado como 1.0.

    O a2a-sdk segue a regra da spec de ler a ausencia do header como 0.3 e recusa
    o request no handler v1.0. Este endpoint so fala 1.0 (e o unico
    `protocolVersion` do card), e o cliente do avaliador nao manda o header, entao
    a ausencia vira 1.0. Um cliente que declare outra versao continua recusado.
    """

    def build(self, request: Request) -> ServerCallContext:
        contexto = super().build(request)
        cabecalhos = contexto.state.setdefault("headers", {})
        if not (cabecalhos.get(VERSAO_A2A) or cabecalhos.get(VERSAO_A2A.lower())):
            cabecalhos[VERSAO_A2A.lower()] = "1.0"
        return contexto



def agent_card(url_publica: str) -> AgentCard:
    return AgentCard(
        name="Central de Salas",
        description="Reserva salas de reuniao da Hill Valley Tech.",
        provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
        version="1.0.0",
        supported_interfaces=[AgentInterface(url=f"{url_publica}/a2a", protocol_binding="JSONRPC", protocol_version="1.0")],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="reservar-sala",
                name="Reservar sala",
                description="Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                tags=["salas", "agenda"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
                examples=[
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            )
        ],
    )


def criar_app() -> Starlette:
    host = os.environ.get("AGENTE_HOST", "127.0.0.1")
    porta = int(os.environ.get("AGENTE_PORT", "7300"))
    url_publica = os.environ.get("AGENTE_URL_PUBLICA", f"http://{host}:{porta}").rstrip("/")
    mcp = ClienteMCP(os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp"))

    card = agent_card(url_publica)
    handler = DefaultRequestHandler(
        agent_executor=ExecutorDeReservas(mcp), task_store=InMemoryTaskStore(), agent_card=card
    )

    @asynccontextmanager
    async def ciclo_de_vida(_app: Starlette):
        await mcp.abrir()
        _log(f"agente A2A em {url_publica}/a2a, card em {url_publica}/.well-known/agent-card.json")
        try:
            yield
        finally:
            await mcp.fechar()

    rotas = [*create_agent_card_routes(agent_card=card), *create_jsonrpc_routes(request_handler=handler, rpc_url="/a2a", context_builder=ContextoV1PorPadrao())]
    return Starlette(routes=rotas, lifespan=ciclo_de_vida)


def main() -> None:
    host = os.environ.get("AGENTE_HOST", "127.0.0.1")
    porta = int(os.environ.get("AGENTE_PORT", "7300"))
    uvicorn.run(criar_app(), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
