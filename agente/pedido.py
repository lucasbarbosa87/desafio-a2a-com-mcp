"""Leitura dos dois formatos fixos de texto que o agente aceita. Sem LLM, sem heuristica.

    reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>
    escolha=<id da sala> | escolha=recusar
"""

from __future__ import annotations

CAMPOS_DO_PEDIDO = ("sala", "inicio", "fim", "responsavel")
RECUSAR = "recusar"


class FormatoInvalidoError(ValueError):
    pass


def _pares(tokens: list[str]) -> dict[str, str]:
    campos: dict[str, str] = {}
    for token in tokens:
        chave, sep, valor = token.partition("=")
        if not sep or not chave or not valor:
            raise FormatoInvalidoError(f"esperava chave=valor, veio {token!r}")
        if chave in campos:
            raise FormatoInvalidoError(f"campo repetido: {chave}")
        campos[chave] = valor
    return campos


def ler_pedido(texto: str) -> dict[str, str]:
    """`reservar sala=... inicio=... fim=... responsavel=...` vira um dict com os quatro campos."""
    tokens = texto.split()
    if not tokens or tokens[0] != "reservar":
        raise FormatoInvalidoError(
            "formato esperado: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
        )
    campos = _pares(tokens[1:])
    faltando = [c for c in CAMPOS_DO_PEDIDO if c not in campos]
    sobrando = [c for c in campos if c not in CAMPOS_DO_PEDIDO]
    if faltando or sobrando:
        raise FormatoInvalidoError(f"campos faltando: {faltando}; campos desconhecidos: {sobrando}")
    return campos


def ler_escolha(texto: str) -> str | None:
    """`escolha=<valor>` devolve o valor; qualquer outra coisa devolve None."""
    tokens = texto.split()
    if len(tokens) != 1:
        return None
    chave, sep, valor = tokens[0].partition("=")
    return valor if chave == "escolha" and sep and valor else None
