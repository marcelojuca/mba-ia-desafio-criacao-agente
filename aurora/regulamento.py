"""Leitura do regulamento por capítulo.

O regulamento nunca é carregado inteiro em instrução ou evento: ele é dividido
nos capítulos `## Capítulo ...` de `dados/regulamento.md`, e o especialista de
regulamento lê só o capítulo que precisa por meio de uma tool.
"""

from __future__ import annotations

import re
from functools import lru_cache

from . import config

_TITULO = re.compile(r"^## Capítulo ([IVXLC]+): (.+)$", re.MULTILINE)


@lru_cache(maxsize=1)
def capitulos() -> list[dict]:
    texto = (config.DADOS_DIR / "regulamento.md").read_text(encoding="utf-8")
    marcas = list(_TITULO.finditer(texto))
    resultado = []
    for i, m in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        resultado.append(
            {
                "numero": i + 1,
                "romano": m.group(1),
                "titulo": m.group(2).strip(),
                "texto": texto[m.start():fim].strip(),
            }
        )
    return resultado


def indice() -> str:
    """Só os títulos, para a instrução do especialista (nunca o conteúdo)."""
    return "\n".join(f"{c['numero']}. Capítulo {c['romano']}: {c['titulo']}" for c in capitulos())


def capitulo(numero: int) -> dict | None:
    for c in capitulos():
        if c["numero"] == numero:
            return c
    return None
