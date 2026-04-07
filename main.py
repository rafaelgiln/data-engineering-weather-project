from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd
from google.cloud import storage
from flask import Flask, Request, jsonify, request as flask_request

from batch_weather import get_weather_for_first_n_municipios
from silver_batch import bronze_to_silver_http


def _resolve_partition_date(dt_unix: Any) -> str:
    """
    Resolve a data de partição no formato YYYY-MM-DD.
    """
    if isinstance(dt_unix, (int, float)):
        return datetime.fromtimestamp(dt_unix, tz=timezone.utc).strftime("%Y-%m-%d")
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _build_bronze_rows(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Constrói linhas da camada bronze preservando o payload completo da API
    em formato JSON string para uso posterior na silver.
    """
    rows: List[Dict[str, Any]] = []
    collected_at = datetime.now(timezone.utc).isoformat()

    for item in items:
        weather = item.get("weather", {})
        sys_data = weather.get("sys", {})

        rows.append(
            {
                "municipio_csv": item.get("municipio"),
                "latitude_csv": item.get("latitude"),
                "longitude_csv": item.get("longitude"),
                "api_city_id": weather.get("id"),
                "api_city_name": weather.get("name"),
                "country": sys_data.get("country"),
                "dt_unix": weather.get("dt"),
                "collected_at_utc": collected_at,
                "weather_raw_json": json.dumps(weather, ensure_ascii=False),
            }
        )

    return rows


def _upload_bronze_parquet_to_gcs(
    items: List[Dict[str, Any]],
    bucket_name: str,
    prefix: str = "weather_bronze/",
) -> List[str]:
    """
    Salva os dados da camada bronze em Parquet no Cloud Storage,
    particionando por data (dt=YYYY-MM-DD).

    :param items: Lista de itens retornados pela API.
    :param bucket_name: Nome do bucket do Cloud Storage.
    :param prefix: Prefixo/pasta lógica dentro do bucket.
    :return: Lista de objetos criados no bucket.
    """
    if not prefix.endswith("/"):
        prefix = f"{prefix}/"

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    rows = _build_bronze_rows(items)
    if not rows:
        return []

    partitioned_rows: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        partition_date = _resolve_partition_date(row.get("dt_unix"))
        partitioned_rows.setdefault(partition_date, []).append(row)

    written_objects: List[str] = []
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for partition_date, partition_rows in partitioned_rows.items():
        df = pd.DataFrame(partition_rows)
        object_name = f"{prefix}dt={partition_date}/bronze_{timestamp}.parquet"
        local_tmp_path = f"/tmp/bronze_{partition_date}_{timestamp}.parquet"

        df.to_parquet(local_tmp_path, index=False)
        bucket.blob(object_name).upload_from_filename(
            local_tmp_path,
            content_type="application/octet-stream",
        )
        written_objects.append(object_name)

    return written_objects


def fetch_weather_http(request: Request):
    """
    Cloud Function HTTP que:
    - lê o parâmetro opcional ?limit=N (quantidade de municípios, padrão 10)
    - chama a API de clima para os primeiros N municípios do CSV
    - opcionalmente salva o resultado da bronze em Parquet no Cloud Storage
      (particionado por data) se a variável GCS_BUCKET_NAME estiver configurada.
    """
    try:
        # Lê parâmetro 'limit' da query string (padrão: 10)
        limit_param = request.args.get("limit")
        limit = int(limit_param) if limit_param is not None else 10
        if limit <= 0:
            limit = 10
    except ValueError:
        limit = 10

    try:
        resultados = get_weather_for_first_n_municipios(n=limit)
    except Exception as exc:  # captura erro de rede, etc.
        return (
            jsonify({"error": "failed_to_fetch_weather", "details": str(exc)}),
            500,
        )

    response_payload: Dict[str, Any] = {
        "count": len(resultados),
        "items": resultados,
    }

    # Se o nome do bucket estiver definido, salva automaticamente no GCS
    bucket_name = os.getenv("GCS_BUCKET_NAME")
    written_objects: List[str] = []

    if bucket_name:
        try:
            written_objects = _upload_bronze_parquet_to_gcs(
                items=resultados,
                bucket_name=bucket_name,
                prefix="weather_bronze/",
            )
            response_payload["bronze_objects"] = {
                "bucket": bucket_name,
                "names": written_objects,
            }
        except Exception as exc:
            # Não falha a função só porque o upload quebrou; apenas reporta o erro.
            response_payload["gcs_upload_error"] = str(exc)

    return jsonify(response_payload)


# Aplicação Flask para rodar no Cloud Run
app = Flask(__name__)


@app.route("/", methods=["GET"])
def fetch_weather_route():
    """
    Endpoint raiz para Cloud Run.
    Reutiliza a mesma lógica da função fetch_weather_http.
    """
    return fetch_weather_http(flask_request)


@app.route("/bronze-to-silver", methods=["POST", "GET"])
def bronze_to_silver_route():
    """
    Endpoint para executar conversão de bronze para silver.
    """
    return bronze_to_silver_http(flask_request)

