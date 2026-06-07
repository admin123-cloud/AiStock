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


OUT_DIR = ROOT / "reports" / "gen3_dynamic_router_strong_weight_probe_v1"
STRONG_PLUSWEAK = (
    ROOT / "reports" / "gen3_strong_v2_independent_source_v1" / "strong_v2_main_up_plus_weak04_hold5_closed_trades.csv"
)

INITIAL_CAPITAL = 150_000.0
SLOTS = 5
SOURCE_COST_BPS = 30.0

VARIANTS = [
    {
        "variant": "plusweak_slot20_limit2",
        "route_stake_pct": {"down_panic": 0.20, "range_gap": 0.20, "strong_main": 0.20},
        "route_daily_limit": {"down_panic": 2, "range_gap": 1, "strong_main": 2},
    },
    {
        "variant": "plusweak_strong40_limit1",
        "route_stake_pct": {"down_panic": 0.20, "range_gap": 0.20, "strong_main": 0.40},
        "route_daily_limit": {"down_panic": 2, "range_gap": 1, "strong_main": 1},
    },
    {
        "variant": "plusweak_strong40_limit2",
        "route_stake_pct": {"down_panic": 0.20, "range_gap": 0.20, "strong_main": 0.40},
        "route_daily_limit": {"down_panic": 2, "range_gap": 1, "strong_main": 2},
    },
]


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


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


def _standardize_strong_plusweak() -> pd.DataFrame:
    d = pd.read_csv(STRONG_PLUSWEAK, low_memory=False)
    out = pd.DataFrame()
    out["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    out["code"] = d["code"].astype(str)
    out["name"] = d.get("name", "")
    out["route"] = "strong_main"
    out["route_source"] = "strong_plusweak_hold5"
    out["route_priority"] = router.ROUTE_PRIORITY["strong_main"]
    out["score"] = pd.to_numeric(d.get("g3_strong_score", d.get("score_volume5", d.get("v4_score", 0.0))), errors="coerce").fillna(0.0)
    out["entry_price"] = pd.to_numeric(d.get("entry_price"), errors="coerce")
    out["policy_net_ret"] = pd.to_numeric(d.get("net_ret"), errors="coerce")
    return out


def _load_candidates() -> pd.DataFrame:
    d = pd.concat([router.standardize_panic(), router.standardize_range(), _standardize_strong_plusweak()], ignore_index=True)
    d = d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"]).copy()
    d["route_priority"] = pd.to_numeric(d["route_priority"], errors="coerce").fillna(0)
    d["score"] = pd.to_numeric(d["score"], errors="coerce").fillna(0)
    return d.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False])


def _simulate(candidates: pd.DataFrame, route_stake_pct: dict[str, float], route_daily_limit: dict[str, int]) -> tuple[pd.DataFrame, pd.DataFrame]:
    cal = router._trade_calendar(candidates["entry_date"].min(), candidates["policy_exit_date"].max())
    close_map = router.load_daily_close(candidates)
    by_day = {day: g.copy() for day, g in candidates.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict] = []
    closed: list[dict] = []
    rows: list[dict] = []
    mtm_cost = SOURCE_COST_BPS / 10000.0

    for day in cal:
        realized = 0.0
        still = []
        for pos in open_pos:
            if pos["policy_exit_date"] <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["policy_net_ret"]))
                cash += exit_value
                pnl = exit_value - float(pos["stake"])
                realized += pnl
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = pnl
                closed.append(out)
            else:
                still.append(pos)
        open_pos = still

        opened = 0
        skipped = 0
        route_opened = {k: 0 for k in route_daily_limit}
        todays = by_day.get(day)
        if todays is not None:
            todays = todays.sort_values(["route_priority", "score"], ascending=[False, False])
            for row in todays.itertuples(index=False):
                route = str(row.route)
                if opened >= router.DAILY_OPEN_LIMIT or len(open_pos) >= SLOTS:
                    skipped += 1
                    continue
                if route_opened.get(route, 0) >= route_daily_limit.get(route, 1):
                    skipped += 1
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * float(route_stake_pct.get(route, 0.20))
                if stake <= 0 or cash < stake:
                    skipped += 1
                    continue
                pos = row._asdict()
                pos["stake"] = stake
                pos["route_stake_pct"] = route_stake_pct.get(route, 0.20)
                cash -= stake
                open_pos.append(pos)
                route_opened[route] = route_opened.get(route, 0) + 1
                opened += 1

        mtm_value = 0.0
        worst_open_mtm_ret = 0.0
        missing_close_positions = 0
        for pos in open_pos:
            close = close_map.get((str(pos["code"]), day))
            entry_price = float(pos["entry_price"])
            if close is None or entry_price <= 0:
                mtm_value += float(pos["stake"])
                missing_close_positions += 1
                continue
            mtm_ret = close / entry_price - 1.0 - mtm_cost
            worst_open_mtm_ret = min(worst_open_mtm_ret, float(mtm_ret))
            mtm_value += float(pos["stake"]) * (1.0 + float(mtm_ret))
        equity = cash + mtm_value
        rows.append(
            {
                "date": day,
                "cash": cash,
                "reserved_principal": sum(float(p["stake"]) for p in open_pos),
                "mtm_value": mtm_value,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "skipped": skipped,
                "realized_pnl": realized,
                "worst_open_mtm_ret": worst_open_mtm_ret,
                "missing_close_positions": missing_close_positions,
                "open_strong_main": sum(1 for p in open_pos if p["route"] == "strong_main"),
                "open_range_gap": sum(1 for p in open_pos if p["route"] == "range_gap"),
                "open_down_panic": sum(1 for p in open_pos if p["route"] == "down_panic"),
            }
        )
    curve = pd.DataFrame(rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, pd.DataFrame(closed)


def _summary(curve: pd.DataFrame, closed: pd.DataFrame) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    part = curve[pd.to_datetime(curve["date"]).ge(pd.Timestamp("2024-06-01"))].copy()
    recent_return = None
    recent_dd = None
    if not part.empty:
        recent_return = float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0)
        recent_dd = _max_drawdown(part["equity"])
    return {
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": _max_drawdown(curve["equity"]),
        "recent_return": recent_return,
        "recent_max_drawdown": recent_dd,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
        "max_reserved_principal_ratio": float((curve["reserved_principal"] / curve["equity"]).max()),
    }


def _windows(curve: pd.DataFrame, closed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, (start, end) in router.WINDOWS.items():
        part = curve[curve["date"].between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        trades = closed[pd.to_datetime(closed["entry_date"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        rows.append(
            {
                "window": name,
                "return": float(part["equity"].iloc[-1] / part["equity"].iloc[0] - 1.0) if not part.empty else 0.0,
                "max_drawdown": _max_drawdown(part["equity"]) if not part.empty else 0.0,
                "trade_count": int(len(trades)),
                "win_rate": float((trades["policy_net_ret"] > 0).mean()) if not trades.empty else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _routes(closed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for route, part in closed.groupby("route"):
        net = pd.to_numeric(part["policy_net_ret"], errors="coerce")
        rows.append(
            {
                "route": route,
                "trade_count": int(len(part)),
                "win_rate": float((net > 0).mean()),
                "avg_trade_return": float(net.mean()),
                "worst_trade": float(net.min()),
                "sum_realized_pnl": float(part["realized_pnl"].sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("sum_realized_pnl", ascending=False)


def _write_report(summary_df: pd.DataFrame) -> None:
    lines = [
        "# G3 strong route 仓位提权对照 v1",
        "",
        "## 边界",
        "",
        "- 只测试仓位/路由资源分配，不改候选源，不改退出规则。",
        "- strong route 使用 `main_up + weak_recovery` 候选；panic/range 仍为 20%。",
        "- 这是研究口径，尚未加入跌停不可卖和高滑点压力。",
        "",
        "## 结果",
        "",
        _md_table(
            summary_df[
                [
                    "variant",
                    "total_return",
                    "max_drawdown",
                    "recent_return",
                    "recent_max_drawdown",
                    "trade_count",
                    "win_rate",
                    "avg_trade_return",
                    "worst_trade",
                    "worst_open_mtm_ret",
                    "max_reserved_principal_ratio",
                ]
            ],
            pct_cols={
                "total_return",
                "max_drawdown",
                "recent_return",
                "recent_max_drawdown",
                "win_rate",
                "avg_trade_return",
                "worst_trade",
                "worst_open_mtm_ret",
                "max_reserved_principal_ratio",
            },
        ),
        "",
        "## 判断",
        "",
        "- 如果 strong40 明显抬升近两年收益且回撤可控，说明 G3 V3 的主要错误之一是把强势周期按防守账户稀释了。",
        "- 如果 strong40 回撤或最差持仓显著恶化，下一步必须先做 strong 专属执行压力，而不是继续加仓。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = _load_candidates()
    rows = []
    for spec in VARIANTS:
        curve, closed = _simulate(candidates, spec["route_stake_pct"], spec["route_daily_limit"])
        variant_dir = OUT_DIR / spec["variant"]
        variant_dir.mkdir(parents=True, exist_ok=True)
        curve.to_csv(variant_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(variant_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        _windows(curve, closed).to_csv(variant_dir / "window_summary.csv", index=False, encoding="utf-8-sig")
        _routes(closed).to_csv(variant_dir / "route_attribution.csv", index=False, encoding="utf-8-sig")
        row = _summary(curve, closed)
        row.update(spec)
        rows.append(row)
    summary_df = pd.DataFrame(rows)
    summary_df.to_csv(OUT_DIR / "variant_summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps({"status": "completed", "variants": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(summary_df)
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()
