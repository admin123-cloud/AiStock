from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "gen3_range_weak_30m_tail_visibility_v1"
SOURCES = {
    "range_stress_top1": ROOT / "reports" / "gen3_range_weak_shadow_mtm_v1" / "range_stress_top1_cost30_closed_trades.csv",
    "weak_low_top1": ROOT / "reports" / "gen3_range_weak_shadow_mtm_v1" / "weak_low_top1_cost30_closed_trades.csv",
}
WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "valid_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-12-31"),
    "full": ("2020-01-01", "2026-12-31"),
}


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _pct(value: object) -> str:
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
            val = row[col]
            if col in pct_cols:
                item[col] = _pct(val)
            elif isinstance(val, float):
                item[col] = f"{val:.4f}"
            else:
                item[col] = "" if pd.isna(val) else str(val)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _load_trades() -> pd.DataFrame:
    parts = []
    for book, path in SOURCES.items():
        d = pd.read_csv(path, low_memory=False)
        d["book"] = book
        parts.append(d)
    out = pd.concat(parts, ignore_index=True)
    for col in ["trade_date", "entry_date", "policy_exit_date"]:
        out[col] = pd.to_datetime(out[col], errors="coerce").dt.normalize()
    for col in [
        "entry_price",
        "net_ret",
        "cost_bps",
        "amount_ratio20",
        "drawdown20",
        "breadth_ma20",
        "index_mom20",
        "close_position",
        "gap_open",
    ]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out["bad10"] = out["net_ret"] <= -0.10
    out["bad15"] = out["net_ret"] <= -0.15
    out["year"] = out["entry_date"].dt.year
    return out.dropna(subset=["code", "entry_date", "policy_exit_date", "entry_price", "net_ret"]).reset_index(drop=True)


def _load_bars(code: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if not re.fullmatch(r"[0-9A-Z.]+", str(code)):
        return pd.DataFrame()
    sql = f"""
    SELECT code, datetime, open, high, low, close, volume, amount
    FROM kline_minute_30
    WHERE code = {_sql_literal(str(code))}
      AND datetime >= toDateTime({_sql_literal(start.strftime("%Y-%m-%d 00:00:00"))})
      AND datetime <= toDateTime({_sql_literal((end + pd.Timedelta(days=1)).strftime("%Y-%m-%d 23:59:59"))})
    ORDER BY datetime
    """
    d = clickhouse_query_df(sql)
    if d.empty:
        return d
    d["datetime"] = pd.to_datetime(d["datetime"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["datetime", "open", "high", "low", "close"]).copy()


def _scale_from_entry_open(bars: pd.DataFrame, entry_price: float, entry_date: pd.Timestamp) -> tuple[pd.DataFrame, float, str]:
    if bars.empty or entry_price <= 0:
        return bars, 1.0, "missing"
    same_day = bars[bars["datetime"].dt.normalize().eq(entry_date)].copy()
    if not same_day.empty and float(same_day.iloc[0]["open"]) > 0:
        ref = float(same_day.iloc[0]["open"])
        source = "entry_day_first_30m_open"
    elif float(bars.iloc[0]["open"]) > 0:
        ref = float(bars.iloc[0]["open"])
        source = "first_available_30m_open"
    else:
        ref = 0.0
        source = "invalid_open"
    scale = entry_price / ref if ref > 0 else 1.0
    out = bars.copy()
    for col in ["open", "high", "low", "close"]:
        out[f"adj_{col}"] = out[col] * scale
    return out, float(scale), source


def _first_dt(path: pd.DataFrame, col: str, threshold: float) -> pd.Timestamp | pd.NaT:
    hit = path[path[col] <= threshold]
    if hit.empty:
        return pd.NaT
    return pd.Timestamp(hit.iloc[0]["datetime"])


def _exit_ret(path: pd.DataFrame, trigger_col: str, threshold: float, cost_bps: float) -> tuple[bool, pd.Timestamp | pd.NaT, float | None]:
    hit = path[path[trigger_col] <= threshold]
    if hit.empty:
        return False, pd.NaT, None
    bar = hit.iloc[0]
    return True, pd.Timestamp(bar["datetime"]), float(bar["close_ret"]) - cost_bps / 10000.0


def _no_reclaim_exit(path: pd.DataFrame, cost_bps: float, wait_bars: int = 2) -> tuple[bool, pd.Timestamp | pd.NaT, float | None, bool | None]:
    stop = path[path["low_ret"] <= -0.05]
    if stop.empty:
        return False, pd.NaT, None, None
    first_idx = stop.index[0]
    loc = path.index.get_loc(first_idx)
    after = path.iloc[loc + 1 : loc + 1 + wait_bars].copy()
    if after.empty:
        return False, pd.NaT, None, False
    reclaimed = bool((after["close_ret"] >= 0.0).any())
    if reclaimed:
        return False, pd.NaT, None, True
    bar = after.iloc[-1]
    return True, pd.Timestamp(bar["datetime"]), float(bar["close_ret"]) - cost_bps / 10000.0, False


def _replay(row: pd.Series) -> dict:
    code = str(row["code"])
    entry_date = pd.Timestamp(row["entry_date"]).normalize()
    exit_date = pd.Timestamp(row["policy_exit_date"]).normalize()
    entry_price = float(row["entry_price"])
    cost_bps = float(row.get("cost_bps") or 30.0)
    bars = _load_bars(code, entry_date, exit_date)
    out = row.to_dict()
    if bars.empty:
        out.update({"status": "missing_30m", "bars": 0})
        return out
    bars, scale, scale_source = _scale_from_entry_open(bars, entry_price, entry_date)
    path = bars[(bars["datetime"].dt.normalize() >= entry_date) & (bars["datetime"].dt.normalize() <= exit_date)].copy()
    if path.empty:
        out.update({"status": "no_bar_in_holding_window", "bars": 0, "scale": scale, "scale_source": scale_source})
        return out
    path["low_ret"] = path["adj_low"] / entry_price - 1.0
    path["close_ret"] = path["adj_close"] / entry_price - 1.0
    path["high_ret"] = path["adj_high"] / entry_price - 1.0
    min_low_idx = path["low_ret"].idxmin()
    min_close_idx = path["close_ret"].idxmin()
    out.update(
        {
            "status": "ok",
            "bars": int(len(path)),
            "scale": scale,
            "scale_source": scale_source,
            "min_30m_low_ret": float(path["low_ret"].min()),
            "min_30m_low_datetime": pd.Timestamp(path.loc[min_low_idx, "datetime"]),
            "min_30m_close_ret": float(path["close_ret"].min()),
            "min_30m_close_datetime": pd.Timestamp(path.loc[min_close_idx, "datetime"]),
            "max_30m_close_ret": float(path["close_ret"].max()),
        }
    )
    for threshold in [-0.05, -0.08, -0.10, -0.12]:
        tag = str(abs(int(threshold * 100)))
        out[f"m30_low_le_{tag}pct_datetime"] = _first_dt(path, "low_ret", threshold)
        out[f"m30_close_le_{tag}pct_datetime"] = _first_dt(path, "close_ret", threshold)
        out[f"m30_low_le_{tag}pct"] = pd.notna(out[f"m30_low_le_{tag}pct_datetime"])
        out[f"m30_close_le_{tag}pct"] = pd.notna(out[f"m30_close_le_{tag}pct_datetime"])
    for policy, col, threshold in [
        ("m30_close_m5_exit", "close_ret", -0.05),
        ("m30_close_m8_exit", "close_ret", -0.08),
        ("m30_low_m8_exit_at_close", "low_ret", -0.08),
        ("m30_low_m12_exit_at_close", "low_ret", -0.12),
    ]:
        hit, dt, ret = _exit_ret(path, col, threshold, cost_bps)
        out[f"{policy}_hit"] = hit
        out[f"{policy}_datetime"] = dt
        out[f"{policy}_net_ret"] = ret if ret is not None else float(row["net_ret"])
        out[f"{policy}_delta_vs_fixed"] = float(out[f"{policy}_net_ret"]) - float(row["net_ret"])
    hit, dt, ret, reclaimed = _no_reclaim_exit(path, cost_bps, wait_bars=2)
    out["m30_low_m5_no_reclaim2_exit_hit"] = hit
    out["m30_low_m5_no_reclaim2_exit_datetime"] = dt
    out["m30_low_m5_reclaimed_in_2bar"] = reclaimed
    out["m30_low_m5_no_reclaim2_exit_net_ret"] = ret if ret is not None else float(row["net_ret"])
    out["m30_low_m5_no_reclaim2_exit_delta_vs_fixed"] = float(out["m30_low_m5_no_reclaim2_exit_net_ret"]) - float(row["net_ret"])
    return out


def _summarize_visibility(d: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    ok = d[d["status"].eq("ok")].copy()
    if ok.empty:
        return pd.DataFrame()
    rows = []
    for keys, g in ok.groupby(group_cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        base = dict(zip(group_cols, keys))
        bad10 = g[g["bad10"]].copy()
        row = {
            **base,
            "rows": int(len(g)),
            "bad10": int(g["bad10"].sum()),
            "bad15": int(g["bad15"].sum()),
            "mean_net_ret": float(g["net_ret"].mean()),
            "median_net_ret": float(g["net_ret"].median()),
            "worst_net_ret": float(g["net_ret"].min()),
            "mean_min_30m_low": float(g["min_30m_low_ret"].mean()),
            "p10_min_30m_low": float(g["min_30m_low_ret"].quantile(0.10)),
            "m30_close5_rate": float(g["m30_close_le_5pct"].mean()),
            "m30_low8_rate": float(g["m30_low_le_8pct"].mean()),
            "m30_close8_rate": float(g["m30_close_le_8pct"].mean()),
            "bad10_m30_close5_cover": float(bad10["m30_close_le_5pct"].mean()) if len(bad10) else 0.0,
            "bad10_m30_low8_cover": float(bad10["m30_low_le_8pct"].mean()) if len(bad10) else 0.0,
            "bad10_m30_close8_cover": float(bad10["m30_close_le_8pct"].mean()) if len(bad10) else 0.0,
        }
        rows.append(row)
    return pd.DataFrame(rows)


def _policy_probe(d: pd.DataFrame) -> pd.DataFrame:
    ok = d[d["status"].eq("ok")].copy()
    policies = [
        "m30_close_m5_exit",
        "m30_close_m8_exit",
        "m30_low_m8_exit_at_close",
        "m30_low_m12_exit_at_close",
        "m30_low_m5_no_reclaim2_exit",
    ]
    rows = []
    for policy in policies:
        ret_col = f"{policy}_net_ret"
        hit_col = f"{policy}_hit"
        delta_col = f"{policy}_delta_vs_fixed"
        for book, g in ok.groupby("book", dropna=False):
            rows.append(
                {
                    "policy": policy,
                    "book": book,
                    "rows": int(len(g)),
                    "hit_rate": float(g[hit_col].mean()),
                    "fixed_mean": float(g["net_ret"].mean()),
                    "policy_mean": float(g[ret_col].mean()),
                    "delta_mean": float(g[delta_col].mean()),
                    "fixed_bad10_rate": float((g["net_ret"] <= -0.10).mean()),
                    "policy_bad10_rate": float((g[ret_col] <= -0.10).mean()),
                    "fixed_worst": float(g["net_ret"].min()),
                    "policy_worst": float(g[ret_col].min()),
                    "bad10_cover": float(g.loc[g["bad10"], hit_col].mean()) if bool(g["bad10"].any()) else 0.0,
                    "good5_hurt_rate": float(((g["net_ret"] >= 0.05) & (g[delta_col] < -0.02)).mean()),
                }
            )
    return pd.DataFrame(rows)


def _window_summary(d: pd.DataFrame) -> pd.DataFrame:
    ok = d[d["status"].eq("ok")].copy()
    rows = []
    for window, (start, end) in WINDOWS.items():
        w = ok[(ok["entry_date"] >= pd.Timestamp(start)) & (ok["entry_date"] <= pd.Timestamp(end))]
        if w.empty:
            rows.append({"window": window, "rows": 0})
            continue
        bad10 = w[w["bad10"]]
        rows.append(
            {
                "window": window,
                "rows": int(len(w)),
                "bad10": int(w["bad10"].sum()),
                "mean_net_ret": float(w["net_ret"].mean()),
                "worst_net_ret": float(w["net_ret"].min()),
                "m30_close5_rate": float(w["m30_close_le_5pct"].mean()),
                "m30_low8_rate": float(w["m30_low_le_8pct"].mean()),
                "bad10_m30_close5_cover": float(bad10["m30_close_le_5pct"].mean()) if len(bad10) else 0.0,
                "bad10_m30_low8_cover": float(bad10["m30_low_le_8pct"].mean()) if len(bad10) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _write_report(visible: pd.DataFrame, summary: pd.DataFrame, windows: pd.DataFrame, policy: pd.DataFrame, worst: pd.DataFrame) -> None:
    pct_cols = {
        "mean_net_ret",
        "median_net_ret",
        "worst_net_ret",
        "mean_min_30m_low",
        "p10_min_30m_low",
        "m30_close5_rate",
        "m30_low8_rate",
        "m30_close8_rate",
        "bad10_m30_close5_cover",
        "bad10_m30_low8_cover",
        "bad10_m30_close8_cover",
        "hit_rate",
        "fixed_mean",
        "policy_mean",
        "delta_mean",
        "fixed_bad10_rate",
        "policy_bad10_rate",
        "fixed_worst",
        "policy_worst",
        "bad10_cover",
        "good5_hurt_rate",
        "net_ret",
        "min_30m_low_ret",
        "min_30m_close_ret",
        "m30_close_m5_exit_delta_vs_fixed",
        "m30_low_m8_exit_at_close_delta_vs_fixed",
    }
    status = visible.groupby("status").size().reset_index(name="rows")
    worst_view = worst[
        [
            "book",
            "entry_date",
            "policy_exit_date",
            "code",
            "name",
            "net_ret",
            "min_30m_low_ret",
            "min_30m_close_ret",
            "m30_close_le_5pct",
            "m30_low_le_8pct",
            "m30_close_m5_exit_delta_vs_fixed",
            "m30_low_m8_exit_at_close_delta_vs_fixed",
        ]
    ].copy()
    lines = [
        "# G3 Range/Weak 30m 尾部可见性审计 V1",
        "",
        "## 口径",
        "",
        "- 样本：第39步 30bps shadow 成交，`range_stress_top1` 与 `weak_low_top1` 全部闭合交易。",
        "- 买入口径：沿用 shadow 的 `entry_date` 与 `entry_price`，用买入日第一根 30m open 将分钟价格校准到日线价格尺度。",
        "- 可见性：只统计买入后到原固定退出日之间的 30m bar，不使用退出后的信息。",
        "- 本报告只判断执行层是否能看见尾部风险，不把任何 30m 规则直接纳入正式 G3。",
        "",
        "## 数据覆盖",
        "",
        _md_table(status),
        "",
        "## 分链路可见性",
        "",
        _md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口可见性",
        "",
        _md_table(windows, pct_cols=pct_cols),
        "",
        "## 规则探针",
        "",
        _md_table(policy, pct_cols=pct_cols),
        "",
        "## 最差样本复盘",
        "",
        _md_table(worst_view, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- `30m close <= -5%` 是最敏感的尾部可见信号，但会打到一部分后续修复样本，不能直接硬接入。",
        "- `30m low <= -8%` 对 bad10 的覆盖通常更高，但按触发 bar 收盘退出可能已经有较大滑落，需要后续做成交延迟和跌停不可卖压力。",
        "- `30m close <= -8%` 更像极端保护，误伤少一些，但覆盖不足，适合做尾部保护而不是普通卖出规则。",
        "- 下一步应把最有希望的 1-2 个 30m 执行规则放进 range/weak shadow 的逐日 MTM 复算，观察它是否真正降低组合回撤，而不是只改善单笔均值。",
        "",
    ]
    (OUT_DIR / "range_weak_30m_tail_visibility_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = _load_trades()
    visible = pd.DataFrame([_replay(row) for _, row in trades.iterrows()])
    visible.to_csv(OUT_DIR / "range_weak_30m_visibility_trades.csv", index=False, encoding="utf-8-sig")
    ok = visible[visible["status"].eq("ok")].copy()
    summary = _summarize_visibility(ok, ["book"])
    windows = _window_summary(ok)
    policy = _policy_probe(ok)
    worst = ok.sort_values("net_ret").head(30).copy()
    summary.to_csv(OUT_DIR / "visibility_by_book.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "visibility_by_window.csv", index=False, encoding="utf-8-sig")
    policy.to_csv(OUT_DIR / "m30_policy_probe.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "worst30_30m_replay.csv", index=False, encoding="utf-8-sig")
    _write_report(visible, summary, windows, policy, worst)
    print(
        json.dumps(
            {
                "out_dir": str(OUT_DIR),
                "rows": int(len(visible)),
                "ok_rows": int(len(ok)),
                "status": visible.groupby("status").size().to_dict(),
                "summary": summary.to_dict(orient="records"),
                "policy": policy.to_dict(orient="records"),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
