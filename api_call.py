from __future__ import annotations

import os
from typing import Any, Dict

import requests
from dotenv import load_dotenv


BASE_URL = "https://api.openweathermap.org/data/2.5/weather"


def load_api_key(env_var_name: str = "OPENWEATHER_API_KEY") -> str:
    """
    Carrega a API key a partir de variável de ambiente.
    Espera que o arquivo .env contenha a chave OPENWEATHER_API_KEY.
    """
    load_dotenv()

    api_key = os.getenv(env_var_name)
    if not api_key:
        raise RuntimeError(
            f"Variável de ambiente {env_var_name} não encontrada. "
            "Verifique se o arquivo .env existe e contém a chave correta."
        )
    return api_key


def get_current_weather(
    lat: float,
    lon: float,
    units: str = "metric",
    lang: str = "pt_br",
) -> Dict[str, Any]:
    """
    Faz chamada à API Current Weather da OpenWeather.

    :param lat: Latitude da localização.
    :param lon: Longitude da localização.
    :param units: Unidade de medida ('standard', 'metric' ou 'imperial').
    :param lang: Código de idioma (ex.: 'pt_br', 'en', 'es').
    :return: Dicionário com os dados retornados pela API.
    """
    api_key = load_api_key()

    params = {
        "lat": lat,
        "lon": lon,
        "appid": api_key,
        "units": units,
        "lang": lang,
    }

    response = requests.get(BASE_URL, params=params, timeout=10)
    response.raise_for_status()

    return response.json()


def print_weather_summary(weather_data: Dict[str, Any]) -> None:
    """
    Imprime um resumo simples das principais informações de clima.

    :param weather_data: Dicionário retornado por get_current_weather.
    """
    temp = weather_data["main"]["temp"]
    description = weather_data["weather"][0]["description"]
    city_name = weather_data["name"]

    print(f"Cidade: {city_name}")
    print(f"Temperatura: {temp} °C")
    print(f"Condição: {description}")


def main() -> None:
    """Função principal de exemplo de uso."""
    # Exemplo: São Paulo
    latitude = -23.55
    longitude = -46.63

    weather = get_current_weather(latitude, longitude)
    print_weather_summary(weather)


if __name__ == "__main__":
    main()