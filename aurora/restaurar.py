"""Restaura reservas e visitantes ao estado de dados/ e apaga as sessões.

Uso: uv run python -m aurora.restaurar
"""

from . import config, db


def main() -> None:
    db.restaurar()
    for sufixo in ("", "-wal", "-shm"):
        caminho = config.SESSOES_DB.with_name(config.SESSOES_DB.name + sufixo)
        caminho.unlink(missing_ok=True)
    print("Dados restaurados ao estado inicial de dados/ e sessões apagadas.")


if __name__ == "__main__":
    main()
