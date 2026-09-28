from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_range_weak_shadow_mtm_v1 import _annual, _metrics, _simulate_mtm
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _md_table, _trade_calendar


SOURCE = ROOT / "reports" / "gen3_range_weak_rank_capacity_v1" / "topn_selected_candidates.parquet"
OUT_DIR = ROOT / "reports" / "gen3_range_weak_source_quality_v1"
RET_COL = "fwd_ret_open_to_close_5d"
WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}
COST_BPS = 50
HOLD_DAYS = 5


def _pct(x: object) -> str:
    if x is None or pd.isna(x):
        return ""
    return f"{float(x):.2%}"


def _load() -> pd.DataFrame:
    d = pd.read_parquet(SOURCE)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    for col in [
        "top_n",
        "entry_open",
        RET_COL,
        "candidate_score",
        "amount20",
        "amount_ratio20",
        "breadth_ma20",
        "index_mom20",
        "close_position",
        "drawdown20",
        "range_pos60",
        "gap_open",
        "mom20",
        "runup_from_60d_low",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["entry_date", "code", "entry_open", RET_COL, "top_n"]).copy()


def _mask(d: pd.DataFrame, name: str) -> pd.Series:
    base_range = d["probe_family"].eq("range") & d["probe"].eq("stress_no_crash") & d["top_n"].eq(1)
    base_weak = d["probe_family"].eq("weak_rebound") & d["probe"].eq("low_not_chasing") & d["top_n"].eq(1)
    if name == "range_base_top1":
        return base_range
    if name == "range_quality_v1":
        return (
            base_range
            & d["breadth_ma20"].gt(0.25)
            & d["amount_ratio20"].le(2.0)
            & d["drawdown20"].gt(-0.30)
        )
    if name == "range_quality_v2":
        return (
            base_range
            & d["breadth_ma20"].gt(0.25)
            & d["amount_ratio20"].le(2.0)
            & d["drawdown20"].gt(-0.30)
            & ((d["index_mom20"].lt(0.0)) | (d["index_mom20"].gt(0.04)))
        )
    if name == "range_quality_v3_deep_floor":
        return (
            base_range
            & d["range_pos60"].le(0.20)
            & d["breadth_ma20"].gt(0.25)
            & d["amount_ratio20"].le(1.8)
            & d["drawdown20"].between(-0.30, -0.08, inclusive="both")
        )
    if name == "weak_base_top1":
        return base_weak
    if name == "weak_quality_v1":
        return (
            base_weak
            & d["amount_ratio20"].le(2.0)
            & d["close_position"].lt(0.99)
            & ~d["breadth_ma20"].between(0.40, 0.55, inclusive="both")
        )
    if name == "weak_quality_v2":
        return (
            base_weak
            & d["amount_ratio20"].le(2.0)
            & d["close_position"].between(0.75, 0.99, inclusive="left")
            & d["index_mom20"].ge(0.0)
        )
    if name == "weak_quality_v3_repair_not_hot":
        return (
            base_weak
            & d["amount_ratio20"].le(1.8)
            & d["close_position"].between(0.60, 0.99, inclusive="left")
            & d["drawdown20"].between(-0.35, -0.08, inclusive="both")
            & d["mom20"].lt(0.05)
        )
    raise ValueError(name)


def _summary(d: pd.DataFrame, profile: str, group_cols: list[str]) -> pd.DataFrame:
    x = d.copy()
    x["net50"] = x[RET_COL] - COST_BPS / 10000.0
    x["bad10"] = x["net50"].le(-0.10)
    x["good5"] = x["net50"].ge(0.05)

    def q10(s: pd.Series) -> float:
        s = s.dropna()
        return np.nan if s.empty else float(s.quantile(0.10))

    out = (
        x.groupby(group_cols, dropna=False)
        .agg(
            rows=("code", "size"),
            days=("entry_date", "nunique"),
            mean_ret=("net50", "mean"),
            median_ret=("net50", "median"),
            win_rate=("net50", lambda s: float((s > 0).mean()) if len(s) else np.nan),
            p10=("net50", q10),
            worst=("net50", "min"),
            bad10_rate=("bad10", "mean"),
            good5_rate=("good5", "mean"),
        )
        .reset_index()
    )
    out["profile"] = profile
    return out


def _next_exit_dates(entry_dates: pd.Series) -> dict[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(entry_dates.min()).normalize()
    end = pd.Timestamp(entry_dates.max()).normalize() + pd.Timedelta(days=30)
    cal = _trade_calendar(start, end)
    out: dict[pd.Timestamp, pd.Timestamp] = {}
    for i, day in enumerate(cal):
        if i + HOLD_DAYS - 1 < len(cal):
            out[day] = cal[i + HOLD_DAYS - 1]
    return out


def _to_mtm_candidates(d: pd.DataFrame, profile: str) -> pd.DataFrame:
    x = d.copy()
    exit_map = _next_exit_dates(x["entry_date"])
    x["policy_exit_date"] = x["entry_date"].map(exit_map)
    x["book"] = profile
    x["rank"] = 1
    x["entry_price"] = x["entry_open"]
    x["net_ret"] = x[RET_COL] - COST_BPS / 10000.0
    x["cost_bps"] = COST_BPS
    x = x.dropna(subset=["policy_exit_date", "entry_price", "net_ret"])
    return x.sort_values(["entry_date", "candidate_score", "amount20"], ascending=[True, False, False])


def _profile_report(source: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    profiles = [
        "range_base_top1",
        "range_quality_v1",
        "range_quality_v2",
        "range_quality_v3_deep_floor",
        "weak_base_top1",
        "weak_quality_v1",
        "weak_quality_v2",
        "weak_quality_v3_repair_not_hot",
    ]
    event_parts = []
    annual_parts = []
    mtm_rows = []
    mtm_annual = []
    for profile in profiles:
        part = source[_mask(source, profile)].copy()
        if part.empty:
            continue
        part["year"] = part["entry_date"].dt.year
        part.to_csv(OUT_DIR / f"{profile}_events.csv", index=False, encoding="utf-8-sig")
        event_parts.append(_summary(part.assign(scope="full"), profile, ["scope"]))
        annual_parts.append(_summary(part, profile, ["year"]))

        if len(part) >= 30 and part["entry_date"].nunique() >= 30:
            candidates = _to_mtm_candidates(part, profile)
            closed, curve = _simulate_mtm(candidates)
            candidates.to_csv(OUT_DIR / f"{profile}_cost50_candidates.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(OUT_DIR / f"{profile}_cost50_closed_trades.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(OUT_DIR / f"{profile}_cost50_mtm_curve.csv", index=False, encoding="utf-8-sig")
            mtm_rows.append(_metrics(profile, COST_BPS, curve, closed))
            mtm_annual.append(_annual(profile, COST_BPS, curve, closed))

    event = pd.concat(event_parts, ignore_index=True) if event_parts else pd.DataFrame()
    annual = pd.concat(annual_parts, ignore_index=True) if annual_parts else pd.DataFrame()
    mtm = pd.DataFrame(mtm_rows)
    mtm_year = pd.concat(mtm_annual, ignore_index=True) if mtm_annual else pd.DataFrame()
    return event, annual, mtm, mtm_year


def _write_report(event: pd.DataFrame, annual: pd.DataFrame, mtm: pd.DataFrame, mtm_year: pd.DataFrame) -> None:
    pct_cols = {
        "mean_ret",
        "median_ret",
        "win_rate",
        "p10",
        "worst",
        "bad10_rate",
        "good5_rate",
        "total_ret",
        "max_drawdown",
        "worst_open_mtm_ret",
        "mean_trade_ret",
        "median_trade_ret",
        "worst_trade",
        "return",
    }
    lines = [
        "# G3 Range/Weak 候选源质量重筛 V1",
        "",
        "## 口径",
        "",
        "- 输入：第38步 Top1 range/weak 候选源，不新增复杂因子。",
        "- 成本：事件收益统一扣 50bps，MTM 也按 50bps 复算。",
        "- 目的：验证“减少交易次数 + 更硬安全边际”是否能让 range/weak 从影子链路升级。",
        "- 防过拟合约束：只测试少数有业务含义的过滤，所有结果按 train/valid/blind 年份展开，不按单一最优结果改正式策略。",
        "",
        "## 50bps 事件质量",
        "",
        _md_table(event.sort_values(["profile"]), pct_cols=pct_cols),
        "",
        "## 50bps MTM",
        "",
        _md_table(mtm.sort_values(["book"]), pct_cols=pct_cols),
        "",
        "## 年度事件质量",
        "",
        _md_table(annual.sort_values(["profile", "year"]), pct_cols=pct_cols),
        "",
        "## 年度 MTM",
        "",
        _md_table(mtm_year.sort_values(["book", "year"]), pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 如果严格版本在 50bps 下仍只有低个位数年化，说明 range/weak 候选源不是调几个护栏能解决。",
        "- 如果交易次数大幅下降但年度仍不稳定，则不能进入正式 G3，只能保留为研究标签。",
        "- 下一步应把通过 50bps 事件和 MTM 双审计的极少数 profile，与 panic/strong 主组合做 5%-10% 权重压力，而不是直接加为第三主链。",
        "",
    ]
    (OUT_DIR / "source_quality_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    event, annual, mtm, mtm_year = _profile_report(_load())
    event.to_csv(OUT_DIR / "event_summary_50bps.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "event_annual_50bps.csv", index=False, encoding="utf-8-sig")
    mtm.to_csv(OUT_DIR / "mtm_summary_50bps.csv", index=False, encoding="utf-8-sig")
    mtm_year.to_csv(OUT_DIR / "mtm_annual_50bps.csv", index=False, encoding="utf-8-sig")
    _write_report(event, annual, mtm, mtm_year)
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "event": event.to_dict(orient="records"),
                "mtm": mtm.to_dict(orient="records"),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
