from __future__ import annotations

import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path, reports_root  # noqa: E402


START = pd.Timestamp("2020-01-01")
END = pd.Timestamp("2026-06-18")
PRE_END = pd.Timestamp("2024-09-30")
POST_START = pd.Timestamp("2024-10-01")
OUT_DIR = report_path("gen3_pre_2024_10_state_alpha_closure_v5")
SLOT_PCT = 0.25
MAX_SLOTS = 4


@dataclass(frozen=True)
class HealthVariant:
    key: str
    label: str
    lookback_days: int
    min_count: int
    min_avg_ret: float
    min_win_rate: float
    max_drawdown_floor: float
    institutional_min_count: int | None = None
    institutional_min_avg_ret: float | None = None
    institutional_min_win_rate: float | None = None
    institutional_max_drawdown_floor: float | None = None


HEALTH_VARIANTS = [
    HealthVariant("health_240_loose", "滚动健康度 240D loose", 240, 6, 0.000, 0.50, -0.18),
    HealthVariant("health_240_balanced", "滚动健康度 240D balanced", 240, 8, 0.003, 0.52, -0.15),
    HealthVariant("health_480_balanced", "滚动健康度 480D balanced", 480, 12, 0.003, 0.52, -0.18),
    HealthVariant("health_720_stable", "滚动健康度 720D stable", 720, 18, 0.004, 0.52, -0.20),
    HealthVariant(
        "dual_240_inst2_pre8",
        "双源健康度 240D：pre8 + institutional2",
        240,
        8,
        0.003,
        0.52,
        -0.15,
        institutional_min_count=2,
        institutional_min_avg_ret=0.000,
        institutional_min_win_rate=0.50,
        institutional_max_drawdown_floor=-0.35,
    ),
    HealthVariant(
        "dual_480_inst2_pre12",
        "双源健康度 480D：pre12 + institutional2",
        480,
        12,
        0.003,
        0.52,
        -0.18,
        institutional_min_count=2,
        institutional_min_avg_ret=0.000,
        institutional_min_win_rate=0.50,
        institutional_max_drawdown_floor=-0.35,
    ),
]


def rp(*parts: str) -> Path:
    return reports_root().joinpath(*parts)


def read_csv(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False, **kwargs)


def normalize_date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.normalize()


def pct(v) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def max_drawdown(series: pd.Series) -> float:
    if series.empty:
        return np.nan
    peak = series.cummax()
    return float((series / peak - 1.0).min())


def source_trade_id(df: pd.DataFrame) -> pd.Series:
    return (
        df["entry_date_norm"].dt.strftime("%Y-%m-%d")
        + "|"
        + df["code"].astype(str)
        + "|"
        + df["policy_exit_date_norm"].dt.strftime("%Y-%m-%d")
    )


def standardize(
    df: pd.DataFrame,
    source_key: str,
    source_label: str,
    alpha_family: str,
    ret_col: str = "policy_net_ret",
    name_col: str = "name",
) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["entry_date_norm"] = normalize_date(out["entry_date"])
    out["policy_exit_date_norm"] = normalize_date(out["policy_exit_date"])
    out["ret_norm"] = pd.to_numeric(out[ret_col] if ret_col in out.columns else out.get("net_ret"), errors="coerce")
    out["source_key"] = source_key
    out["source_label"] = source_label
    out["alpha_family"] = alpha_family
    if "name" not in out.columns and name_col in out.columns:
        out["name"] = out[name_col]
    if "route" not in out.columns:
        out["route"] = alpha_family
    if "score" not in out.columns:
        out["score"] = 0.0
    out = out[
        (out["entry_date_norm"] >= START)
        & (out["entry_date_norm"] <= END)
        & out["entry_date_norm"].notna()
        & out["policy_exit_date_norm"].notna()
        & out["ret_norm"].notna()
    ].copy()
    out["trade_id"] = source_trade_id(out)
    return out


def load_range_no_adx() -> pd.DataFrame:
    path = rp("gen3_combo_range_filter_v1", "range_no_adx_downtrend_cost30_closed_trades.csv")
    df = standardize(read_csv(path), "pre_state_alpha", "pre_state_alpha：range_no_adx + down_panic", "pre_state_alpha")
    if df.empty:
        return df
    df["component"] = np.where(df["route"].eq("down_panic"), "down_panic_core", "range_no_adx_downtrend")
    df["component_priority"] = np.where(df["component"].eq("down_panic_core"), 120, 100)
    return df


def load_panic_context_gate(existing_ids: set[str]) -> pd.DataFrame:
    path = rp("gen3_panic_v2_research", "final_candidate_v1", "m30_close5_full_nextopen_cost30_closed_trades.csv")
    raw = standardize(read_csv(path), "pre_state_alpha", "pre_state_alpha：panic_context_gate", "pre_state_alpha")
    if raw.empty:
        return raw
    allowed_volume = raw["g3_volume_context"].isin(["healthy_release", "neutral_volume"])
    allowed_cap = raw["g3_capitulation_strength"].isin(["extreme_capitulation", "strong_capitulation"])
    allowed_env = raw["g3_repair_env_label"].isin(["extreme_clearance", "strong_clearance", "low_position_wait_repair"])
    gated = raw[allowed_volume & (allowed_cap | allowed_env)].copy()
    gated = gated[~gated["trade_id"].isin(existing_ids)].copy()
    gated["route"] = "panic_context_gate"
    gated["component"] = "panic_context_gate"
    gated["component_priority"] = 130
    return gated


def load_pre_state_alpha() -> pd.DataFrame:
    base = load_range_no_adx()
    gate = load_panic_context_gate(set(base["trade_id"]) if not base.empty else set())
    out = pd.concat([base, gate], ignore_index=True, sort=False)
    if out.empty:
        return out
    out["source_priority"] = 200 + pd.to_numeric(out["component_priority"], errors="coerce").fillna(0)
    return out.sort_values(["entry_date_norm", "source_priority", "score"], ascending=[True, False, False]).copy()


def load_institutional_mainwave() -> pd.DataFrame:
    frames = []
    scheduler = read_csv(rp("wave_style_model_scheduler_v1", "scheduler_focus_240d_score120_aggr25", "closed_trades.csv"))
    if not scheduler.empty:
        x = standardize(
            scheduler,
            "institutional_mainwave",
            "institutional_mainwave：scheduler_focus_240d_score120",
            "institutional_mainwave",
            ret_col="net_ret",
            name_col="stock_name",
        )
        x["component"] = "wave_scheduler_score120"
        x["source_priority"] = 300
        frames.append(x)
    router = read_csv(rp("gen3_market_state_router_strategy_v1", "g3_route_execution_mandate_candidate_closed_trades.csv"))
    if not router.empty:
        r = standardize(
            router[router.get("mode", pd.Series(dtype=str)).eq("institutional_mainwave")].copy(),
            "institutional_mainwave",
            "institutional_mainwave：state_router",
            "institutional_mainwave",
            ret_col="policy_net_ret",
        )
        r["component"] = "state_router_institutional"
        r["source_priority"] = 320
        frames.append(r)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    out = out.drop_duplicates("trade_id", keep="first")
    return out.sort_values(["entry_date_norm", "source_priority", "score"], ascending=[True, False, False]).copy()


def load_current_g3_v4_combo() -> pd.DataFrame:
    df = read_csv(rp("gen3_v4_research_package_v1", "v4_h10_margin__cost30", "closed_trades.csv"))
    out = standardize(df, "current_g3_v4_combo", "当前 G3 V4 h10 cost30", "current_g3_v4_combo")
    if out.empty:
        return out
    out["component"] = out["route"].astype(str)
    out["source_priority"] = 250
    return out


def rolling_source_history(candidates: pd.DataFrame, variant: HealthVariant) -> dict[str, dict[pd.Timestamp, dict]]:
    result: dict[str, dict[pd.Timestamp, dict]] = defaultdict(dict)
    if candidates.empty:
        return result
    events_by_source: dict[str, list[tuple[pd.Timestamp, float]]] = defaultdict(list)
    dates = sorted(candidates["entry_date_norm"].dropna().unique())
    exits = candidates[["source_key", "policy_exit_date_norm", "ret_norm"]].sort_values("policy_exit_date_norm")
    exit_i = 0
    exit_rows = list(exits.itertuples(index=False))

    windows: dict[str, deque[tuple[pd.Timestamp, float]]] = defaultdict(deque)
    for current_date in dates:
        while exit_i < len(exit_rows) and exit_rows[exit_i].policy_exit_date_norm < current_date:
            row = exit_rows[exit_i]
            windows[row.source_key].append((row.policy_exit_date_norm, float(row.ret_norm)))
            exit_i += 1
        cutoff = current_date - pd.Timedelta(days=variant.lookback_days)
        for source_key in list(windows.keys()):
            q = windows[source_key]
            while q and q[0][0] < cutoff:
                q.popleft()
            vals = pd.Series([v for _, v in q], dtype=float)
            if vals.empty:
                stats = {
                    "count": 0,
                    "avg_ret": np.nan,
                    "win_rate": np.nan,
                    "drawdown": np.nan,
                    "enabled": False,
                    "health_score": -999.0,
                }
            else:
                equity = (1.0 + vals).cumprod()
                dd = max_drawdown(equity)
                min_count = variant.min_count
                min_avg_ret = variant.min_avg_ret
                min_win_rate = variant.min_win_rate
                max_drawdown_floor = variant.max_drawdown_floor
                if source_key == "institutional_mainwave":
                    min_count = variant.institutional_min_count or min_count
                    min_avg_ret = variant.institutional_min_avg_ret if variant.institutional_min_avg_ret is not None else min_avg_ret
                    min_win_rate = variant.institutional_min_win_rate if variant.institutional_min_win_rate is not None else min_win_rate
                    max_drawdown_floor = (
                        variant.institutional_max_drawdown_floor
                        if variant.institutional_max_drawdown_floor is not None
                        else max_drawdown_floor
                    )
                enabled = (
                    len(vals) >= min_count
                    and float(vals.mean()) >= min_avg_ret
                    and float((vals > 0).mean()) >= min_win_rate
                    and (pd.isna(dd) or dd >= max_drawdown_floor)
                )
                stats = {
                    "count": int(len(vals)),
                    "avg_ret": float(vals.mean()),
                    "win_rate": float((vals > 0).mean()),
                    "drawdown": dd,
                    "enabled": bool(enabled),
                    "health_score": float(vals.mean() * 100 + ((vals > 0).mean() - 0.5) * 3 + min(len(vals), 60) * 0.01),
                }
            result[source_key][current_date] = stats
    return result


def always_on_history(candidates: pd.DataFrame) -> dict[str, dict[pd.Timestamp, dict]]:
    result: dict[str, dict[pd.Timestamp, dict]] = defaultdict(dict)
    for source_key, g in candidates.groupby("source_key"):
        for d in sorted(g["entry_date_norm"].dropna().unique()):
            result[source_key][d] = {
                "count": 999,
                "avg_ret": 1.0,
                "win_rate": 1.0,
                "drawdown": 0.0,
                "enabled": True,
                "health_score": 999.0,
            }
    return result


def backtest_slots(
    candidates: pd.DataFrame,
    key: str,
    label: str,
    health: dict[str, dict[pd.Timestamp, dict]] | None = None,
    priority_mode: str = "health_score",
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        empty = pd.DataFrame()
        return empty, empty, empty
    health = health or always_on_history(candidates)
    active: list[dict] = []
    selected = []
    curve_rows = []
    health_rows = []
    equity = 1.0
    last_date = candidates["entry_date_norm"].min()
    by_date = {d: g.copy() for d, g in candidates.groupby("entry_date_norm")}
    all_dates = sorted(set(candidates["entry_date_norm"].dropna()) | set(candidates["policy_exit_date_norm"].dropna()))

    for current_date in all_dates:
        due = [p for p in active if p["policy_exit_date_norm"] <= current_date]
        if due:
            for p in due:
                equity *= 1.0 + float(p["ret_norm"]) * SLOT_PCT
            active = [p for p in active if p["policy_exit_date_norm"] > current_date]
            curve_rows.append({"date": current_date, "equity": equity, "event": "exit"})
            last_date = current_date

        day = by_date.get(current_date)
        if day is None or len(active) >= MAX_SLOTS:
            continue
        rows = []
        for row in day.to_dict("records"):
            stats = health.get(row["source_key"], {}).get(current_date)
            if not stats or not stats.get("enabled", False):
                continue
            out = dict(row)
            out.update(
                {
                    "router_key": key,
                    "router_label": label,
                    "health_count": stats["count"],
                    "health_avg_ret": stats["avg_ret"],
                    "health_win_rate": stats["win_rate"],
                    "health_drawdown": stats["drawdown"],
                    "health_score": stats["health_score"],
                }
            )
            rows.append(out)
            health_rows.append(
                {
                    "router_key": key,
                    "date": current_date,
                    "source_key": row["source_key"],
                    **stats,
                }
            )
        if not rows:
            continue
        order_cols = ["health_score", "source_priority", "score"] if priority_mode == "health_score" else ["source_priority", "health_score", "score"]
        ranked = pd.DataFrame(rows).sort_values(order_cols, ascending=[False] * len(order_cols))
        used_codes = {p["code"] for p in active}
        for _, r in ranked.iterrows():
            if len(active) >= MAX_SLOTS:
                break
            if r["code"] in used_codes:
                continue
            rec = r.to_dict()
            active.append(rec)
            selected.append(rec)
            used_codes.add(r["code"])

    if active:
        for p in sorted(active, key=lambda x: x["policy_exit_date_norm"]):
            equity *= 1.0 + float(p["ret_norm"]) * SLOT_PCT
            curve_rows.append({"date": p["policy_exit_date_norm"], "equity": equity, "event": "forced_final_exit"})

    trades = pd.DataFrame(selected)
    curve = pd.DataFrame(curve_rows)
    if curve.empty:
        curve = pd.DataFrame([{"date": last_date, "equity": 1.0, "event": "init"}])
    health_df = pd.DataFrame(health_rows).drop_duplicates(["router_key", "date", "source_key"]) if health_rows else pd.DataFrame()
    return trades, curve, health_df


def metrics_from_trades(trades: pd.DataFrame, curve: pd.DataFrame, key: str, label: str) -> dict:
    ret = pd.to_numeric(trades.get("ret_norm", pd.Series(dtype=float)), errors="coerce").dropna()
    by_year = (
        trades.loc[ret.index].assign(year=lambda x: x["entry_date_norm"].dt.year).groupby("year")["ret_norm"].agg(["count", "mean"])
        if len(ret)
        else pd.DataFrame()
    )
    pos = ret[ret > 0].sort_values(ascending=False)
    pos_sum = float(pos.sum()) if len(pos) else 0.0
    total = float(curve["equity"].iloc[-1] - 1.0) if not curve.empty else np.nan
    dd = max_drawdown(curve["equity"]) if not curve.empty else np.nan
    return {
        "key": key,
        "label": label,
        "trade_count": int(len(ret)),
        "win_rate": float((ret > 0).mean()) if len(ret) else np.nan,
        "avg_ret": float(ret.mean()) if len(ret) else np.nan,
        "median_ret": float(ret.median()) if len(ret) else np.nan,
        "capital_return_proxy": total,
        "capital_max_drawdown_proxy": dd,
        "worst_trade": float(ret.min()) if len(ret) else np.nan,
        "best_trade": float(ret.max()) if len(ret) else np.nan,
        "top5_positive_share": float(pos.head(5).sum() / pos_sum) if pos_sum > 0 else np.nan,
        "positive_years": int((by_year["mean"] > 0).sum()) if not by_year.empty else 0,
        "min_year_count": int(by_year["count"].min()) if not by_year.empty else 0,
        "diagnosis": diagnose(ret, by_year, pos, pos_sum),
    }


def diagnose(ret: pd.Series, by_year: pd.DataFrame, pos: pd.Series, pos_sum: float) -> str:
    if len(ret) < 20:
        return "交易数太少"
    if ret.mean() < 0.005 or (ret > 0).mean() < 0.5:
        return "策略单笔收益太低"
    if not by_year.empty and (by_year["mean"] > 0).sum() < 3:
        return "年度稳定性不足"
    if pos_sum > 0 and float(pos.head(5).sum() / pos_sum) > 0.5:
        return "收益集中度偏高"
    return "可继续建模"


def annual_metrics(trades: pd.DataFrame, key: str, label: str) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows = []
    for year, g in trades.assign(year=lambda x: x["entry_date_norm"].dt.year).groupby("year"):
        curve = pd.DataFrame({"equity": (1.0 + g["ret_norm"].fillna(0) * SLOT_PCT).cumprod()})
        row = metrics_from_trades(g, curve, key, label)
        row["year"] = int(year)
        rows.append(row)
    return pd.DataFrame(rows)


def window_metrics(trades: pd.DataFrame, curve: pd.DataFrame, key: str, label: str) -> pd.DataFrame:
    windows = [
        ("pre_2024_10", START, PRE_END),
        ("post_2024_10", POST_START, END),
        ("full", START, END),
    ]
    rows = []
    for name, start, end in windows:
        t = trades[(trades["entry_date_norm"] >= start) & (trades["entry_date_norm"] <= end)].copy() if not trades.empty else trades
        c = curve[(curve["date"] >= start) & (curve["date"] <= end)].copy() if not curve.empty else curve
        if not c.empty:
            first = float(c["equity"].iloc[0])
            c = c.copy()
            c["equity"] = c["equity"] / first if first else c["equity"]
        row = metrics_from_trades(t, c, key, label)
        row["window"] = name
        rows.append(row)
    return pd.DataFrame(rows)


def group_metrics(trades: pd.DataFrame, key: str, label: str) -> pd.DataFrame:
    rows = []
    for col in ["source_key", "alpha_family", "component", "route"]:
        if trades.empty or col not in trades.columns:
            continue
        for value, g in trades.groupby(col, dropna=False):
            curve = pd.DataFrame({"equity": (1.0 + g["ret_norm"].fillna(0) * SLOT_PCT).cumprod()})
            row = metrics_from_trades(g, curve, key, label)
            row["group_col"] = col
            row["group_value"] = str(value)
            rows.append(row)
    return pd.DataFrame(rows)


def format_table(df: pd.DataFrame, cols: list[str], limit: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    out = df.copy()
    if limit:
        out = out.head(limit)
    for col in out.columns:
        if col.endswith("rate") or col in {
            "avg_ret",
            "median_ret",
            "capital_return_proxy",
            "capital_max_drawdown_proxy",
            "worst_trade",
            "best_trade",
            "top5_positive_share",
            "health_avg_ret",
            "health_win_rate",
            "health_drawdown",
        }:
            out[col] = out[col].map(pct)
    return out[[c for c in cols if c in out.columns]].to_markdown(index=False)


def write_report(summary: pd.DataFrame, windows: pd.DataFrame, annual: pd.DataFrame, groups: pd.DataFrame, health_log: pd.DataFrame) -> None:
    best = summary.sort_values(["capital_return_proxy", "avg_ret"], ascending=[False, False])
    pre = windows[windows["window"].eq("pre_2024_10")].sort_values("capital_return_proxy", ascending=False)
    post = windows[windows["window"].eq("post_2024_10")].sort_values("capital_return_proxy", ascending=False)
    health_tail = health_log.sort_values("date").tail(80) if not health_log.empty else pd.DataFrame()

    report = f"""# G3 State Alpha 研究闭环第五阶段 v5

## 目标

本阶段把第四阶段结论收敛为一个独立研究路由：`pre_2024_state_alpha = panic_context_gate + range_no_adx_downtrend + down_panic_core`，并与 `institutional_mainwave` 做无硬日期滚动健康度切换回测。

约束：

- 不修改当前正式/影子交易链路。
- 不使用 `2024-10-01` 作为最终规则。
- 健康度只使用该信号源在当前入场日前已经退出的历史样本。
- 回放使用最多 4 槽、每槽 25% 的退出日结算代理资金曲线。

## 核心结论

1. `pre_2024_state_alpha` 可以作为研究闭环源成立，但和机构主升全周期切换仍需要工程前压力测试。
2. 无硬日期滚动健康度能避免“强行按 2024-10 切换”，但健康度开关会牺牲一部分早期样本，收益通常低于 hindsight 静态最优。
3. 当前最值得迁移的是独立 pre-state-alpha 的候选生成与健康度台账，而不是直接替换 G3 shadow。
4. 下一步可以进入“影子前审计接口”设计：今日候选、健康度、阻断原因、路由归因先出 observe-only 文件，不允许自动下单。

## 全周期策略对比

{format_table(best, ["key", "label", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return_proxy", "capital_max_drawdown_proxy", "worst_trade", "top5_positive_share", "positive_years", "diagnosis"])}

## 2024-10 前窗口

{format_table(pre, ["key", "label", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return_proxy", "capital_max_drawdown_proxy", "worst_trade", "diagnosis"])}

## 2024-10 后窗口

{format_table(post, ["key", "label", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return_proxy", "capital_max_drawdown_proxy", "worst_trade", "diagnosis"])}

## 年度拆分

{format_table(annual.sort_values(["key", "year"]), ["key", "year", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return_proxy", "capital_max_drawdown_proxy", "worst_trade", "diagnosis"], limit=120)}

## 路由归因

{format_table(groups.sort_values(["key", "group_col", "capital_return_proxy"], ascending=[True, True, False]), ["key", "group_col", "group_value", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return_proxy", "worst_trade", "diagnosis"], limit=120)}

## 健康度尾部样本

{format_table(health_tail, ["router_key", "date", "source_key", "count", "avg_ret", "win_rate", "drawdown", "enabled", "health_score"], limit=80)}

## 闭环判断

- 研究闭环：可以闭合。`pre_2024_state_alpha` 的候选源、组合回放、滚动健康度、年度/窗口/归因表都已形成。
- 交易闭环：仍不直接闭合。原因是这一步仍是退出日结算代理曲线，没有接入真实每日 mark-to-market、今日候选生成、准入阻断和 observe-only 台账。
- 推荐迁移边界：只迁移为 `observe_only` 研究路由，输出健康度与候选，不改 `institutional_mainwave` 和当前 G3 shadow 下单权限。

## 产物

- `closure_summary.csv`
- `closure_window_summary.csv`
- `closure_annual_summary.csv`
- `closure_group_summary.csv`
- `closure_health_log.csv`
- `*_selected.csv`
- `*_curve.csv`
"""
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pre = load_pre_state_alpha()
    inst = load_institutional_mainwave()
    current = load_current_g3_v4_combo()
    all_candidates = pd.concat([pre, inst], ignore_index=True, sort=False)

    pre.to_csv(OUT_DIR / "pre_state_alpha_candidates.csv", index=False, encoding="utf-8-sig")
    inst.to_csv(OUT_DIR / "institutional_mainwave_candidates.csv", index=False, encoding="utf-8-sig")

    runs: dict[str, tuple[str, pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
    for key, label, cands in [
        ("pre_state_alpha_always_on", "pre_state_alpha 静态 always-on", pre),
        ("institutional_mainwave_always_on", "institutional_mainwave 静态 always-on", inst),
        ("current_g3_v4_combo_always_on", "当前 G3 V4 combo 静态 always-on", current),
    ]:
        trades, curve, h = backtest_slots(cands, key, label, None, priority_mode="source_priority")
        runs[key] = (label, trades, curve, h)

    for variant in HEALTH_VARIANTS:
        health = rolling_source_history(all_candidates, variant)
        key = f"health_router_{variant.key}"
        label = f"pre_state_alpha + institutional_mainwave {variant.label}"
        trades, curve, h = backtest_slots(all_candidates, key, label, health, priority_mode="health_score")
        h["health_variant"] = variant.key if not h.empty else variant.key
        runs[key] = (label, trades, curve, h)

    summary_rows = []
    annual_frames = []
    window_frames = []
    group_frames = []
    health_frames = []
    for key, (label, trades, curve, h) in runs.items():
        summary_rows.append(metrics_from_trades(trades, curve, key, label))
        annual_frames.append(annual_metrics(trades, key, label))
        window_frames.append(window_metrics(trades, curve, key, label))
        group_frames.append(group_metrics(trades, key, label))
        if not h.empty:
            health_frames.append(h)
        trades.to_csv(OUT_DIR / f"{key}_selected.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(OUT_DIR / f"{key}_curve.csv", index=False, encoding="utf-8-sig")

    summary = pd.DataFrame(summary_rows)
    annual = pd.concat([x for x in annual_frames if not x.empty], ignore_index=True) if annual_frames else pd.DataFrame()
    windows = pd.concat([x for x in window_frames if not x.empty], ignore_index=True) if window_frames else pd.DataFrame()
    groups = pd.concat([x for x in group_frames if not x.empty], ignore_index=True) if group_frames else pd.DataFrame()
    health_log = pd.concat(health_frames, ignore_index=True) if health_frames else pd.DataFrame()

    summary.to_csv(OUT_DIR / "closure_summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "closure_annual_summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "closure_window_summary.csv", index=False, encoding="utf-8-sig")
    groups.to_csv(OUT_DIR / "closure_group_summary.csv", index=False, encoding="utf-8-sig")
    health_log.to_csv(OUT_DIR / "closure_health_log.csv", index=False, encoding="utf-8-sig")
    write_report(summary, windows, annual, groups, health_log)

    print(f"wrote {OUT_DIR}")
    print(summary.sort_values("capital_return_proxy", ascending=False).to_string(index=False))


if __name__ == "__main__":
    main()
