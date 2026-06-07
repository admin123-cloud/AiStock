from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_validate_intraday_confirm import _confirm_signals, _label_signals, _load_minute_bars  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_four_path_independent_candidates" / "validation_v1" / "labeled_candidates.parquet"
OUT_DIR = ROOT / "reports" / "gen3_range_30m_confirm_v1"


def pct(x: float | None) -> str:
    if x is None or pd.isna(x):
        return ""
    return f"{float(x) * 100:.2f}%"


def stress_reclaim_mask(df: pd.DataFrame) -> pd.Series:
    bottom = df["range_pos60"].le(0.20)
    stress = df["big_down_rate"].ge(0.10)
    reclaim = df["close_position"].ge(0.55) | df["lower_shadow_ratio"].ge(0.25)
    not_crash = df["big_down_rate"].lt(0.35)
    return bottom & stress & reclaim & not_crash


def summarize(df: pd.DataFrame, group_cols: list[str], ret_col: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=group_cols + ["rows", "days", "mean_ret", "median_ret", "win_rate", "worst", "p10", "best"])

    def win(s: pd.Series) -> float:
        s = s.dropna()
        return np.nan if s.empty else float((s > 0).mean())

    def p10(s: pd.Series) -> float:
        s = s.dropna()
        return np.nan if s.empty else float(s.quantile(0.10))

    return (
        df.groupby(group_cols, dropna=False)
        .agg(
            rows=("code", "size"),
            days=("entry_date", "nunique"),
            mean_ret=(ret_col, "mean"),
            median_ret=(ret_col, "median"),
            win_rate=(ret_col, win),
            worst=(ret_col, "min"),
            p10=(ret_col, p10),
            best=(ret_col, "max"),
        )
        .reset_index()
        .sort_values(group_cols)
    )


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for c in df.columns:
            v = row[c]
            if c in pct_cols:
                item[c] = pct(v)
            elif isinstance(v, float):
                item[c] = f"{v:.4f}"
            else:
                item[c] = "" if pd.isna(v) else str(v)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def build() -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = pd.read_parquet(SOURCE)
    base["entry_date"] = pd.to_datetime(base["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    base["trade_date"] = pd.to_datetime(base["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    base = base[base["g3_chain"].eq("range_box_bottom")].copy()
    base = base[stress_reclaim_mask(base)].copy()
    base["year"] = pd.to_datetime(base["entry_date"], errors="coerce").dt.year
    base["window"] = np.where(pd.to_datetime(base["entry_date"]) >= pd.Timestamp("2025-01-01"), "blind_2025_plus", "train_to_2024")
    base["range_route"] = "box_bottom_stress_reclaim_daily"

    bars = _load_minute_bars(base, period=30)
    confirmed_raw = _confirm_signals(base, bars)
    confirmed = _label_signals(confirmed_raw)
    if not confirmed.empty:
        confirmed["year"] = pd.to_datetime(confirmed["entry_date"], errors="coerce").dt.year
        confirmed["window"] = np.where(pd.to_datetime(confirmed["entry_date"]) >= pd.Timestamp("2025-01-01"), "blind_2025_plus", "train_to_2024")
        confirmed["range_route"] = "box_bottom_stress_reclaim_30m_confirm"

    base_key = base[["entry_date", "code"]].drop_duplicates()
    conf_key = confirmed[["entry_date", "code"]].drop_duplicates() if not confirmed.empty else pd.DataFrame(columns=["entry_date", "code"])
    pass_rate = base_key.merge(conf_key.assign(confirmed=1), on=["entry_date", "code"], how="left")
    pass_rate["confirmed"] = pass_rate["confirmed"].fillna(0).astype(int)
    pass_rate["year"] = pd.to_datetime(pass_rate["entry_date"], errors="coerce").dt.year
    pass_rate_year = pass_rate.groupby("year").agg(candidates=("code", "size"), confirmed=("confirmed", "sum")).reset_index()
    pass_rate_year["confirm_rate"] = pass_rate_year["confirmed"] / pass_rate_year["candidates"]

    base_summary = summarize(base, ["range_route"], "fwd_ret_open_to_close_5d")
    confirmed_summary = summarize(confirmed, ["range_route"], "fwd_ret_confirm_to_close_5d")
    year_base = summarize(base, ["range_route", "year"], "fwd_ret_open_to_close_5d")
    year_confirmed = summarize(confirmed, ["range_route", "year"], "fwd_ret_confirm_to_close_5d")
    window_base = summarize(base, ["range_route", "window"], "fwd_ret_open_to_close_5d")
    window_confirmed = summarize(confirmed, ["range_route", "window"], "fwd_ret_confirm_to_close_5d")

    combined_summary = pd.concat([base_summary, confirmed_summary], ignore_index=True)
    combined_year = pd.concat([year_base, year_confirmed], ignore_index=True)
    combined_window = pd.concat([window_base, window_confirmed], ignore_index=True)

    base.to_parquet(OUT_DIR / "daily_candidates.parquet", index=False)
    base.to_csv(OUT_DIR / "daily_candidates.csv", index=False, encoding="utf-8-sig")
    confirmed.to_parquet(OUT_DIR / "confirmed_30m_candidates.parquet", index=False)
    confirmed.to_csv(OUT_DIR / "confirmed_30m_candidates.csv", index=False, encoding="utf-8-sig")
    combined_summary.to_csv(OUT_DIR / "summary_compare.csv", index=False, encoding="utf-8-sig")
    combined_year.to_csv(OUT_DIR / "year_compare.csv", index=False, encoding="utf-8-sig")
    combined_window.to_csv(OUT_DIR / "window_compare.csv", index=False, encoding="utf-8-sig")
    pass_rate_year.to_csv(OUT_DIR / "confirm_rate_by_year.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"mean_ret", "median_ret", "win_rate", "worst", "p10", "best", "confirm_rate"}
    report = [
        "# G3 横盘箱体底部 30m 确认审计 V1",
        "",
        "## 口径",
        "",
        "- 日线候选固定为 `box_bottom_stress_reclaim`，不做新增参数搜索。",
        "- 30m 确认复用 G3 已有 `range_box_bottom` 盘中反转确认：10:00 后，阳线、收盘位置较强、量能不弱，并突破前一根高点或不再创新低。",
        "- 分钟买入价用买入日 `日线收盘 / 当日最后一根30m收盘` 做价格口径校准，避免分钟/日线复权不一致。",
        "- 当前只评估确认过滤是否改善 2024/2026 失败段，不进入正式组合。",
        "",
        "## 总体对比",
        "",
        md_table(combined_summary, pct_cols=pct_cols),
        "",
        "## 窗口对比",
        "",
        md_table(combined_window, pct_cols=pct_cols),
        "",
        "## 年度对比",
        "",
        md_table(combined_year, pct_cols=pct_cols),
        "",
        "## 30m 确认通过率",
        "",
        md_table(pass_rate_year, pct_cols=pct_cols),
        "",
        "## 初步判断",
        "",
    ]
    if confirmed.empty:
        report.append("- 30m 确认没有产出样本；需要检查分钟覆盖或重写确认规则。")
    else:
        y = combined_year
        conf_2024 = y[(y["range_route"].eq("box_bottom_stress_reclaim_30m_confirm")) & (y["year"].eq(2024))]
        conf_2026 = y[(y["range_route"].eq("box_bottom_stress_reclaim_30m_confirm")) & (y["year"].eq(2026))]
        report.extend(
            [
                "- 若 30m 后 2024/2026 仍为负，说明问题不只是盘中确认，而是横盘环境定义或候选池结构仍不够独立。",
                "- 若 30m 后样本明显减少但年度没有改善，不能继续调确认参数追结果，应回到箱体/冰点定义。",
                f"- 2024 30m 结果：{pct(float(conf_2024['mean_ret'].iloc[0])) if not conf_2024.empty else '无样本'}；2026 30m 结果：{pct(float(conf_2026['mean_ret'].iloc[0])) if not conf_2026.empty else '无样本'}。",
            ]
        )
    report.extend(
        [
            "",
            "## 下一步",
            "",
            "- 如果 30m 确认有效，再做 slot 复算；如果无效，停止扩展 range 当前版本，重新定义横盘市场。",
            "- 不要把该结果和 G3 panic/strong 正式合并，除非通过年度稳定性和执行压力审计。",
            "",
        ]
    )
    (OUT_DIR / "range_30m_confirm_report_cn.md").write_text("\n".join(report), encoding="utf-8")

    summary = {
        "out_dir": str(OUT_DIR),
        "daily_rows": int(len(base)),
        "confirmed_rows": int(len(confirmed)),
        "confirm_rate": float(len(conf_key) / len(base_key)) if len(base_key) else None,
        "date_min": str(base["entry_date"].min()) if not base.empty else None,
        "date_max": str(base["entry_date"].max()) if not base.empty else None,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    print(json.dumps(build(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
