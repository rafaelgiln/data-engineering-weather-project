from __future__ import annotations

from typing import Any, Dict, List

from api_call import get_current_weather
from municipios_reader import iter_municipios


def get_weather_for_first_n_municipios(n: int = 10) -> List[Dict[str, Any]]:
    """
    Faz a chamada da API de clima para os primeiros N municípios
    listados no arquivo municipios.csv, usando latitude e longitude.

    :param n: quantidade de municípios a processar.
    :return: lista de dicionários com dados da API para cada município.
    """
    resultados: List[Dict[str, Any]] = []

    for i, municipio in enumerate(iter_municipios()):
        if i >= n:
            break

        nome = municipio.get("nome")
        lat_str = municipio.get("latitude")
        lon_str = municipio.get("longitude")

        if lat_str is None or lon_str is None:
            print(f"Pulando município sem coordenadas: {nome}")
            continue

        try:
            lat = float(lat_str)
            lon = float(lon_str)
        except ValueError:
            print(f"Coordenadas inválidas para município {nome}: lat={lat_str}, lon={lon_str}")
            continue

        print(f"Buscando clima para {nome} (lat={lat}, lon={lon})...")
        weather_data = get_current_weather(lat=lat, lon=lon)
        resultados.append(
            {
                "municipio": nome,
                "latitude": lat,
                "longitude": lon,
                "weather": weather_data,
            }
        )

    return resultados


def imprimir_resumo(resultados: List[Dict[str, Any]]) -> None:
    """
    Imprime um resumo simples dos resultados obtidos na API.
    """
    for item in resultados:
        municipio = item["municipio"]
        weather = item["weather"]

        temp = weather["main"]["temp"]
        descricao = weather["weather"][0]["description"]
        cidade_api = weather.get("name", "")

        print("-" * 60)
        print(f"Município (CSV): {municipio}")
        if cidade_api:
            print(f"Nome retornado pela API: {cidade_api}")
        print(f"Temperatura: {temp} °C")
        print(f"Condição: {descricao}")


def main() -> None:
    """
    Executa a busca de clima para os 10 primeiros municípios do CSV.
    """
    resultados = get_weather_for_first_n_municipios(n=10)
    imprimir_resumo(resultados)


if __name__ == "__main__":
    main()

