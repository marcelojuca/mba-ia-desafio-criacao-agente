"""Runtime do ADK (App + Runner + sessão persistida) e regras de confirmação."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from google.adk.apps import App, ResumabilityConfig
from google.adk.runners import Runner
from google.adk.sessions import Session
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types

from . import config, db
from .agents import root_agent

CONFIRMACAO = "adk_request_confirmation"

app = App(
    name=config.APP_NAME,
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
)
session_service = SqliteSessionService(str(config.SESSOES_DB))
runner = Runner(app=app, session_service=session_service)

# Um turno por vez em cada sessão (evita gravações concorrentes no mesmo histórico).
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


class SessaoNaoEncontrada(Exception):
    pass


class ConfirmacaoInexistente(Exception):
    pass


def _user_id(apartamento: str) -> str:
    return f"apto-{apartamento}"


async def criar_sessao(apartamento: str) -> str:
    # O apartamento é fixado aqui, uma única vez: no state e no vínculo do banco.
    s = await session_service.create_session(
        app_name=config.APP_NAME,
        user_id=_user_id(apartamento),
        state={"apartamento": apartamento},
    )
    db.vincular_sessao(s.id, apartamento)
    return s.id


async def obter_sessao(session_id: str) -> Session:
    apartamento = db.apartamento_da_sessao(session_id)
    if not apartamento:
        raise SessaoNaoEncontrada(session_id)
    s = await session_service.get_session(
        app_name=config.APP_NAME, user_id=_user_id(apartamento), session_id=session_id
    )
    if s is None:
        raise SessaoNaoEncontrada(session_id)
    return s


def confirmacoes_pendentes(session: Session) -> list[dict[str, Any]]:
    """Pedidos de confirmação do ADK ainda sem resposta, lidos dos eventos da sessão."""
    respondidas: set[str] = set()
    pedidos: list[tuple[Any, Any]] = []
    for ev in session.events:
        for fr in ev.get_function_responses():
            if fr.name == CONFIRMACAO and fr.id:
                respondidas.add(fr.id)
        for fc in ev.get_function_calls():
            if fc.name == CONFIRMACAO and fc.id:
                pedidos.append((ev, fc))
    pendentes = []
    for ev, fc in pedidos:
        if fc.id in respondidas:
            continue
        args = fc.args or {}
        original = args.get("originalFunctionCall") or {}
        conf = args.get("toolConfirmation") or {}
        detalhes = conf.get("payload") or original.get("args") or {}
        pendentes.append(
            {
                "id": fc.id,
                "acao": original.get("name", ""),
                "detalhes": detalhes,
                "mensagem": conf.get("hint", ""),
                "_invocation_id": ev.invocation_id,
            }
        )
    return pendentes


def _publico(pendentes: list[dict]) -> list[dict]:
    return [{k: v for k, v in p.items() if not k.startswith("_")} for p in pendentes]


async def _executar(session: Session, mensagem: types.Content, invocation_id: str | None) -> str:
    textos: list[str] = []
    async for ev in runner.run_async(
        user_id=session.user_id,
        session_id=session.id,
        new_message=mensagem,
        invocation_id=invocation_id,
    ):
        if ev.partial or not ev.content or not ev.content.parts or ev.author == "user":
            continue
        for p in ev.content.parts:
            if p.text and not p.thought:
                textos.append(p.text.strip())
    return "\n\n".join(t for t in textos if t)


async def enviar_mensagem(session_id: str, texto: str) -> dict:
    session = await obter_sessao(session_id)
    async with _locks[session_id]:
        msg = types.Content(role="user", parts=[types.Part(text=texto)])
        resposta = await _executar(session, msg, None)
        session = await obter_sessao(session_id)
        return {"resposta": resposta, "confirmacoes_pendentes": _publico(confirmacoes_pendentes(session))}


async def responder_confirmacao(session_id: str, confirmacao_id: str, confirmado: bool) -> dict:
    session = await obter_sessao(session_id)
    async with _locks[session_id]:
        session = await obter_sessao(session_id)
        pendente = next(
            (p for p in confirmacoes_pendentes(session) if p["id"] == confirmacao_id), None
        )
        if pendente is None:
            # Id desconhecido, de outra sessão ou já respondido: nada é executado.
            raise ConfirmacaoInexistente(confirmacao_id)
        msg = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=confirmacao_id,
                        name=CONFIRMACAO,
                        response={"confirmed": bool(confirmado)},
                    )
                )
            ],
        )
        resposta = await _executar(session, msg, pendente["_invocation_id"])
        session = await obter_sessao(session_id)
        return {"resposta": resposta, "confirmacoes_pendentes": _publico(confirmacoes_pendentes(session))}


async def eventos(session_id: str) -> list[dict]:
    session = await obter_sessao(session_id)
    return [ev.model_dump(mode="json", exclude_none=True, by_alias=True) for ev in session.events]
