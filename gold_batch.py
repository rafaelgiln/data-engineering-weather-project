from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd
from google.cloud import storage
from flask import Request, jsonify


GOLD_COLUMNS = [
    "dt_ref",
    "municipio_csv",
    "api_city_id",
    "country",
    "temp_media",
    "temp_min_dia",
    "temp_max_dia",
    "umidade_media",
    "vento_medio",
    "qtd_registros",
    "atualizado_em_utc",
]


def _processed_blob_name(blob_name: str, source_prefix: str, processed_prefix: str) -> str:
    relative_name = blob_name.removeprefix(source_prefix)
    return f"{processed_prefix}{relative_name}"


def _build_gold_from_silver_df(df_silver: pd.DataFrame) -> pd.DataFrame:
    """
    Constrói agregações diárias (Gold) a partir do Silver.
    """
    if df_silver.empty:
        return pd.DataFrame(columns=GOLD_COLUMNS)

    if "dt_unix" not in df_silver.columns:
        return pd.DataFrame(columns=GOLD_COLUMNS)

    df = df_silver.copy()
    df["dt_ref"] = pd.to_datetime(df["dt_unix"], unit="s", utc=True, errors="coerce").dt.date
    df = df.dropna(subset=["dt_ref"])

    numeric_cols = ["temp", "humidity", "wind_speed"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = pd.NA

    group_cols = ["dt_ref", "municipio_csv", "api_city_id", "country"]
    for col in group_cols:
        if col not in df.columns:
            df[col] = pd.NA

    gold = (
        df.groupby(group_cols, dropna=False)
        .agg(
            temp_media=("temp", "mean"),
            temp_min_dia=("temp", "min"),
            temp_max_dia=("temp", "max"),
            umidade_media=("humidity", "mean"),
            vento_medio=("wind_speed", "mean"),
            qtd_registros=("dt_unix", "count"),
        )
        .reset_index()
    )

    gold["atualizado_em_utc"] = datetime.now(timezone.utc).isoformat()
    gold["dt_ref"] = gold["dt_ref"].astype(str)
    gold = gold.reindex(columns=GOLD_COLUMNS)
    return gold


def _convert_silver_to_gold_parquet(
    silver_bucket_name: str,
    gold_bucket_name: str,
    silver_prefix: str = "weather_silver/",
    gold_prefix: str = "weather_gold/",
    silver_processed_prefix: str = "weather_silver/processed/",
    max_files: int | None = None,
) -> Dict[str, Any]:
    """
    Lê Parquets da Silver no GCS, agrega para Gold e grava Parquet
    particionado por data. Move arquivos processados para evitar reprocessamento.
    """
    client = storage.Client()
    silver_bucket = client.bucket(silver_bucket_name)
    gold_bucket = client.bucket(gold_bucket_name)

    if not silver_prefix.endswith("/"):
        silver_prefix = f"{silver_prefix}/"
    if not gold_prefix.endswith("/"):
        gold_prefix = f"{gold_prefix}/"
    if not silver_processed_prefix.endswith("/"):
        silver_processed_prefix = f"{silver_processed_prefix}/"

    silver_blobs: List[storage.Blob] = []
    for blob in client.list_blobs(silver_bucket, prefix=silver_prefix):
        if blob.name.startswith(silver_processed_prefix):
            continue
        if not blob.name.endswith(".parquet"):
            continue
        silver_blobs.append(blob)
        if max_files is not None and max_files > 0 and len(silver_blobs) >= max_files:
            break

    if not silver_blobs:
        return {
            "message": "Nenhum arquivo Parquet encontrado na camada silver.",
            "silver_bucket": silver_bucket_name,
            "gold_bucket": gold_bucket_name,
            "files_processed": 0,
            "rows_written": 0,
        }

    files_processed = 0
    rows_written = 0
    gold_objects: List[str] = []
    processed_blobs: List[str] = []
    skipped_blobs: List[str] = []

    for blob in silver_blobs:
        local_silver_path = f"/tmp/{blob.name.replace('/', '_')}"
        blob.download_to_filename(local_silver_path, timeout=120)
        df_silver = pd.read_parquet(local_silver_path)

        df_gold = _build_gold_from_silver_df(df_silver)
        if df_gold.empty:
            skipped_blobs.append(blob.name)
            continue

        source_key = blob.name.replace("/", "_").replace(".parquet", "")
        source_generation = blob.generation or "na"

        for dt_ref, part_df in df_gold.groupby("dt_ref"):
            gold_blob_name = f"{gold_prefix}dt={dt_ref}/{source_key}_{source_generation}.parquet"
            local_gold_path = f"/tmp/{source_key}_{source_generation}_{dt_ref}.parquet"

            gold_blob = gold_bucket.blob(gold_blob_name)
            if not gold_blob.exists(client):
                part_df.to_parquet(local_gold_path, index=False)
                gold_blob.upload_from_filename(
                    local_gold_path,
                    content_type="application/octet-stream",
                )
                rows_written += len(part_df)

            gold_objects.append(gold_blob_name)

        destination_name = _processed_blob_name(blob.name, silver_prefix, silver_processed_prefix)
        silver_bucket.copy_blob(blob, silver_bucket, new_name=destination_name)
        blob.delete()
        processed_blobs.append(destination_name)
        files_processed += 1

    if files_processed == 0:
        return {
            "message": "Arquivos lidos, mas sem dados válidos para gerar gold.",
            "silver_bucket": silver_bucket_name,
            "gold_bucket": gold_bucket_name,
            "files_processed": files_processed,
            "rows_written": rows_written,
            "skipped_blobs": skipped_blobs,
        }

    return {
        "message": "Conversão silver -> gold concluída com sucesso.",
        "silver_bucket": silver_bucket_name,
        "gold_bucket": gold_bucket_name,
        "gold_prefix": gold_prefix,
        "silver_processed_prefix": silver_processed_prefix,
        "files_processed": files_processed,
        "rows_written": rows_written,
        "gold_objects": gold_objects,
        "processed_blobs": processed_blobs,
        "skipped_blobs": skipped_blobs,
    }


def silver_to_gold_http(request: Request):
    """
    Endpoint HTTP para converter Parquet da silver em Parquet agregado da gold.
    """
    silver_bucket_name = os.getenv("SILVER_BUCKET_NAME", "my-weather-bucket-silver")
    gold_bucket_name = os.getenv("GOLD_BUCKET_NAME", "my-weather-bucket-gold")
    silver_prefix = request.args.get("silver_prefix", "weather_silver/")
    gold_prefix = request.args.get("gold_prefix", "weather_gold/")
    silver_processed_prefix = request.args.get("silver_processed_prefix", "weather_silver/processed/")
    max_files_param = request.args.get("max_files")

    try:
        max_files = int(max_files_param) if max_files_param else 10
        if max_files <= 0:
            max_files = 10
    except ValueError:
        max_files = 10

    try:
        result = _convert_silver_to_gold_parquet(
            silver_bucket_name=silver_bucket_name,
            gold_bucket_name=gold_bucket_name,
            silver_prefix=silver_prefix,
            gold_prefix=gold_prefix,
            silver_processed_prefix=silver_processed_prefix,
            max_files=max_files,
        )
        return jsonify(result)
    except Exception as exc:
        return (
            jsonify({"error": "silver_to_gold_failed", "details": str(exc)}),
            500,
        )

