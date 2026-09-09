from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_winner_similarity_scheduler_v1 import BASE_VARIANTS, SOURCE_DIR, _load_candidate_pool  # noqa: E402
from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("score120_tdx_vs_sw_diffusion_v1")
FORMAL_SOURCE = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
CURRENT_SCHEDULER_TRADES = report_path(
    "wave_style_model_scheduler_v1",
    "scheduler_focus_240d_score120_aggr25",
    "closed_trades.csv",
)
OLD_RESTORED = report_path(
    "score120_formal_tdx_industry_rebuild_v1",
    "formal_score120_restored_same_candidate_tdx_industry.csv",
)


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return None
    except Exception:
        pass
    return value


def _clean(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _code6(series: pd.Series) -> pd.Series:
    text = series.fillna("").astype(str)
    extracted = text.str.extract(r"(\d{6})", expand=False)
    return extracted.fillna(text.str.strip())


def _date_text(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.strftime("%Y-%m-%d").fillna("")


def _key(df: pd.DataFrame, date_col: str = "entry_date", code_col: str = "code") -> pd.Series:
    return _date_text(df[date_col]) + "|" + _code6(df[code_col])


def _quoted(values: list[str]) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in sorted(set(values)) if str(v).strip())


def _tdx_industry_map(codes: list[str]) -> pd.DataFrame:
    if not codes or not clickhouse_table_exists("source_sector_stocks"):
        return pd.DataFrame(columns=["code", "code6", "tdx_industry_name", "tdx_industry_code"])
    raw = clickhouse_query_df(
        f"""
        SELECT
            canonical_code AS code,
            any(sector_code) AS tdx_industry_code,
            any(sector_name) AS tdx_industry_name
        FROM source_sector_stocks
        WHERE source = 'tdx'
          AND startsWith(sector_code, '881')
          AND canonical_code IN ({_quoted(codes)})
        GROUP BY canonical_code
        """
    )
    if raw.empty:
        return pd.DataFrame(columns=["code", "code6", "tdx_industry_name", "tdx_industry_code"])
    raw["code6"] = _code6(raw["code"])
    return raw[["code", "code6", "tdx_industry_name", "tdx_industry_code"]]


def _build_diffusion(pool: pd.DataFrame, sector_col: str, prefix: str) -> pd.DataFrame:
    work = pool.copy()
    for col in ["rank_key", "wave_style_score", "ret5", "ret20", "amount_rank", "big_up_days20", "limit_up_days20"]:
        work[col] = pd.to_numeric(work.get(col), errors="coerce")
    work["entry_date"] = pd.to_datetime(work["entry_date"], errors="coerce").dt.normalize()
    work[sector_col] = work[sector_col].map(_clean)
    work = work[work[sector_col] != ""].copy()
    day_stock = (
        work.sort_values(["entry_date", "code_raw", "rank_key"], ascending=[True, True, False])
        .drop_duplicates(["entry_date", "code_raw"])
        .copy()
    )
    sector = (
        day_stock.groupby(["entry_date", sector_col], dropna=False)
        .agg(
            candidate_count=("code_raw", "count"),
            avg_score=("wave_style_score", "mean"),
            max_score=("wave_style_score", "max"),
            avg_ret5=("ret5", "mean"),
            avg_ret20=("ret20", "mean"),
            big_up_sum=("big_up_days20", "sum"),
            limit_up_sum=("limit_up_days20", "sum"),
            avg_amount_rank=("amount_rank", "mean"),
        )
        .reset_index()
        .sort_values([sector_col, "entry_date"])
    )
    market = (
        day_stock.groupby("entry_date")
        .agg(market_candidate_count=("code_raw", "count"), market_avg_score=("wave_style_score", "mean"))
        .reset_index()
    )
    sector = sector.merge(market, on="entry_date", how="left")
    sector["share"] = sector["candidate_count"] / sector["market_candidate_count"].replace(0, pd.NA)
    for col in ["candidate_count", "avg_score", "avg_ret5", "avg_ret20", "share"]:
        sector[f"{col}_pct"] = sector[col].rank(pct=True)
    sector[f"{prefix}_diffusion_score"] = (
        sector["candidate_count_pct"] * 30.0
        + sector["avg_score_pct"] * 20.0
        + sector["avg_ret5_pct"] * 15.0
        + sector["avg_ret20_pct"] * 15.0
        + sector["share_pct"] * 20.0
    )
    return sector.rename(
        columns={
            sector_col: f"{prefix}_sector",
            "candidate_count": f"{prefix}_candidate_count",
            "share": f"{prefix}_share",
            "avg_score": f"{prefix}_avg_score",
        }
    )[
        [
            "entry_date",
            f"{prefix}_sector",
            f"{prefix}_candidate_count",
            f"{prefix}_share",
            f"{prefix}_avg_score",
            f"{prefix}_diffusion_score",
        ]
    ]


def _attach_scores(df: pd.DataFrame, sw_diff: pd.DataFrame, tdx_diff: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["entry_date"] = pd.to_datetime(out["entry_date"], errors="coerce").dt.normalize()
    out = out.merge(sw_diff, left_on=["entry_date", "sw_sector"], right_on=["entry_date", "sw_sector"], how="left")
    out = out.merge(tdx_diff, left_on=["entry_date", "tdx_sector"], right_on=["entry_date", "tdx_sector"], how="left")
    out["sw_pass65"] = pd.to_numeric(out.get("sw_diffusion_score"), errors="coerce") >= 65.0
    out["tdx_pass65"] = pd.to_numeric(out.get("tdx_diffusion_score"), errors="coerce") >= 65.0
    out["tdx_rescue"] = out["tdx_pass65"] & ~out["sw_pass65"]
    out["tdx_loss"] = out["sw_pass65"] & ~out["tdx_pass65"]
    return out


def _metrics(df: pd.DataFrame, label: str) -> dict[str, Any]:
    ret = pd.to_numeric(df.get("net_ret", pd.Series(dtype=float)), errors="coerce")
    pnl = pd.to_numeric(df.get("realized_pnl", pd.Series(dtype=float)), errors="coerce")
    return {
        "sample": label,
        "rows": int(len(df)),
        "avg_net_ret": float(ret.mean()) if ret.notna().any() else None,
        "sum_net_ret": float(ret.sum()) if ret.notna().any() else None,
        "win_rate": float((ret > 0).mean()) if ret.notna().any() else None,
        "sum_realized_pnl": float(pnl.sum()) if pnl.notna().any() else None,
        "sw_pass65": int(df.get("sw_pass65", pd.Series(dtype=bool)).sum()),
        "tdx_pass65": int(df.get("tdx_pass65", pd.Series(dtype=bool)).sum()),
        "tdx_rescue": int(df.get("tdx_rescue", pd.Series(dtype=bool)).sum()),
        "tdx_loss": int(df.get("tdx_loss", pd.Series(dtype=bool)).sum()),
    }


def _best_per_key(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "key" not in df.columns:
        return df.copy()
    out = df.copy()
    out["__rank_key"] = pd.to_numeric(out.get("rank_key"), errors="coerce")
    out["__wave_style_score"] = pd.to_numeric(out.get("wave_style_score"), errors="coerce")
    return (
        out.sort_values(["key", "__rank_key", "__wave_style_score"], ascending=[True, False, False])
        .drop_duplicates(["key"], keep="first")
        .drop(columns=["__rank_key", "__wave_style_score"], errors="ignore")
        .reset_index(drop=True)
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pool = _load_candidate_pool(SOURCE_DIR, BASE_VARIANTS)
    pool["code6"] = _code6(pool["code_raw"])
    pool["key"] = _date_text(pool["entry_date"]) + "|" + pool["code6"]
    pool["sw_sector"] = pool["l2_sector_name"].map(_clean)

    codes = sorted(set(pool["code_raw"].fillna("").astype(str)) | set(pool["code6"]))
    tdx_map = _tdx_industry_map(codes)
    pool = pool.merge(tdx_map.add_prefix("tdx_map_"), left_on="code6", right_on="tdx_map_code6", how="left")
    pool["tdx_sector"] = pool.get("tdx_map_tdx_industry_name", pd.Series("", index=pool.index)).map(_clean)

    sw_diff = _build_diffusion(pool, "sw_sector", "sw")
    tdx_diff = _build_diffusion(pool, "tdx_sector", "tdx")
    pool_cmp = _attach_scores(pool, sw_diff, tdx_diff)

    formal = pd.read_csv(FORMAL_SOURCE, encoding="utf-8-sig", low_memory=False)
    formal["key"] = _key(formal, "entry_date", "code")
    formal_keys = set(formal["key"])
    current = pd.read_csv(CURRENT_SCHEDULER_TRADES, encoding="utf-8-sig", low_memory=False)
    current["code6"] = _code6(current["code"])
    current["key"] = _date_text(current["entry_date"]) + "|" + current["code6"]
    current_keys = set(current["key"])

    formal_in_pool = pool_cmp[pool_cmp["key"].isin(formal_keys)].copy()
    current_in_pool = pool_cmp[pool_cmp["key"].isin(current_keys)].copy()
    formal_best = _best_per_key(formal_in_pool)
    current_best = _best_per_key(current_in_pool)
    missing_formal = formal[~formal["key"].isin(set(formal_in_pool.get("key", pd.Series(dtype=str))))].copy()
    old_restored = pd.read_csv(OLD_RESTORED, encoding="utf-8-sig", low_memory=False) if OLD_RESTORED.exists() else pd.DataFrame()

    metrics = pd.DataFrame(
        [
            _metrics(pool_cmp, "all_candidate_pool"),
            _metrics(pool_cmp[pool_cmp["sw_pass65"]], "all_candidate_pool_sw65"),
            _metrics(pool_cmp[pool_cmp["tdx_pass65"]], "all_candidate_pool_tdx65"),
            _metrics(pool_cmp[pool_cmp["tdx_rescue"]], "all_candidate_pool_tdx_rescue"),
            _metrics(pool_cmp[pool_cmp["tdx_loss"]], "all_candidate_pool_tdx_loss"),
            _metrics(formal_in_pool, "formal_26_found_in_current_pool"),
            _metrics(formal_best, "formal_26_found_in_current_pool_best_per_key"),
            _metrics(current_in_pool, "current_scheduler_trades_in_pool"),
            _metrics(current_best, "current_scheduler_trades_in_pool_best_per_key"),
        ]
    )

    formal_view_cols = [
        "key",
        "code_raw",
        "stock_name",
        "entry_date",
        "net_ret",
        "wave_style_score",
        "sw_sector",
        "tdx_sector",
        "sw_diffusion_score",
        "tdx_diffusion_score",
        "sw_pass65",
        "tdx_pass65",
        "tdx_rescue",
        "tdx_loss",
    ]
    formal_view = formal_in_pool[[c for c in formal_view_cols if c in formal_in_pool.columns]].copy()
    formal_best_view = formal_best[[c for c in formal_view_cols if c in formal_best.columns]].copy()
    current_view = current_in_pool[[c for c in formal_view_cols if c in current_in_pool.columns]].copy()
    current_best_view = current_best[[c for c in formal_view_cols if c in current_best.columns]].copy()
    missing_cols = [
        "key",
        "code",
        "name",
        "entry_date",
        "net_ret",
        "realized_pnl",
        "wave_style_score",
        "sector_diffusion_score",
        "sector_for_distinct",
    ]
    missing_view = missing_formal[[c for c in missing_cols if c in missing_formal.columns]].copy()

    summary = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_dir": str(SOURCE_DIR),
        "pool_rows": int(len(pool_cmp)),
        "pool_unique_days": int(pool_cmp["entry_date"].nunique()),
        "pool_codes": int(pool_cmp["code6"].nunique()),
        "tdx_mapped_rows": int((pool_cmp["tdx_sector"] != "").sum()),
        "formal_26_found_rows_in_current_pool": int(len(formal_in_pool)),
        "formal_26_found_unique_keys_in_current_pool": int(formal_in_pool["key"].nunique()) if not formal_in_pool.empty else 0,
        "formal_26_sw65_rows_in_current_pool": int(formal_in_pool["sw_pass65"].sum()) if not formal_in_pool.empty else 0,
        "formal_26_tdx65_rows_in_current_pool": int(formal_in_pool["tdx_pass65"].sum()) if not formal_in_pool.empty else 0,
        "formal_26_sw65_best_per_key": int(formal_best["sw_pass65"].sum()) if not formal_best.empty else 0,
        "formal_26_tdx65_best_per_key": int(formal_best["tdx_pass65"].sum()) if not formal_best.empty else 0,
        "formal_26_missing_unique_keys_from_current_pool": int(len(missing_formal)),
        "formal_26_missing_sum_realized_pnl": float(pd.to_numeric(missing_formal.get("realized_pnl"), errors="coerce").sum())
        if not missing_formal.empty
        else 0.0,
        "current_scheduler_trades_found_rows_in_pool": int(len(current_in_pool)),
        "current_scheduler_trades_found_unique_keys_in_pool": int(current_in_pool["key"].nunique()) if not current_in_pool.empty else 0,
        "all_pool_sw65": int(pool_cmp["sw_pass65"].sum()),
        "all_pool_tdx65": int(pool_cmp["tdx_pass65"].sum()),
        "all_pool_tdx_rescue": int(pool_cmp["tdx_rescue"].sum()),
        "all_pool_tdx_loss": int(pool_cmp["tdx_loss"].sum()),
        "old_restored_same_gate_all_pass": int(old_restored.get("same_gate_all_pass", pd.Series(dtype=bool)).sum()) if not old_restored.empty else None,
        "scope_note": "This is a same-current-candidate-pool A/B between current SW l2 sector and current TDX 881 industry. It does not recover the missing old full daily candidate cross-section.",
    }

    sw_diff.to_csv(OUT_DIR / "sw_sector_diffusion.csv", index=False, encoding="utf-8-sig")
    tdx_diff.to_csv(OUT_DIR / "tdx_sector_diffusion.csv", index=False, encoding="utf-8-sig")
    pool_cmp.to_csv(OUT_DIR / "candidate_pool_with_sw_tdx_diffusion.csv", index=False, encoding="utf-8-sig")
    formal_view.to_csv(OUT_DIR / "formal_26_in_current_pool_sw_tdx_diffusion.csv", index=False, encoding="utf-8-sig")
    formal_best_view.to_csv(OUT_DIR / "formal_26_best_per_key_sw_tdx_diffusion.csv", index=False, encoding="utf-8-sig")
    missing_view.to_csv(OUT_DIR / "formal_26_missing_from_current_pool.csv", index=False, encoding="utf-8-sig")
    current_view.to_csv(OUT_DIR / "current_scheduler_trades_sw_tdx_diffusion.csv", index=False, encoding="utf-8-sig")
    current_best_view.to_csv(OUT_DIR / "current_scheduler_trades_best_per_key_sw_tdx_diffusion.csv", index=False, encoding="utf-8-sig")
    metrics.to_csv(OUT_DIR / "sample_metrics.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")

    lines = [
        "# Score120 TDX vs 申万扩散分 A/B v1",
        "",
        "## 结论",
        "",
        f"- 当前可加载全量候选池：{summary['pool_rows']} 行，{summary['pool_unique_days']} 个交易日，{summary['pool_codes']} 只股票。",
        f"- TDX 881 行业映射覆盖：{summary['tdx_mapped_rows']} / {summary['pool_rows']}。",
        f"- 全池申万 diff>=65：{summary['all_pool_sw65']}；TDX diff>=65：{summary['all_pool_tdx65']}。",
        f"- TDX 相对申万新增通过：{summary['all_pool_tdx_rescue']}；TDX 相对申万丢失：{summary['all_pool_tdx_loss']}。",
        f"- 正式 26 笔在当前全量候选池中可找到唯一 key：{summary['formal_26_found_unique_keys_in_current_pool']} / 26；展开为 variant 行：{summary['formal_26_found_rows_in_current_pool']}。",
        f"- 当前全量候选池缺失正式 key：{summary['formal_26_missing_unique_keys_from_current_pool']} / 26；这些缺失交易合计 PnL：{summary['formal_26_missing_sum_realized_pnl']:,.2f}。",
        f"- 正式 26 笔按每个 key 最优行去重后，申万通过：{summary['formal_26_sw65_best_per_key']}；TDX 通过：{summary['formal_26_tdx65_best_per_key']}。",
        "",
        "## 边界",
        "",
        f"- {summary['scope_note']}",
        "- 旧正式 26 笔已在 `score120_formal_tdx_industry_rebuild_v1` 中恢复为同候选、同分数、同 gate 全通过；本报告用于补充当前可加载全池的行业体系 A/B。",
        "",
        "## 样本指标",
        "",
        metrics.to_markdown(index=False),
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
