from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.gen3_build_dynamic_router_combo_v1 as router
import scripts.gen3_package_v4_research_candidate_v1 as v4pkg
from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import (
    _load_daily_prices,
    _next_trade_date_map,
    _price_maps,
    _ret_from_price,
)
from scripts.gen3_backtest_strong_volume5_confirm_fail_exit_v1 import _apply_policy, _prepare_base
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar


PACKAGE_DIR = ROOT / "reports" / "gen3_v4_research_package_v1"
OUT_DIR = ROOT / "reports" / "gen3_v4_strong_confirm_d3_visible_combo_v1"
INITIAL_CAPITAL = 150_000.0
RANGE_VARIANT = "v4_h10_margin"

PROFILES = [
    {"profile": "cost30", "cost_bps": 30.0, "range_shock": 0.0, "all_shock": 0.0},
    {"profile": "cost50", "cost_bps": 50.0, "range_shock": 0.0, "all_shock": 0.0},
    {"profile": "cost100", "cost_bps": 100.0, "range_shock": 0.0, "all_shock": 0.0},
    {"profile": "cost30_all_shock2", "cost_bps": 30.0, "range_shock": 0.0, "all_shock": 0.02},
]


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def _summary(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str, diagnostics: dict[str, Any]) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    recent = curve[pd.to_datetime(curve["date"]).ge(pd.Timestamp("2024-06-01"))].copy()
    route_parts: dict[str, Any] = {}
    for route, g in closed.groupby("route"):
        rnet = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        route_parts[f"{route}_trades"] = int(len(g))
        route_parts[f"{route}_avg_ret"] = float(rnet.mean()) if len(rnet) else None
        route_parts[f"{route}_pnl"] = float(pd.to_numeric(g["realized_pnl"], errors="coerce").fillna(0.0).sum())
    return {
        "variant": variant,
        "profile": profile,
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": _max_drawdown(curve["equity"]),
        "recent_return": float(recent["equity"].iloc[-1] / recent["equity"].iloc[0] - 1.0) if not recent.empty else None,
        "recent_max_drawdown": _max_drawdown(recent["equity"]) if not recent.empty else None,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
        **diagnostics,
        **route_parts,
    }


def _load_v4_non_strong() -> pd.DataFrame:
    d = pd.read_csv(PACKAGE_DIR / f"{RANGE_VARIANT}_candidates_standardized.csv", low_memory=False)
    d = d[~d["route"].astype(str).eq("strong_main")].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["entry_price"] = pd.to_numeric(d["entry_price"], errors="coerce")
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    d["score"] = pd.to_numeric(d["score"], errors="coerce").fillna(0.0)
    return d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def _apply_visible_d4_exit(confirm: pd.DataFrame, mode: str, cost_bps: float) -> tuple[pd.DataFrame, dict[str, Any]]:
    d = confirm.copy()
    d["visible_execution_mode"] = mode
    d["visible_execution_note"] = "not_early_exit"
    early = d.get("exit_reason_proxy", pd.Series(dtype=str)).astype(str).ne("fixed_h5")
    if not early.any():
        return d, {"early_exit_rows": 0, "d4_open_rows": 0, "limit_delay_rows": 0, "missing_price_rows": 0}

    daily = _load_daily_prices(d)
    maps = _price_maps(daily)
    calendar = _trade_calendar(d["entry_date"].min(), d["policy_exit_date"].max() + pd.Timedelta(days=30))
    next_map = _next_trade_date_map(calendar)
    limit_delay_rows = 0
    missing_price_rows = 0
    d4_open_rows = 0

    for idx, row in d[early].iterrows():
        code = str(row["code"])
        entry_price = float(row.get("entry_price") or 0.0)
        confirm_date = pd.Timestamp(row["policy_exit_date"]).normalize()
        target_date = next_map.get(confirm_date, confirm_date)
        note = "d4_open"
        if mode == "d4_open_limit_delay":
            delay_count = 0
            while bool(maps["limit_down"].get((code, target_date), False)) and target_date in next_map and delay_count < 10:
                target_date = next_map[target_date]
                delay_count += 1
            if delay_count > 0:
                limit_delay_rows += 1
                note = f"d4_open_limit_delayed_{delay_count}"
        elif mode != "d4_open":
            raise ValueError(mode)

        price = maps["open"].get((code, target_date))
        ret = _ret_from_price(price, entry_price, cost_bps)
        if ret is None:
            missing_price_rows += 1
            d.at[idx, "visible_execution_note"] = "missing_d4_open_keep_proxy"
            continue
        d.at[idx, "policy_exit_date"] = target_date
        d.at[idx, "net_ret"] = ret
        d.at[idx, "visible_execution_note"] = note
        d4_open_rows += 1

    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["net_ret"] = pd.to_numeric(d["net_ret"], errors="coerce")
    return d.dropna(subset=["policy_exit_date", "net_ret"]), {
        "early_exit_rows": int(early.sum()),
        "d4_open_rows": int(d4_open_rows),
        "limit_delay_rows": int(limit_delay_rows),
        "missing_price_rows": int(missing_price_rows),
    }


def _standardize_visible_strong(mode: str, veto_l3_hot: bool = True) -> tuple[pd.DataFrame, dict[str, Any]]:
    base = _prepare_base(30.0)
    confirm = _apply_policy(base, "confirm_d3_le0", 30.0)
    if veto_l3_hot and "l3_s3" in confirm.columns:
        l3 = pd.to_numeric(confirm["l3_s3"], errors="coerce")
        confirm = confirm[l3.lt(0.50) | l3.isna()].copy()
    visible, diag = _apply_visible_d4_exit(confirm, mode, 30.0)

    out = pd.DataFrame()
    out["entry_date"] = pd.to_datetime(visible["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(visible["policy_exit_date"], errors="coerce").dt.normalize()
    out["code"] = visible["code"].astype(str)
    out["name"] = visible.get("name", "")
    out["route"] = "strong_main"
    out["route_source"] = f"strong_volume5_confirm_d3_visible_{mode}_veto_l3_s3_ge50"
    out["route_priority"] = 2
    out["score"] = pd.to_numeric(visible.get("v4_score", visible.get("score_volume5")), errors="coerce").fillna(0.0)
    out["entry_price"] = pd.to_numeric(visible.get("entry_price"), errors="coerce")
    out["policy_net_ret"] = pd.to_numeric(visible.get("net_ret"), errors="coerce")
    out["exit_reason_proxy"] = visible.get("exit_reason_proxy", "")
    out["visible_execution_note"] = visible.get("visible_execution_note", "")
    out["l3_s3"] = pd.to_numeric(visible.get("l3_s3"), errors="coerce")
    out = out.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])
    diag["strong_source_rows"] = int(len(out))
    return out, diag


def _build_candidates(mode: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    non_strong = _load_v4_non_strong()
    strong, diag = _standardize_visible_strong(mode=mode, veto_l3_hot=True)
    d = pd.concat([non_strong, strong], ignore_index=True)
    d["route_priority"] = pd.to_numeric(d["route_priority"], errors="coerce").fillna(0)
    d["score"] = pd.to_numeric(d["score"], errors="coerce").fillna(0.0)
    return d.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False]), diag


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = _pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _write_report(summary: pd.DataFrame) -> None:
    pct_cols = {
        "total_return",
        "max_drawdown",
        "recent_return",
        "recent_max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
        "strong_main_avg_ret",
    }
    lines = [
        "# G3 V4 strong_main confirm_d3 可见执行组合 v1",
        "",
        "## 边界",
        "",
        "- D3 收盘确认失败后，最早按 D4 开盘退出；这一步不再使用 D3 收盘作为可成交卖点。",
        "- `d4_open_limit_delay` 会在 D4 近似跌停不可卖时继续顺延到下一个可卖开盘。",
        "- 仍是日线执行近似，尚未模拟真实排队、逐笔流动性和 30m 触发当根细节。",
        "- down_panic/range_gap 沿用 V4 H10 margin，不改买点。",
        "",
        "## 结果",
        "",
        _md_table(
            summary[
                [
                    "variant",
                    "profile",
                    "strong_source_rows",
                    "early_exit_rows",
                    "d4_open_rows",
                    "limit_delay_rows",
                    "trade_count",
                    "total_return",
                    "max_drawdown",
                    "recent_return",
                    "recent_max_drawdown",
                    "win_rate",
                    "avg_trade_return",
                    "worst_trade",
                    "strong_main_trades",
                    "strong_main_avg_ret",
                    "strong_main_pnl",
                ]
            ],
            pct_cols=pct_cols,
        ),
        "",
        "## 判断",
        "",
        "- 如果 D4 可见执行后收益/回撤仍优于原 V4，说明 confirm_d3 方向有继续做实时化价值。",
        "- 如果 D4 开盘后收益明显塌陷，则此前 D3 代理有较强未来函数收益，不能升级。",
        "- 全链路 -2% 若仍只有小幅改善，说明下一步还要处理 strong 的仓位路径和成交敏感性。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    modes = ["d4_open", "d4_open_limit_delay"]
    rows = []
    for mode in modes:
        candidates, diag = _build_candidates(mode)
        candidates.to_csv(OUT_DIR / f"{mode}_candidates_standardized.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            stressed = v4pkg._apply_stress(candidates, profile)
            curve, closed = v4pkg._simulate(stressed)
            run_dir = OUT_DIR / f"{mode}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            router.summarize_windows(curve, closed).to_csv(run_dir / "window_summary.csv", index=False, encoding="utf-8-sig")
            router.summarize_routes(closed).to_csv(run_dir / "route_attribution.csv", index=False, encoding="utf-8-sig")
            rows.append(_summary(curve, closed, mode, profile["profile"], diag))

    summary = pd.DataFrame(rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps({"status": "completed", "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(summary)
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
