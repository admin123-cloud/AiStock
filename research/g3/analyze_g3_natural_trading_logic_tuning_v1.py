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

from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("g3_natural_trading_logic_tuning_v1")
FORMAL_DIR = report_path("g3_formal_unified_five_strategy_contract_v1")
ROUTER_DIR = report_path("g2_g3_market_style_router_v1")

FORMAL_TRADES = FORMAL_DIR / "formal_unified_closed_trades.csv"
FORMAL_SELECTED = FORMAL_DIR / "formal_unified_selected_candidates.csv"
ROUTER_WINDOW = ROUTER_DIR / "window_summary.csv"


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(str(path))
    return pd.read_csv(path, encoding="utf-8-sig", low_memory=False)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
    except Exception:
        return default
    return x if math.isfinite(x) else default


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return "--"
    if not math.isfinite(x):
        return "--"
    return f"{x:.1%}"


def _money(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return "--"
    if not math.isfinite(x):
        return "--"
    return f"{x:,.0f}"


def _md_table(
    df: pd.DataFrame,
    pct_cols: set[str] | None = None,
    money_cols: set[str] | None = None,
    max_rows: int = 40,
) -> str:
    if df.empty:
        return "_无数据_"
    d = df.head(max_rows).copy()
    for col in pct_cols or set():
        if col in d.columns:
            d[col] = d[col].map(_pct)
    for col in money_cols or set():
        if col in d.columns:
            d[col] = d[col].map(_money)
    return d.to_markdown(index=False)


def _metrics(df: pd.DataFrame) -> dict[str, Any]:
    ret = pd.to_numeric(df.get("net_ret"), errors="coerce").dropna()
    pnl = pd.to_numeric(df.get("realized_pnl"), errors="coerce")
    return {
        "trade_count": int(len(df)),
        "win_rate": float((ret > 0).mean()) if len(ret) else None,
        "avg_ret": float(ret.mean()) if len(ret) else None,
        "median_ret": float(ret.median()) if len(ret) else None,
        "worst_ret": float(ret.min()) if len(ret) else None,
        "best_ret": float(ret.max()) if len(ret) else None,
        "loss5_rate": float((ret <= -0.05).mean()) if len(ret) else None,
        "loss10_rate": float((ret <= -0.10).mean()) if len(ret) else None,
        "sum_pnl": float(pnl.sum()) if pnl.notna().any() else 0.0,
    }


def _group_metrics(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for keys, part in df.groupby(cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = {col: key for col, key in zip(cols, keys)}
        row.update(_metrics(part))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["sum_pnl", "trade_count"], ascending=[False, False]) if rows else pd.DataFrame()


def _prepare_trades() -> pd.DataFrame:
    d = _read_csv(FORMAL_TRADES)
    for col in ["entry_date", "exit_date", "policy_exit_date", "context_date", "decision_date"]:
        if col in d.columns:
            d[col] = pd.to_datetime(d[col], errors="coerce").dt.normalize()
    for col in [
        "net_ret",
        "realized_pnl",
        "score",
        "raw_score",
        "wave_style_score",
        "sector_diffusion_score",
        "stake",
        "entry_equity",
        "mom20",
        "mom60",
        "up_rate",
        "big_down_rate",
        "limit_down_proxy_rate",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["hold_days"] = (d["exit_date"] - d["entry_date"]).dt.days
    return d


def _overlap_same_stock(trades: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for code, part in trades.sort_values(["code", "entry_date", "exit_date"]).groupby("code", dropna=False):
        open_rows: list[dict[str, Any]] = []
        for row in part.to_dict("records"):
            entry = pd.Timestamp(row.get("entry_date"))
            still_open = []
            for prev in open_rows:
                prev_exit = pd.Timestamp(prev.get("exit_date"))
                if prev_exit >= entry:
                    rows.append(
                        {
                            "code": code,
                            "name": row.get("name"),
                            "first_entry_date": pd.Timestamp(prev.get("entry_date")).strftime("%Y-%m-%d"),
                            "first_exit_date": prev_exit.strftime("%Y-%m-%d"),
                            "second_entry_date": entry.strftime("%Y-%m-%d"),
                            "second_exit_date": pd.Timestamp(row.get("exit_date")).strftime("%Y-%m-%d"),
                            "first_strategy": prev.get("trade_strategy"),
                            "second_strategy": row.get("trade_strategy"),
                            "first_ret": prev.get("net_ret"),
                            "second_ret": row.get("net_ret"),
                            "second_pnl": row.get("realized_pnl"),
                            "sector_for_distinct": row.get("sector_for_distinct"),
                        }
                    )
                    still_open.append(prev)
            still_open.append(row)
            open_rows = still_open
    out = pd.DataFrame(rows)
    if not out.empty:
        out["first_ret"] = pd.to_numeric(out["first_ret"], errors="coerce")
        out["second_ret"] = pd.to_numeric(out["second_ret"], errors="coerce")
        out["second_pnl"] = pd.to_numeric(out["second_pnl"], errors="coerce")
        out = out.sort_values(["second_entry_date", "code"]).reset_index(drop=True)
    return out


def _quick_failures(trades: pd.DataFrame) -> pd.DataFrame:
    d = trades[(pd.to_numeric(trades["hold_days"], errors="coerce") <= 3) & (pd.to_numeric(trades["net_ret"], errors="coerce") <= -0.03)].copy()
    keep = [
        "entry_date",
        "exit_date",
        "code",
        "name",
        "trade_strategy",
        "trade_strategy_label",
        "route",
        "mode",
        "market_style",
        "mom20",
        "mom60",
        "up_rate",
        "big_down_rate",
        "net_ret",
        "realized_pnl",
        "exit_reason",
        "sector_for_distinct",
    ]
    keep = [c for c in keep if c in d.columns]
    return d.sort_values(["net_ret", "entry_date"])[keep]


def _policy_exit_drag(trades: pd.DataFrame) -> pd.DataFrame:
    d = trades[trades.get("exit_reason", pd.Series(dtype=str)).fillna("").astype(str).eq("policy_exit_remaining")].copy()
    return _group_metrics(d, ["trade_strategy", "trade_strategy_label"]) if not d.empty else pd.DataFrame()


def _style_strategy_matrix(trades: pd.DataFrame) -> pd.DataFrame:
    return _group_metrics(trades, ["market_style", "trade_strategy_label"])


def _g2_supplement_profile(trades: pd.DataFrame) -> pd.DataFrame:
    d = trades[trades.get("trade_strategy", pd.Series(dtype=str)).fillna("").astype(str).eq("volume_runup_supplement")].copy()
    if d.empty:
        return pd.DataFrame()
    d["year"] = d["entry_date"].dt.year
    return _group_metrics(d, ["year", "market_style"])


def _natural_rule_candidates(trades: pd.DataFrame, overlap: pd.DataFrame) -> pd.DataFrame:
    rows = [
        {
            "rule_id": "N1",
            "rule_name": "同一只股票持仓期内禁止重复开新仓，除非显式加仓合同成立",
            "target": "买点/仓位",
            "evidence": f"发现 {len(overlap)} 次同票重叠开仓；这类行为在历史里可能赚钱，但实盘上更像隐式加仓，不应伪装成两笔独立二槽交易。",
            "natural_logic": "交易员会先确认已有仓位是否盈利、是否触发加仓条件，再决定加仓；不会把同一只股票连续两天当成两张互不相干的新票。",
            "implementation_hint": "默认同票 open_position 存在时跳过；若允许加仓，必须满足首仓浮盈、30m 继续强、总股票敞口不超过单槽或显式金字塔上限。",
            "priority": "P0",
        },
        {
            "rule_id": "N2",
            "rule_name": "policy_exit_remaining 改成状态化剩余仓管理",
            "target": "卖点",
            "evidence": "policy_exit_remaining 是最大拖累退出原因之一，说明不少交易在没有继续走强时被动耗到策略到期。",
            "natural_logic": "盈利仓可以给趋势时间，未盈利仓不该无理由等待；剩余仓应回答“还有没有继续持有的证据”。",
            "implementation_hint": "未触发止盈且 D3/D5 无跟随强度时提前退出；触发止盈后才允许以前低/30m 结构持有到合同末端。",
            "priority": "P0",
        },
        {
            "rule_id": "N3",
            "rule_name": "G2 空档补位默认半槽化，不与 G3 主路由争同等风险预算",
            "target": "选股/仓位",
            "evidence": "volume_runup_supplement 胜率低于 G3 主路由，但仍有正贡献；它更像提高资金利用率的补位，不是主引擎。",
            "natural_logic": "补位交易应该轻一些、短一些、错了更快撤；否则补位会在行为上变成主策略。",
            "implementation_hint": "G2 补位默认 25% 槽位；只有市场温度正常、G3 无持仓拥挤、G2 买点质量高时提升到 50%。",
            "priority": "P1",
        },
        {
            "rule_id": "N4",
            "rule_name": "市场状态与策略语义分开解释",
            "target": "策略切换/页面解释",
            "evidence": "机构主升和强势交易并不总发生在 standard_uptrend；个股主升可能领先指数，但页面和合同需要说清楚。",
            "natural_logic": "自然交易里有“个股主升逆市场”和“市场主升共振”两种完全不同的胆量，不能只用一个主升标签覆盖。",
            "implementation_hint": "为候选增加 context_tag：market_aligned_mainwave / stock_leads_market / repair_against_downrisk，用于仓位和解释，不作为硬 gate。",
            "priority": "P1",
        },
        {
            "rule_id": "N5",
            "rule_name": "短持仓失败样本做买点体检，不直接为了收益删样本",
            "target": "买点",
            "evidence": "持仓 0-3 天且亏损超过 3% 的交易反映入场后很快被市场否定，是买点自然度问题。",
            "natural_logic": "好的买点即使失败，也应给出几根 30m 的验证空间；开仓后马上被打穿，多半是追高、状态错配或确认不足。",
            "implementation_hint": "新增 quick_fail 标签：开仓后三日内触发止损/大亏的样本进入复盘池，优先检查跳空、30m 背离、板块退潮和同主线拥挤。",
            "priority": "P1",
        },
    ]
    return pd.DataFrame(rows)


def run() -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = _prepare_trades()
    selected = _read_csv(FORMAL_SELECTED)
    windows = _read_csv(ROUTER_WINDOW) if ROUTER_WINDOW.exists() else pd.DataFrame()

    overlap = _overlap_same_stock(trades)
    quick_fail = _quick_failures(trades)
    policy_drag = _policy_exit_drag(trades)
    style_matrix = _style_strategy_matrix(trades)
    g2_profile = _g2_supplement_profile(trades)
    rules = _natural_rule_candidates(trades, overlap)

    route_metrics = _group_metrics(trades, ["route_parent_label"])
    strategy_metrics = _group_metrics(trades, ["trade_strategy", "trade_strategy_label"])
    exit_metrics = _group_metrics(trades, ["exit_reason"])

    overlap.to_csv(OUT_DIR / "same_stock_overlap_audit.csv", index=False, encoding="utf-8-sig")
    quick_fail.to_csv(OUT_DIR / "quick_failure_buy_point_review.csv", index=False, encoding="utf-8-sig")
    policy_drag.to_csv(OUT_DIR / "policy_exit_remaining_drag.csv", index=False, encoding="utf-8-sig")
    style_matrix.to_csv(OUT_DIR / "market_style_strategy_matrix.csv", index=False, encoding="utf-8-sig")
    g2_profile.to_csv(OUT_DIR / "g2_supplement_natural_profile.csv", index=False, encoding="utf-8-sig")
    rules.to_csv(OUT_DIR / "natural_rule_candidates.csv", index=False, encoding="utf-8-sig")

    selected_count = len(selected)
    closed_count = len(trades)
    total_pnl = pd.to_numeric(trades.get("realized_pnl"), errors="coerce").sum()
    win_rate = (pd.to_numeric(trades.get("net_ret"), errors="coerce") > 0).mean()
    avg_ret = pd.to_numeric(trades.get("net_ret"), errors="coerce").mean()
    quick_fail_count = len(quick_fail)
    policy_exit_count = int(trades.get("exit_reason", pd.Series(dtype=str)).fillna("").astype(str).eq("policy_exit_remaining").sum())

    meta = {
        "version": "g3_natural_trading_logic_tuning_v1",
        "source_trades": str(FORMAL_TRADES),
        "selected_count": selected_count,
        "closed_count": closed_count,
        "total_pnl": float(total_pnl),
        "win_rate": float(win_rate),
        "avg_ret": float(avg_ret),
        "same_stock_overlap_count": int(len(overlap)),
        "quick_failure_count": int(quick_fail_count),
        "policy_exit_remaining_count": int(policy_exit_count),
        "output_dir": str(OUT_DIR),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    formal_window = windows[
        (windows.get("model", "") == "g3_final_with_g2_gap_supplement")
        & (windows.get("window", "") .isin(["full", "pre_2024_09", "post_2024_10", "2022_bear", "2024", "2025", "2026ytd"]))
    ].copy() if not windows.empty else pd.DataFrame()

    report = [
        "# G3 自然交易逻辑调优审计 v1",
        "",
        "## 结论",
        "",
        "当前 G3 最终版不是要继续追求历史收益最大化，而是要把赚钱方式变得更像真实交易员：买点有上下文，补位有边界，卖点能解释，策略切换不生硬。",
        "",
        "本轮审计建议保持正式收益合同不动，先把 5 条自然交易规则加入候选观察层和纸面复盘层。只有当这些规则在影子盘中稳定改善交易行为，再考虑进入实盘 gate 或仓位缩放。",
        "",
        "## 总览",
        "",
        f"- 选中候选：{selected_count}",
        f"- 闭合交易：{closed_count}",
        f"- 历史总 PnL：{_money(total_pnl)}",
        f"- 胜率：{_pct(win_rate)}",
        f"- 单笔均值：{_pct(avg_ret)}",
        f"- 同票重叠开仓：{len(overlap)} 次",
        f"- 0-3 天快速失败：{quick_fail_count} 笔",
        f"- `policy_exit_remaining`：{policy_exit_count} 笔",
        "",
        "## 自然交易规则候选",
        "",
        _md_table(rules, max_rows=20),
        "",
        "## 策略收益不是调优唯一目标",
        "",
        _md_table(strategy_metrics, {"win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss5_rate", "loss10_rate"}, {"sum_pnl"}),
        "",
        "解释：`机构主升Score120` 是主引擎，`震荡弱势修复` 是样本支柱，`量能续强补位` 是提高资金利用率的补位。调优时不应把补位策略按主引擎标准硬拔高，也不应为了删亏损而破坏它的角色。",
        "",
        "## 同票重叠开仓",
        "",
        _md_table(overlap, {"first_ret", "second_ret"}, {"second_pnl"}, max_rows=30),
        "",
        "判断：同票重叠不是一定错误，但当前合同没有显式“加仓”语义。自然交易上，它应该被归入加仓合同，而不是两张独立新票。建议默认禁止同票重叠；允许时必须满足首仓浮盈、30m 继续强、总个股敞口上限三个条件。",
        "",
        "## 快速失败买点",
        "",
        _md_table(quick_fail, {"mom20", "mom60", "up_rate", "big_down_rate", "net_ret"}, {"realized_pnl"}, max_rows=30),
        "",
        "判断：快速失败不是优先删样本，而是买点体检池。重点回看是否追高、是否 30m 确认不足、是否板块退潮、是否补位误用了主策略仓位。",
        "",
        "## 被动到期退出",
        "",
        _md_table(policy_drag, {"win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss5_rate", "loss10_rate"}, {"sum_pnl"}),
        "",
        "判断：`policy_exit_remaining` 不符合优秀交易员的剩余仓管理直觉。未盈利仓不应无理由耗到期；已止盈仓才值得交给前低保护和 30m 结构继续跟踪。",
        "",
        "## 市场状态与策略语义",
        "",
        _md_table(style_matrix, {"win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss5_rate", "loss10_rate"}, {"sum_pnl"}, max_rows=80),
        "",
        "判断：个股主升可以领先市场状态，但系统需要解释清楚：这是市场共振主升、个股领先主升，还是修复环境里的强票。这个解释字段比粗暴切换策略更自然。",
        "",
        "## G2 补位画像",
        "",
        _md_table(g2_profile, {"win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss5_rate", "loss10_rate"}, {"sum_pnl"}, max_rows=60),
        "",
        "判断：G2 补位应该继续存在，但默认更适合半槽化和快进快出。它的职责是填空档，不是证明自己比 G3 主路由更强。",
        "",
        "## 分窗口基线",
        "",
        _md_table(formal_window, {"return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade"}, max_rows=20),
        "",
        "## 下一步落地顺序",
        "",
        "1. 先把 N1/N2/N3 作为影子观察标签加入 G3 历史成交和逐日复盘页面，不直接拦单。",
        "2. 用最近 30 个交易日纸面盘记录：被 N1/N2/N3 标记的交易，真实后续表现是否更差、是否更难执行。",
        "3. 若验证成立，再把 N1 做成硬约束，把 N2 做成卖出建议，把 N3 做成仓位缩放。",
        "4. N4/N5 先用于解释和复盘，不急着变成硬 gate，避免把策略调成只会讲道理但不会进攻。",
        "",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8")
    return meta


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
