"""API HTTP do assistente (contrato do desafio)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import db, service

import logging

log = logging.getLogger("aurora.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.inicializar()
    yield


app = FastAPI(title="Residencial Aurora", lifespan=lifespan)


@app.exception_handler(Exception)
async def falha_do_modelo(_, exc: Exception):
    # Falhas do provedor do modelo (cota, sobrecarga) viram 503 legível em vez de 500 genérico.
    log.exception("Falha ao processar a requisição")
    return JSONResponse(
        status_code=503,
        content={"detail": f"Assistente indisponível no momento: {type(exc).__name__}. Tente novamente."},
    )


class NovaSessao(BaseModel):
    apartamento: str


class Mensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


@app.post("/sessoes", status_code=201)
async def criar_sessao(body: NovaSessao):
    apartamento = body.apartamento.strip()
    if not db.apartamento_existe(apartamento):
        raise HTTPException(422, "Apartamento inexistente.")
    return {"session_id": await service.criar_sessao(apartamento)}


@app.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, body: Mensagem):
    try:
        return await service.enviar_mensagem(session_id, body.texto)
    except service.SessaoNaoEncontrada:
        raise HTTPException(404, "Sessão não encontrada.")


@app.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, body: RespostaConfirmacao):
    try:
        return await service.responder_confirmacao(session_id, body.id, body.confirmado)
    except service.SessaoNaoEncontrada:
        raise HTTPException(404, "Sessão não encontrada.")
    except service.ConfirmacaoInexistente:
        return JSONResponse(
            status_code=409,
            content={"detail": "Não existe confirmação pendente com esse id nesta sessão."},
        )


@app.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str):
    try:
        return await service.eventos(session_id)
    except service.SessaoNaoEncontrada:
        raise HTTPException(404, "Sessão não encontrada.")


@app.get("/apartamentos/{numero}/reservas")
async def reservas_do_apartamento(numero: str):
    return db.listar_reservas(numero)


@app.get("/apartamentos/{numero}/visitantes")
async def visitantes_do_apartamento(numero: str):
    return db.listar_visitantes(numero)
