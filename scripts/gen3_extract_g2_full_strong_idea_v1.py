from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "gen3_g2_full_strong_idea_extraction_v1"

G2_MODULE_SUMMARY = (
    ROOT
    / "reports"
    / "gen2_v2_complete_strategy_2020"
    / "diagnostics"
    / "market_style_alpha_modules_v1"
    / "summary.csv"
)
G2_BREAKOUT_ADDON = (
    ROOT
    / "reports"
    / "gen2_v2_complete_strategy_2020"
    / "diagnostics"
    / "breakout_style_addon"
    / "summary.json"
)
G2_ROUTER = (
    ROOT
    / "reports"
    / "gen2_v2_complete_strategy_2020"
    / "diagnostics"
    / "market_style_buy_router_v2.json"
)
G3_STRONG_STRESS = (
    ROOT
    / "reports"
    / "gen3_strong_volume5_risk_layer_execution_stress_v1"
    / "risk_layer_execution_stress_summary_raw.csv"
)
G3_FINAL_METRICS = (
    ROOT
    / "reports"
    / "gen3_final_candidate_package_v1"
    / "final_candidate_metrics.csv"
)


def pct(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def as_num(value: float | int | None, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):.{digits}f}"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows: list[dict[str, object]] = []
    for _, row in df.iterrows():
        item: dict[str, object] = {}
        for col in df.columns:
            val = row[col]
            if col in pct_cols:
                item[col] = pct(val)
            elif isinstance(val, float):
                item[col] = as_num(val, 4)
            else:
                item[col] = "" if pd.isna(val) else val
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_g2_modules() -> pd.DataFrame:
    df = pd.read_csv(G2_MODULE_SUMMARY)
    keep = [
        "variant",
        "window",
        "signal_count",
        "trade_count",
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
    ]
    return df[keep].copy()


def load_big_bull_addon() -> pd.DataFrame:
    raw = json.loads(G2_BREAKOUT_ADDON.read_text(encoding="utf-8"))
    df = pd.DataFrame(raw)
    keep = [
        "variant",
        "window",
        "signal_count",
        "trade_count",
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
    ]
    return df[keep].copy()


def load_g3_strong() -> pd.DataFrame:
    df = pd.read_csv(G3_STRONG_STRESS)
    df = df[
        (df["window"].eq("full"))
        & (df["cost_bps"].isin([30.0, 50.0, 100.0]))
        & (
            (df["policy"].eq("half_d2_le0_then_d3") & df["variant"].eq("next_open_second_leg"))
            | (df["policy"].eq("half_d2_le0_then_d3") & df["variant"].eq("same_close"))
            | (df["policy"].eq("confirm_d3_full") & df["variant"].eq("same_close"))
        )
    ].copy()
    df["name"] = df["policy"] + " / " + df["variant"] + " / " + df["cost_bps"].astype(int).astype(str) + "bps"
    return df[
        [
            "name",
            "closed",
            "total_ret",
            "max_drawdown",
            "win_rate",
            "mean_trade_ret",
            "worst_trade",
            "bad10_rate",
        ]
    ].rename(
        columns={
            "closed": "trade_count",
            "total_ret": "total_return",
            "mean_trade_ret": "avg_trade_return",
        }
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    g2_modules = load_g2_modules()
    g2_full = g2_modules[g2_modules["window"].eq("full")].copy()
    g2_windows = g2_modules[g2_modules["variant"].isin(["trend_breakout", "router_all_modules"])].copy()
    big_bull = load_big_bull_addon()
    big_bull_full = big_bull[big_bull["window"].eq("full")].copy()
    g3_strong = load_g3_strong()
    router = json.loads(G2_ROUTER.read_text(encoding="utf-8"))

    g2_full.to_csv(OUT_DIR / "g2_market_style_module_full.csv", index=False, encoding="utf-8-sig")
    g2_windows.to_csv(OUT_DIR / "g2_trend_router_window_compare.csv", index=False, encoding="utf-8-sig")
    big_bull_full.to_csv(OUT_DIR / "g2_big_bull_addon_full.csv", index=False, encoding="utf-8-sig")
    g3_strong.to_csv(OUT_DIR / "g3_current_strong_compare.csv", index=False, encoding="utf-8-sig")

    trend_full = g2_full[g2_full["variant"].eq("trend_breakout")].iloc[0].to_dict()
    router_full = g2_full[g2_full["variant"].eq("router_all_modules")].iloc[0].to_dict()
    weak_full = g2_full[g2_full["variant"].eq("weak_recovery_volume5")].iloc[0].to_dict()
    range_full = g2_full[g2_full["variant"].eq("range_bottom_icepoint")].iloc[0].to_dict()
    panic_full = g2_full[g2_full["variant"].eq("panic_reversal_probe")].iloc[0].to_dict()

    routes = router.get("routes", [])
    main_up = next((r for r in routes if r.get("market_style") == "main_up"), {})
    weak_recovery = next((r for r in routes if r.get("market_style") == "weak_recovery"), {})

    findings = [
        {
            "item": "strong_idea_to_absorb",
            "conclusion": "G2 full 的主要有效思想是强势主升时集中使用 volume5_keep80_runup + sector_score_bonus，并允许更完整的趋势持有。",
            "evidence": f"trend_breakout full {pct(trend_full['total_return'])}, max_dd {pct(trend_full['max_drawdown'])}, trades {int(trend_full['trade_count'])}",
        },
        {
            "item": "not_to_copy_big_bull_directly",
            "conclusion": "big_bull 可以保留为二次突破研究方向，但不能直接从 G2 full 继承为正式链路。",
            "evidence": "breakout addon full 回撤超过 19%，且此前归因显示 official full 并未真实让 big_bull 进入正式成交集。",
        },
        {
            "item": "range_weak_need_rebuild",
            "conclusion": "G2 旧的 range/panic/weak 模块不能作为 G3 震荡和下跌周期的答案。",
            "evidence": f"weak_recovery full {pct(weak_full['total_return'])}; range full {pct(range_full['total_return'])}; panic probe full {pct(panic_full['total_return'])}",
        },
        {
            "item": "g3_strong_missing_attack",
            "conclusion": "G3 当前 strong 为了执行压力和风险层做了大量削弱，收益明显低于 G2 trend_breakout 的强势思想原型。",
            "evidence": "G3 strong next-open 30bps full 约 +113.87%，同口径 same-close 约 +213%，而 G2 trend_breakout full 为 +335.55%。",
        },
    ]
    (OUT_DIR / "extraction_findings.json").write_text(
        json.dumps(findings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report = [
        "# G3 吸收 G2 Full 强势赚钱思想 V1",
        "",
        "## 本步目标",
        "",
        "不是复刻 G2 full 参数，而是抽取它在强势市场中真正赚钱的结构，并判断哪些可以进入 G3，哪些必须重建。",
        "",
        "## 核心结论",
        "",
        "- 可以吸收：`volume5_keep80_runup + sector_score_bonus` 的强势资金延续思想。",
        "- 可以吸收：强势主升市场里更高进攻权重，不应被 panic 防守权重长期压成 50%。",
        "- 可以吸收：趋势持有和 30m/前低退出组合，但要重新做实盘可见性与执行压力审计。",
        "- 不能照搬：`big_bull` 作为正式补仓源。它在旧诊断中能补 2026，但 train/full 回撤破坏目标。",
        "- 不能照搬：G2 旧的 range、panic、weak 模块。它们在旧候选池中样本太少或收益不稳，必须重新定义独立候选源。",
        "",
        "## G2 市场风格模块 Full 对比",
        "",
        md_table(
            g2_full[
                [
                    "variant",
                    "signal_count",
                    "trade_count",
                    "total_return",
                    "max_drawdown",
                    "win_rate",
                    "avg_trade_return",
                ]
            ],
            {"total_return", "max_drawdown", "win_rate", "avg_trade_return"},
        ),
        "",
        "解读：G2 的 full 高收益几乎完全来自 `trend_breakout`，也就是强势环境里的 volume5 主线。旧的 weak/range/panic 不是可直接继承的挣钱源。",
        "",
        "## G2 Trend 与 Router 分窗口",
        "",
        md_table(
            g2_windows[
                [
                    "variant",
                    "window",
                    "signal_count",
                    "trade_count",
                    "total_return",
                    "max_drawdown",
                    "win_rate",
                    "avg_trade_return",
                ]
            ],
            {"total_return", "max_drawdown", "win_rate", "avg_trade_return"},
        ),
        "",
        "解读：`trend_breakout` 在 train、valid、blind 都为正，这说明它不是单纯 2026 反推出来的孤立现象。但 G3 不能直接搬参数，必须把它拆成可见信号、仓位路由、退出代理三层。",
        "",
        "## G2 强势路由思想",
        "",
        f"- main_up 条件：`{main_up.get('condition', '')}`",
        f"- main_up 买法：`{main_up.get('buy_logic', '')}`",
        f"- main_up 权重：`{main_up.get('position_weight', '')}`",
        f"- weak_recovery 条件：`{weak_recovery.get('condition', '')}`",
        f"- weak_recovery 买法：`{weak_recovery.get('buy_logic', '')}`",
        f"- weak_recovery 权重：`{weak_recovery.get('position_weight', '')}`",
        "",
        "这部分是 G3 应吸收的“思想”：强势用资金延续，弱修复降权试错，失败修复和下跌不硬买。",
        "",
        "## Big Bull 补仓诊断",
        "",
        md_table(
            big_bull_full[
                [
                    "variant",
                    "signal_count",
                    "trade_count",
                    "total_return",
                    "max_drawdown",
                    "win_rate",
                    "avg_trade_return",
                ]
            ],
            {"total_return", "max_drawdown", "win_rate", "avg_trade_return"},
        ),
        "",
        "解读：`big_bull` 可以作为 G3 strong_v2 的影子研究对象，但不能直接并入正式强势引擎。它需要单独解决旧周期止损密集和回撤扩大的问题。",
        "",
        "## G3 当前 Strong 对比",
        "",
        md_table(
            g3_strong,
            {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "bad10_rate"},
        ),
        "",
        "解读：G3 当前 strong 链路已经验证过部分强势思想，但为了执行压力选择了更保守的 next-open/二段腿版本，收益弹性被明显削弱。下一版应先恢复强势进攻原型，再逐步加执行约束，而不是一开始就把进攻压扁。",
        "",
        "## G3 下一步设计原则",
        "",
        "1. `strong_v2`：吸收 G2 的强势赚钱思想，不复刻 G2 参数；先做强势主升独立候选源。",
        "2. `range_v2`：重新定义箱体底部、冰点、缩量、反抽确认；不沿用旧 range_bottom_icepoint。",
        "3. `down_panic_v3`：继续保留 panic 思路，但下跌周期只买非理性出清，不买普通下跌反弹。",
        "4. `weak_rebound_v2`：弱反弹必须有承接和筹码压力减轻，不能只靠指数回到 MA60。",
        "5. 组合层按市场风格路由，不再固定 `panic 50% + strong 50%`。",
        "",
        "## 本步完成",
        "",
        "- 已确认 G2 full 的可吸收部分是强势主升的资金延续结构。",
        "- 已确认旧 range/weak/panic 模块不能直接继承。",
        "- 已确认 G3 当前收益低的关键问题是 strong 进攻弹性被过早削弱。",
        "",
        "## 下一步目标",
        "",
        "开发 `G3 strong_v2`：用 G3 独立脚本生成强势主升候选源，保留 G2 的资金延续思想，但重新定义 G3 自己的市场状态、候选过滤、仓位权重和实盘可见性字段。",
        "",
    ]
    (OUT_DIR / "g3_g2_full_strong_idea_extraction_report_cn.md").write_text(
        "\n".join(report),
        encoding="utf-8",
    )

    manifest = {
        "status": "completed",
        "output_dir": str(OUT_DIR),
        "inputs": {
            "g2_module_summary": str(G2_MODULE_SUMMARY),
            "g2_breakout_addon": str(G2_BREAKOUT_ADDON),
            "g2_router": str(G2_ROUTER),
            "g3_strong_stress": str(G3_STRONG_STRESS),
            "g3_final_metrics": str(G3_FINAL_METRICS),
        },
        "next_step": "build_g3_strong_v2_independent_source",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
