# Residencial Aurora: assistente virtual com Google ADK

Assistente do aplicativo dos moradores do Residencial Aurora. Pelo chat o morador
reserva o salão de festas, a churrasqueira e a quadra, cancela as próprias reservas,
autoriza visitantes e tira dúvidas sobre o regulamento. O assistente é exposto por uma API
FastAPI em `http://localhost:8000`.

Enunciado do desafio: [Full Cycle — Criação de Agente](https://plataforma.fullcycle.com.br/courses/a091b0fe-a5c6-4287-a3d3-1ec61defcfd3/413/224/266/conteudos?projeto=70&fase=309)

O princípio do projeto é: **o modelo decide o caminho, o código decide o que é permitido.**
Todas as regras críticas estão em tools, no banco e na API, e não no prompt.

```
aurora/
├── config.py       caminhos, variáveis de ambiente e modelos
├── db.py           armazenamento SQLite do condomínio (reservas, visitantes, vínculo sessão→apto)
├── regulamento.py  divisão do regulamento por capítulo
├── tools.py        tools dos especialistas (todas as regras de negócio)
├── agents.py       agente principal + especialistas
├── agent.py        entrada para `adk web`
├── service.py      App, Runner, sessão persistida e fluxo de confirmação
├── api.py          rotas HTTP (contrato do desafio)
└── restaurar.py    comando de restauração dos dados
dados/              estado inicial (inalterado)
var/                bancos gerados em tempo de execução (fora do Git)
```

## Arquitetura

```
                 morador ── API (FastAPI) ── Runner(App resumível, SqliteSessionService)
                                                  │
                                        assistente_aurora  (principal)
                    ┌─────────────── transfer ────┼──── transfer ───────────────┐
           especialista_reservas                  │                 especialista_visitantes
           listar_areas_comuns                    │ AgentTool       listar_meus_visitantes
           consultar_disponibilidade              │                 autorizar_visitante (confirmação)
           listar_minhas_reservas        especialista_regulamento
           reservar_area (confirmação se taxa)    ler_capitulo_regulamento
           cancelar_minha_reserva
```

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `assistente_aurora` (principal) | Conversa com o morador e encaminha cada pedido. Não tem tools de dados nem o regulamento nas instruções. | Raiz do `App`, recebe toda mensagem nova. | Ponto único de entrada. Mantém o prompt principal pequeno e barato. |
| `especialista_reservas` | Áreas, disponibilidade, reservar, listar e cancelar reservas do próprio apartamento. | `sub_agents` do principal, por **transferência** (`transfer_to_agent`). | A reserva com taxa pausa a execução para confirmação. Um sub-agente transferido roda na **mesma sessão**, então o pedido de confirmação fica registrado nos eventos dela e o Runner consegue retomar exatamente esse agente quando chega a resposta. O `AgentTool` descarta a sessão aninhada e não consegue retomar (o próprio ADK avisa isso em `agent_tool.py`). |
| `especialista_visitantes` | Autorizar entrada e listar visitantes do próprio apartamento. | `sub_agents` do principal, por **transferência**. | Mesmo motivo: toda autorização pausa para confirmação. |
| `especialista_regulamento` | Responde dúvidas lendo **um capítulo por vez** do regulamento. | **`AgentTool`** do principal. | O `AgentTool` roda o especialista numa sessão aninhada **descartada**. Só a resposta final curta volta para a sessão do morador. O texto dos capítulos lidos nunca entra nos eventos da sessão principal (Garantia 4). |

Os especialistas de reservas e visitantes têm `disallow_transfer_to_peers=True`. Eles só
voltam para o principal, o que mantém a topologia previsível para a retomada das
confirmações.

**Armazenamento**
- `var/condominio.db` (SQLite): apartamentos, áreas, reservas, visitantes e a tabela
  `sessoes` (vínculo sessão→apartamento). Os arquivos de `dados/` são lidos só na criação
  do banco e na restauração.
- `var/sessoes.db` (SQLite, `SqliteSessionService` do ADK): sessões, state e eventos.

Nenhum serviço externo é necessário.

**Modelos:** Gemini via Google AI Studio. O padrão é `gemini-3.1-flash-lite` para todos os
agentes, configurável por `AURORA_MODELO_PRINCIPAL` e `AURORA_MODELO_ESPECIALISTAS`. Ele foi
escolhido porque o fluxo completo do avaliador passou com ele e porque tem cota diária
gratuita maior: no free tier, o `gemini-3-flash` permite só 20 requisições por dia, menos do
que as algumas dezenas que o fluxo usa. As
chamadas usam retentativa automática em 429/5xx (`_modelo` em `aurora/agents.py`), porque a
API às vezes responde "high demand".

## Garantias

### 1. Cobrança ou acesso só com confirmação
- **`aurora/tools.py` → `reservar_area`:** quando `a["taxa"] > 0` e
  `tool_context.tool_confirmation is None`, a tool **não grava**. Ela chama
  `tool_context.request_confirmation(hint=..., payload={"area", "data", "taxa"})` e
  devolve `aguardando_confirmacao`. Só grava quando o ADK a reexecuta com
  `tool_confirmation.confirmed == True`. Com `confirmed == False`, devolve
  `nao_confirmada` sem gravar. A quadra (taxa 0) é gravada direto, sem confirmação.
- **`aurora/tools.py` → `autorizar_visitante`:** sempre pede confirmação
  (`payload={"nome", "data"}`) e só grava com `confirmed == True`.
- **`aurora/service.py` → `confirmacoes_pendentes`:** as pendências são calculadas a partir
  dos **eventos da sessão**: chamadas `adk_request_confirmation` sem `FunctionResponse` de
  mesmo id. Viram `confirmacoes_pendentes` na resposta, com `acao` (nome da tool) e
  `detalhes` (o payload).
- **`aurora/service.py` → `responder_confirmacao`:** sob um lock da sessão, procura o id
  entre as pendências **daquela sessão**. Se não estiver lá (inexistente, de outra sessão ou
  já respondido), lança `ConfirmacaoInexistente`, a API devolve **409** e nada roda. Se
  estiver, envia ao Runner um `FunctionResponse(name="adk_request_confirmation",
  response={"confirmed": ...})` com o `invocation_id` da pausa
  (`App(resumability_config=ResumabilityConfig(is_resumable=True))`). O ADK então reexecuta a
  tool original com os **argumentos originais**. Depois disso o id deixa de estar pendente,
  então um reenvio recebe 409.

**Por que não depende do modelo:** o único jeito de criar um `ToolConfirmation` é a rota
`POST /sessoes/{id}/confirmacoes`. Texto no chat ("já estou confirmando") é só texto, e a
tool continua vendo `tool_confirmation is None`. O modelo também não consegue trocar a área
ou a data entre o pedido e a aprovação, porque a retomada usa a chamada original.

### 2. Cada sessão pertence a um apartamento
- **`aurora/service.py` → `criar_sessao`:** o apartamento é gravado uma única vez, no state
  da sessão (`{"apartamento": ...}`) e na tabela `sessoes` (`db.vincular_sessao`).
- **`aurora/tools.py` → `_apartamento_da_sessao`:** é a **única** fonte de apartamento das
  tools. Lê o vínculo de `sessoes` pelo `tool_context.session.id` e exige que ele bata com o
  state. **Nenhuma tool tem parâmetro de apartamento**, então o modelo não tem como escolher
  outro.
- **`aurora/db.py`:** todas as consultas e gravações filtram por `apartamento = ?`
  (`listar_reservas`, `listar_visitantes`, `cancelar_reserva`,
  `cancelar_reserva_por_codigo`, com `UPDATE ... WHERE apartamento = ?`). Cancelar a reserva
  de outro apartamento devolve `nao_encontrada`, a mesma resposta de uma reserva que não
  existe, sem código nem dono.
- **`aurora/db.py` → `data_livre` / `aurora/tools.py` → `consultar_disponibilidade`:**
  devolvem só `disponivel: true/false`, nunca o código ou o apartamento da reserva. Ao
  reservar uma data ocupada, `reservar_area` diz apenas "já está ocupado(a)".

**Por que não depende do modelo:** mesmo que o morador convença o modelo de que é do 302,
as tools continuam usando o apartamento do vínculo da sessão, e os dados de outros
apartamentos nunca chegam a uma resposta de tool. Por isso também não chegam aos eventos.

### 3. Nada se perde no reinício
- **`aurora/service.py`:** `SqliteSessionService(str(config.SESSOES_DB))` persiste sessões,
  state e eventos em `var/sessoes.db`. O `Runner` é construído sobre esse serviço e sobre o
  `App` resumível, então uma pausa de confirmação também sobrevive ao reinício (o
  `invocation_id` é lido dos eventos persistidos).
- **`aurora/db.py`:** reservas e visitantes ficam em `var/condominio.db`.
  `db.inicializar()` (chamado no `lifespan` de `aurora/api.py`) só carrega `dados/` quando o
  banco está vazio. Reiniciar a API não restaura nada.
- **Códigos únicos:** `reservas.codigo UNIQUE`, e reservas canceladas não são apagadas
  (`status = 'cancelada'`). `db._novo_codigo` sorteia `RSV-` + 6 caracteres alfanuméricos,
  formato que nunca coincide com os códigos de 4 dígitos de `dados/`, e sorteia de novo em
  caso de colisão.

### 4. O regulamento é consultado, não carregado
- **`aurora/agents.py` → `root_agent`:** a instrução do principal não contém o regulamento.
  Ela só manda consultar o `especialista_regulamento`.
- **`aurora/agents.py` → `especialista_regulamento`:** a instrução tem apenas o **índice**
  de títulos (`regulamento.indice()`). O conteúdo vem da tool `ler_capitulo_regulamento`
  (`aurora/tools.py`), que devolve **um único capítulo** (`aurora/regulamento.py` →
  `capitulo`).
- O especialista é um `AgentTool`. Em `google/adk/tools/agent_tool.py` ele roda numa
  sessão aninhada que é descartada, e só o texto final volta como `functionResponse` para a
  sessão do morador. Assim, nenhum evento da sessão principal contém capítulos inteiros, e
  muito menos capítulos de outros assuntos.

### 5. Dois moradores, uma reserva
- **`aurora/db.py` → `SCHEMA`:**
  `CREATE UNIQUE INDEX ux_reserva_ativa ON reservas(area, data) WHERE status = 'ativa'`.
  É um índice único **parcial**: no máximo uma reserva ativa por área e data, e uma
  cancelada não bloqueia a data.
- **`aurora/db.py` → `criar_reserva`:** faz o `INSERT` direto. Se outra transação gravou
  primeiro, o SQLite rejeita com `IntegrityError` (`UNIQUE constraint failed: reservas.area,
  reservas.data`), que vira `ResultadoReserva(ok=False, motivo="ocupada")`. `reservar_area`
  devolve então "já está ocupado(a) … A reserva não foi feita", uma resposta normal com
  **HTTP 200**.
- A conferência prévia (`data_livre`) é só para UX. **A exclusividade é decidida pelo banco
  no instante do INSERT.** O SQLite serializa escritas (WAL + `busy_timeout`), então duas
  aprovações simultâneas nunca deixam duas reservas ativas.

## Como rodar

**Pré-requisitos**
- Python 3.12+ e [uv](https://docs.astral.sh/uv/)
- Chave do Google AI Studio: <https://aistudio.google.com/apikey>

**Variáveis do `.env`** (copie de `.env.example`)

| Variável | Obrigatória | Descrição |
|---|---|---|
| `GOOGLE_API_KEY` | sim | Chave do Google AI Studio |
| `GOOGLE_GENAI_USE_VERTEXAI` | sim (`FALSE`) | Usa a API do Gemini, não a Vertex AI |
| `AURORA_MODELO_PRINCIPAL` | não | Modelo do agente principal (padrão `gemini-3.1-flash-lite`) |
| `AURORA_MODELO_ESPECIALISTAS` | não | Modelo dos especialistas (padrão: o mesmo do principal) |

**Comandos**

```bash
cp .env.example .env          # e preencha GOOGLE_API_KEY
uv sync

# Restaurar os dados: volta reservas e visitantes ao estado de dados/ e apaga as sessões
uv run python -m aurora.restaurar

# Subir a API em http://localhost:8000
uv run uvicorn aurora.api:app --port 8000
```

Não há serviço externo para subir: os dois bancos são SQLite em `var/`, criados
automaticamente.

Para depurar transferências, tools e confirmações visualmente: `uv run adk web .`
(selecione o app `aurora`).

**Rotas**

| Método | Rota | Descrição |
|---|---|---|
| POST | `/sessoes` | `{"apartamento": "101"}` → 201 `{"session_id"}` |
| POST | `/sessoes/{id}/mensagens` | `{"texto"}` → `{"resposta", "confirmacoes_pendentes"}` |
| POST | `/sessoes/{id}/confirmacoes` | `{"id", "confirmado"}` → 200 (mesmo formato) ou 409 |
| GET | `/sessoes/{id}/eventos` | Todos os eventos da sessão, em ordem |
| GET | `/apartamentos/{n}/reservas` | Reservas ativas do apartamento |
| GET | `/apartamentos/{n}/visitantes` | Visitantes autorizados do apartamento |
