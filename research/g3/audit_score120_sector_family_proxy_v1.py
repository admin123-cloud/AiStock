from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_winner_similarity_scheduler_v1 import BASE_VARIANTS, SOURCE_DIR, _load_candidate_pool  # noqa: E402
from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("score120_sector_family_proxy_v1")
CURRENT_BASE_TRADES = report_path("wave_style_model_scheduler_v1", "scheduler_focus_240d_score120_aggr25", "closed_trades.csv")
CURRENT_OVERLAY_TRADES = report_path("score120_sector_diffusion_30m_overlay_v1", "base_trades_with_sector_diffusion_30m.csv")
OLD_SELECTED = Path(
    r"F:\Stock\AiStockResearchArchive\backups\g3_reset_20260620_003808\reports"
    r"\gen3_pre_2024_10_state_alpha_closure_v5\institutional_mainwave_candidates.csv"
)
OLD_FORMAL = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")


def _code6(series: pd.Series) -> pd.Series:
    text = series.fillna("").astype(str)
    extracted = text.str.extract(r"(\d{6})", expand=False)
    return extracted.fillna(text.str.strip())


def _date_text(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.strftime("%Y-%m-%d").fillna("")


def _clean(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _sector_family(name: Any) -> str:
    sector = _clean(name)
    if sector in {"软件开发", "IT服务", "软件服务"}:
        return "软件服务族"
    if sector in {"半导体", "电子化学品"}:
        return "半导体链"
    if sector in {"元件", "元器件"}:
        return "元器件族"
    if sector in {"消费电子"}:
        return "消费电子"
    return sector or "未分类"


def _current_sw_map(codes: list[str]) -> pd.DataFrame:
    if not codes or not clickhouse_table_exists("sector_stocks") or not clickhouse_table_exists("sectors"):
        return pd.DataFrame(columns=["code", "sw_l2_code", "sw_l2_name", "code6"])
    quoted = ",".join("'" + c.replace("'", "''") + "'" for c in sorted(set(codes)) if c)
    raw = clickhouse_query_df(
        f"""
        SELECT ss.stock_code AS code, any(s.code) AS sw_l2_code, any(s.name) AS sw_l2_name
        FROM sector_stocks ss
        INNER JOIN sectors s ON ss.sector_code = s.code
        WHERE ss.stock_code IN ({quoted})
          AND s.type = 'industry'
          AND s.level = 2
        GROUP BY ss.stock_code
        """
    )
    if raw.empty:
        return pd.DataFrame(columns=["code", "sw_l2_code", "sw_l2_name", "code6"])
    raw["code6"] = _code6(raw["code"])
    return raw


def _build_diffusion(pool: pd.DataFrame, sector_col: str, prefix: str) -> pd.DataFrame:
    work = pool.copy()
    for col in ["rank_key", "wave_style_score", "ret5", "ret20", "amount_rank", "big_up_days20", "limit_up_days20"]:
        work[col] = pd.to_numeric(work.get(col), errors="coerce")
    work["entry_date"] = pd.to_datetime(work["entry_date"], errors="coerce").dt.normalize()
    day_stock = (
        work.sort_values(["entry_date", "code_raw", "rank_key"], ascending=[True, True, False])
        .drop_duplicates(["entry_date", "code_raw"])
        .copy()
    )
    sector = (
        day_stock.groupby(["entry_date", sector_col], dropna=False)
        .agg(
            sector_candidate_count=("code_raw", "count"),
            sector_avg_score=("wave_style_score", "mean"),
            sector_max_score=("wave_style_score", "max"),
            sector_avg_ret5=("ret5", "mean"),
            sector_avg_ret20=("ret20", "mean"),
            sector_big_up_sum=("big_up_days20", "sum"),
            sector_limit_up_sum=("limit_up_days20", "sum"),
            sector_avg_amount_rank=("amount_rank", "mean"),
        )
        .reset_index()
        .sort_values([sector_col, "entry_date"])
    )
    for col in ["sector_candidate_count", "sector_avg_score", "sector_avg_ret5", "sector_avg_ret20", "sector_big_up_sum"]:
        sector[f"{col}_ma5"] = sector.groupby(sector_col)[col].transform(lambda x: x.rolling(5, min_periods=1).mean())
        sector[f"{col}_chg5"] = sector.groupby(sector_col)[col].transform(lambda x: x / x.shift(5).replace(0, pd.NA) - 1.0)
    market = day_stock.groupby("entry_date").agg(market_candidate_count=("code_raw", "count"), market_avg_score=("wave_style_score", "mean")).reset_index()
    sector = sector.merge(market, on="entry_date", how="left")
    sector["sector_share"] = sector["sector_candidate_count"] / sector["market_candidate_count"].replace(0, pd.NA)
    for col in ["sector_candidate_count", "sector_avg_score", "sector_avg_ret5", "sector_avg_ret20", "sector_share"]:
        sector[f"{col}_pct"] = sector[col].rank(pct=True)
    sector[f"{prefix}_diffusion_score"] = (
        sector["sector_candidate_count_pct"] * 30.0
        + sector["sector_avg_score_pct"] * 20.0
        + sector["sector_avg_ret5_pct"] * 15.0
        + sector["sector_avg_ret20_pct"] * 15.0
        + sector["sector_share_pct"] * 20.0
    )
    rename = {
        sector_col: f"{prefix}_sector",
        "sector_candidate_count": f"{prefix}_candidate_count",
        "sector_share": f"{prefix}_share",
        "sector_avg_score": f"{prefix}_avg_score",
    }
    keep = ["entry_date", sector_col, "sector_candidate_count", "sector_share", "sector_avg_score", f"{prefix}_diffusion_score"]
    return sector[keep].rename(columns=rename)


def _metrics(df: pd.DataFrame, label: str) -> dict[str, Any]:
    ret = pd.to_numeric(df.get("net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "sample": label,
        "rows": int(len(df)),
        "avg_net_ret": float(ret.mean()) if ret.notna().any() else None,
        "sum_net_ret": float(ret.sum()) if ret.notna().any() else None,
        "win_rate": float((ret > 0).mean()) if ret.notna().any() else None,
    }


def _prepare_candidate_frame(path: Path, *, name_col: str = "stock_name") -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    code_col = "code_raw" if "code_raw" in df.columns else "code"
    df["code6"] = _code6(df[code_col])
    if "name" not in df.columns and name_col in df.columns:
        df["name"] = df[name_col]
    return df


def _attach_diffusion(df: pd.DataFrame, sw: pd.DataFrame, fam: pd.DataFrame, sw_col: str, family_col: str) -> pd.DataFrame:
    out = df.copy()
    out = out.merge(
        sw,
        left_on=["entry_date", sw_col],
        right_on=["entry_date", "sw_sector"],
        how="left",
    )
    out = out.merge(
        fam,
        left_on=["entry_date", family_col],
        right_on=["entry_date", "family_sector"],
        how="left",
    )
    out["sw_pass65"] = pd.to_numeric(out.get("sw_diffusion_score"), errors="coerce") >= 65.0
    out["family_pass65"] = pd.to_numeric(out.get("family_diffusion_score"), errors="coerce") >= 65.0
    out["family_rescue"] = out["family_pass65"] & ~out["sw_pass65"]
    out["family_loss"] = out["sw_pass65"] & ~out["family_pass65"]
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pool = _load_candidate_pool(SOURCE_DIR, BASE_VARIANTS)
    pool["entry_date"] = pd.to_datetime(pool["entry_date"], errors="coerce").dt.normalize()
    pool["sw_sector"] = pool["l2_sector_name"].map(_clean)
    pool["family_sector"] = pool["sw_sector"].map(_sector_family)
    sw_diff = _build_diffusion(pool, "sw_sector", "sw")
    family_diff = _build_diffusion(pool, "family_sector", "family")

    base = _prepare_candidate_frame(CURRENT_BASE_TRADES)
    base["sw_sector"] = base["l2_sector_name"].map(_clean)
    base["family_sector"] = base["sw_sector"].map(_sector_family)
    base_cmp = _attach_diffusion(base, sw_diff, family_diff, "sw_sector", "family_sector")

    overlay = _prepare_candidate_frame(CURRENT_OVERLAY_TRADES)
    overlay["sw_sector"] = overlay["l2_sector_name"].map(_clean)
    overlay["family_sector"] = overlay["sw_sector"].map(_sector_family)
    overlay_cmp = _attach_diffusion(overlay, sw_diff, family_diff, "sw_sector", "family_sector")

    old = _prepare_candidate_frame(OLD_SELECTED)
    formal = _prepare_candidate_frame(OLD_FORMAL)
    formal_keys = set(_date_text(formal["entry_date"]) + "|" + formal["code6"])
    old["old_formal_hit"] = (_date_text(old["entry_date"]) + "|" + old["code6"]).isin(formal_keys)
    codes = sorted(set(old.get("code", pd.Series(dtype=str)).fillna("").astype(str)) | set(old["code6"]))
    sw_map = _current_sw_map(codes)
    if not sw_map.empty:
        old = old.merge(sw_map.add_prefix("current_"), left_on="code6", right_on="current_code6", how="left")
        old["sw_sector"] = old["current_sw_l2_name"].map(_clean)
    else:
        old["sw_sector"] = old["l2_sector_name"].map(_clean)
    old["family_sector"] = old["sw_sector"].map(_sector_family)
    old_cmp = _attach_diffusion(old, sw_diff, family_diff, "sw_sector", "family_sector")

    samples = []
    for label, frame in [
        ("current_scheduler_base_all", base_cmp),
        ("current_scheduler_base_sw65", base_cmp[base_cmp["sw_pass65"]]),
        ("current_scheduler_base_family65", base_cmp[base_cmp["family_pass65"]]),
        ("current_overlay_all", overlay_cmp),
        ("current_overlay_sw65", overlay_cmp[overlay_cmp["sw_pass65"]]),
        ("current_overlay_family65", overlay_cmp[overlay_cmp["family_pass65"]]),
        ("old_selected_all", old_cmp),
        ("old_selected_sw65", old_cmp[old_cmp["sw_pass65"]]),
        ("old_selected_family65", old_cmp[old_cmp["family_pass65"]]),
        ("old_formal_hits_all", old_cmp[old_cmp["old_formal_hit"]]),
        ("old_formal_hits_sw65", old_cmp[old_cmp["old_formal_hit"] & old_cmp["sw_pass65"]]),
        ("old_formal_hits_family65", old_cmp[old_cmp["old_formal_hit"] & old_cmp["family_pass65"]]),
    ]:
        row = _metrics(frame, label)
        row["sw_pass_count"] = int(frame["sw_pass65"].sum()) if "sw_pass65" in frame.columns else 0
        row["family_pass_count"] = int(frame["family_pass65"].sum()) if "family_pass65" in frame.columns else 0
        row["family_rescue_count"] = int(frame["family_rescue"].sum()) if "family_rescue" in frame.columns else 0
        row["family_loss_count"] = int(frame["family_loss"].sum()) if "family_loss" in frame.columns else 0
        samples.append(row)
    metrics = pd.DataFrame(samples)

    summary = {
        "status": "completed",
        "pool_rows": int(len(pool)),
        "pool_unique_days": int(pool["entry_date"].nunique()),
        "current_base_rows": int(len(base_cmp)),
        "current_base_sw65": int(base_cmp["sw_pass65"].sum()),
        "current_base_family65": int(base_cmp["family_pass65"].sum()),
        "current_base_family_rescue": int(base_cmp["family_rescue"].sum()),
        "current_base_family_loss": int(base_cmp["family_loss"].sum()),
        "old_selected_rows": int(len(old_cmp)),
        "old_selected_sw65": int(old_cmp["sw_pass65"].sum()),
        "old_selected_family65": int(old_cmp["family_pass65"].sum()),
        "old_selected_family_rescue": int(old_cmp["family_rescue"].sum()),
        "old_selected_family_loss": int(old_cmp["family_loss"].sum()),
        "old_formal_hits": int(old_cmp["old_formal_hit"].sum()),
        "old_formal_hits_sw65": int((old_cmp["old_formal_hit"] & old_cmp["sw_pass65"]).sum()),
        "old_formal_hits_family65": int((old_cmp["old_formal_hit"] & old_cmp["family_pass65"]).sum()),
        "old_formal_hits_family_rescue": int((old_cmp["old_formal_hit"] & old_cmp["family_rescue"]).sum()),
        "old_formal_hits_family_loss": int((old_cmp["old_formal_hit"] & old_cmp["family_loss"]).sum()),
        "rule": "audit_only_family_proxy; not used by production gates",
    }

    sw_diff.to_csv(OUT_DIR / "sw_sector_diffusion.csv", index=False, encoding="utf-8-sig")
    family_diff.to_csv(OUT_DIR / "family_sector_diffusion.csv", index=False, encoding="utf-8-sig")
    base_cmp.to_csv(OUT_DIR / "current_base_with_sw_vs_family_diffusion.csv", index=False, encoding="utf-8-sig")
    overlay_cmp.to_csv(OUT_DIR / "current_overlay_with_sw_vs_family_diffusion.csv", index=False, encoding="utf-8-sig")
    old_cmp.to_csv(OUT_DIR / "old_selected_with_sw_vs_family_diffusion.csv", index=False, encoding="utf-8-sig")
    metrics.to_csv(OUT_DIR / "sample_metrics.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Score120 行业族合并代理审计 v1",
        "",
        "## 结论",
        "",
        f"- 当前全候选池：{summary['pool_rows']} 行，{summary['pool_unique_days']} 个交易日。",
        f"- 当前 scheduler base：申万二级通过 {summary['current_base_sw65']}，行业族通过 {summary['current_base_family65']}；行业族新增 {summary['current_base_family_rescue']}，丢失 {summary['current_base_family_loss']}。",
        f"- 旧 81 行候选：申万二级通过 {summary['old_selected_sw65']}，行业族通过 {summary['old_selected_family65']}；行业族新增 {summary['old_selected_family_rescue']}，丢失 {summary['old_selected_family_loss']}。",
        f"- 旧正式命中 26 行：申万二级通过 {summary['old_formal_hits_sw65']}，行业族通过 {summary['old_formal_hits_family65']}；行业族新增 {summary['old_formal_hits_family_rescue']}，丢失 {summary['old_formal_hits_family_loss']}。",
        "- 该审计只验证行业族代理效果，不改变正式 gate。若行业族只新增少量候选或同时造成大量丢失，不应作为生产兜底。",
        "",
        "## 样本收益",
        "",
        metrics.to_markdown(index=False),
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
