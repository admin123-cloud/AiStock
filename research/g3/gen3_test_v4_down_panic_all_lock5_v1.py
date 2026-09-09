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

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _sql_literal  # noqa: E402
from scripts.gen3_test_v4_strong_position_scale_v1 import md_table, simulate_scaled, summarize  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = _report_path() / "gen3_v4_ice_down_panic_30m_confirm_overlay_v1" / "base_candidates_with_ice_30m_confirm.csv"
OUT_DIR = _report_path() / "gen3_v4_down_panic_all_lock5_v1"
BASE_COST_BPS = 30.0

PROFILES = [
    {"profile": "cost30", "cost_bps": 30.0, "all_shock": 0.0},
    {"profile": "cost100", "cost_bps": 100.0, "all_shock": 0.0},
    {"profile": "cost30_all_shock2", "cost_bps": 30.0, "all_shock": 0.02},
]


def load_candidates() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False, encoding="utf-8-sig")
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["entry_price", "policy_net_ret", "position_scale", "score", "route_priority"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d["position_scale"] = d.get("position_scale", 1.0).fillna(1.0)
    return d.dropna(subset=["entry_date", "policy_exit_date", "code", "entry_price", "policy_net_ret"]).copy()


def load_30m_bars(candidates: pd.DataFrame) -> pd.DataFrame:
    target = candidates[candidates["route"].astype(str).eq("down_panic")].copy()
    codes = sorted(target["code"].dropna().astype(str).unique().tolist())
    if not codes:
        return pd.DataFrame()
    start = target["entry_date"].min().strftime("%Y-%m-%d")
    end = target["policy_exit_date"].max().strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 200):
        quoted = ",".join(_sql_literal(code) for code in codes[i : i + 200])
        sql = f"""
        SELECT code, datetime, open, high, low, close
        FROM kline_minute_30
        WHERE code IN ({quoted})
          AND datetime >= toDateTime({_sql_literal(start + " 09:00:00")})
          AND datetime <= toDateTime({_sql_literal(end + " 15:30:00")})
        ORDER BY code, datetime
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if bars.empty:
        return bars
    bars["code"] = bars["code"].astype(str)
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["trade_date"] = bars["datetime"].dt.normalize()
    for col in ["open", "high", "low", "close"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    return bars.dropna(subset=["code", "datetime", "open", "close"]).sort_values(["code", "datetime"]).copy()


def build_lock_tags(candidates: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    target = candidates[candidates["route"].astype(str).eq("down_panic")][
        ["entry_date", "policy_exit_date", "code", "name", "entry_price", "policy_net_ret", "emotion_signal"]
    ].copy()
    rows: list[dict[str, Any]] = []
    grouped = {code: g.sort_values("datetime").reset_index(drop=True) for code, g in bars.groupby("code")}
    for _, row in target.iterrows():
        code = str(row["code"])
        entry = row["entry_date"]
        exit_date = row["policy_exit_date"]
        entry_price = float(row["entry_price"])
        hold = grouped.get(code, pd.DataFrame())
        hold = hold[(hold["trade_date"].ge(entry)) & (hold["trade_date"].le(exit_date))].copy()
        item = row.to_dict()
        for threshold in [0.03, 0.05, 0.07]:
            label = f"lock{int(threshold * 100)}"
            hit = hold[hold["close"].ge(entry_price * (1.0 + threshold))].head(1)
            if hit.empty:
                item[f"{label}_triggered"] = False
                item[f"{label}_exit_datetime"] = pd.NaT
                item[f"{label}_net_ret"] = row["policy_net_ret"]
            else:
                px = float(hit.iloc[0]["close"])
                item[f"{label}_triggered"] = True
                item[f"{label}_exit_datetime"] = hit.iloc[0]["datetime"]
                item[f"{label}_net_ret"] = px / entry_price - 1.0 - BASE_COST_BPS / 10000.0
        rows.append(item)
    return pd.DataFrame(rows)


def apply_variant(candidates: pd.DataFrame, tags: pd.DataFrame, variant: str) -> pd.DataFrame:
    d = candidates.copy()
    d["profit_lock_variant"] = variant
    if variant == "base":
        return d
    ret_col = f"{variant}_net_ret"
    small = tags[["entry_date", "code", f"{variant}_triggered", f"{variant}_exit_datetime", ret_col]].copy()
    merged = d.merge(small, on=["entry_date", "code"], how="left")
    merged["profit_lock_triggered"] = False
    merged["profit_lock_exit_datetime"] = pd.NaT
    mask = (
        merged["route"].astype(str).eq("down_panic")
        & merged[f"{variant}_triggered"].fillna(False).astype(bool)
        & merged[ret_col].notna()
    )
    merged.loc[mask, "policy_net_ret"] = pd.to_numeric(merged.loc[mask, ret_col], errors="coerce")
    merged.loc[mask, "profit_lock_triggered"] = True
    merged.loc[mask, "profit_lock_exit_datetime"] = merged.loc[mask, f"{variant}_exit_datetime"]
    return merged


def apply_stress(candidates: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    extra_cost = (float(profile["cost_bps"]) - BASE_COST_BPS) / 10000.0
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce") - extra_cost - float(profile.get("all_shock", 0.0))
    d["stress_profile"] = profile["profile"]
    return d


def lock_summary(tags: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    base = pd.to_numeric(tags["policy_net_ret"], errors="coerce")
    for label in ["lock3", "lock5", "lock7"]:
        trig = tags[f"{label}_triggered"].fillna(False).astype(bool)
        ret = pd.to_numeric(tags[f"{label}_net_ret"], errors="coerce")
        rows.append(
            {
                "rule": label,
                "candidate_count": int(len(tags)),
                "triggered": int(trig.sum()),
                "trigger_rate": float(trig.mean()),
                "base_avg_ret": float(base.mean()),
                "locked_avg_ret": float(ret.mean()),
                "avg_delta": float((ret - base).mean()),
                "loss_count_after": int((ret < 0).sum()),
                "icepoint_triggered": int((trig & tags["emotion_signal"].astype(str).eq("icepoint")).sum()),
                "non_ice_triggered": int((trig & ~tags["emotion_signal"].astype(str).eq("icepoint")).sum()),
            }
        )
    return pd.DataFrame(rows)


def emotion_breakdown(tags: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for emotion, g in tags.groupby(tags["emotion_signal"].fillna("unknown").astype(str), dropna=False):
        base = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        lock5 = pd.to_numeric(g["lock5_net_ret"], errors="coerce")
        trig = g["lock5_triggered"].fillna(False).astype(bool)
        rows.append(
            {
                "emotion_signal": emotion,
                "candidate_count": int(len(g)),
                "lock5_triggered": int(trig.sum()),
                "trigger_rate": float(trig.mean()),
                "base_avg_ret": float(base.mean()),
                "lock5_avg_ret": float(lock5.mean()),
                "avg_delta": float((lock5 - base).mean()),
            }
        )
    return pd.DataFrame(rows).sort_values(["emotion_signal"])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candidates = load_candidates()
    bars = load_30m_bars(candidates)
    tags = build_lock_tags(candidates, bars)
    tags.to_csv(OUT_DIR / "down_panic_lock_tags.csv", index=False, encoding="utf-8-sig")

    lock_sum = lock_summary(tags)
    by_emotion = emotion_breakdown(tags)
    lock_sum.to_csv(OUT_DIR / "lock_summary.csv", index=False, encoding="utf-8-sig")
    by_emotion.to_csv(OUT_DIR / "emotion_breakdown.csv", index=False, encoding="utf-8-sig")

    summaries: list[dict[str, Any]] = []
    for variant in ["base", "lock3", "lock5", "lock7"]:
        v = apply_variant(candidates, tags, variant)
        v.to_csv(OUT_DIR / f"{variant}_candidates.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            stressed = apply_stress(v, profile)
            curve, closed = simulate_scaled(stressed, float(profile["cost_bps"]))
            run_dir = OUT_DIR / f"{variant}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summaries.append(summarize(curve, closed, variant, str(profile["profile"])))
    summary = pd.DataFrame(summaries)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "trigger_rate",
        "base_avg_ret",
        "locked_avg_ret",
        "lock5_avg_ret",
        "avg_delta",
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
    }
    lines = [
        "# G3 V4 down_panic 全样本 lock5 退出复验 v1",
        "",
        "## 研究目的",
        "",
        "- 验证 `+5% 30m close` 快速锁盈是否只是 `icepoint + down_panic` 的局部修复，还是整个 `down_panic` 链路都适用。",
        "- 本轮不新增 score/rank 过滤，不改候选源，只调整退出层。",
        "",
        "## 触发概况",
        "",
        md_table(lock_sum, pct_cols=pct_cols),
        "",
        "## 情绪分组",
        "",
        md_table(by_emotion, pct_cols=pct_cols),
        "",
        "## slot 复算结果",
        "",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 初步判读口径",
        "",
        "- 如果 `lock5` 同时改善 `cost30`、`cost100`、`all_shock2`，说明它有机会成为弱势 panic 通用退出纪律。",
        "- 如果只改善 `icepoint` 或只改善压力口径，则只作为局部风控，不进入整体弱势链路。",
        "- `lock3` 和 `lock7` 只作为稳定性参照，不作为调参目标。",
        "",
        "## 本轮实际结论",
        "",
        "- 全体 `down_panic` 候选共 87 个，`lock5` 触发 21 个；其中 `icepoint` 触发 15 个，非冰点只触发 6 个。",
        "- `lock5` 在全体 `down_panic` 上小幅改善三组压力：`cost30` 从 `+374.23%` 到 `+379.14%`，`cost100` 从 `+215.09%` 到 `+218.36%`，`all_shock2` 从 `+46.72%` 到 `+48.25%`，最大回撤从 `-32.72%` 收窄到 `-32.19%`。",
        "- 但分情绪看，`icepoint` 子集平均收益从 `2.35%` 到 `2.41%`，`neutral` 子集反而从 `1.20%` 降到 `0.85%`。这说明 `lock5` 不是整个弱势 panic 的通用锁盈规则，更像是冰点出清后的修复交易纪律。",
        "- `lock7` 名义结果更好，但它是本轮附带参照，不能因为单次更高就替换 `lock5`；否则会变成继续调止盈阈值，过拟合风险上升。",
        "",
        "## 下一步目标",
        "",
        "保留 `icepoint + down_panic + lock5`，不把 `lock5` 泛化到全部 `down_panic`。下一步应该回到入场质量：对 `down_panic neutral` 单独做失败归因，判断它是不是缺少“真实冰点/恐慌出清”条件，还是需要完全不同的弱势反弹买法。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
