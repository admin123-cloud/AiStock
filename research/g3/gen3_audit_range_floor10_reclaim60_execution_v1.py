from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = _report_path() / "gen3_range_ice_recent3_threshold_stability_v1" / "floor10_reclaim60__cost30" / "closed_trades.csv"
OUT_DIR = _report_path() / "gen3_range_floor10_reclaim60_execution_audit_v1"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, max_rows: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    view = df.copy()
    if max_rows is not None:
        view = view.head(max_rows)
    rows: list[dict[str, Any]] = []
    for _, row in view.iterrows():
        item: dict[str, Any] = {}
        for col in view.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_trades() -> pd.DataFrame:
    d = pd.read_csv(SOURCE)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    for col in [
        "entry_price_adjusted",
        "policy_net_ret",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "bar_ret",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["code", "entry_date", "confirm_datetime", "entry_price_adjusted", "policy_net_ret"]).copy()


def load_daily_ohlc(trades: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(trades["code"].astype(str).unique().tolist())
    start = pd.to_datetime(trades["entry_date"]).min().strftime("%Y-%m-%d")
    end = (pd.to_datetime(trades["entry_date"]).max() + pd.Timedelta(days=15)).strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 500):
        batch = codes[i : i + 500]
        quoted = ", ".join(f"'{code}'" for code in batch)
        part = clickhouse_query_df(
            f"""
            SELECT code, trade_date, open, high, low, close
            FROM kline_daily
            WHERE code IN ({quoted})
              AND trade_date BETWEEN toDate(%(start)s) AND toDate(%(end)s)
            ORDER BY code, trade_date
            """,
            {"start": start, "end": end},
        )
        if not part.empty:
            parts.append(part)
    daily = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if daily.empty:
        return daily
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close"]:
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    daily = daily.dropna(subset=["code", "trade_date", "open", "high", "low", "close"])
    daily = daily.sort_values(["code", "trade_date"]).reset_index(drop=True)
    g = daily.groupby("code", sort=False)
    for offset in [1, 2, 3]:
        daily[f"d{offset}_date"] = g["trade_date"].shift(-offset)
        daily[f"d{offset}_open"] = g["open"].shift(-offset)
        daily[f"d{offset}_close"] = g["close"].shift(-offset)
    return daily.rename(columns={"trade_date": "entry_date"})


def enrich(trades: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "code",
        "entry_date",
        "open",
        "high",
        "low",
        "close",
        "d1_date",
        "d1_open",
        "d1_close",
        "d2_date",
        "d2_open",
        "d2_close",
        "d3_date",
        "d3_open",
        "d3_close",
    ]
    d = trades.merge(daily[cols], on=["code", "entry_date"], how="left")
    d["confirm_time"] = d["confirm_datetime"].dt.strftime("%H:%M")
    d["late_confirm"] = d["confirm_datetime"].dt.time >= pd.Timestamp("14:30").time()
    d["ultra_late_confirm"] = d["confirm_datetime"].dt.time >= pd.Timestamp("15:00").time()
    d["d0_high_to_close"] = d["close"] / d["high"] - 1.0
    d["d0_confirm_to_close"] = d["close"] / d["entry_price_adjusted"] - 1.0
    d["d1_open_ret"] = d["d1_open"] / d["entry_price_adjusted"] - 1.0
    d["d1_close_ret"] = d["d1_close"] / d["entry_price_adjusted"] - 1.0
    d["d2_open_ret"] = d["d2_open"] / d["entry_price_adjusted"] - 1.0
    d["d2_close_ret"] = d["d2_close"] / d["entry_price_adjusted"] - 1.0
    d["d3_open_ret"] = d["d3_open"] / d["entry_price_adjusted"] - 1.0
    d["d0_giveback_5p"] = d["d0_high_to_close"].le(-0.05)
    d["d0_weak_close"] = d["d0_confirm_to_close"].le(-0.02)
    d["d1_gapdown_3p"] = d["d1_open_ret"].le(-0.03)
    d["d1_weak_confirm"] = d["d1_close_ret"].le(-0.03)
    d["d2_still_weak"] = d["d2_close_ret"].le(-0.03)
    d["early_weak_any"] = d[["d0_weak_close", "d1_gapdown_3p", "d1_weak_confirm", "d2_still_weak"]].any(axis=1)
    return d


def flag_summary(d: pd.DataFrame) -> pd.DataFrame:
    flags = [
        ("late_confirm", "14:30或之后确认，可能接近尾盘成交"),
        ("ultra_late_confirm", "15:00确认，真实成交可能要到下一交易日"),
        ("d0_giveback_5p", "入场日从日内高点回落超过5%，属于冲高回落/突破失败警戒"),
        ("d0_weak_close", "入场日至收盘相对确认价跌超2%，属于当日弱确认"),
        ("d1_gapdown_3p", "下一交易日开盘相对确认价低开超3%，属于隔夜冲击"),
        ("d1_weak_confirm", "下一交易日收盘相对确认价跌超3%，属于D1早期弱确认"),
        ("d2_still_weak", "第二个交易日收盘相对确认价仍跌超3%，属于D2持续弱确认"),
        ("early_weak_any", "D0/D1/D2任一早弱信号"),
    ]
    rows: list[dict[str, Any]] = []
    total_sum = float(d["policy_net_ret"].sum())
    for flag, desc in flags:
        m = d[flag].fillna(False)
        hit = d[m].copy()
        miss = d[~m].copy()
        rows.append(
            {
                "flag": flag,
                "中文解释": desc,
                "hit_count": int(len(hit)),
                "hit_rate": float(len(hit) / len(d)) if len(d) else 0.0,
                "hit_avg_ret": float(hit["policy_net_ret"].mean()) if len(hit) else 0.0,
                "hit_sum_ret": float(hit["policy_net_ret"].sum()) if len(hit) else 0.0,
                "miss_avg_ret": float(miss["policy_net_ret"].mean()) if len(miss) else 0.0,
                "miss_sum_ret": float(miss["policy_net_ret"].sum()) if len(miss) else 0.0,
                "hit_loss_count": int((hit["policy_net_ret"] < 0).sum()) if len(hit) else 0,
                "hit_worst_ret": float(hit["policy_net_ret"].min()) if len(hit) else 0.0,
                "total_sum_share": float(hit["policy_net_ret"].sum() / total_sum) if total_sum else 0.0,
            }
        )
    return pd.DataFrame(rows)


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    x = d.copy()
    x["year"] = pd.to_datetime(x["entry_date"], errors="coerce").dt.year
    rows: list[dict[str, Any]] = []
    for year, g in x.groupby("year"):
        rows.append(
            {
                "year": int(year),
                "trade_count": int(len(g)),
                "sum_ret": float(g["policy_net_ret"].sum()),
                "avg_ret": float(g["policy_net_ret"].mean()),
                "win_rate": float((g["policy_net_ret"] > 0).mean()),
                "early_weak_count": int(g["early_weak_any"].sum()),
                "late_confirm_count": int(g["late_confirm"].sum()),
                "worst_ret": float(g["policy_net_ret"].min()),
            }
        )
    return pd.DataFrame(rows)


def worst_trades(d: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    keep = [
        "entry_date",
        "code",
        "name",
        "confirm_time",
        "policy_net_ret",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "d0_high_to_close",
        "d0_confirm_to_close",
        "d1_open_ret",
        "d1_close_ret",
        "d2_close_ret",
        "late_confirm",
        "ultra_late_confirm",
        "d0_giveback_5p",
        "d0_weak_close",
        "d1_gapdown_3p",
        "d1_weak_confirm",
        "d2_still_weak",
    ]
    return d.sort_values("policy_net_ret").head(n)[keep].copy()


def write_report(d: pd.DataFrame, flags: pd.DataFrame, years: pd.DataFrame, worst: pd.DataFrame) -> None:
    pct_cols = {
        "policy_net_ret",
        "range_pos60",
        "close_position",
        "amount_ratio3",
        "d0_high_to_close",
        "d0_confirm_to_close",
        "d1_open_ret",
        "d1_close_ret",
        "d2_close_ret",
        "hit_rate",
        "hit_avg_ret",
        "hit_sum_ret",
        "miss_avg_ret",
        "miss_sum_ret",
        "hit_worst_ret",
        "total_sum_share",
        "sum_ret",
        "avg_ret",
        "win_rate",
        "worst_ret",
    }
    early = flags.loc[flags["flag"].eq("early_weak_any")].iloc[0]
    d1 = flags.loc[flags["flag"].eq("d1_weak_confirm")].iloc[0]
    d0gb = flags.loc[flags["flag"].eq("d0_giveback_5p")].iloc[0]
    late = flags.loc[flags["flag"].eq("late_confirm")].iloc[0]
    conclusion = [
        f"- 这批 `floor10_reclaim60` 一共 {len(d)} 笔，当前 30bps 口径单笔净收益合计为 {pct(d['policy_net_ret'].sum())}，平均单笔 {pct(d['policy_net_ret'].mean())}。",
        f"- `early_weak_any` 命中 {int(early['hit_count'])} 笔，意思是 D0/D1/D2 任一早期走弱；命中组平均 {pct(early['hit_avg_ret'])}，未命中组平均 {pct(early['miss_avg_ret'])}。",
        f"- `d1_weak_confirm` 命中 {int(d1['hit_count'])} 笔，意思是买入后下一交易日收盘已经比确认价跌超3%；这是最需要继续验证的快速退出/减仓信号。",
        f"- `d0_giveback_5p` 命中 {int(d0gb['hit_count'])} 笔，意思是入场日从日内高点回落超过5%；如果命中少，说明“冲高回落/突破失败”不是这条源的主要风险根。",
        f"- `late_confirm` 命中 {int(late['hit_count'])} 笔，意思是 14:30 或之后才确认；需要单独看 15:00 交易能否真实成交，不应默认等同于盘中可买。",
    ]
    lines = [
        "# G3 floor10_reclaim60 执行质量审计 v1",
        "",
        "## 策略名解释",
        "",
        "- `floor10_reclaim60`：中文是“冰点后3日窗口里，箱体位置不高于10%，且日线收盘修复不低于60%的横盘箱体底部30m承接买法”。它不是强势突破策略，而是横盘/箱体底部的恐慌后修复源。",
        "- `late_confirm`：14:30 或之后才出现确认，真实执行上可能接近尾盘。",
        "- `ultra_late_confirm`：15:00 才出现确认，真实交易一般要额外审计是否只能次日成交。",
        "- `d0_giveback_5p`：入场日从日内高点回落超过5%，用来代表“冲高回落/突破失败”警戒。",
        "- `d1_weak_confirm`：买入后下一交易日收盘相对确认价跌超3%，用来代表 D1 早期弱确认。",
        "- `d2_still_weak`：第二个交易日收盘相对确认价仍跌超3%，用来代表 D2 持续弱确认。",
        "",
        "## 关键结论",
        "",
        *conclusion,
        "",
        "## 风险标记汇总",
        "",
        md_table(flags, pct_cols=pct_cols),
        "",
        "## 年度分布",
        "",
        md_table(years, pct_cols=pct_cols),
        "",
        "## 最差20笔成交",
        "",
        md_table(worst, pct_cols=pct_cols),
        "",
        "## 下一步判断",
        "",
        "- 如果 `d1_weak_confirm` 或 `d2_still_weak` 明显拖累收益，下一步应测试 D1/D2 减仓或快速退出，而不是继续调止盈止损阈值。",
        "- 如果 `d0_giveback_5p` 命中少或拖累不明显，说明“入场日冲高回落/突破失败”不是这条横盘源的核心问题，可以放到次级规则。",
        "- 如果 `late_confirm`/`ultra_late_confirm` 占比高，必须先做真实成交口径：尾盘确认转次日开盘成交压力测试。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_trades()
    daily = load_daily_ohlc(trades)
    enriched = enrich(trades, daily)
    flags = flag_summary(enriched)
    years = year_summary(enriched)
    worst = worst_trades(enriched)
    enriched.to_csv(OUT_DIR / "audited_trades.csv", index=False, encoding="utf-8-sig")
    flags.to_csv(OUT_DIR / "flag_summary.csv", index=False, encoding="utf-8-sig")
    years.to_csv(OUT_DIR / "year_summary.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "worst20_trades.csv", index=False, encoding="utf-8-sig")
    write_report(enriched, flags, years, worst)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
