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


SOURCE = _report_path() / "gen3_range_30m_volume_acceptance_v1" / "box_stress_accept_h5__cost30" / "closed_trades.csv"
OUT_DIR = _report_path() / "gen3_range_box_stress_accept_failure_audit_v1"


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item: dict[str, Any] = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_daily_context(trades: pd.DataFrame) -> pd.DataFrame:
    codes = sorted(trades["code"].dropna().astype(str).unique().tolist())
    start = pd.to_datetime(trades["entry_date"]).min().strftime("%Y-%m-%d")
    end = pd.to_datetime(trades["entry_date"]).max().strftime("%Y-%m-%d")
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
    daily["entry_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close"]:
        daily[f"daily_{col}"] = pd.to_numeric(daily[col], errors="coerce")
    return daily[["code", "entry_date", "daily_open", "daily_high", "daily_low", "daily_close"]]


def summarize_flag(d: pd.DataFrame, flag: str) -> dict[str, Any]:
    part = d[d[flag]].copy()
    net = pd.to_numeric(part["policy_net_ret"], errors="coerce")
    return {
        "flag": flag,
        "trade_count": int(len(part)),
        "loss_count": int((net < 0).sum()),
        "loss_rate": float((net < 0).mean()) if len(net) else 0.0,
        "avg_ret": float(net.mean()) if len(net) else 0.0,
        "sum_pnl": float(pd.to_numeric(part["realized_pnl"], errors="coerce").sum()) if len(part) else 0.0,
        "worst_ret": float(net.min()) if len(net) else 0.0,
    }


def year_summary(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for year, g in d.groupby(pd.to_datetime(d["entry_date"]).dt.year):
        net = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        rows.append(
            {
                "year": int(year),
                "trade_count": int(len(g)),
                "total_pnl": float(pd.to_numeric(g["realized_pnl"], errors="coerce").sum()),
                "avg_ret": float(net.mean()),
                "loss_rate": float((net < 0).mean()),
                "late_confirm_rate": float(g["late_confirm"].mean()),
                "d0_weak_close_rate": float(g["d0_weak_close"].mean()),
                "d0_giveback_rate": float(g["d0_giveback"].mean()),
            }
        )
    return pd.DataFrame(rows).sort_values("year")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = pd.read_csv(SOURCE)
    trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    trades["confirm_datetime"] = pd.to_datetime(trades["confirm_datetime"], errors="coerce")
    for col in [
        "policy_net_ret",
        "realized_pnl",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_3d",
        "fwd_ret_confirm_to_close_5d",
        "entry_price_adjusted",
        "daily_entry_close",
    ]:
        trades[col] = pd.to_numeric(trades[col], errors="coerce")
    daily = load_daily_context(trades)
    d = trades.merge(daily, on=["code", "entry_date"], how="left")

    d["year"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.year
    d["late_confirm"] = d["confirm_datetime"].dt.strftime("%H:%M:%S").ge("14:30:00")
    d["d0_weak_close"] = d["fwd_ret_confirm_to_close_1d"].le(-0.02)
    d["d1_weak_confirm"] = d["fwd_ret_confirm_to_close_2d"].le(-0.03)
    d["d2_still_weak"] = d["fwd_ret_confirm_to_close_3d"].le(-0.03)
    d["d0_close_below_open"] = d["daily_close"].lt(d["daily_open"])
    d["d0_giveback"] = (d["daily_close"] / d["daily_high"] - 1.0).le(-0.05)
    d["bad_trade"] = d["policy_net_ret"].lt(0)
    d["early_weak_or_giveback"] = d["d0_weak_close"] | d["d1_weak_confirm"] | d["d0_giveback"]

    flag_rows = [
        summarize_flag(d, flag)
        for flag in [
            "late_confirm",
            "d0_weak_close",
            "d1_weak_confirm",
            "d2_still_weak",
            "d0_close_below_open",
            "d0_giveback",
            "early_weak_or_giveback",
        ]
    ]
    flags = pd.DataFrame(flag_rows)
    years = year_summary(d)
    worst = d.sort_values("policy_net_ret").head(20)

    d.to_csv(OUT_DIR / "audited_trades.csv", index=False, encoding="utf-8-sig")
    flags.to_csv(OUT_DIR / "flag_summary.csv", index=False, encoding="utf-8-sig")
    years.to_csv(OUT_DIR / "year_summary.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "worst20_trades.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "loss_rate",
        "avg_ret",
        "worst_ret",
        "late_confirm_rate",
        "d0_weak_close_rate",
        "d0_giveback_rate",
    }
    lines = [
        "# G3 box_stress_accept_h5 失败归因审计 v1",
        "",
        "## 策略名解释",
        "",
        "- `box_stress_accept_h5`：中文是“横盘箱体底部放量承接，持有5日”。它不是强趋势突破买法，而是在箱体底部或市场大跌出清附近，等盘中 30m 放量承接后买入。",
        "- `late_confirm`：确认bar出现在14:30或之后，代表偏尾盘才出现承接。",
        "- `d0_weak_close`：入场确认后，当天收盘相对确认价已经跌超过2%。",
        "- `d1_weak_confirm`：到下一交易日收盘，相对确认价跌超过3%。",
        "- `d0_giveback`：入场日从日内高点回落超过5%，代表冲高回落/突破失败风险。",
        "",
        "## 标签汇总",
        "",
        md_table(flags, pct_cols=pct_cols),
        "",
        "## 年度归因",
        "",
        md_table(years, pct_cols=pct_cols),
        "",
        "## 最差20笔",
        "",
        md_table(
            worst[
                [
                    "entry_date",
                    "code",
                    "name",
                    "confirm_datetime",
                    "policy_net_ret",
                    "realized_pnl",
                    "late_confirm",
                    "d0_weak_close",
                    "d1_weak_confirm",
                    "d0_giveback",
                ]
            ],
            pct_cols={"policy_net_ret"},
        ),
        "",
        "## 下一步口径",
        "",
        "- 如果 `d0_weak_close`、`d1_weak_confirm` 或 `d0_giveback` 能覆盖多数亏损，下一轮验证快速减仓/退出。",
        "- 如果尾盘确认亏损显著，应单独测试14:30后信号降权或次日确认，不直接追尾盘承接。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
