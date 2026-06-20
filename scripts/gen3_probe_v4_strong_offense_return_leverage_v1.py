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
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table
from utils.paths import report_path


PACKAGE_DIR = report_path("gen3_v4_strong_offense_candidate_v1")
INPUT_PATH = PACKAGE_DIR / "g3_v4_strong_offense_closed_trades.csv"
OUT_DIR = report_path("gen3_v4_strong_offense_return_leverage_probe_v1")

INITIAL_CAPITAL = 150_000.0
BASE_COST_BPS = 30.0

PROFILES = [
    {"profile": "cost30", "extra_cost_bps": 0.0, "all_shock": 0.0},
    {"profile": "cost100", "extra_cost_bps": 70.0, "all_shock": 0.0},
    {"profile": "all_shock2", "extra_cost_bps": 0.0, "all_shock": 0.02},
]

VARIANTS = [
    {
        "variant": "base_slot20",
        "slot_pct": 0.20,
        "route_mult": {"down_panic": 1.0, "range_gap": 1.0, "strong_main": 1.0},
        "max_trade_pct": 0.30,
    },
    {
        "variant": "slot25_all",
        "slot_pct": 0.25,
        "route_mult": {"down_panic": 1.0, "range_gap": 1.0, "strong_main": 1.0},
        "max_trade_pct": 0.35,
    },
    {
        "variant": "slot30_all",
        "slot_pct": 0.30,
        "route_mult": {"down_panic": 1.0, "range_gap": 1.0, "strong_main": 1.0},
        "max_trade_pct": 0.40,
    },
    {
        "variant": "slot35_all",
        "slot_pct": 0.35,
        "route_mult": {"down_panic": 1.0, "range_gap": 1.0, "strong_main": 1.0},
        "max_trade_pct": 0.45,
    },
    {
        "variant": "strong150_slot20",
        "slot_pct": 0.20,
        "route_mult": {"down_panic": 1.0, "range_gap": 1.0, "strong_main": 1.5},
        "max_trade_pct": 0.35,
    },
    {
        "variant": "strong200_slot20",
        "slot_pct": 0.20,
        "route_mult": {"down_panic": 1.0, "range_gap": 1.0, "strong_main": 2.0},
        "max_trade_pct": 0.45,
    },
    {
        "variant": "offense_mix_slot25",
        "slot_pct": 0.25,
        "route_mult": {"down_panic": 0.75, "range_gap": 1.25, "strong_main": 1.5},
        "max_trade_pct": 0.40,
    },
    {
        "variant": "offense_mix_slot30",
        "slot_pct": 0.30,
        "route_mult": {"down_panic": 0.75, "range_gap": 1.25, "strong_main": 1.5},
        "max_trade_pct": 0.45,
    },
    {
        "variant": "return_max_slot35",
        "slot_pct": 0.35,
        "route_mult": {"down_panic": 0.50, "range_gap": 1.25, "strong_main": 1.5},
        "max_trade_pct": 0.50,
    },
]


def pct(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def cagr(curve: pd.DataFrame) -> float:
    if curve.empty:
        return 0.0
    start = pd.Timestamp(curve["date"].iloc[0])
    end = pd.Timestamp(curve["date"].iloc[-1])
    years = max((end - start).days / 365.25, 1e-9)
    total = float(curve["equity"].iloc[-1] / INITIAL_CAPITAL)
    return float(total ** (1.0 / years) - 1.0)


def load_candidates() -> pd.DataFrame:
    d = pd.read_csv(INPUT_PATH, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    d["entry_price"] = pd.to_numeric(d["entry_price"], errors="coerce")
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    d["score"] = pd.to_numeric(d.get("score", 0.0), errors="coerce").fillna(0.0)
    if "route_priority" not in d.columns:
        priority = {"down_panic": 3, "strong_main": 2, "range_gap": 1}
        d["route_priority"] = d["route"].map(priority).fillna(0)
    d["route_priority"] = pd.to_numeric(d["route_priority"], errors="coerce").fillna(0)
    return d.dropna(subset=["entry_date", "policy_exit_date", "code", "entry_price", "policy_net_ret"]).copy()


def apply_profile(candidates: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    d["policy_net_ret"] = (
        pd.to_numeric(d["policy_net_ret"], errors="coerce")
        - float(profile["extra_cost_bps"]) / 10000.0
        - float(profile["all_shock"])
    )
    return d


def simulate(candidates: pd.DataFrame, variant: dict[str, Any], profile: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = apply_profile(candidates, profile)
    d = d.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False]).copy()
    cal = _trade_calendar(d["entry_date"].min(), d["policy_exit_date"].max())
    close_map = router.load_daily_close(d)
    by_day = {day: g.copy() for day, g in d.groupby("entry_date")}

    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    route_mult = dict(variant["route_mult"])
    slot_pct = float(variant["slot_pct"])
    max_trade_pct = float(variant["max_trade_pct"])
    mtm_cost = BASE_COST_BPS / 10000.0 + float(profile["extra_cost_bps"]) / 10000.0

    for day in cal:
        realized = 0.0
        still = []
        for pos in open_pos:
            if pos["policy_exit_date"] <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["policy_net_ret"]))
                cash += exit_value
                pnl = exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = pnl
                closed.append(out)
                realized += pnl
            else:
                still.append(pos)
        open_pos = still

        todays = by_day.get(day)
        opened = 0
        skipped_cash = 0
        if todays is not None:
            equity_before_day = cash + sum(float(p["stake"]) for p in open_pos)
            todays = todays.sort_values(["route_priority", "score"], ascending=[False, False])
            for row in todays.itertuples(index=False):
                route = str(row.route)
                mult = float(route_mult.get(route, 1.0))
                stake = equity_before_day * min(slot_pct * mult, max_trade_pct)
                if stake <= 0 or cash < stake:
                    skipped_cash += 1
                    continue
                pos = row._asdict()
                pos["stake"] = stake
                pos["position_mult"] = mult
                cash -= stake
                open_pos.append(pos)
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
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "skipped_cash": skipped_cash,
                "realized_pnl": realized,
                "worst_open_mtm_ret": worst_open_mtm_ret,
                "missing_close_positions": missing_close_positions,
            }
        )

    curve = pd.DataFrame(rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, pd.DataFrame(closed)


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, variant: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    route_pnl = {}
    for route, g in closed.groupby("route"):
        route_pnl[f"{route}_pnl"] = float(pd.to_numeric(g["realized_pnl"], errors="coerce").fillna(0.0).sum())
    return {
        "variant": variant["variant"],
        "profile": profile["profile"],
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "cagr": cagr(curve),
        "max_drawdown": max_drawdown(curve["equity"]),
        "ret_dd_ratio": abs(float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0) / max_drawdown(curve["equity"]))
        if max_drawdown(curve["equity"]) < 0
        else None,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
        "avg_invested_ratio": float((curve["reserved_principal"] / curve["equity"]).mean()),
        "no_position_rate": float(curve["open_positions"].eq(0).mean()),
        "cash_skip_days": int(curve["skipped_cash"].gt(0).sum()),
        **route_pnl,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = load_candidates()
    summaries: list[dict[str, Any]] = []
    for variant in VARIANTS:
        for profile in PROFILES:
            curve, closed = simulate(candidates, variant, profile)
            run_dir = OUT_DIR / f"{variant['variant']}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summaries.append(summarize(curve, closed, variant, profile))

    summary = pd.DataFrame(summaries)
    summary = summary.sort_values(["profile", "cagr"], ascending=[True, False])
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")

    cost30 = summary[summary["profile"].eq("cost30")].sort_values("cagr", ascending=False)
    shock = summary[summary["profile"].eq("all_shock2")].sort_values("cagr", ascending=False)
    pct_cols = {
        "total_return",
        "cagr",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
        "avg_invested_ratio",
        "no_position_rate",
    }
    lines = [
        "# G3 V4 strong offense 收益进攻性仓位探针 v1",
        "",
        "## 口径",
        "",
        "- 只使用已验证的 strong_offense 候选交易，不新增候选源。",
        "- 探针只改变单笔仓位与路线倍率，不代表正式规则。",
        "- `all_shock2` 表示每笔收益额外下修 2 个百分点，用于观察进攻仓位抗冲击能力。",
        "",
        "## cost30 排名",
        "",
        md_table(cost30, pct_cols=pct_cols),
        "",
        "## all_shock2 排名",
        "",
        md_table(shock, pct_cols=pct_cols),
        "",
        "## 初步判断",
        "",
        "- 如果提高单笔仓位后 CAGR 明显改善但回撤近似线性放大，收益瓶颈主要是资本利用率。",
        "- 如果进攻混合仓位优于简单全路线加仓，说明 strong_main/range_gap 是更值得吃仓位的收益源，down_panic 更像补充频次。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")

    payload = {
        "status": "completed",
        "out_dir": str(OUT_DIR),
        "best_cost30": cost30.head(1).to_dict(orient="records"),
        "best_all_shock2": shock.head(1).to_dict(orient="records"),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
