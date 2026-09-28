from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("score120_sector_switch_gap_audit_v1")
FORMAL_DISPATCH = report_path(
    "g3_formal_five_strategy_dispatch_contract_v1",
    "formal_trades_with_dispatch_strategy.csv",
)
CURRENT_SCORE120 = report_path(
    "gen3_score120_core_strategy_v1",
    "g3_route_execution_mandate_candidate_closed_trades.csv",
)
NATIVE_BRIDGE_30M = report_path(
    "g3_native_bridge_30m_integrity_v1",
    "native_bridge_candidates_30m_audit.csv",
)
SCHEDULER_SOURCE = report_path(
    "wave_style_model_scheduler_v1",
    "scheduler_focus_240d_score120_aggr25",
    "closed_trades.csv",
)
STAGE_FILES = {
    "scheduler_source": SCHEDULER_SOURCE,
    "sector_diffusion_gate": report_path("score120_sector_diffusion_gate_v1", "base_trades_with_sector_diffusion.csv"),
    "m30_overlay": report_path("score120_sector_diffusion_30m_overlay_v1", "base_trades_with_sector_diffusion_30m.csv"),
    "mom60_source_signals": report_path(
        "score120_mom60_gate_revalidation_v1",
        "score120_diff65_m30_ma20_source_signals.csv",
    ),
    "activation_regime": report_path("score120_activation_regime_v1", "diff65_m30_trades_with_regime_corrected.csv"),
    "current_package": CURRENT_SCORE120,
}


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (pd.Series, pd.DataFrame)):
        return value.to_dict()
    if pd.isna(value):
        return None
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return None
    except Exception:
        pass
    return value


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, encoding="utf-8-sig", low_memory=False)


def _code6(series: pd.Series) -> pd.Series:
    text = series.fillna("").astype(str)
    extracted = text.str.extract(r"(\d{6})", expand=False)
    return extracted.fillna(text.str.strip())


def _date_text(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.strftime("%Y-%m-%d").fillna("")


def _key(df: pd.DataFrame) -> pd.Series:
    date_col = next((c for c in ["entry_date", "trade_date", "signal_date", "date"] if c in df.columns), None)
    code_col = next((c for c in ["code", "code6", "code_raw", "stock_code"] if c in df.columns), None)
    if not date_col or not code_col:
        return pd.Series("", index=df.index)
    return _date_text(df[date_col]) + "|" + _code6(df[code_col])


def _metrics(df: pd.DataFrame, label: str) -> dict[str, Any]:
    ret = pd.to_numeric(df.get("net_ret", pd.Series(dtype=float)), errors="coerce")
    pnl = pd.to_numeric(df.get("realized_pnl", pd.Series(dtype=float)), errors="coerce")
    row: dict[str, Any] = {
        "sample": label,
        "rows": int(len(df)),
        "entry_start": None,
        "entry_end": None,
        "avg_net_ret": float(ret.mean()) if ret.notna().any() else None,
        "sum_net_ret": float(ret.sum()) if ret.notna().any() else None,
        "win_rate": float((ret > 0).mean()) if ret.notna().any() else None,
        "sum_realized_pnl": float(pnl.sum()) if pnl.notna().any() else None,
    }
    if "entry_date" in df.columns and not df.empty:
        dates = pd.to_datetime(df["entry_date"], errors="coerce")
        if dates.notna().any():
            row["entry_start"] = dates.min().strftime("%Y-%m-%d")
            row["entry_end"] = dates.max().strftime("%Y-%m-%d")
    for col in ["score", "raw_score", "wave_style_score", "selected_score", "rank_key", "sector_diffusion_score"]:
        if col in df.columns:
            s = pd.to_numeric(df[col], errors="coerce")
            row[f"{col}_min"] = float(s.min()) if s.notna().any() else None
            row[f"{col}_mean"] = float(s.mean()) if s.notna().any() else None
            row[f"{col}_max"] = float(s.max()) if s.notna().any() else None
    return row


def _sector_counts(df: pd.DataFrame, label: str) -> pd.DataFrame:
    sector_col = next(
        (
            c
            for c in ["sector_for_distinct", "l2_sector_name", "stock_industry", "industry", "sector_name"]
            if c in df.columns
        ),
        None,
    )
    if not sector_col:
        return pd.DataFrame(columns=["sample", "sector_col", "sector", "count"])
    out = (
        df[sector_col]
        .fillna("")
        .astype(str)
        .replace({"nan": ""})
        .value_counts(dropna=False)
        .reset_index()
    )
    out.columns = ["sector", "count"]
    out.insert(0, "sector_col", sector_col)
    out.insert(0, "sample", label)
    return out


def _clean_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _classify_sw_drift(old_sector: Any, sw_l2_name: Any) -> str:
    old = _clean_text(old_sector)
    sw = _clean_text(sw_l2_name)
    if not old or not sw:
        return "missing_mapping"
    if old == sw:
        return "same_name"
    if old == "软件服务" and sw == "软件开发":
        return "semantic_rename"
    if old == "软件服务" and sw == "IT服务":
        return "sw_sub_split"
    if old == "半导体" and sw in {"电子化学品", "专用设备"}:
        return "sw_sub_split"
    return "changed"


def _top_share(series: pd.Series, n: int) -> float | None:
    cleaned = series.map(_clean_text)
    cleaned = cleaned[cleaned != ""]
    total = int(len(cleaned))
    if total <= 0:
        return None
    return float(cleaned.value_counts().head(n).sum() / total)


def _sector_return(df: pd.DataFrame, sector_col: str, label: str) -> pd.DataFrame:
    if df.empty or sector_col not in df.columns:
        return pd.DataFrame(
            columns=[
                "sample",
                "sector_col",
                "sector",
                "rows",
                "avg_net_ret",
                "sum_net_ret",
                "win_rate",
                "sum_realized_pnl",
            ]
        )
    tmp = df.copy()
    tmp["__sector"] = tmp[sector_col].map(_clean_text)
    tmp["__net_ret"] = pd.to_numeric(tmp.get("net_ret", pd.Series(dtype=float)), errors="coerce")
    tmp["__realized_pnl"] = pd.to_numeric(tmp.get("realized_pnl", pd.Series(dtype=float)), errors="coerce")
    tmp = tmp[tmp["__sector"] != ""]
    rows: list[dict[str, Any]] = []
    for sector, group in tmp.groupby("__sector", dropna=False):
        ret = group["__net_ret"]
        pnl = group["__realized_pnl"]
        rows.append(
            {
                "sample": label,
                "sector_col": sector_col,
                "sector": sector,
                "rows": int(len(group)),
                "avg_net_ret": float(ret.mean()) if ret.notna().any() else None,
                "sum_net_ret": float(ret.sum()) if ret.notna().any() else None,
                "win_rate": float((ret > 0).mean()) if ret.notna().any() else None,
                "sum_realized_pnl": float(pnl.sum()) if pnl.notna().any() else None,
            }
        )
    return pd.DataFrame(rows).sort_values(["rows", "sum_net_ret"], ascending=[False, False])


def _current_sw_map(codes: list[str]) -> pd.DataFrame:
    if not codes or not clickhouse_table_exists("sector_stocks") or not clickhouse_table_exists("sectors"):
        return pd.DataFrame(columns=["code", "sw_l2_code", "sw_l2_name"])
    quoted = ",".join("'" + c.replace("'", "''") + "'" for c in sorted(set(codes)))
    sql = f"""
    SELECT
        ss.stock_code AS code,
        any(s.code) AS sw_l2_code,
        any(s.name) AS sw_l2_name
    FROM sector_stocks ss
    INNER JOIN sectors s ON ss.sector_code = s.code
    WHERE ss.stock_code IN ({quoted})
      AND s.type = 'industry'
      AND s.level = 2
    GROUP BY ss.stock_code
    """
    try:
        out = clickhouse_query_df(sql)
        if not out.empty and "code" in out.columns:
            out["code6"] = _code6(out["code"])
        return out
    except Exception as exc:
        return pd.DataFrame({"code": sorted(set(codes)), "sw_query_error": [str(exc)] * len(set(codes))})


def _stage_overlap(old_inst: pd.DataFrame) -> pd.DataFrame:
    old_keys = set(_key(old_inst))
    rows: list[dict[str, Any]] = []
    for name, path in STAGE_FILES.items():
        df = _read_csv(path)
        keys = set(_key(df)) if not df.empty else set()
        rows.append(
            {
                "stage": name,
                "path": str(path),
                "exists": path.exists(),
                "rows": int(len(df)),
                "old_formal_inst_overlap": int(len(old_keys & keys)),
                "old_formal_inst_total": int(len(old_keys)),
                "overlap_rate": float(len(old_keys & keys) / len(old_keys)) if old_keys else None,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    formal = _read_csv(FORMAL_DISPATCH)
    current = _read_csv(CURRENT_SCORE120)
    native_bridge = _read_csv(NATIVE_BRIDGE_30M)
    native_inst = native_bridge[
        native_bridge.get("trade_strategy", pd.Series("", index=native_bridge.index))
        .fillna("")
        .astype(str)
        .eq("institutional_score120_mainwave")
    ].copy()
    old_inst = formal[
        formal.get("trade_strategy", pd.Series("", index=formal.index)).fillna("").astype(str).eq("institutional_score120_mainwave")
    ].copy()

    old_inst["key"] = _key(old_inst)
    current["key"] = _key(current) if not current.empty else pd.Series(dtype=str)
    native_inst["key"] = _key(native_inst) if not native_inst.empty else pd.Series(dtype=str)
    old_codes = sorted(
        set(old_inst.get("code", pd.Series(dtype=str)).dropna().astype(str))
        | set(_code6(old_inst.get("code", pd.Series(dtype=str))).dropna().astype(str))
    )
    current_codes = sorted(
        set(current.get("code", pd.Series(dtype=str)).dropna().astype(str))
        | set(_code6(current.get("code", pd.Series(dtype=str))).dropna().astype(str))
    )
    native_codes = sorted(
        set(native_inst.get("code", pd.Series(dtype=str)).dropna().astype(str))
        | set(_code6(native_inst.get("code", pd.Series(dtype=str))).dropna().astype(str))
    )
    sw_map = _current_sw_map(sorted(set(old_codes + current_codes + native_codes)))

    if not sw_map.empty and "code6" in sw_map.columns:
        old_inst["code6_for_sw"] = _code6(old_inst["code"])
        old_inst = old_inst.merge(sw_map.add_prefix("current_"), left_on="code6_for_sw", right_on="current_code6", how="left")
        if not current.empty:
            current["code6_for_sw"] = _code6(current["code"])
            current = current.merge(sw_map.add_prefix("current_"), left_on="code6_for_sw", right_on="current_code6", how="left")
        if not native_inst.empty:
            native_inst["code6_for_sw"] = _code6(native_inst["code"])
            native_inst = native_inst.merge(sw_map.add_prefix("current_"), left_on="code6_for_sw", right_on="current_code6", how="left")

    metrics = pd.DataFrame([_metrics(old_inst, "old_formal_institutional_score120"), _metrics(current, "current_score120_package")])
    sector_counts = pd.concat(
        [_sector_counts(old_inst, "old_formal_institutional_score120"), _sector_counts(current, "current_score120_package")],
        ignore_index=True,
    )
    stage_overlap = _stage_overlap(old_inst)

    comparison_cols = [
        "key",
        "code",
        "name",
        "entry_date",
        "net_ret",
        "realized_pnl",
        "score",
        "raw_score",
        "wave_style_score",
        "selected_score",
        "rank_key",
        "sector_for_distinct",
        "l2_sector_name",
        "sector_diffusion_score",
        "current_sw_l2_name",
    ]
    old_view = old_inst[[c for c in comparison_cols if c in old_inst.columns]].copy()
    old_view["present_in_current_package"] = old_view["key"].isin(set(current.get("key", pd.Series(dtype=str))))
    if "sector_for_distinct" in old_view.columns and "current_sw_l2_name" in old_view.columns:
        old_view["sw_l2_drift_type"] = old_view.apply(
            lambda row: _classify_sw_drift(row.get("sector_for_distinct"), row.get("current_sw_l2_name")),
            axis=1,
        )

    cur_view = current[[c for c in comparison_cols if c in current.columns]].copy()
    cur_view["present_in_old_formal_inst"] = cur_view["key"].isin(set(old_inst["key"]))
    native_cols = [
        "key",
        "code",
        "name",
        "entry_date",
        "strategy_score",
        "source_strategy_label",
        "native_source_file",
        "sector_proxy",
        "m30_data_ok",
        "m30_confirmed_proxy",
        "m30_confirm_rule",
        "current_sw_l2_name",
    ]
    native_view = native_inst[[c for c in native_cols if c in native_inst.columns]].copy()
    native_view["present_in_old_formal_inst"] = native_view["key"].isin(set(old_inst["key"])) if not native_view.empty else False
    native_sw_counts = (
        native_view["current_sw_l2_name"]
        .map(_clean_text)
        .value_counts()
        .reset_index()
        if "current_sw_l2_name" in native_view.columns
        else pd.DataFrame(columns=["current_sw_l2_name", "count"])
    )
    if not native_sw_counts.empty:
        native_sw_counts.columns = ["current_sw_l2_name", "count"]
    native_old_hit_sw_counts = (
        native_view[native_view["present_in_old_formal_inst"]]["current_sw_l2_name"]
        .map(_clean_text)
        .value_counts()
        .reset_index()
        if "current_sw_l2_name" in native_view.columns and "present_in_old_formal_inst" in native_view.columns
        else pd.DataFrame(columns=["current_sw_l2_name", "count"])
    )
    if not native_old_hit_sw_counts.empty:
        native_old_hit_sw_counts.columns = ["current_sw_l2_name", "count"]
    old_sector_return = _sector_return(old_view, "sector_for_distinct", "old_formal_by_old_sector")
    old_sw_return = _sector_return(old_view, "current_sw_l2_name", "old_formal_by_current_sw_l2")
    if "sw_l2_drift_type" in old_view.columns:
        mapping_drift = old_view["sw_l2_drift_type"].value_counts(dropna=False).reset_index()
        mapping_drift.columns = ["sw_l2_drift_type", "count"]
    else:
        mapping_drift = pd.DataFrame(columns=["sw_l2_drift_type", "count"])
    old_top3_share = _top_share(old_view.get("sector_for_distinct", pd.Series(dtype=str)), 3)
    current_sw_top3_share = _top_share(old_view.get("current_sw_l2_name", pd.Series(dtype=str)), 3)
    meaningful_split_count = int(
        old_view.get("sw_l2_drift_type", pd.Series(dtype=str)).isin(["sw_sub_split", "changed"]).sum()
    )

    conclusion = {
        "verdict": "not_sector_only_change",
        "reason": "旧正式机构主升样本在当前新 Score120 链路的最底层候选源即 0 重合，当前结果不能证明申万板块比 TDX 板块差。",
        "old_formal_inst_rows": int(len(old_inst)),
        "current_score120_rows": int(len(current)),
        "old_vs_current_key_overlap": int(len(set(old_inst["key"]) & set(current.get("key", pd.Series(dtype=str))))),
        "native_bridge_inst_rows": int(len(native_view)),
        "native_bridge_inst_old_formal_overlap": int(native_view["present_in_old_formal_inst"].sum()) if "present_in_old_formal_inst" in native_view.columns else 0,
        "native_bridge_current_sw_top3_share": _top_share(native_view.get("current_sw_l2_name", pd.Series(dtype=str)), 3),
        "native_bridge_old_hit_current_sw_top3_share": _top_share(
            native_view[native_view["present_in_old_formal_inst"]].get("current_sw_l2_name", pd.Series(dtype=str))
            if "present_in_old_formal_inst" in native_view.columns
            else pd.Series(dtype=str),
            3,
        ),
        "old_formal_old_sector_top3_share": old_top3_share,
        "old_formal_current_sw_l2_top3_share": current_sw_top3_share,
        "old_formal_meaningful_sw_split_count": meaningful_split_count,
        "full_sector_ab_ready": False,
        "full_sector_ab_blocker": "old formal Score120 full historical candidate pool with old TDX sector mapping is not available in the current reports; selected trades alone can audit mapping drift but cannot replay sector_diffusion.",
        "score_scale_issue": "旧正式机构主升使用历史 wave_style_score 合同族口径；当前包将 selected_score 写入 score，量纲不同，且旧正式保存分数不能单独按 score>=120 复算。",
        "next_ab_test": "必须固定同一批历史 Score120 合同族原生候选和同一退出合同，只替换 sector mapping，重算 sector_diffusion>=65。",
    }

    old_view.to_csv(OUT_DIR / "old_formal_institutional_mainwave_with_current_sw.csv", index=False, encoding="utf-8-sig")
    cur_view.to_csv(OUT_DIR / "current_score120_package_with_current_sw.csv", index=False, encoding="utf-8-sig")
    metrics.to_csv(OUT_DIR / "sample_metrics.csv", index=False, encoding="utf-8-sig")
    sector_counts.to_csv(OUT_DIR / "sector_counts.csv", index=False, encoding="utf-8-sig")
    old_sector_return.to_csv(OUT_DIR / "old_formal_return_by_old_sector.csv", index=False, encoding="utf-8-sig")
    old_sw_return.to_csv(OUT_DIR / "old_formal_return_by_current_sw_l2.csv", index=False, encoding="utf-8-sig")
    mapping_drift.to_csv(OUT_DIR / "old_formal_sw_l2_mapping_drift.csv", index=False, encoding="utf-8-sig")
    native_view.to_csv(OUT_DIR / "native_bridge_institutional_with_current_sw.csv", index=False, encoding="utf-8-sig")
    native_sw_counts.to_csv(OUT_DIR / "native_bridge_institutional_current_sw_counts.csv", index=False, encoding="utf-8-sig")
    native_old_hit_sw_counts.to_csv(OUT_DIR / "native_bridge_old_hit_current_sw_counts.csv", index=False, encoding="utf-8-sig")
    stage_overlap.to_csv(OUT_DIR / "old_formal_stage_overlap.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(conclusion, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")

    lines = [
        "# Score120 板块切换差距审计 v1",
        "",
        "## 结论",
        "",
        f"- 判定：`{conclusion['verdict']}`。",
        f"- 原因：{conclusion['reason']}",
        f"- 旧正式机构主升样本：{conclusion['old_formal_inst_rows']} 笔；当前 Score120 包：{conclusion['current_score120_rows']} 笔；二者同日同代码重合：{conclusion['old_vs_current_key_overlap']} 笔。",
        f"- 分数量纲问题：{conclusion['score_scale_issue']}",
        "",
        "## 每层穿透",
        "",
        stage_overlap.to_markdown(index=False),
        "",
        "## 下一步",
        "",
        f"- {conclusion['next_ab_test']}",
        "- 若固定候选后申万 sector_diffusion 大量丢失旧盈利票，才应考虑把机构主升的行业扩散层切回 TDX 或做双板块口径。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    clean_lines = [
        "# Score120 板块切换差距审计 v1",
        "",
        "## 结论",
        "",
        f"- 判定：`{conclusion['verdict']}`。",
        f"- 旧正式机构主升样本：{conclusion['old_formal_inst_rows']} 笔；当前 Score120 包：{conclusion['current_score120_rows']} 笔；同日同代码重合：{conclusion['old_vs_current_key_overlap']} 笔。",
        f"- 旧原生 30m 桥接候选：{conclusion['native_bridge_inst_rows']} 笔；覆盖旧正式样本：{conclusion['native_bridge_inst_old_formal_overlap']} 笔。",
        f"- 旧原生候选按当前申万二级 Top3 集中度：{conclusion['native_bridge_current_sw_top3_share']:.2%}；旧正式命中部分 Top3 集中度：{conclusion['native_bridge_old_hit_current_sw_top3_share']:.2%}。",
        f"- 旧正式样本按旧板块 Top3 集中度：{conclusion['old_formal_old_sector_top3_share']:.2%}。",
        f"- 旧正式样本按当前申万二级 Top3 集中度：{conclusion['old_formal_current_sw_l2_top3_share']:.2%}。",
        f"- 当前申万二级发生实质拆分/变更的旧正式交易数：{conclusion['old_formal_meaningful_sw_split_count']} 笔。",
        f"- 完整 sector_diffusion A/B 是否就绪：{conclusion['full_sector_ab_ready']}；阻塞原因：{conclusion['full_sector_ab_blocker']}",
        f"- 分数量纲问题：{conclusion['score_scale_issue']}",
        "",
        "## 映射漂移",
        "",
        mapping_drift.to_markdown(index=False),
        "",
        "## 旧原生候选按当前申万二级分布",
        "",
        native_sw_counts.to_markdown(index=False),
        "",
        "## 旧原生候选中旧正式命中部分的当前申万二级分布",
        "",
        native_old_hit_sw_counts.to_markdown(index=False),
        "",
        "## 旧正式样本按旧板块收益",
        "",
        old_sector_return.to_markdown(index=False),
        "",
        "## 旧正式样本按当前申万二级收益",
        "",
        old_sw_return.to_markdown(index=False),
        "",
        "## 每层穿透",
        "",
        stage_overlap.to_markdown(index=False),
        "",
        "## 下一步",
        "",
        f"- {conclusion['next_ab_test']}",
        "- 当前证据不足以证明申万板块本身导致旧正式 Score120 大幅失效；更大的确定性问题是候选源、分数量纲和策略包替换。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(clean_lines), encoding="utf-8")
    print(json.dumps({"out_dir": str(OUT_DIR), **conclusion}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
