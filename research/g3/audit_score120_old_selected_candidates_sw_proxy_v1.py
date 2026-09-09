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

from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE = Path(
    r"F:\Stock\AiStockResearchArchive\backups\g3_reset_20260620_003808\reports"
    r"\gen3_pre_2024_10_state_alpha_closure_v5\institutional_mainwave_candidates.csv"
)
FORMAL_SOURCE = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
OUT_DIR = report_path("score120_old_selected_candidates_sw_proxy_v1")


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


def _top_share(series: pd.Series, n: int = 3) -> float | None:
    cleaned = series.map(_clean)
    cleaned = cleaned[cleaned != ""]
    if cleaned.empty:
        return None
    return float(cleaned.value_counts().head(n).sum() / len(cleaned))


def _classify(old_sector: Any, sw_sector: Any) -> str:
    old = _clean(old_sector)
    sw = _clean(sw_sector)
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
    if old == "元器件" and sw == "元件":
        return "semantic_rename"
    return "changed"


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


def _counts(df: pd.DataFrame, col: str, label: str) -> pd.DataFrame:
    if col not in df.columns:
        return pd.DataFrame(columns=["sample", "sector", "count"])
    out = df[col].map(_clean).value_counts().reset_index()
    out.columns = ["sector", "count"]
    out.insert(0, "sample", label)
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source = pd.read_csv(SOURCE, encoding="utf-8-sig", low_memory=False)
    formal = pd.read_csv(FORMAL_SOURCE, encoding="utf-8-sig", low_memory=False)
    source["code6"] = _code6(source["code"])
    source["key"] = _date_text(source["entry_date"]) + "|" + source["code6"]
    formal["key"] = _date_text(formal["entry_date"]) + "|" + _code6(formal["code"])
    old_keys = set(formal["key"])
    source["old_formal_hit"] = source["key"].isin(old_keys)

    codes = sorted(set(source["code"].fillna("").astype(str)) | set(source["code6"]))
    sw = _current_sw_map(codes)
    if not sw.empty:
        source = source.merge(sw.add_prefix("current_"), left_on="code6", right_on="current_code6", how="left")
    else:
        source["current_sw_l2_name"] = ""
        source["current_sw_l2_code"] = ""
    source["sw_l2_drift_type"] = source.apply(lambda row: _classify(row.get("l2_sector_name"), row.get("current_sw_l2_name")), axis=1)

    counts = pd.concat(
        [
            _counts(source, "l2_sector_name", "old_selected_candidates_old_sector"),
            _counts(source, "current_sw_l2_name", "old_selected_candidates_current_sw_l2"),
            _counts(source[source["old_formal_hit"]], "l2_sector_name", "old_formal_hits_old_sector"),
            _counts(source[source["old_formal_hit"]], "current_sw_l2_name", "old_formal_hits_current_sw_l2"),
        ],
        ignore_index=True,
    )
    drift = source["sw_l2_drift_type"].value_counts().reset_index()
    drift.columns = ["sw_l2_drift_type", "count"]
    drift_hits = source[source["old_formal_hit"]]["sw_l2_drift_type"].value_counts().reset_index()
    drift_hits.columns = ["sw_l2_drift_type", "count"]

    summary = {
        "status": "completed",
        "source": str(SOURCE),
        "rows": int(len(source)),
        "unique_days": int(source["entry_date"].nunique()),
        "old_formal_hits": int(source["old_formal_hit"].sum()),
        "old_sector_top3_share": _top_share(source["l2_sector_name"]),
        "current_sw_top3_share": _top_share(source["current_sw_l2_name"]),
        "old_formal_hits_old_sector_top3_share": _top_share(source[source["old_formal_hit"]]["l2_sector_name"]),
        "old_formal_hits_current_sw_top3_share": _top_share(source[source["old_formal_hit"]]["current_sw_l2_name"]),
        "sw_sub_split_or_changed": int(source["sw_l2_drift_type"].isin(["sw_sub_split", "changed"]).sum()),
        "old_formal_hits_sw_sub_split_or_changed": int(
            source[source["old_formal_hit"]]["sw_l2_drift_type"].isin(["sw_sub_split", "changed"]).sum()
        ),
        "full_diffusion_ab_ready": False,
        "full_diffusion_ab_blocker": "This source has one selected institutional candidate per signal day; it preserves old sector mapping but is not the full daily cross-section needed to recompute sector_diffusion_score.",
    }
    source.to_csv(OUT_DIR / "old_selected_candidates_with_current_sw.csv", index=False, encoding="utf-8-sig")
    counts.to_csv(OUT_DIR / "sector_counts.csv", index=False, encoding="utf-8-sig")
    drift.to_csv(OUT_DIR / "mapping_drift_all.csv", index=False, encoding="utf-8-sig")
    drift_hits.to_csv(OUT_DIR / "mapping_drift_old_formal_hits.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Score120 旧选中候选申万映射代理审计 v1",
        "",
        "## 结论",
        "",
        f"- 旧选中候选：{summary['rows']} 行，覆盖旧正式机构主升：{summary['old_formal_hits']} / 26。",
        f"- 旧候选按旧行业 Top3 集中度：{summary['old_sector_top3_share']:.2%}；按当前申万二级 Top3 集中度：{summary['current_sw_top3_share']:.2%}。",
        f"- 旧正式命中部分按旧行业 Top3 集中度：{summary['old_formal_hits_old_sector_top3_share']:.2%}；按当前申万二级 Top3 集中度：{summary['old_formal_hits_current_sw_top3_share']:.2%}。",
        f"- 全部旧候选中申万实质拆分/变更：{summary['sw_sub_split_or_changed']} 行；旧正式命中部分：{summary['old_formal_hits_sw_sub_split_or_changed']} 行。",
        f"- 完整扩散分 A/B 是否就绪：{summary['full_diffusion_ab_ready']}；原因：{summary['full_diffusion_ab_blocker']}",
        "",
        "## 全部旧候选映射漂移",
        "",
        drift.to_markdown(index=False),
        "",
        "## 旧正式命中部分映射漂移",
        "",
        drift_hits.to_markdown(index=False),
        "",
        "## 行业分布",
        "",
        counts.to_markdown(index=False),
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
