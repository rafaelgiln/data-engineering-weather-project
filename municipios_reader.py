from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Dict


PROJECT_ROOT = Path(__file__).resolve().parent
CSV_PATH = PROJECT_ROOT / "municipios.csv"


def iter_municipios() -> Iterable[Dict[str, str]]:
    """
    Lê o arquivo municipios.csv e devolve um iterador de dicionários,
    onde cada dicionário representa uma linha (município).
    """
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"Arquivo CSV não encontrado em: {CSV_PATH}")

    with CSV_PATH.open(mode="r", encoding="utf-8", newline="") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            yield row


def listar_municipios(limit: int | None = None) -> None:
    """
    Lista o conteúdo do municipios.csv no terminal.

    :param limit: quantidade máxima de linhas a exibir (None = todas).
    """
    count = 0
    for municipio in iter_municipios():
        print(municipio)
        count += 1
        if limit is not None and count >= limit:
            break


def main() -> None:
    """
    Exemplo de uso: lista os primeiros 10 municípios.
    """
    listar_municipios(limit=10)


if __name__ == "__main__":
    main()

