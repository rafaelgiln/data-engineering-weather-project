from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

from google.cloud import storage
from flask import Flask, Request, jsonify, request as flask_request

from batch_weather import get_weather_for_first_n_municipios


def _upload_to_gcs(
    data: Dict[str, Any],
    bucket_name: str,
    prefix: str = "weather",
) -> str:
    """
    Salva o dicionário `data` como JSON em um objeto no Cloud Storage.

    :param data: Dados a serem salvos.
    :param bucket_name: Nome do bucket do Cloud Storage.
    :param prefix: Prefixo/pasta lógica dentro do bucket.
    :return: Nome completo do objeto criado.
    """
    client = storage.Client()
    bucket = client.bucket(bucket_name)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    blob_name = f"{prefix}/weather_{timestamp}.json"
    blob = bucket.blob(blob_name)

    blob.upload_from_string(
        data=json.dumps(data, ensure_ascii=False, indent=2),
        content_type="application/json",
    )

    return blob_name


def fetch_weather_http(request: Request):
    """
    Cloud Function HTTP que:
    - lê o parâmetro opcional ?limit=N (quantidade de municípios, padrão 10)
    - chama a API de clima para os primeiros N municípios do CSV
    - opcionalmente salva o resultado em um bucket do Cloud Storage
      se a variável de ambiente GCS_BUCKET_NAME estiver configurada.
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
    blob_name = None

    if bucket_name:
        try:
            blob_name = _upload_to_gcs(
                data=response_payload,
                bucket_name=bucket_name,
                prefix="weather_batch",
            )
            response_payload["gcs_object"] = {
                "bucket": bucket_name,
                "name": blob_name,
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

