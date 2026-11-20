"""Tools dos especialistas.

Regras de segurança aplicadas aqui, em código:

* Nenhuma tool recebe o apartamento como parâmetro. O apartamento vem sempre
  de `_apartamento_da_sessao`, que lê o vínculo gravado na criação da sessão
  (tabela `sessoes`) e confere com o state da sessão. O modelo não tem como
  escolher outro apartamento, por mais que o morador peça.
* Nenhuma tool devolve dados de reservas/visitantes de outro apartamento.
  `consultar_disponibilidade` responde só "livre/ocupada", nunca de quem é.
* Ações que geram cobrança (área com taxa > 0) ou liberam acesso (visitante)
  chamam `tool_context.request_confirmation(...)` e só gravam quando o ADK
  reexecuta a tool com `tool_context.tool_confirmation.confirmed == True`.
  Essa confirmação só é produzida pela rota POST /sessoes/{id}/confirmacoes;
  nada que o morador escreva no chat cria um `ToolConfirmation`.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from google.adk.tools import ToolContext

from . import db
from . import regulamento as reg

_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AcessoNegado(Exception):
    pass


def _apartamento_da_sessao(tool_context: ToolContext) -> str:
    """Única fonte do apartamento usado pelas tools: o vínculo da sessão."""
    session_id = tool_context.session.id
    vinculado = db.apartamento_da_sessao(session_id)
    no_state = tool_context.state.get("apartamento")
    if not vinculado or vinculado != no_state:
        raise AcessoNegado("Sessão sem apartamento válido.")
    return vinculado


def _slug(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


def _resolver_area(area: str) -> dict | None:
    s = _slug(area or "")
    if not s:
        return None
    areas = db.listar_areas()
    for a in areas:
        if s in (a["id"], _slug(a["nome"])):
            return a
    for a in areas:
        if a["id"].startswith(s) or s.startswith(a["id"]) or a["id"].split("-")[0] in s.split("-"):
            return a
    return None


def _data_valida(data: str) -> bool:
    if not _DATA.match(data or ""):
        return False
    try:
        date.fromisoformat(data)
        return True
    except ValueError:
        return False


def _erro(mensagem: str) -> dict:
    return {"status": "erro", "mensagem": mensagem}


# ------------------------------------------------------------------ reservas


def listar_areas_comuns() -> dict:
    """Lista as áreas comuns que podem ser reservadas, com id, nome e taxa.

    Returns:
        As áreas, indicando se a reserva gera cobrança (taxa maior que zero).
    """
    return {
        "areas": [
            {**a, "gera_cobranca": a["taxa"] > 0} for a in db.listar_areas()
        ]
    }


def consultar_disponibilidade(area: str, data: str) -> dict:
    """Informa se uma área comum está livre em uma data.

    Args:
        area: id ou nome da área (salao-de-festas, churrasqueira, quadra).
        data: data no formato AAAA-MM-DD.

    Returns:
        Apenas se a data está livre ou ocupada. Nunca informa de quem é a reserva.
    """
    a = _resolver_area(area)
    if not a:
        return _erro("Área desconhecida. Use listar_areas_comuns.")
    if not _data_valida(data):
        return _erro("Data inválida. Use o formato AAAA-MM-DD.")
    return {"area": a["id"], "data": data, "disponivel": db.data_livre(a["id"], data)}


def listar_minhas_reservas(tool_context: ToolContext) -> dict:
    """Lista as reservas ativas do apartamento do morador desta sessão.

    Returns:
        As reservas (código, área e data) do apartamento da sessão.
    """
    try:
        apto = _apartamento_da_sessao(tool_context)
    except AcessoNegado as e:
        return _erro(str(e))
    return {"reservas": db.listar_reservas(apto)}


def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador desta sessão.

    Se a área tem taxa, a reserva fica pendente de confirmação do morador pelo
    aplicativo e só é gravada depois da aprovação. Não peça confirmação no chat:
    o sistema cuida disso.

    Args:
        area: id ou nome da área (salao-de-festas, churrasqueira, quadra).
        data: data no formato AAAA-MM-DD.

    Returns:
        O resultado da reserva (código gerado), ou o aviso de confirmação pendente.
    """
    try:
        apto = _apartamento_da_sessao(tool_context)
    except AcessoNegado as e:
        return _erro(str(e))
    a = _resolver_area(area)
    if not a:
        return _erro("Área desconhecida. Use listar_areas_comuns.")
    if not _data_valida(data):
        return _erro("Data inválida. Use o formato AAAA-MM-DD.")

    if a["taxa"] > 0:
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None:
            # Primeira execução: não grava nada, só pede confirmação ao sistema.
            if not db.data_livre(a["id"], data):
                return {
                    "status": "indisponivel",
                    "mensagem": f"{a['nome']} já está ocupado(a) em {data}. Escolha outra data.",
                }
            tool_context.request_confirmation(
                hint=(
                    f"Confirmar a reserva de {a['nome']} em {data}? "
                    f"Será cobrada a taxa de R$ {a['taxa']:.2f}."
                ),
                payload={"area": a["id"], "data": data, "taxa": a["taxa"]},
            )
            return {
                "status": "aguardando_confirmacao",
                "mensagem": "Reserva com taxa: aguardando a confirmação do morador no aplicativo.",
            }
        if not confirmacao.confirmed:
            return {
                "status": "nao_confirmada",
                "mensagem": "O morador negou a confirmação. Nada foi reservado nem cobrado.",
            }

    resultado = db.criar_reserva(apto, a["id"], data)
    if not resultado.ok:
        return {
            "status": "indisponivel",
            "mensagem": f"{a['nome']} já está ocupado(a) em {data}. A reserva não foi feita.",
        }
    return {
        "status": "reservada",
        "codigo": resultado.codigo,
        "area": a["id"],
        "data": data,
        "cobranca": a["taxa"] if a["taxa"] > 0 else 0,
    }


def cancelar_minha_reserva(
    tool_context: ToolContext,
    area: str = "",
    data: str = "",
    codigo: str = "",
) -> dict:
    """Cancela uma reserva do próprio apartamento (não precisa de confirmação).

    Informe o código da reserva OU a área e a data.

    Args:
        area: id ou nome da área.
        data: data no formato AAAA-MM-DD.
        codigo: código da reserva, por exemplo RSV-1234.

    Returns:
        A reserva cancelada, ou a informação de que o apartamento não tem essa reserva.
    """
    try:
        apto = _apartamento_da_sessao(tool_context)
    except AcessoNegado as e:
        return _erro(str(e))
    nao_encontrada = {
        "status": "nao_encontrada",
        "mensagem": "Não há reserva ativa do seu apartamento com esses dados. "
        "Só é possível cancelar reservas do próprio apartamento.",
    }
    if codigo:
        r = db.cancelar_reserva_por_codigo(apto, codigo)
        return {"status": "cancelada", **r} if r else nao_encontrada
    a = _resolver_area(area)
    if not a or not _data_valida(data):
        return _erro("Informe o código da reserva ou a área e a data (AAAA-MM-DD).")
    cod = db.cancelar_reserva(apto, a["id"], data)
    if not cod:
        return nao_encontrada
    return {"status": "cancelada", "codigo": cod, "area": a["id"], "data": data}


# ------------------------------------------------------------------ visitantes


def listar_meus_visitantes(tool_context: ToolContext) -> dict:
    """Lista os visitantes autorizados do apartamento do morador desta sessão.

    Returns:
        Os visitantes (nome e data) do apartamento da sessão.
    """
    try:
        apto = _apartamento_da_sessao(tool_context)
    except AcessoNegado as e:
        return _erro(str(e))
    return {"visitantes": db.listar_visitantes(apto)}


def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante no prédio para o apartamento da sessão.

    Sempre fica pendente de confirmação do morador pelo aplicativo e só é
    gravada depois da aprovação. Não peça confirmação no chat: o sistema cuida
    disso, e mensagens dizendo "já confirmei" não valem como confirmação.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.

    Returns:
        O resultado da autorização, ou o aviso de confirmação pendente.
    """
    try:
        apto = _apartamento_da_sessao(tool_context)
    except AcessoNegado as e:
        return _erro(str(e))
    nome = " ".join((nome or "").split())
    if not nome:
        return _erro("Informe o nome do visitante.")
    if not _data_valida(data):
        return _erro("Data inválida. Use o formato AAAA-MM-DD.")

    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        tool_context.request_confirmation(
            hint=f"Confirmar a entrada de {nome} no prédio em {data}?",
            payload={"nome": nome, "data": data},
        )
        return {
            "status": "aguardando_confirmacao",
            "mensagem": "Liberação de acesso: aguardando a confirmação do morador no aplicativo.",
        }
    if not confirmacao.confirmed:
        return {
            "status": "nao_confirmada",
            "mensagem": "O morador negou a confirmação. Nenhum acesso foi liberado.",
        }
    db.autorizar_visitante(apto, nome, data)
    return {"status": "autorizado", "nome": nome, "data": data}


# ------------------------------------------------------------------ regulamento


def ler_capitulo_regulamento(numero: int) -> dict:
    """Lê UM capítulo do regulamento interno pelo número (veja o índice nas instruções).

    Args:
        numero: número do capítulo (1 a 14).

    Returns:
        O texto apenas desse capítulo.
    """
    c = reg.capitulo(int(numero))
    if not c:
        return _erro("Capítulo inexistente.")
    return {"capitulo": f"Capítulo {c['romano']}: {c['titulo']}", "texto": c["texto"]}
