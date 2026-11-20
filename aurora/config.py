"""Configuração central: caminhos e variáveis de ambiente."""

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

DADOS_DIR = RAIZ / "dados"
VAR_DIR = Path(os.getenv("AURORA_VAR_DIR", RAIZ / "var"))
VAR_DIR.mkdir(parents=True, exist_ok=True)

# Banco do condomínio (reservas, visitantes, vínculo sessão→apartamento).
CONDOMINIO_DB = VAR_DIR / "condominio.db"
# Banco das sessões do ADK (eventos e state), persistido em SQLite.
SESSOES_DB = VAR_DIR / "sessoes.db"

APP_NAME = "residencial_aurora"

MODELO_PRINCIPAL = os.getenv("AURORA_MODELO_PRINCIPAL") or "gemini-3.1-flash-lite"
MODELO_ESPECIALISTAS = os.getenv("AURORA_MODELO_ESPECIALISTAS") or MODELO_PRINCIPAL
