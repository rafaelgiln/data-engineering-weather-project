from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd
from google.cloud import storage
from flask import Request, jsonify


def _build_silver_rows(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Converte os itens da camada bronze em linhas tabulares para a silver.
    """
    rows: List[Dict[str, Any]] = []

    for item in items:
        weather = item.get("weather", {})
        main = weather.get("main", {})
        wind = weather.get("wind", {})
        clouds = weather.get("clouds", {})
        sys_data = weather.get("sys", {})
        coord = weather.get("coord", {})
        weather_list = weather.get("weather", [])
        weather_obj = weather_list[0] if weather_list else {}

        rows.append(
            {
                "municipio_csv": item.get("municipio"),
                "latitude_csv": item.get("latitude"),
                "longitude_csv": item.get("longitude"),
                "api_city_id": weather.get("id"),
                "api_city_name": weather.get("name"),
                "country": sys_data.get("country"),
                "timezone_offset_seconds": weather.get("timezone"),
                "lat_api": coord.get("lat"),
                "lon_api": coord.get("lon"),
                "weather_id": weather_obj.get("id"),
                "weather_main": weather_obj.get("main"),
                "weather_description": weather_obj.get("description"),
                "weather_icon": weather_obj.get("icon"),
                "base": weather.get("base"),
                "temp": main.get("temp"),
                "feels_like": main.get("feels_like"),
                "temp_min": main.get("temp_min"),
                "temp_max": main.get("temp_max"),
                "pressure": main.get("pressure"),
                "humidity": main.get("humidity"),
                "sea_level": main.get("sea_level"),
                "grnd_level": main.get("grnd_level"),
                "visibility": weather.get("visibility"),
                "wind_speed": wind.get("speed"),
                "wind_deg": wind.get("deg"),
                "wind_gust": wind.get("gust"),
                "clouds_all": clouds.get("all"),
                "dt_unix": weather.get("dt"),
                "sunrise_unix": sys_data.get("sunrise"),
                "sunset_unix": sys_data.get("sunset"),
                "cod": weather.get("cod"),
                "processed_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        )

    return rows


def _convert_bronze_json_to_silver_parquet(
    bronze_bucket_name: str,
    silver_bucket_name: str,
    bronze_prefix: str = "weather_batch/",
    silver_prefix: str = "weather_silver/",
) -> Dict[str, Any]:
    """
    Lê JSONs da bronze no GCS, converte para Parquet e grava na silver.
    """
    client = storage.Client()
    bronze_bucket = client.bucket(bronze_bucket_name)
    silver_bucket = client.bucket(silver_bucket_name)

    blobs = list(client.list_blobs(bronze_bucket, prefix=bronze_prefix))
    json_blobs = [blob for blob in blobs if blob.name.endswith(".json")]

    if not json_blobs:
        return {
            "message": "Nenhum arquivo JSON encontrado na camada bronze.",
            "bronze_bucket": bronze_bucket_name,
            "bronze_prefix": bronze_prefix,
            "silver_bucket": silver_bucket_name,
            "files_processed": 0,
            "rows_written": 0,
        }

    all_rows: List[Dict[str, Any]] = []
    files_processed = 0

    for blob in json_blobs:
        content = blob.download_as_text(encoding="utf-8")
        payload = json.loads(content)
        items = payload.get("items", [])

        if not isinstance(items, list):
            continue

        all_rows.extend(_build_silver_rows(items))
        files_processed += 1

    if not all_rows:
        return {
            "message": "Arquivos lidos, mas sem itens válidos para conversão.",
            "bronze_bucket": bronze_bucket_name,
            "silver_bucket": silver_bucket_name,
            "files_processed": files_processed,
            "rows_written": 0,
        }

    df = pd.DataFrame(all_rows)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parquet_filename = f"silver_weather_{timestamp}.parquet"
    local_tmp_path = f"/tmp/{parquet_filename}"
    silver_blob_name = f"{silver_prefix}{parquet_filename}"

    df.to_parquet(local_tmp_path, index=False)
    silver_bucket.blob(silver_blob_name).upload_from_filename(
        local_tmp_path,
        content_type="application/octet-stream",
    )

    return {
        "message": "Conversão bronze -> silver concluída com sucesso.",
        "bronze_bucket": bronze_bucket_name,
        "silver_bucket": silver_bucket_name,
        "files_processed": files_processed,
        "rows_written": len(df),
        "silver_object": silver_blob_name,
    }


def bronze_to_silver_http(request: Request):
    """
    Endpoint HTTP para converter JSON da bronze para Parquet na silver.
    """
    bronze_bucket_name = os.getenv("GCS_BUCKET_NAME")
    silver_bucket_name = os.getenv("SILVER_BUCKET_NAME", "weather-silver-python")
    bronze_prefix = request.args.get("bronze_prefix", "weather_batch/")
    silver_prefix = request.args.get("silver_prefix", "weather_silver/")

    if not bronze_bucket_name:
        return (
            jsonify(
                {
                    "error": "missing_env_var",
                    "details": "Defina a variável de ambiente GCS_BUCKET_NAME.",
                }
            ),
            400,
        )

    try:
        result = _convert_bronze_json_to_silver_parquet(
            bronze_bucket_name=bronze_bucket_name,
            silver_bucket_name=silver_bucket_name,
            bronze_prefix=bronze_prefix,
            silver_prefix=silver_prefix,
        )
        return jsonify(result)
    except Exception as exc:
        return (
            jsonify({"error": "bronze_to_silver_failed", "details": str(exc)}),
            500,
        )
