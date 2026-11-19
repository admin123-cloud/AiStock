from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _ret_from_price, _sql_literal  # noqa: E402
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _trade_calendar  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SRC_DIR = ROOT / "reports" / "gen3_v4_strong_entry_warning_early_exit_v1"
QUALITY_PATH = ROOT / "reports" / "gen3_v4_strong_entry_quality_audit_v1" / "entry_quality_enriched.csv"
OUT_DIR = ROOT / "reports" / "gen3_v4_strong_entry_warning_d1d2_30m_weak_confirm_v1"
BASE_PATH = SRC_DIR / "base_093_candidates.csv"

COST_BPS = 30.0
WEAK_RET = -0.03
CUTOFF = "10:30:00"


def pct(v: Any) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
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


def next_trade_date_map(calendar: list[pd.Timestamp]) -> dict[pd.Timestamp, pd.Timestamp]:
    return {calendar[i]: calendar[i + 1] for i in range(len(calendar) - 1)}


def load_base() -> pd.DataFrame:
    d = pd.read_csv(BASE_PATH, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["score", "strong_day_rank", "entry_price", "policy_net_ret", "route_priority"]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["entry_date", "policy_exit_date", "code", "entry_price", "policy_net_ret"]).copy()

    q = pd.read_csv(QUALITY_PATH, low_memory=False, encoding="utf-8-sig")
    q["entry_date"] = pd.to_datetime(q["entry_date"], errors="coerce").dt.normalize()
    q["code"] = q["code"].astype(str)
    keep = [
        "entry_date",
        "code",
        "entry_upper_shadow",
        "entry_break_high20",
        "d1_close_ret",
        "d2_close_ret",
        "min_low_ret",
        "max_high_ret",
        "giveback_from_high",
        "failure_tag",
    ]
    q = q[[c for c in keep if c in q.columns]].copy()
    for col in ["entry_upper_shadow", "entry_break_high20", "d1_close_ret", "d2_close_ret", "min_low_ret", "max_high_ret", "giveback_from_high"]:
        if col in q.columns:
            q[col] = pd.to_numeric(q[col], errors="coerce")
    d = d.merge(q, on=["entry_date", "code"], how="left", suffixes=("", "_quality"))
    for col in [
        "entry_upper_shadow",
        "entry_break_high20",
        "d1_close_ret",
        "d2_close_ret",
        "min_low_ret",
        "max_high_ret",
        "giveback_from_high",
        "failure_tag",
    ]:
        qcol = f"{col}_quality"
        if qcol in d.columns:
            if col in d.columns:
                d[col] = d[col].combine_first(d[qcol])
            else:
                d[col] = d[qcol]
            d = d.drop(columns=[qcol])
    d["entry_warning_strict"] = d["entry_upper_shadow"].ge(0.50) & d["entry_break_high20"].le(-0.02)
    d["original_policy_net_ret"] = d["policy_net_ret"]
    return d


def add_d1_d2_dates(d: pd.DataFrame) -> pd.DataFrame:
    calendar = _trade_calendar(d["entry_date"].min(), d["policy_exit_date"].max() + pd.Timedelta(days=20))
    nxt = next_trade_date_map(calendar)
    out = d.copy()
    out["d1_date"] = out["entry_date"].map(nxt)
    out["d2_date"] = out["d1_date"].map(nxt)
    return out


def load_30m_bars(candidates: pd.DataFrame) -> pd.DataFrame:
    rows = candidates[["code", "d1_date", "d2_date"]].copy()
    pairs: set[tuple[str, str]] = set()
    for row in rows.itertuples(index=False):
        for day in [row.d1_date, row.d2_date]:
            if pd.notna(day):
                pairs.add((str(row.code), pd.Timestamp(day).strftime("%Y-%m-%d")))
    if not pairs:
        return pd.DataFrame()
    codes = sorted({code for code, _ in pairs})
    dates = sorted({day for _, day in pairs})
    parts: list[pd.DataFrame] = []
    for di in range(0, len(dates), 80):
        date_chunk = dates[di : di + 80]
        date_list = ",".join(f"toDate({_sql_literal(day)})" for day in date_chunk)
        for ci in range(0, len(codes), 250):
            code_chunk = codes[ci : ci + 250]
            quoted = ",".join(_sql_literal(code) for code in code_chunk)
            sql = f"""
            SELECT code, datetime, open, high, low, close, volume, amount
            FROM kline_minute_30
            WHERE code IN ({quoted})
              AND toDate(datetime) IN ({date_list})
            ORDER BY code, datetime
            """
            part = clickhouse_query_df(sql)
            if not part.empty:
                parts.append(part)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if bars.empty:
        return bars
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["trade_date"] = bars["datetime"].dt.normalize()
    bars["time_text"] = bars["datetime"].dt.strftime("%H:%M:%S")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    return bars.dropna(subset=["code", "datetime", "trade_date", "open", "high", "low", "close"]).sort_values(["code", "datetime"])


def bar_context(g: pd.DataFrame, entry_price: float, cutoff: str = CUTOFF) -> dict[str, Any]:
    if g.empty or entry_price <= 0:
        return {"status": "missing"}
    usable = g[g["time_text"] <= cutoff].sort_values("datetime")
    if usable.empty:
        return {"status": "no_cutoff_bar", "bar_count": int(len(g))}
    bar = usable.iloc[-1]
    after = g[g["datetime"] >= bar["datetime"]]
    return {
        "status": "ok",
        "bar_count": int(len(g)),
        "cutoff_datetime": pd.Timestamp(bar["datetime"]),
        "cutoff_close": float(bar["close"]),
        "cutoff_ret": float(bar["close"]) / entry_price - 1.0,
        "cutoff_low_ret": float(usable["low"].min()) / entry_price - 1.0,
        "day_high_after_cutoff_ret": float(after["high"].max()) / entry_price - 1.0 if not after.empty else pd.NA,
        "day_close_ret": float(g.sort_values("datetime")["close"].iloc[-1]) / entry_price - 1.0,
    }


def apply_policy(candidates: pd.DataFrame, bars: pd.DataFrame, action: str) -> pd.DataFrame:
    d = candidates.copy()
    d["m30_action"] = action
    d["m30_exit_note"] = "base_policy"
    d["m30_exit_datetime"] = pd.NaT
    d["m30_exit_ret_30bps"] = pd.NA
    d["m30_d1_1030_ret"] = pd.NA
    d["m30_d2_1030_ret"] = pd.NA
    d["m30_d1_high_after_1030_ret"] = pd.NA
    d["m30_d2_high_after_1030_ret"] = pd.NA
    by_key = {(str(code), pd.Timestamp(day).normalize()): g.copy() for (code, day), g in bars.groupby(["code", "trade_date"])}

    for idx, row in d[d["route"].astype(str).eq("strong_main") & d["entry_warning_strict"].fillna(False)].iterrows():
        code = str(row["code"])
        entry_price = float(row["entry_price"])
        old_exit = pd.Timestamp(row["policy_exit_date"]).normalize()
        chosen: dict[str, Any] | None = None
        chosen_note = ""
        for label, date_col in [("d1", "d1_date"), ("d2", "d2_date")]:
            day = row.get(date_col)
            if pd.isna(day):
                continue
            day = pd.Timestamp(day).normalize()
            if day >= old_exit:
                continue
            ctx = bar_context(by_key.get((code, day), pd.DataFrame()), entry_price)
            if label == "d1":
                d.at[idx, "m30_d1_1030_ret"] = ctx.get("cutoff_ret", pd.NA)
                d.at[idx, "m30_d1_high_after_1030_ret"] = ctx.get("day_high_after_cutoff_ret", pd.NA)
            else:
                d.at[idx, "m30_d2_1030_ret"] = ctx.get("cutoff_ret", pd.NA)
                d.at[idx, "m30_d2_high_after_1030_ret"] = ctx.get("day_high_after_cutoff_ret", pd.NA)
            if ctx.get("status") == "ok" and float(ctx["cutoff_ret"]) <= WEAK_RET:
                chosen = ctx
                chosen_note = f"strict_warning_{label}_1030_weak3"
                break
        if not chosen:
            continue
        exit_ret = _ret_from_price(float(chosen["cutoff_close"]), entry_price, COST_BPS)
        if exit_ret is None:
            continue
        original = float(row["policy_net_ret"])
        d.at[idx, "m30_exit_note"] = chosen_note
        d.at[idx, "m30_exit_datetime"] = chosen["cutoff_datetime"]
        d.at[idx, "m30_exit_ret_30bps"] = exit_ret
        if action == "fast_exit":
            d.at[idx, "policy_exit_date"] = pd.Timestamp(chosen["cutoff_datetime"]).normalize()
            d.at[idx, "policy_net_ret"] = exit_ret
        elif action == "half_proxy":
            d.at[idx, "policy_net_ret"] = 0.5 * exit_ret + 0.5 * original
        else:
            raise ValueError(action)

    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    return d


def apply_persistent_policy(candidates: pd.DataFrame, bars: pd.DataFrame, action: str) -> pd.DataFrame:
    d = candidates.copy()
    d["m30_action"] = f"persistent_{action}"
    d["m30_exit_note"] = "base_policy"
    d["m30_exit_datetime"] = pd.NaT
    d["m30_exit_ret_30bps"] = pd.NA
    d["m30_d1_1030_ret"] = pd.NA
    d["m30_d2_1030_ret"] = pd.NA
    d["m30_d1_high_after_1030_ret"] = pd.NA
    d["m30_d2_high_after_1030_ret"] = pd.NA
    by_key = {(str(code), pd.Timestamp(day).normalize()): g.copy() for (code, day), g in bars.groupby(["code", "trade_date"])}

    for idx, row in d[d["route"].astype(str).eq("strong_main") & d["entry_warning_strict"].fillna(False)].iterrows():
        code = str(row["code"])
        entry_price = float(row["entry_price"])
        old_exit = pd.Timestamp(row["policy_exit_date"]).normalize()
        d1 = row.get("d1_date")
        d2 = row.get("d2_date")
        if pd.isna(d1) or pd.isna(d2):
            continue
        d1 = pd.Timestamp(d1).normalize()
        d2 = pd.Timestamp(d2).normalize()
        if d2 >= old_exit:
            continue
        d1_ctx = bar_context(by_key.get((code, d1), pd.DataFrame()), entry_price)
        d2_ctx = bar_context(by_key.get((code, d2), pd.DataFrame()), entry_price)
        d.at[idx, "m30_d1_1030_ret"] = d1_ctx.get("cutoff_ret", pd.NA)
        d.at[idx, "m30_d2_1030_ret"] = d2_ctx.get("cutoff_ret", pd.NA)
        d.at[idx, "m30_d1_high_after_1030_ret"] = d1_ctx.get("day_high_after_cutoff_ret", pd.NA)
        d.at[idx, "m30_d2_high_after_1030_ret"] = d2_ctx.get("day_high_after_cutoff_ret", pd.NA)
        if d1_ctx.get("status") != "ok" or d2_ctx.get("status") != "ok":
            continue
        if float(d1_ctx["cutoff_ret"]) > WEAK_RET or float(d2_ctx["cutoff_ret"]) > WEAK_RET:
            continue
        exit_ret = _ret_from_price(float(d2_ctx["cutoff_close"]), entry_price, COST_BPS)
        if exit_ret is None:
            continue
        original = float(row["policy_net_ret"])
        d.at[idx, "m30_exit_note"] = "strict_warning_d1_and_d2_1030_weak3"
        d.at[idx, "m30_exit_datetime"] = d2_ctx["cutoff_datetime"]
        d.at[idx, "m30_exit_ret_30bps"] = exit_ret
        if action == "fast_exit":
            d.at[idx, "policy_exit_date"] = pd.Timestamp(d2_ctx["cutoff_datetime"]).normalize()
            d.at[idx, "policy_net_ret"] = exit_ret
        elif action == "half_proxy":
            d.at[idx, "policy_net_ret"] = 0.5 * exit_ret + 0.5 * original
        else:
            raise ValueError(action)

    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    return d


def classify(row: pd.Series) -> str:
    old = row["original_policy_net_ret"]
    new = row["policy_net_ret"]
    delta = new - old
    if old < 0 and delta > 0:
        return "救亏损"
    if old > 0 and delta < 0:
        return "误伤盈利"
    if old < 0 and delta < 0:
        return "加深亏损"
    if old > 0 and delta > 0:
        return "改善盈利"
    return "中性"


def summary(candidates: pd.DataFrame, variant: str) -> dict[str, Any]:
    d = candidates.copy()
    strong = d[d["route"].astype(str).eq("strong_main")]
    triggered = strong[strong["m30_exit_note"].astype(str).ne("base_policy")]
    return {
        "variant": variant,
        "rows": int(len(d)),
        "strong_rows": int(len(strong)),
        "triggered_rows": int(len(triggered)),
        "all_mean_ret": float(d["policy_net_ret"].mean()),
        "strong_mean_ret": float(strong["policy_net_ret"].mean()) if len(strong) else 0.0,
        "triggered_original_mean": float(triggered["original_policy_net_ret"].mean()) if len(triggered) else 0.0,
        "triggered_new_mean": float(triggered["policy_net_ret"].mean()) if len(triggered) else 0.0,
        "triggered_delta_mean": float((triggered["policy_net_ret"] - triggered["original_policy_net_ret"]).mean()) if len(triggered) else 0.0,
        "triggered_worst_new": float(triggered["policy_net_ret"].min()) if len(triggered) else 0.0,
        "note_counts": {str(k): int(v) for k, v in triggered["m30_exit_note"].value_counts().to_dict().items()},
        "outcome_counts": {str(k): int(v) for k, v in triggered["outcome"].value_counts().to_dict().items()} if "outcome" in triggered.columns else {},
    }


def write_report(summary_df: pd.DataFrame, triggered: pd.DataFrame) -> None:
    pct_cols = {
        "all_mean_ret",
        "strong_mean_ret",
        "triggered_original_mean",
        "triggered_new_mean",
        "triggered_delta_mean",
        "triggered_worst_new",
        "原始均值",
        "新均值",
        "平均变化",
    }
    by_outcome = (
        triggered.groupby("outcome")
        .agg(
            笔数=("code", "count"),
            原始均值=("original_policy_net_ret", "mean"),
            新均值=("policy_net_ret", "mean"),
            平均变化=("delta_ret", "mean"),
            D1触发=("m30_exit_note", lambda s: int(s.astype(str).str.contains("_d1_").sum())),
            D2触发=("m30_exit_note", lambda s: int(s.astype(str).str.contains("_d2_").sum())),
        )
        .reset_index()
        if not triggered.empty
        else pd.DataFrame()
    )
    focus = triggered.sort_values("delta_ret").copy()
    for col in [
        "original_policy_net_ret",
        "policy_net_ret",
        "delta_ret",
        "m30_exit_ret_30bps",
        "m30_d1_1030_ret",
        "m30_d2_1030_ret",
        "m30_d1_high_after_1030_ret",
        "m30_d2_high_after_1030_ret",
        "max_high_ret",
    ]:
        if col in focus.columns:
            focus[col] = focus[col].map(pct)
    focus["entry_date"] = pd.to_datetime(focus["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    focus_cols = [
        "entry_date",
        "code",
        "name",
        "original_policy_net_ret",
        "policy_net_ret",
        "delta_ret",
        "outcome",
        "m30_exit_note",
        "m30_exit_ret_30bps",
        "m30_d1_1030_ret",
        "m30_d2_1030_ret",
        "m30_d1_high_after_1030_ret",
        "m30_d2_high_after_1030_ret",
        "max_high_ret",
        "failure_tag",
    ]
    focus = focus[[c for c in focus_cols if c in focus.columns]].rename(
        columns={
            "entry_date": "入场日",
            "code": "代码",
            "name": "名称",
            "original_policy_net_ret": "原始收益",
            "policy_net_ret": "30m处理后",
            "delta_ret": "变化",
            "outcome": "结果",
            "m30_exit_note": "触发",
            "m30_exit_ret_30bps": "30m退出收益",
            "m30_d1_1030_ret": "D1 10:30",
            "m30_d2_1030_ret": "D2 10:30",
            "m30_d1_high_after_1030_ret": "D1 10:30后最高",
            "m30_d2_high_after_1030_ret": "D2 10:30后最高",
            "max_high_ret": "持仓最高",
            "failure_tag": "失败标签",
        }
    )

    lines = [
        "# G3 V4 strong_main 入场警戒 + D1/D2 30m 早期弱确认 v1",
        "",
        "## 边界",
        "",
        "- 不使用 score/rank 新增过滤。",
        "- 固定样本：`strong_second_score_ge_093` 中的 strong_main。",
        "- 入场质量警戒：入场日上影 >= 50% 且收盘低于 20 日高点 2% 以上。",
        "- 早期弱确认：D1 或 D2 的 10:30 30m 收盘相对入场价 <= -3%。",
        "- 只验证结构，不调阈值；`half_proxy` 仍是半仓代理，不是完整现金流复算。",
        "",
        "## 汇总",
        "",
        md_table(summary_df, pct_cols=pct_cols),
        "",
        "## 触发结果分类",
        "",
        md_table(by_outcome, pct_cols=pct_cols),
        "",
        "## 触发明细",
        "",
        md_table(focus.head(40)),
        "",
        "## 下一步目标",
        "",
        "- 如果 30m 早期确认仍然误伤盈利，说明强势票的 D1/D2 早盘弱并不等于失败，需要改成“弱后无法修复”而不是“早盘一弱就卖”。",
        "- 如果 30m 能减少加深亏损但总体收益下降，下一步只把它作为仓位减半/观察标签，不进入正式退出。",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = add_d1_d2_dates(load_base())
    bars = load_30m_bars(base)
    variants = {"base": base.copy()}
    for action in ["fast_exit", "half_proxy"]:
        d = apply_policy(base, bars, action)
        d["delta_ret"] = d["policy_net_ret"] - d["original_policy_net_ret"]
        d["outcome"] = d.apply(classify, axis=1)
        variants[f"m30_d1d2_1030_{action}"] = d
        d.to_csv(OUT_DIR / f"m30_d1d2_1030_{action}_candidates.csv", index=False, encoding="utf-8-sig")
        p = apply_persistent_policy(base, bars, action)
        p["delta_ret"] = p["policy_net_ret"] - p["original_policy_net_ret"]
        p["outcome"] = p.apply(classify, axis=1)
        variants[f"m30_d1_and_d2_1030_{action}"] = p
        p.to_csv(OUT_DIR / f"m30_d1_and_d2_1030_{action}_candidates.csv", index=False, encoding="utf-8-sig")

    base_out = base.copy()
    base_out["m30_exit_note"] = "base_policy"
    base_out["outcome"] = "中性"
    base_out["delta_ret"] = 0.0
    base_out.to_csv(OUT_DIR / "base_candidates.csv", index=False, encoding="utf-8-sig")

    rows = []
    for name, d in variants.items():
        if name == "base":
            dd = base_out.copy()
        else:
            dd = d
        rows.append(summary(dd, name))
    summary_df = pd.DataFrame(rows)
    summary_df.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")

    triggered = variants["m30_d1_and_d2_1030_half_proxy"]
    triggered = triggered[triggered["route"].astype(str).eq("strong_main") & triggered["m30_exit_note"].astype(str).ne("base_policy")].copy()
    triggered.to_csv(OUT_DIR / "triggered_trades_persistent_half_proxy.csv", index=False, encoding="utf-8-sig")
    write_report(summary_df, triggered)
    print(f"done: {OUT_DIR}")
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()
