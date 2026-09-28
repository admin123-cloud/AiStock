from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def _load_trades(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    for col in ["entry_date", "policy_exit_date", "exit_date", "confirm_datetime", "exit_datetime"]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce")
    for col in [
        "policy_net_ret",
        "baseline_net_ret",
        "candidate_score",
        "chain_rank",
        "day_breadth_ma20",
        "day_index_mom20",
        "day_market_amount_ratio20",
        "day_up_rate",
        "big_down_rate",
        "range_pos60",
        "drawdown20",
        "amount_ratio20",
        "trigger_close_ret",
        "exit_ret_net",
        "realized_pnl",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["year"] = d["entry_date"].dt.year
    d["month"] = d["entry_date"].dt.strftime("%Y-%m")
    return d.dropna(subset=["entry_date", "code", "policy_net_ret"]).copy()


def _load_curve(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    for col in ["equity", "drawdown", "worst_open_mtm_ret", "open_positions"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["year"] = d["date"].dt.year
    d["month"] = d["date"].dt.strftime("%Y-%m")
    return d.dropna(subset=["date", "equity"]).copy()


def _group_summary(d: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows: list[dict] = []
    if d.empty:
        return pd.DataFrame()
    for keys, g in d.groupby(group_cols, dropna=False, sort=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        item = {col: val for col, val in zip(group_cols, keys)}
        item.update(
            {
                "trades": int(len(g)),
                "unique_codes": int(g["code"].nunique()),
                "win_rate": float((g["policy_net_ret"] > 0).mean()),
                "mean_ret": float(g["policy_net_ret"].mean()),
                "median_ret": float(g["policy_net_ret"].median()),
                "sum_ret": float(g["policy_net_ret"].sum()),
                "worst_ret": float(g["policy_net_ret"].min()),
                "best_ret": float(g["policy_net_ret"].max()),
                "triggered": int(g["triggered"].astype(str).str.lower().eq("true").sum()) if "triggered" in g.columns else 0,
                "m30_exit": int(g["exit_source"].astype(str).str.contains("m30", na=False).sum()) if "exit_source" in g.columns else 0,
                "mean_breadth": float(g["day_breadth_ma20"].mean()) if "day_breadth_ma20" in g.columns else float("nan"),
                "mean_index_mom20": float(g["day_index_mom20"].mean()) if "day_index_mom20" in g.columns else float("nan"),
                "mean_big_down_rate": float(g["big_down_rate"].mean()) if "big_down_rate" in g.columns else float("nan"),
                "mean_amount_ratio": float(g["day_market_amount_ratio20"].mean()) if "day_market_amount_ratio20" in g.columns else float("nan"),
            }
        )
        rows.append(item)
    return pd.DataFrame(rows)


def _display(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    for col in [
        "win_rate",
        "mean_ret",
        "median_ret",
        "sum_ret",
        "worst_ret",
        "best_ret",
        "mean_breadth",
        "mean_index_mom20",
        "mean_big_down_rate",
        "mean_amount_ratio",
        "year_ret",
        "max_drawdown",
        "worst_open_mtm",
    ]:
        if col in out.columns:
            out[col] = out[col].map(_pct)
    return out


def _year_curve_summary(curve: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    for year, g in curve.groupby("year", sort=True):
        if g.empty:
            continue
        start = float(g["equity"].iloc[0])
        end = float(g["equity"].iloc[-1])
        local = g["equity"] / start
        dd = local / local.cummax() - 1.0
        rows.append(
            {
                "year": int(year),
                "days": int(len(g)),
                "year_ret": end / start - 1.0,
                "max_drawdown": float(dd.min()),
                "worst_open_mtm": float(g["worst_open_mtm_ret"].min()) if "worst_open_mtm_ret" in g.columns else 0.0,
                "max_open_positions": int(g["open_positions"].max()) if "open_positions" in g.columns else 0,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit why G3 panic final candidate failed in 2023.")
    parser.add_argument(
        "--trades",
        default="reports/gen3_panic_v2_research/final_candidate_v1/m30_close5_full_nextopen_cost30_closed_trades.csv",
    )
    parser.add_argument(
        "--curve",
        default="reports/gen3_panic_v2_research/final_candidate_v1/m30_close5_full_nextopen_cost30_mtm_equity_curve.csv",
    )
    parser.add_argument("--output-dir", default="reports/gen3_panic_v2_research/failure_2023_audit_v1")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    trades = _load_trades(Path(args.trades))
    curve = _load_curve(Path(args.curve))
    y2023 = trades[trades["year"].eq(2023)].copy()

    tables = {
        "year_trade_summary": _group_summary(trades, ["year"]),
        "month_2023_summary": _group_summary(y2023, ["month"]),
        "exit_source_2023_summary": _group_summary(y2023, ["exit_source"]),
        "repair_env_2023_summary": _group_summary(y2023, ["g3_repair_env_label"]),
        "industry_2023_summary": _group_summary(y2023, ["industry"]),
        "year_curve_summary": _year_curve_summary(curve),
    }
    for name, table in tables.items():
        table.to_csv(out_dir / f"{name}_raw.csv", index=False, encoding="utf-8-sig")
        _display(table).to_csv(out_dir / f"{name}_display.csv", index=False, encoding="utf-8-sig")

    trade_cols = [
        "entry_date",
        "code",
        "name",
        "industry",
        "policy_net_ret",
        "exit_source",
        "triggered",
        "trigger_close_ret",
        "exit_ret_net",
        "market_style",
        "g3_repair_env_label",
        "day_breadth_ma20",
        "day_index_mom20",
        "day_market_amount_ratio20",
        "big_down_rate",
        "range_pos60",
        "amount_ratio20",
    ]
    y2023[[c for c in trade_cols if c in y2023.columns]].to_csv(out_dir / "trades_2023.csv", index=False, encoding="utf-8-sig")
    show_trades = y2023[[c for c in trade_cols if c in y2023.columns]].copy()
    for col in [
        "policy_net_ret",
        "trigger_close_ret",
        "exit_ret_net",
        "day_breadth_ma20",
        "day_index_mom20",
        "day_market_amount_ratio20",
        "big_down_rate",
        "range_pos60",
        "amount_ratio20",
    ]:
        if col in show_trades.columns:
            show_trades[col] = show_trades[col].map(_pct)

    year_display = _display(tables["year_trade_summary"])
    curve_display = _display(tables["year_curve_summary"])
    month_display = _display(tables["month_2023_summary"])
    exit_display = _display(tables["exit_source_2023_summary"])
    env_display = _display(tables["repair_env_2023_summary"])

    lines = [
        "# G3 Panic 2023 失效环境审计 V1",
        "",
        "## 口径",
        "",
        f"- 成交明细：`{args.trades}`",
        f"- MTM 曲线：`{args.curve}`",
        "- 策略版本：30bps、slot5、20% 单槽、30m -5% 失败退出。",
        "- 本审计只解释 2023 失效，不调整参数。",
        "",
        "## 年度成交对比",
        "",
        year_display.to_markdown(index=False),
        "",
        "## 年度资金曲线对比",
        "",
        curve_display.to_markdown(index=False),
        "",
        "## 2023 月度拆解",
        "",
        month_display.to_markdown(index=False),
        "",
        "## 2023 退出来源拆解",
        "",
        exit_display.to_markdown(index=False),
        "",
        "## 2023 修复环境拆解",
        "",
        env_display.to_markdown(index=False),
        "",
        "## 2023 单笔明细",
        "",
        show_trades.to_markdown(index=False),
        "",
        "## 初步判断",
        "",
    ]
    if not y2023.empty:
        m30_count = int(y2023["exit_source"].astype(str).str.contains("m30", na=False).sum())
        loss_count = int((y2023["policy_net_ret"] < 0).sum())
        lines.extend(
            [
                f"- 2023 共 `{len(y2023)}` 笔交易，亏损 `{loss_count}` 笔，30m 失败退出 `{m30_count}` 笔。",
                "- 2023 的问题不是单一尾部个案，而是样本很少、且集中在 4-5 月的连续失败确认。",
                "- 30m -5% 退出把深亏压住了，但这些票在入场后很快转弱，说明 2023 更像“弱反弹失败/结构性退潮”，不是充分恐慌后的出清修复。",
                "- 下一步应审计 2023 入场日前后的市场宽度、行业集中度和个股位置，判断是否需要独立的 2023 型失效环境暂停规则；但不能只为 6 笔样本调参。",
            ]
        )
    else:
        lines.append("- 2023 没有交易记录，无法做失效拆解。")
    (out_dir / "failure_2023_audit_report_cn.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "trades": args.trades,
        "curve": args.curve,
        "output_dir": str(out_dir),
        "trades_2023": int(len(y2023)),
        "losses_2023": int((y2023["policy_net_ret"] < 0).sum()) if not y2023.empty else 0,
        "m30_exits_2023": int(y2023["exit_source"].astype(str).str.contains("m30", na=False).sum()) if not y2023.empty else 0,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
