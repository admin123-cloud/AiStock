from __future__ import annotations

from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
V4_DIR = ROOT / "reports" / "gen3_v4_research_package_v1"
D4_DIR = ROOT / "reports" / "gen3_v4_strong_confirm_d3_visible_combo_v1"
GUARDED_DIR = ROOT / "reports" / "gen3_guarded_candidate_package_v1"
OUT_DIR = ROOT / "reports" / "gen3_v4_guarded_decision_matrix_v1"


def pct(v: float | int | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows: list[dict[str, str]] = []
    for _, row in df.iterrows():
        item: dict[str, str] = {}
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


def get_row(df: pd.DataFrame, **conds: str) -> pd.Series:
    m = pd.Series(True, index=df.index)
    for col, value in conds.items():
        m &= df[col].astype(str).eq(str(value))
    d = df[m]
    if d.empty:
        raise ValueError(f"missing row {conds}")
    return d.iloc[0]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    v4 = pd.read_csv(V4_DIR / "g3_v4_research_summary.csv")
    d4 = pd.read_csv(D4_DIR / "summary.csv")
    guarded = pd.read_csv(GUARDED_DIR / "g3_guarded_candidate_summary.csv")
    guarded_stress = pd.read_csv(GUARDED_DIR / "g3_guarded_execution_stress_summary.csv")
    v4_windows = pd.read_csv(V4_DIR / "g3_v4_research_windows.csv")
    d4_windows = pd.read_csv(D4_DIR / "d4_open__cost30" / "window_summary.csv")
    guarded_windows = pd.read_csv(GUARDED_DIR / "g3_guarded_candidate_windows.csv")

    rows: list[dict] = []
    specs = [
        ("V4_H10_margin", get_row(v4, variant="v4_h10_margin", profile="cost30"), "高攻击基准"),
        ("V4_H10_margin_100bps", get_row(v4, variant="v4_h10_margin", profile="cost100"), "高攻击成本压力"),
        ("V4_H10_margin_all_shock2", get_row(v4, variant="v4_h10_margin", profile="cost30_all_shock2"), "高攻击逐笔额外2%冲击"),
        ("D4_visible_confirm", get_row(d4, variant="d4_open", profile="cost30"), "confirm_d3 改 D4 可见执行"),
        ("D4_visible_all_shock2", get_row(d4, variant="d4_open", profile="cost30_all_shock2"), "D4 可见逐笔额外2%冲击"),
        (
            "Guarded_volume5",
            get_row(guarded, guard="strong_breadth_score_volume5_guard"),
            "稳健 guarded 基准",
        ),
        (
            "Guarded_volume5_100bps",
            get_row(guarded_stress, profile="close_100bps"),
            "guarded 成本压力",
        ),
        (
            "Guarded_volume5_nextopen",
            get_row(guarded_stress, profile="nextopen_limitdown_30bps"),
            "guarded 次日开盘/跌停延迟",
        ),
        (
            "Guarded_volume5_haircut2",
            get_row(guarded_stress, profile="nextopen_haircut2_30bps"),
            "guarded 次日开盘再额外2%冲击",
        ),
    ]
    for name, row, note in specs:
        rows.append(
            {
                "candidate": name,
                "note": note,
                "trade_count": int(row.get("trade_count", row.get("trade_count", 0))),
                "total_return": float(row.get("total_return", row.get("total_return", 0))),
                "max_drawdown": float(row.get("max_drawdown", row.get("max_drawdown", 0))),
                "win_rate": float(row.get("win_rate", 0)),
                "avg_trade_return": float(row.get("avg_trade_return", row.get("avg_trade_return", 0))),
                "worst_trade": float(row.get("worst_trade", 0)),
            }
        )
    matrix = pd.DataFrame(rows)
    matrix["pass_normal"] = matrix["total_return"].gt(1.0) & matrix["max_drawdown"].gt(-0.20)
    matrix["pass_cost100"] = matrix["candidate"].isin(["V4_H10_margin_100bps", "Guarded_volume5_100bps"]) & matrix[
        "total_return"
    ].gt(0.5)
    matrix["pass_tail2"] = matrix["candidate"].str.contains("shock2|haircut2") & matrix["total_return"].gt(0.0)
    matrix.to_csv(OUT_DIR / "decision_matrix.csv", index=False, encoding="utf-8-sig")

    # Normalize window schemas.
    win_rows: list[dict] = []
    for _, r in v4_windows[v4_windows["variant"].eq("v4_h10_margin") & v4_windows["profile"].eq("cost30")].iterrows():
        win_rows.append(
            {
                "candidate": "V4_H10_margin",
                "window": r["window"],
                "return": r["return"],
                "max_drawdown": r["max_drawdown"],
                "trade_count": r["trade_count"],
                "win_rate": r["win_rate"],
            }
        )
    for _, r in d4_windows.iterrows():
        win_rows.append(
            {
                "candidate": "D4_visible_confirm",
                "window": r["window"],
                "return": r["return"],
                "max_drawdown": r["max_drawdown"],
                "trade_count": r["trade_count"],
                "win_rate": r["win_rate"],
            }
        )
    gw = guarded_windows[guarded_windows["guard"].eq("strong_breadth_score_volume5_guard")]
    for _, r in gw.iterrows():
        win_rows.append(
            {
                "candidate": "Guarded_volume5",
                "window": r["window"],
                "return": r["return"],
                "max_drawdown": r["max_drawdown"],
                "trade_count": r["trade_count"],
                "win_rate": r["win_rate"],
            }
        )
    windows = pd.DataFrame(win_rows)
    windows.to_csv(OUT_DIR / "window_matrix.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "return"}
    lines = [
        "# G3 V4 与 guarded 攻守取舍审计 v1",
        "",
        "## 结论",
        "",
        "- V4 H10 是当前攻击性最高的研究版本，但 all-chain 逐笔额外 2% 冲击不通过。",
        "- D4 可见执行证明 D3 代理退出有水分，正常成本仍有收益，但极端冲击更差。",
        "- guarded 版牺牲大量攻击性换稳定性，100bps/次日开盘压力更可接受，但极端 2% 冲击仍不通过。",
        "- 下一步不应继续调 D3/30m 单阈值退出；更合理的是从 V4 H10 的 strong_main 做“入场质量 + 仓位路径”分层，目标是在不砍掉 strong 收益核心的情况下压 all-chain 冲击。",
        "",
        "## 决策矩阵",
        "",
        md_table(matrix, pct_cols=pct_cols),
        "",
        "## 分窗口矩阵",
        "",
        md_table(windows, pct_cols={"return", "max_drawdown", "win_rate"}),
        "",
        "## 下一步目标",
        "",
        "- 基于 V4 H10 strong_main 成交明细，审计 all_shock2 下由哪些 entry_date/code/source/score 造成复利路径坍塌。",
        "- 测试只影响 strong_main 的仓位路径：例如高质量全仓、脆弱强势半仓，而不是删除整条强势链路。",
        "- down_panic/range_gap 暂时保持 V4 H10，不把弱势/横盘链路一起调参，避免过拟合。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()
