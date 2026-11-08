"""Regras de sala: dados do starter, politica de uso e calculo de alternativas.

Tudo aqui e domínio puro, sem protocolo. As reservas vivem em memoria: as do
arquivo sao carregadas na subida e as criadas durante a execucao se somam a
elas ate o processo terminar.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS = Path(__file__).resolve().parent.parent / "dados"
FUSO_SP = timezone(timedelta(hours=-3))
ABRE, FECHA = time(8, 0), time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


class RegraVioladaError(Exception):
    """Pedido que fere uma regra de sala; a mensagem e a do enunciado."""


@dataclass(frozen=True)
class Sala:
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


@dataclass(frozen=True)
class Reserva:
    id: str
    sala: str
    inicio: str
    fim: str
    responsavel: str

    def intervalo(self) -> tuple[datetime, datetime]:
        return datetime.fromisoformat(self.inicio), datetime.fromisoformat(self.fim)


SALAS: dict[str, Sala] = {s["id"]: Sala(**s) for s in json.loads((DADOS / "salas.json").read_text())}
RESERVAS: list[Reserva] = [Reserva(**r) for r in json.loads((DADOS / "reservas.json").read_text())]
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text()
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()


def _instante(valor: str, campo: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except ValueError as exc:
        raise RegraVioladaError(f"Data invalida em {campo}: {valor}") from exc
    if dt.tzinfo is None:
        raise RegraVioladaError(f"Data sem fuso em {campo}: {valor}")
    return dt


def validar(sala: str, inicio: str, fim: str) -> tuple[Sala, datetime, datetime]:
    """Aplica as regras na ordem: sala, intervalo, janela, duracao."""
    if sala not in SALAS:
        raise RegraVioladaError(f"Sala inexistente: {sala}")
    ini, fi = _instante(inicio, "inicio"), _instante(fim, "fim")
    if fi <= ini:
        raise RegraVioladaError(ERRO_INTERVALO)
    ini_sp, fim_sp = ini.astimezone(FUSO_SP), fi.astimezone(FUSO_SP)
    if ini_sp.time() < ABRE or fim_sp.date() != ini_sp.date() or fim_sp.time() > FECHA:
        raise RegraVioladaError(ERRO_JANELA)
    if fi - ini > DURACAO_MAXIMA:
        raise RegraVioladaError(ERRO_DURACAO)
    return SALAS[sala], ini, fi


def conflitos(sala: str, ini: datetime, fim: datetime) -> list[Reserva]:
    resultado = []
    for r in RESERVAS:
        if r.sala != sala:
            continue
        r_ini, r_fim = r.intervalo()
        if ini < r_fim and r_ini < fim:
            resultado.append(r)
    return resultado


def alternativas(pedida: Sala, ini: datetime, fim: datetime) -> list[str]:
    """Salas livres com capacidade >= a pedida, por capacidade e depois id, no maximo tres."""
    candidatas = [
        s
        for s in SALAS.values()
        if s.id != pedida.id and s.capacidade >= pedida.capacidade and not conflitos(s.id, ini, fim)
    ]
    candidatas.sort(key=lambda s: (s.capacidade, s.id))
    return [s.id for s in candidatas[:MAX_ALTERNATIVAS]]


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> Reserva:
    numeros = [int(r.id.removeprefix("res-")) for r in RESERVAS if r.id.startswith("res-")]
    reserva = Reserva(f"res-{max(numeros, default=0) + 1:04d}", sala, inicio, fim, responsavel)
    RESERVAS.append(reserva)
    return reserva
