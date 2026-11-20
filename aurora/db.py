"""Armazenamento do condomínio em SQLite.

Os arquivos de `dados/` são só o estado inicial: são lidos na criação do banco
e na restauração, nunca alterados. Toda mudança feita pelo assistente vai para
`var/condominio.db`.

Regras garantidas pelo próprio banco (não pelo modelo):
- `ux_reserva_ativa`: índice único parcial em (area, data) para reservas
  ativas. Duas gravações simultâneas para a mesma área e data não conseguem
  coexistir: a segunda falha com IntegrityError no instante do INSERT.
- `reservas.codigo UNIQUE` e reservas canceladas nunca são apagadas (só mudam
  de status), então um código novo nunca repete o de outra reserva.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import string
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS apartamentos (
    numero  TEXT PRIMARY KEY,
    morador TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS areas (
    id   TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    taxa REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reservas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo      TEXT NOT NULL UNIQUE,
    apartamento TEXT NOT NULL REFERENCES apartamentos(numero),
    area        TEXT NOT NULL REFERENCES areas(id),
    data        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ativa' CHECK (status IN ('ativa', 'cancelada')),
    criada_em   TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Exclusividade no instante da gravação: no máximo UMA reserva ativa por área e data.
CREATE UNIQUE INDEX IF NOT EXISTS ux_reserva_ativa
    ON reservas(area, data) WHERE status = 'ativa';
CREATE TABLE IF NOT EXISTS visitantes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL REFERENCES apartamentos(numero),
    nome        TEXT NOT NULL,
    data        TEXT NOT NULL,
    criado_em   TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Vínculo imutável sessão -> apartamento, gravado só na criação da sessão.
CREATE TABLE IF NOT EXISTS sessoes (
    session_id  TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL REFERENCES apartamentos(numero)
);
"""


@contextmanager
def conectar() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.CONDOMINIO_DB, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
    finally:
        conn.close()


def _ler_json(nome: str) -> list[dict]:
    with open(config.DADOS_DIR / nome, encoding="utf-8") as f:
        return json.load(f)


def inicializar() -> None:
    """Cria o schema e, se o banco estiver vazio, carrega o estado inicial."""
    with conectar() as conn:
        conn.executescript(SCHEMA)
        vazio = conn.execute("SELECT COUNT(*) FROM apartamentos").fetchone()[0] == 0
    if vazio:
        restaurar()


def restaurar() -> None:
    """Volta apartamentos, áreas, reservas e visitantes ao estado de `dados/`."""
    with conectar() as conn:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for tabela in ("reservas", "visitantes", "sessoes", "areas", "apartamentos"):
                conn.execute(f"DELETE FROM {tabela}")
            conn.execute("DELETE FROM sqlite_sequence")
            conn.executemany(
                "INSERT INTO apartamentos(numero, morador) VALUES (:numero, :morador)",
                _ler_json("apartamentos.json"),
            )
            conn.executemany(
                "INSERT INTO areas(id, nome, taxa) VALUES (:id, :nome, :taxa)",
                _ler_json("areas.json"),
            )
            conn.executemany(
                "INSERT INTO reservas(codigo, apartamento, area, data) "
                "VALUES (:codigo, :apartamento, :area, :data)",
                _ler_json("reservas.json"),
            )
            conn.executemany(
                "INSERT INTO visitantes(apartamento, nome, data) "
                "VALUES (:apartamento, :nome, :data)",
                _ler_json("visitantes.json"),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------- apartamentos / áreas


def apartamento_existe(numero: str) -> bool:
    with conectar() as conn:
        return conn.execute(
            "SELECT 1 FROM apartamentos WHERE numero = ?", (numero,)
        ).fetchone() is not None


def listar_areas() -> list[dict]:
    with conectar() as conn:
        return [dict(r) for r in conn.execute("SELECT id, nome, taxa FROM areas ORDER BY nome")]


def obter_area(area_id: str) -> dict | None:
    with conectar() as conn:
        r = conn.execute("SELECT id, nome, taxa FROM areas WHERE id = ?", (area_id,)).fetchone()
        return dict(r) if r else None


# ---------------------------------------------------------------- sessões


def vincular_sessao(session_id: str, apartamento: str) -> None:
    with conectar() as conn:
        conn.execute(
            "INSERT INTO sessoes(session_id, apartamento) VALUES (?, ?)",
            (session_id, apartamento),
        )


def apartamento_da_sessao(session_id: str) -> str | None:
    with conectar() as conn:
        r = conn.execute(
            "SELECT apartamento FROM sessoes WHERE session_id = ?", (session_id,)
        ).fetchone()
        return r["apartamento"] if r else None


# ---------------------------------------------------------------- reservas


def listar_reservas(apartamento: str) -> list[dict]:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT codigo, area, data FROM reservas "
            "WHERE apartamento = ? AND status = 'ativa' ORDER BY data, codigo",
            (apartamento,),
        )
        return [dict(r) for r in rows]


def data_livre(area: str, data: str) -> bool:
    """Só responde se está livre: nunca devolve de quem é a reserva."""
    with conectar() as conn:
        return conn.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
            (area, data),
        ).fetchone() is None


@dataclass
class ResultadoReserva:
    ok: bool
    codigo: str | None = None
    motivo: str | None = None


def _novo_codigo() -> str:
    alfabeto = string.ascii_uppercase + string.digits
    return "RSV-" + "".join(secrets.choice(alfabeto) for _ in range(6))


def criar_reserva(apartamento: str, area: str, data: str) -> ResultadoReserva:
    """Grava a reserva. A exclusividade é decidida pelo índice único no INSERT."""
    with conectar() as conn:
        for _ in range(10):
            codigo = _novo_codigo()
            try:
                conn.execute(
                    "INSERT INTO reservas(codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
                    (codigo, apartamento, area, data),
                )
                return ResultadoReserva(ok=True, codigo=codigo)
            except sqlite3.IntegrityError as e:
                msg = str(e)
                if "reservas.codigo" in msg:
                    continue  # colisão de código: sorteia outro
                if "reservas.area" in msg or "reservas.data" in msg:
                    return ResultadoReserva(ok=False, motivo="ocupada")
                raise
    return ResultadoReserva(ok=False, motivo="erro_codigo")


def cancelar_reserva(apartamento: str, area: str, data: str) -> str | None:
    """Cancela a reserva ativa DO PRÓPRIO apartamento. Devolve o código ou None."""
    with conectar() as conn:
        r = conn.execute(
            "UPDATE reservas SET status = 'cancelada' "
            "WHERE apartamento = ? AND area = ? AND data = ? AND status = 'ativa' "
            "RETURNING codigo",
            (apartamento, area, data),
        ).fetchone()
        return r["codigo"] if r else None


def cancelar_reserva_por_codigo(apartamento: str, codigo: str) -> dict | None:
    with conectar() as conn:
        r = conn.execute(
            "UPDATE reservas SET status = 'cancelada' "
            "WHERE apartamento = ? AND codigo = ? AND status = 'ativa' "
            "RETURNING codigo, area, data",
            (apartamento, codigo.strip().upper()),
        ).fetchone()
        return dict(r) if r else None


# ---------------------------------------------------------------- visitantes


def listar_visitantes(apartamento: str) -> list[dict]:
    with conectar() as conn:
        rows = conn.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, id",
            (apartamento,),
        )
        return [dict(r) for r in rows]


def autorizar_visitante(apartamento: str, nome: str, data: str) -> None:
    with conectar() as conn:
        conn.execute(
            "INSERT INTO visitantes(apartamento, nome, data) VALUES (?, ?, ?)",
            (apartamento, nome, data),
        )
