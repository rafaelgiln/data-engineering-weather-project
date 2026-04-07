from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
from google.cloud import storage
from flask import Request, jsonify


SILVER_COLUMNS = [
    "municipio_csv",
    "latitude_csv",
    "longitude_csv",
    "api_city_id",
    "api_city_name",
    "country",
    "timezone_offset_seconds",
    "lat_api",
    "lon_api",
    "weather_id",
    "weather_main",
    "weather_description",
    "weather_icon",
    "base",
    "temp",
    "feels_like",
    "temp_min",
    "temp_max",
    "pressure",
    "humidity",
    "sea_level",
    "grnd_level",
    "visibility",
    "wind_speed",
    "wind_deg",
    "wind_gust",
    "clouds_all",
    "dt_unix",
    "sunrise_unix",
    "sunset_unix",
    "cod",
    "collected_at_utc",
    "silver_processed_at_utc",
]


def _resolve_partition_date(dt_unix: Any) -> str:
    """
    Resolve a data de partição a partir de dt_unix (UTC).
    Caso não exista, usa a data atual em UTC.
    """
    if isinstance(dt_unix, (int, float)):
        return datetime.fromtimestamp(dt_unix, tz=timezone.utc).strftime("%Y-%m-%d")
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _processed_blob_name(blob_name: str, bronze_prefix: str, processed_prefix: str) -> str:
    """
    Gera o nome de destino do arquivo processado na bronze.
    """
    relative_name = blob_name.removeprefix(bronze_prefix)
    return f"{processed_prefix}{relative_name}"


def _build_silver_rows_from_bronze_df(df_bronze: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    Constrói linhas da silver a partir do parquet da bronze.
    """
    rows: List[Dict[str, Any]] = []
    processed_at = datetime.now(timezone.utc).isoformat()

    for row in df_bronze.to_dict(orient="records"):
        weather_raw = row.get("weather_raw_json")
        if not isinstance(weather_raw, str):
            continue

        try:
            weather = json.loads(weather_raw)
        except json.JSONDecodeError:
            continue

        main = weather.get("main", {})
        wind = weather.get("wind", {})
        clouds = weather.get("clouds", {})
        sys_data = weather.get("sys", {})
        coord = weather.get("coord", {})
        weather_list = weather.get("weather", [])
        weather_obj = weather_list[0] if weather_list else {}

        silver_row = {
            "municipio_csv": row.get("municipio_csv"),
            "latitude_csv": _safe_float(row.get("latitude_csv")),
            "longitude_csv": _safe_float(row.get("longitude_csv")),
            "api_city_id": _safe_int(weather.get("id")),
            "api_city_name": weather.get("name"),
            "country": sys_data.get("country"),
            "timezone_offset_seconds": _safe_int(weather.get("timezone")),
            "lat_api": _safe_float(coord.get("lat")),
            "lon_api": _safe_float(coord.get("lon")),
            "weather_id": _safe_int(weather_obj.get("id")),
            "weather_main": weather_obj.get("main"),
            "weather_description": weather_obj.get("description"),
            "weather_icon": weather_obj.get("icon"),
            "base": weather.get("base"),
            "temp": _safe_float(main.get("temp")),
            "feels_like": _safe_float(main.get("feels_like")),
            "temp_min": _safe_float(main.get("temp_min")),
            "temp_max": _safe_float(main.get("temp_max")),
            "pressure": _safe_int(main.get("pressure")),
            "humidity": _safe_int(main.get("humidity")),
            "sea_level": _safe_int(main.get("sea_level")),
            "grnd_level": _safe_int(main.get("grnd_level")),
            "visibility": _safe_int(weather.get("visibility")),
            "wind_speed": _safe_float(wind.get("speed")),
            "wind_deg": _safe_int(wind.get("deg")),
            "wind_gust": _safe_float(wind.get("gust")),
            "clouds_all": _safe_int(clouds.get("all")),
            "dt_unix": _safe_int(weather.get("dt")),
            "sunrise_unix": _safe_int(sys_data.get("sunrise")),
            "sunset_unix": _safe_int(sys_data.get("sunset")),
            "cod": _safe_int(weather.get("cod")),
            "collected_at_utc": row.get("collected_at_utc"),
            "silver_processed_at_utc": processed_at,
        }
        rows.append(silver_row)

    return rows


def _convert_bronze_json_to_silver_parquet(
    bronze_bucket_name: str,
    silver_bucket_name: str,
    bronze_prefix: str = "weather_bronze/",
    silver_prefix: str = "weather_silver/",
    bronze_processed_prefix: str = "weather_bronze/processed/",
    max_files: int | None = None,
) -> Dict[str, Any]:
    """
    Lê Parquets da bronze no GCS, expande para schema colunar e grava
    na silver em Parquet particionado por data.
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

    bronze_blobs = []
    for blob in client.list_blobs(bronze_bucket, prefix=bronze_prefix):
        if blob.name.startswith(bronze_processed_prefix):
            continue
        if not blob.name.endswith(".parquet"):
            continue
        bronze_blobs.append(blob)
        if max_files is not None and max_files > 0 and len(bronze_blobs) >= max_files:
            break

    if not bronze_blobs:
        return {
            "message": "Nenhum arquivo Parquet encontrado na camada bronze.",
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

    for blob in bronze_blobs:
        local_bronze_path = f"/tmp/{blob.name.replace('/', '_')}"
        blob.download_to_filename(local_bronze_path, timeout=120)
        df_bronze = pd.read_parquet(local_bronze_path)

        silver_rows = _build_silver_rows_from_bronze_df(df_bronze)
        if not silver_rows:
            skipped_blobs.append(blob.name)
            continue

        partitioned_rows: Dict[str, List[Dict[str, Any]]] = {}
        for row in silver_rows:
            partition_date = _resolve_partition_date(row.get("dt_unix"))
            partitioned_rows.setdefault(partition_date, []).append(row)

        source_key = blob.name.replace("/", "_").replace(".parquet", "")
        source_generation = blob.generation or "na"

        for partition_date, partition_rows in partitioned_rows.items():
            df = pd.DataFrame(partition_rows)
            df = df.reindex(columns=SILVER_COLUMNS)
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
    silver_bucket_name = os.getenv("SILVER_BUCKET_NAME", "my-weather-bucket-silver")
    bronze_prefix = request.args.get("bronze_prefix", "weather_bronze/")
    silver_prefix = request.args.get("silver_prefix", "weather_silver/")
    bronze_processed_prefix = request.args.get("bronze_processed_prefix", "weather_bronze/processed/")
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
