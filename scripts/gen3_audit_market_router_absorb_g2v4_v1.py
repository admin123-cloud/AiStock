from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "gen3_market_router_absorb_g2v4_v1"

G2_RUN = ROOT / "reports" / "gen2_v2_complete_strategy" / "runs" / "official" / "full"
G2_SIGNALS = G2_RUN / "signals.csv"
G2_TRADES = G2_RUN / "trades.csv"
G2_SUMMARY = ROOT / "reports" / "gen2_v2_complete_strategy" / "summary.csv"
OPEN_STATE = ROOT / "reports" / "gen2_open_state_research_full" / "g2_open_state_daily.csv"
G3_RANGE_CLOSED = (
    ROOT
    / "reports"
    / "gen3_range_first_panic_d1_accept_stability_v1"
    / "d1_reclaim60_amt12__d3neg__cost30"
    / "closed_trades.csv"
)

SIGNAL_START = "2024-07-09"
SIGNAL_END = "2026-06-04"
G3_FULL_START = "2020-01-01"
G3_FULL_END = "2026-06-04"


def pct(x: Any) -> str:
    if x is None or pd.isna(x):
        return "--"
    return f"{float(x) * 100:+.2f}%"


def money(x: Any) -> str:
    if x is None or pd.isna(x):
        return "--"
    return f"{float(x):,.0f}"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, money_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    money_cols = money_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif col in money_cols:
                item[col] = money(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def max_drawdown_from_pnl(pnl: pd.Series, initial: float = 150_000.0) -> float:
    if pnl.empty:
        return 0.0
    equity = initial + pnl.fillna(0.0).cumsum()
    return float((equity / equity.cummax() - 1.0).min())


def load_g2_lots() -> pd.DataFrame:
    trades = pd.read_csv(G2_TRADES, low_memory=False)
    signals = pd.read_csv(G2_SIGNALS, low_memory=False)
    trades["buy_date"] = pd.to_datetime(trades["buy_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    trades["sell_date"] = pd.to_datetime(trades["sell_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    signals["entry_date"] = pd.to_datetime(signals["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    signals["trade_date"] = pd.to_datetime(signals["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")

    group_cols = ["code", "name", "buy_date", "buy_datetime", "buy_price", "v4_rank", "v4_score"]
    lots = (
        trades.groupby(group_cols, dropna=False)
        .agg(
            sell_date=("sell_date", "max"),
            capital=("capital", "first"),
            pnl=("pnl", "sum"),
            return_sum=("return", "sum"),
            exit_reason=("exit_reason", lambda s: "|".join(sorted(set(map(str, s))))),
            sell_legs=("code", "size"),
        )
        .reset_index()
    )
    lots["buy_date"] = pd.to_datetime(lots["buy_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    sig_cols = [
        "entry_date",
        "trade_date",
        "code",
        "source_family",
        "signal_family",
        "g2_v2_buy_logic",
        "g2_open_state",
        "sector_strong",
        "sector_score_bonus",
        "l3_rise",
        "l3_s3",
        "l2_s3",
        "confirm_datetime",
    ]
    sig = signals[[c for c in sig_cols if c in signals.columns]].copy()
    lots = lots.merge(sig, left_on=["buy_date", "code"], right_on=["entry_date", "code"], how="left")
    lots["source_family"] = lots["source_family"].fillna("unknown").astype(str)
    lots["g2_open_state"] = lots["g2_open_state"].fillna("UNKNOWN").astype(str)
    lots["lot_return"] = pd.to_numeric(lots["pnl"], errors="coerce") / pd.to_numeric(lots["capital"], errors="coerce")
    lots["buy_date_ts"] = pd.to_datetime(lots["buy_date"], errors="coerce")
    return lots


def summarize_group(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    rows = []
    for key, g in df.groupby(by, dropna=False):
        if not isinstance(key, tuple):
            key = (key,)
        ret = pd.to_numeric(g["lot_return"], errors="coerce")
        pnl = pd.to_numeric(g["pnl"], errors="coerce")
        row = {col: val for col, val in zip(by, key)}
        row.update(
            {
                "lot_count": int(len(g)),
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
                "avg_lot_return": float(ret.mean()) if len(ret) else 0.0,
                "worst_lot_return": float(ret.min()) if len(ret) else 0.0,
                "best_lot_return": float(ret.max()) if len(ret) else 0.0,
                "pnl": float(pnl.sum()),
                "pnl_share": 0.0,
                "pseudo_return_on_150k": float(pnl.sum() / 150_000.0),
            }
        )
        rows.append(row)
    out = pd.DataFrame(rows)
    if not out.empty:
        total = float(pd.to_numeric(out["pnl"], errors="coerce").sum())
        out["pnl_share"] = out["pnl"] / total if total else 0.0
        out = out.sort_values("pnl", ascending=False)
    return out


def summarize_window(df: pd.DataFrame) -> pd.DataFrame:
    windows = {
        "full_2024_07_09_2026_06_04": ("2024-07-09", "2026-06-04"),
        "train_2024_07_09_2025_03_31": ("2024-07-09", "2025-03-31"),
        "valid_2025_04_01_2025_12_31": ("2025-04-01", "2025-12-31"),
        "blind_2026_01_01_2026_06_04": ("2026-01-01", "2026-06-04"),
    }
    rows = []
    for name, (start, end) in windows.items():
        g = df[df["buy_date_ts"].between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        ret = pd.to_numeric(g["lot_return"], errors="coerce")
        pnl = pd.to_numeric(g["pnl"], errors="coerce")
        rows.append(
            {
                "window": name,
                "lot_count": int(len(g)),
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
                "avg_lot_return": float(ret.mean()) if len(ret) else 0.0,
                "worst_lot_return": float(ret.min()) if len(ret) else 0.0,
                "pnl": float(pnl.sum()),
                "pseudo_return_on_150k": float(pnl.sum() / 150_000.0),
                "pnl_drawdown_proxy": max_drawdown_from_pnl(pnl),
            }
        )
    return pd.DataFrame(rows)


def load_g3_range_reference() -> pd.DataFrame:
    d = pd.read_csv(G3_RANGE_CLOSED, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce")
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    d["realized_pnl"] = pd.to_numeric(d["realized_pnl"], errors="coerce")
    d["year"] = d["entry_date"].dt.year
    return d


def g3_reference_summary(d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, start, end in [
        ("g3_range_full_2020_2026", "2020-01-01", "2026-06-04"),
        ("g3_range_overlap_2024_07_09_2026_06_04", "2024-07-09", "2026-06-04"),
    ]:
        g = d[d["entry_date"].between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        ret = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        rows.append(
            {
                "window": name,
                "trade_count": int(len(g)),
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
                "avg_trade_return": float(ret.mean()) if len(ret) else 0.0,
                "worst_trade": float(ret.min()) if len(ret) else 0.0,
                "pnl": float(pd.to_numeric(g["realized_pnl"], errors="coerce").sum()),
                "pseudo_return_on_150k": float(pd.to_numeric(g["realized_pnl"], errors="coerce").sum() / 150_000.0),
            }
        )
    return pd.DataFrame(rows)


def write_report(
    overall: pd.DataFrame,
    by_source: pd.DataFrame,
    by_state_source: pd.DataFrame,
    by_window: pd.DataFrame,
    top: pd.DataFrame,
    worst: pd.DataFrame,
    g3_ref: pd.DataFrame,
) -> None:
    pct_cols = {
        "win_rate",
        "avg_lot_return",
        "worst_lot_return",
        "best_lot_return",
        "pnl_share",
        "pseudo_return_on_150k",
        "pnl_drawdown_proxy",
        "avg_trade_return",
        "worst_trade",
        "lot_return",
    }
    money_cols = {"pnl", "capital"}
    lines = [
        "# G3 市场路由吸收 G2 v4 强势买法审计 v1",
        "",
        "## 回测与数据范围",
        f"- G2 v4 正式强势样本：{SIGNAL_START} 至 {SIGNAL_END}。",
        f"- G3 横盘参考样本：{G3_FULL_START} 至 {G3_FULL_END}，并单独统计与 G2 重叠窗口。",
        "- 本轮不是重新发明强势买点，而是拆解 G2 v4 强势收益来源，为 G3 市场路由提供强势链路。",
        "- G2 交易有分批止盈，本报告先聚合成 lot，即同一股票同一买入日同一买入价视作一笔开仓。",
        "",
        "## 英文策略名解释",
        "- `g2_v2_complete`：G2 第二代完整策略，包含 volume5 主线和 big_bull 二次突破主线。",
        "- `volume5_keep80_runup`：量能主线，保留 volume5 弱过滤候选，并要求从60日低位涨幅不过高。",
        "- `big_bull_rebreak_2_5d`：大阳线后 2-5 日二次突破，要求盘中强度和细分板块扩散。",
        "- `source_family=volume5`：G2 强势量能持续买法。",
        "- `source_family=big_bull`：G2 强势二次突破买法。",
        "- `g2_open_state=NORMAL/AGGRESSIVE/OFF`：G2 原有开仓状态。NORMAL/AGGRESSIVE 更接近强势或可进攻环境，OFF 代表不应主攻。",
        "- `balanced_77`：G3 横盘参考候选，D1二次30m承接 + 日线修复>=60% + 量能不过热。",
        "",
        "## G2 v4 官方回测摘要",
        md_table(overall, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## G2 v4 按强势买法拆解",
        md_table(by_source, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## G2 v4 按市场状态 + 买法拆解",
        md_table(by_state_source, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## G2 v4 分窗 lot 贡献",
        md_table(by_window, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## G3 横盘候选参考",
        md_table(g3_ref, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## G2 v4 最大盈利 lot",
        md_table(top, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## G2 v4 最大亏损 lot",
        md_table(worst, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 判断",
        "- G3 后续不应继续用横盘/恐慌候选去解释 2024-2026 的主收益。",
        "- 强势环境必须直接启用 G2 v4 的 volume5 与 big_bull 思路；G3 的 range/ice/panic 应作为横盘、弱反弹、下跌环境的补充链路。",
        "- 下一步应做真正的路由回测：强势状态走 G2 v4，非强势状态才走 G3 横盘/恐慌候选，并处理同日冲突、仓位上限、冷却期与压力口径。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    lots = load_g2_lots()
    lots.to_csv(OUT_DIR / "g2_v4_lots_with_source_state.csv", index=False, encoding="utf-8-sig")
    overall = pd.read_csv(G2_SUMMARY, low_memory=False)
    overall.to_csv(OUT_DIR / "g2_v4_official_summary.csv", index=False, encoding="utf-8-sig")
    by_source = summarize_group(lots, ["source_family"])
    by_state_source = summarize_group(lots, ["g2_open_state", "source_family"])
    by_window = summarize_window(lots)
    by_source.to_csv(OUT_DIR / "g2_v4_by_source_family.csv", index=False, encoding="utf-8-sig")
    by_state_source.to_csv(OUT_DIR / "g2_v4_by_open_state_source.csv", index=False, encoding="utf-8-sig")
    by_window.to_csv(OUT_DIR / "g2_v4_by_window_lot.csv", index=False, encoding="utf-8-sig")
    keep = [
        "buy_date",
        "sell_date",
        "code",
        "name",
        "source_family",
        "g2_open_state",
        "signal_family",
        "g2_v2_buy_logic",
        "capital",
        "pnl",
        "lot_return",
        "exit_reason",
    ]
    top = lots.sort_values("pnl", ascending=False).head(20)[keep]
    worst = lots.sort_values("pnl", ascending=True).head(20)[keep]
    top.to_csv(OUT_DIR / "g2_v4_top_lots.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "g2_v4_worst_lots.csv", index=False, encoding="utf-8-sig")
    g3_ref = g3_reference_summary(load_g3_range_reference())
    g3_ref.to_csv(OUT_DIR / "g3_range_reference_summary.csv", index=False, encoding="utf-8-sig")
    write_report(overall, by_source, by_state_source, by_window, top, worst, g3_ref)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
