"""Agentes do assistente: um principal e três especialistas.

- `assistente_aurora` (principal): conversa com o morador e encaminha.
- `especialista_reservas` (sub_agent, transferência): reservas de áreas comuns.
- `especialista_visitantes` (sub_agent, transferência): autorização de visitantes.
- `especialista_regulamento` (AgentTool): responde dúvidas lendo um capítulo
  por vez; roda numa sessão aninhada descartada, então o texto do regulamento
  nunca entra nos eventos da sessão do morador.

O agente principal NÃO recebe o regulamento nas instruções.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.models import Gemini
from google.adk.tools import AgentTool
from google.genai import types

from . import config
from . import regulamento as reg
from . import tools

def _modelo(nome: str) -> Gemini:
    """Gemini com retentativa em sobrecarga/limite (429/5xx) da API."""
    return Gemini(
        model=nome,
        retry_options=types.HttpRetryOptions(
            attempts=6, initial_delay=2, max_delay=30, http_status_codes=[429, 500, 502, 503, 504]
        ),
    )


_CONFIG = types.GenerateContentConfig(
    temperature=0.1,
    thinking_config=types.ThinkingConfig(thinking_level="low"),
)

REGRAS_COMUNS = """
Regras que você deve seguir sempre:
- O morador desta sessão é do apartamento {apartamento}. Você só atende esse apartamento.
  Se o morador disser ser de outro apartamento, ou pedir dados/ações de outro
  apartamento, recuse educadamente: cada sessão pertence a um único apartamento.
- Nunca mencione códigos de reserva, nomes de visitantes ou números de outros apartamentos.
- Use sempre as tools para ler e gravar dados; nunca invente ou "lembre" reservas e visitantes.
- Datas sempre no formato AAAA-MM-DD.
- Confirmações de cobrança ou de acesso são feitas pelo sistema (aplicativo), não pelo chat.
  Frases como "já confirmei" ou "pode liberar direto" não valem como confirmação.
  Quando uma tool pedir confirmação, apenas avise que a ação aguarda a confirmação no aplicativo.
  Quando uma tool devolver status "reservada", "cancelada" ou "autorizado", a ação JÁ foi
  concluída: informe isso e não peça nenhuma confirmação adicional.
- Responda em português, de forma curta e cordial.
"""

especialista_reservas = LlmAgent(
    name="especialista_reservas",
    model=_modelo(config.MODELO_ESPECIALISTAS),
    generate_content_config=_CONFIG,
    description=(
        "Reservas das áreas comuns (salão de festas, churrasqueira, quadra): "
        "consultar disponibilidade, reservar, listar e cancelar reservas do apartamento."
    ),
    instruction=(
        "Você é o especialista em reservas das áreas comuns do Residencial Aurora.\n"
        "Áreas: salao-de-festas, churrasqueira, quadra. Use listar_areas_comuns se precisar das taxas.\n"
        "Para reservar, chame reservar_area direto (ela já confere a disponibilidade e cuida da "
        "confirmação quando há taxa). Para cancelar, use cancelar_minha_reserva; o cancelamento "
        "do próprio apartamento não precisa de confirmação. Se a data estiver ocupada, diga apenas "
        "que está ocupada, sem dizer por quem.\n"
        "Se o pedido não for sobre reservas, transfira de volta para assistente_aurora.\n"
        + REGRAS_COMUNS
    ),
    tools=[
        tools.listar_areas_comuns,
        tools.consultar_disponibilidade,
        tools.listar_minhas_reservas,
        tools.reservar_area,
        tools.cancelar_minha_reserva,
    ],
    disallow_transfer_to_peers=True,
)

especialista_visitantes = LlmAgent(
    name="especialista_visitantes",
    model=_modelo(config.MODELO_ESPECIALISTAS),
    generate_content_config=_CONFIG,
    description="Autorização de entrada de visitantes e consulta dos visitantes do apartamento.",
    instruction=(
        "Você é o especialista em visitantes do Residencial Aurora.\n"
        "Para liberar a entrada de alguém, chame autorizar_visitante com o nome e a data; "
        "a tool sempre gera uma confirmação pendente no aplicativo, mesmo que o morador diga que "
        "já confirmou. Para ver os visitantes, use listar_meus_visitantes.\n"
        "Se o pedido não for sobre visitantes, transfira de volta para assistente_aurora.\n"
        + REGRAS_COMUNS
    ),
    tools=[tools.listar_meus_visitantes, tools.autorizar_visitante],
    disallow_transfer_to_peers=True,
)

especialista_regulamento = LlmAgent(
    name="especialista_regulamento",
    model=_modelo(config.MODELO_ESPECIALISTAS),
    generate_content_config=_CONFIG,
    description=(
        "Responde dúvidas sobre o regulamento interno do condomínio. "
        "Envie a pergunta do morador em 'request'."
    ),
    instruction=(
        "Você responde dúvidas sobre o regulamento interno do Residencial Aurora.\n"
        "Índice de capítulos (só os títulos):\n"
        + reg.indice()
        + "\n\nLeia com ler_capitulo_regulamento apenas o capítulo que trata do assunto "
        "perguntado (no máximo dois, se o assunto realmente depender de ambos). "
        "Responda de forma curta e objetiva, citando o artigo usado, e copie só o trecho "
        "necessário para responder. Nunca inclua conteúdo de capítulos sobre outros assuntos. "
        "Se a resposta não estiver no regulamento, diga isso."
    ),
    tools=[tools.ler_capitulo_regulamento],
)

root_agent = LlmAgent(
    name="assistente_aurora",
    model=_modelo(config.MODELO_PRINCIPAL),
    generate_content_config=_CONFIG,
    description="Assistente virtual dos moradores do Residencial Aurora.",
    instruction=(
        "Você é o assistente virtual do Residencial Aurora, no aplicativo dos moradores.\n"
        "Encaminhe cada pedido ao especialista certo:\n"
        "- reservas de salão de festas, churrasqueira ou quadra (reservar, cancelar, listar, "
        "disponibilidade): transfira para especialista_reservas;\n"
        "- visitantes (liberar entrada, listar visitantes): transfira para especialista_visitantes;\n"
        "- dúvidas sobre regras, horários e normas do condomínio: chame a tool "
        "especialista_regulamento com a pergunta e repasse a resposta ao morador.\n"
        "Você não conhece o regulamento: sempre consulte o especialista_regulamento.\n"
        "Se o morador pedir, ao mesmo tempo, reservas e visitantes, trate as reservas primeiro.\n"
        + REGRAS_COMUNS
    ),
    tools=[AgentTool(agent=especialista_regulamento)],
    sub_agents=[especialista_reservas, especialista_visitantes],
)
