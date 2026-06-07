from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pandas as pd

from utils.market_warehouse import clickhouse_client


MYSQL_INDEX_TABLE = "strategy_backtest_result_index"
CH_TABLE_PREFIX = "strategy_backtest_"


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def _safe_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


def _ch_type_for_series(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "UInt8"
    if pd.api.types.is_integer_dtype(series):
        return "Int64"
    if pd.api.types.is_float_dtype(series):
        return "Float64"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "DateTime"
    return "String"


def _normalize_frame(df: pd.DataFrame, run_id: str, strategy_version: str, artifact_key: str) -> pd.DataFrame:
    frame = df.copy()
    frame.insert(0, "artifact_key", artifact_key)
    frame.insert(0, "strategy_version", strategy_version)
    frame.insert(0, "run_id", run_id)
    frame["created_at"] = datetime.now()

    for col in frame.columns:
        if pd.api.types.is_object_dtype(frame[col]):
            frame[col] = frame[col].apply(lambda v: None if pd.isna(v) else (v if isinstance(v, (str, int, float, bool, datetime, date)) else _safe_json(v)))
    return frame


def _ensure_index_table() -> None:
    ch = clickhouse_client()
    ch.command(
        f"""
        CREATE TABLE IF NOT EXISTS {MYSQL_INDEX_TABLE} (
          run_id String,
          strategy_version String,
          strategy_name String,
          start_date Date,
          end_date Date,
          status String,
          summary_json String,
          params_json String,
          row_counts_json String,
          storage_path String,
          output_dir String,
          error_message String,
          created_at DateTime DEFAULT now(),
          completed_at Nullable(DateTime),
          updated_at DateTime DEFAULT now()
        ) ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (run_id)
        """
    )


def _ensure_artifact_table(table_name: str, frame: pd.DataFrame) -> None:
    ch = clickhouse_client()
    existing = ch.query(
        "SELECT name, type FROM system.columns WHERE database = currentDatabase() AND table = %(table)s",
        parameters={"table": table_name},
    ).result_rows
    existing_map = {str(name): str(tp) for name, tp in existing}

    if not existing_map:
        columns_sql = []
        forced = {
            "run_id": "String",
            "strategy_version": "String",
            "artifact_key": "String",
            "created_at": "DateTime",
        }
        for col in frame.columns:
            col_type = forced.get(col) or _ch_type_for_series(frame[col])
            columns_sql.append(f"`{col}` {col_type}")
        ch.command(
            f"CREATE TABLE IF NOT EXISTS {table_name} ({', '.join(columns_sql)}) ENGINE = MergeTree ORDER BY (run_id, created_at)"
        )
        return

    for col in frame.columns:
        if col in existing_map:
            continue
        col_type = "String" if col in {"run_id", "strategy_version", "artifact_key"} else _ch_type_for_series(frame[col])
        ch.command(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS `{col}` {col_type}")


def _replace_artifact_frame(table_name: str, frame: pd.DataFrame, run_id: str) -> int:
    ch = clickhouse_client()
    _ensure_artifact_table(table_name, frame)
    ch.command(f"ALTER TABLE {table_name} DELETE WHERE run_id = %(run_id)s", parameters={"run_id": run_id})
    if frame.empty:
        return 0
    ch.insert_df(table_name, frame)
    return int(len(frame))


def publish_backtest_result(
    *,
    run_id: str,
    strategy_version: str,
    strategy_name: str,
    start_date: str,
    end_date: str,
    summary: Mapping[str, Any],
    params: Optional[Mapping[str, Any]] = None,
    frames: Optional[Mapping[str, pd.DataFrame]] = None,
    output_dir: str = "",
    status: str = "completed",
    error_message: str = "",
) -> Dict[str, Any]:
    """Persist backtest index/artifacts to ClickHouse."""
    safe_run_id = str(run_id or "").strip()
    safe_strategy_version = str(strategy_version or "").strip()
    if not safe_run_id:
        raise ValueError("run_id is required")
    if not safe_strategy_version:
        raise ValueError("strategy_version is required")

    row_counts: Dict[str, int] = {}
    frames = frames or {}
    for artifact_key, frame in frames.items():
        if frame is None:
            continue
        normalized = _normalize_frame(
            df=pd.DataFrame(frame),
            run_id=safe_run_id,
            strategy_version=safe_strategy_version,
            artifact_key=str(artifact_key),
        )
        table_name = f"{CH_TABLE_PREFIX}{artifact_key}"
        row_counts[str(artifact_key)] = _replace_artifact_frame(table_name, normalized, safe_run_id)

    _ensure_index_table()
    ch = clickhouse_client()
    ch.command(f"ALTER TABLE {MYSQL_INDEX_TABLE} DELETE WHERE run_id = %(run_id)s", parameters={"run_id": safe_run_id})
    ch.insert(
        MYSQL_INDEX_TABLE,
        [[
            safe_run_id,
            safe_strategy_version,
            str(strategy_name or ""),
            str(start_date),
            str(end_date),
            str(status or "completed"),
            _safe_json(dict(summary or {})),
            _safe_json(dict(params or {})),
            _safe_json(row_counts),
            "clickhouse://warehouse",
            str(output_dir or ""),
            str(error_message or ""),
            datetime.now() if status == "completed" else None,
            datetime.now(),
        ]],
        column_names=[
            "run_id",
            "strategy_version",
            "strategy_name",
            "start_date",
            "end_date",
            "status",
            "summary_json",
            "params_json",
            "row_counts_json",
            "storage_path",
            "output_dir",
            "error_message",
            "completed_at",
            "updated_at",
        ],
    )

    return {
        "run_id": safe_run_id,
        "strategy_version": safe_strategy_version,
        "storage_path": "clickhouse://warehouse",
        "index_table": MYSQL_INDEX_TABLE,
        "row_counts": row_counts,
    }
