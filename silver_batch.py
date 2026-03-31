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


def _resolve_partition_date(row: Dict[str, Any]) -> str:
    """
    Resolve a data de partição a partir de dt_unix (UTC).
    Caso não exista, usa a data atual em UTC.
    """
    dt_unix = row.get("dt_unix")
    if isinstance(dt_unix, (int, float)):
        return datetime.fromtimestamp(dt_unix, tz=timezone.utc).strftime("%Y-%m-%d")
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _processed_blob_name(blob_name: str, bronze_prefix: str, processed_prefix: str) -> str:
    """
    Gera o nome de destino do arquivo processado na bronze.
    """
    relative_name = blob_name.removeprefix(bronze_prefix)
    return f"{processed_prefix}{relative_name}"


def _convert_bronze_json_to_silver_parquet(
    bronze_bucket_name: str,
    silver_bucket_name: str,
    bronze_prefix: str = "weather_batch/",
    silver_prefix: str = "weather_silver/",
    bronze_processed_prefix: str = "weather_batch/processed/",
    max_files: int | None = None,
) -> Dict[str, Any]:
    """
    Lê JSONs da bronze no GCS, converte para Parquet e grava na silver.
    """
    client = storage.Client()
    bronze_bucket = client.bucket(bronze_bucket_name)
    silver_bucket = client.bucket(silver_bucket_name)

    if not bronze_prefix.endswith("/"):
        bronze_prefix = f"{bronze_prefix}/"
    if not silver_prefix.endswith("/"):
        silver_prefix = f"{silver_prefix}/"
    if not bronze_processed_prefix.endswith("/"):
        bronze_processed_prefix = f"{bronze_processed_prefix}/"

    json_blobs = []
    for blob in client.list_blobs(bronze_bucket, prefix=bronze_prefix):
        if blob.name.startswith(bronze_processed_prefix):
            continue
        if not blob.name.endswith(".json"):
            continue
        json_blobs.append(blob)
        if max_files is not None and max_files > 0 and len(json_blobs) >= max_files:
            break

    if not json_blobs:
        return {
            "message": "Nenhum arquivo JSON encontrado na camada bronze.",
            "bronze_bucket": bronze_bucket_name,
            "bronze_prefix": bronze_prefix,
            "silver_bucket": silver_bucket_name,
            "files_processed": 0,
            "rows_written": 0,
        }

    files_processed = 0
    rows_written = 0
    silver_objects: List[str] = []
    processed_blobs: List[str] = []
    skipped_blobs: List[str] = []

    for blob in json_blobs:
        content = blob.download_as_text(encoding="utf-8", timeout=120)
        payload = json.loads(content)
        items = payload.get("items", [])

        if not isinstance(items, list):
            continue

        rows = _build_silver_rows(items)
        if not rows:
            skipped_blobs.append(blob.name)
            continue

        partitioned_rows: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            partition_date = _resolve_partition_date(row)
            partitioned_rows.setdefault(partition_date, []).append(row)

        source_key = blob.name.replace("/", "_").replace(".json", "")
        source_generation = blob.generation or "na"

        for partition_date, partition_rows in partitioned_rows.items():
            df = pd.DataFrame(partition_rows)
            silver_blob_name = (
                f"{silver_prefix}dt={partition_date}/"
                f"{source_key}_{source_generation}.parquet"
            )
            local_tmp_path = f"/tmp/{source_key}_{source_generation}_{partition_date}.parquet"

            silver_blob = silver_bucket.blob(silver_blob_name)
            if not silver_blob.exists(client):
                df.to_parquet(local_tmp_path, index=False)
                silver_blob.upload_from_filename(
                    local_tmp_path,
                    content_type="application/octet-stream",
                )
                rows_written += len(df)

            silver_objects.append(silver_blob_name)

        # Move o arquivo da bronze para "processed/" para evitar reprocessamento.
        destination_name = _processed_blob_name(blob.name, bronze_prefix, bronze_processed_prefix)
        bronze_bucket.copy_blob(blob, bronze_bucket, new_name=destination_name)
        blob.delete()
        processed_blobs.append(destination_name)
        files_processed += 1

    if files_processed == 0:
        return {
            "message": "Arquivos lidos, mas sem itens válidos para conversão.",
            "bronze_bucket": bronze_bucket_name,
            "silver_bucket": silver_bucket_name,
            "files_processed": files_processed,
            "rows_written": 0,
            "skipped_blobs": skipped_blobs,
        }

    return {
        "message": "Conversão bronze -> silver concluída com sucesso.",
        "bronze_bucket": bronze_bucket_name,
        "silver_bucket": silver_bucket_name,
        "silver_prefix": silver_prefix,
        "bronze_processed_prefix": bronze_processed_prefix,
        "files_processed": files_processed,
        "rows_written": rows_written,
        "silver_objects": silver_objects,
        "processed_blobs": processed_blobs,
        "skipped_blobs": skipped_blobs,
    }


def bronze_to_silver_http(request: Request):
    """
    Endpoint HTTP para converter JSON da bronze para Parquet na silver.
    """
    bronze_bucket_name = os.getenv("GCS_BUCKET_NAME")
    silver_bucket_name = os.getenv("SILVER_BUCKET_NAME", "weather-silver-python")
    bronze_prefix = request.args.get("bronze_prefix", "weather_batch/")
    silver_prefix = request.args.get("silver_prefix", "weather_silver/")
    bronze_processed_prefix = request.args.get("bronze_processed_prefix", "weather_batch/processed/")
    max_files_param = request.args.get("max_files")

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
        max_files = int(max_files_param) if max_files_param else 5
        if max_files <= 0:
            max_files = 5
    except ValueError:
        max_files = 5

    try:
        result = _convert_bronze_json_to_silver_parquet(
            bronze_bucket_name=bronze_bucket_name,
            silver_bucket_name=silver_bucket_name,
            bronze_prefix=bronze_prefix,
            silver_prefix=silver_prefix,
            bronze_processed_prefix=bronze_processed_prefix,
            max_files=max_files,
        )
        return jsonify(result)
    except Exception as exc:
        return (
            jsonify({"error": "bronze_to_silver_failed", "details": str(exc)}),
            500,
        )
