"""Servidor MCP da Central de Salas (Streamable HTTP, revisao 2026-07-28).

Tres tools, um resource e o ciclo de MRTR na reserva. O MRTR nao e callback:
quando ha conflito, o resolver `escolha_de_sala` devolve um `Elicit` e o SDK
termina a resposta com `resultType: input_required`, carregando a pergunta em
`inputRequests` e o estado selado em `requestState`. O cliente volta depois com
um request novo, e o SDK reconstroi o pedido a partir desse estado.

    REQUEST_STATE_SECRET=... python servidor.py
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Annotated, Any, Literal

import uvicorn
from pydantic import BaseModel, Field, create_model

import dominio
from logs import LogDeRequests
from mcp.server.mcpserver import (
    AcceptedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError

NOME = "central-de-salas"
TTL_REQUEST_STATE = 10 * 60  # segundos; o enunciado pede entre 5 e 30 minutos
MENSAGEM_ELICITATION = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."


def _segredo() -> str:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere um com:\n"
            '  python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    return segredo


# ---------------------------------------------------------------- schemas de saida


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# ---------------------------------------------------------------- servidor

mcp = MCPServer(
    NOME,
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[_segredo()], ttl=TTL_REQUEST_STATE),
)


def _validar(sala: str, inicio: str, fim: str) -> tuple[dominio.Sala, Any, Any]:
    try:
        return dominio.validar(sala, inicio, fim)
    except dominio.RegraVioladaError as exc:
        raise ToolError(str(exc)) from None


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**vars(s)) for s in dominio.SALAS.values()])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    _, ini, fi = _validar(sala, inicio, fim)
    em_conflito = dominio.conflitos(sala, ini, fi)
    return Disponibilidade(
        sala=sala,
        livre=not em_conflito,
        conflitos=[ConflitoOut(id=r.id, inicio=r.inicio, fim=r.fim, responsavel=r.responsavel) for r in em_conflito],
    )


def _schema_da_escolha(opcoes: list[str]) -> type[BaseModel]:
    """Schema plano da elicitation: uma string `sala` restrita as alternativas."""
    return create_model(
        "EscolhaDeSala",
        sala=(Literal[tuple(opcoes)], Field(title="Sala", description="Sala alternativa escolhida")),
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[BaseModel] | None:
    """Resolver do MRTR. Roda antes do corpo da tool, a cada rodada.

    Sala livre: nao pergunta nada (devolve None). Conflito com alternativas:
    devolve o `Elicit`, que o SDK transforma em `input_required`. Conflito sem
    alternativa: erro de execucao. Na rodada de retry ele recalcula a mesma
    pergunta, e o SDK so aceita a resposta se ela bater com o que foi perguntado.
    """
    pedida, ini, fi = _validar(sala, inicio, fim)
    if not dominio.conflitos(sala, ini, fi):
        return None
    opcoes = dominio.alternativas(pedida, ini, fi)
    if not opcoes:
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    return Elicit(MENSAGEM_ELICITATION, _schema_da_escolha(opcoes))


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[BaseModel], Resolve(escolha_de_sala)],
) -> ReservaOut:
    # Os argumentos chegam aqui ja conferidos pelo SDK contra o requestState selado:
    # um retry com argumentos diferentes dos originais e rejeitado antes deste ponto.
    if not isinstance(escolha, AcceptedElicitation):
        return ReservaOut(reservado=False, motivo="recusado" if escolha.action == "decline" else "cancelado")

    destino = sala if escolha.data is None else escolha.data.sala
    _, ini, fi = _validar(destino, inicio, fim)
    if dominio.conflitos(destino, ini, fi):
        raise ToolError(f"Sala ocupada no intervalo: {destino}")
    reserva = dominio.criar_reserva(destino, inicio, fim, responsavel)
    return ReservaOut(
        reserva=reserva.id,
        sala=reserva.sala,
        inicio=reserva.inicio,
        fim=reserva.fim,
        responsavel=reserva.responsavel,
        politica=dominio.POLITICA_VERSAO,
    )


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
              description="Politica de uso das salas. A primeira linha declara a versao.")
def politica_de_uso() -> str:
    return dominio.POLITICA_TEXTO


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    porta = int(os.environ.get("MCP_PORT", "7301"))
    app = LogDeRequests(mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, host=host))
    print(f"servidor MCP {NOME} em http://{host}:{porta}/mcp", file=sys.stderr, flush=True)
    uvicorn.run(app, host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
