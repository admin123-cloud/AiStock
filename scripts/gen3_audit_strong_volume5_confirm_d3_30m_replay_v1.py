from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_strong_volume5_confirm_fail_exit_v1 import _apply_policy, _prepare_base
from scripts.gen3_backtest_strong_volume5_slot_resim_v1 import _md_table
from utils.market_warehouse import clickhouse_query_df


OUT_DIR = ROOT / "reports" / "gen3_strong_volume5_confirm_d3_30m_replay_v1"


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _pct(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v) * 100:.2f}%"


def _load_bars(code: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    sql = f"""
    SELECT code, datetime, open, high, low, close, volume, amount
    FROM kline_minute_30
    WHERE code = {_sql_literal(code)}
      AND datetime >= toDateTime({_sql_literal(start.strftime('%Y-%m-%d 00:00:00'))})
      AND datetime <= toDateTime({_sql_literal((end + pd.Timedelta(days=1)).strftime('%Y-%m-%d 23:59:59'))})
    ORDER BY datetime
    """
    d = clickhouse_query_df(sql)
    if d.empty:
        return d
    d["datetime"] = pd.to_datetime(d["datetime"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    return d.dropna(subset=["datetime", "open", "high", "low", "close"]).copy()


def _scale_bars(bars: pd.DataFrame, entry_price: float, confirm_dt: pd.Timestamp) -> tuple[pd.DataFrame, float, str]:
    if bars.empty or entry_price <= 0:
        return bars, 1.0, "missing"
    exact = bars[bars["datetime"].eq(confirm_dt)]
    ref_price = float(exact["close"].iloc[0]) if not exact.empty and float(exact["close"].iloc[0]) > 0 else None
    source = "confirm_exact"
    if ref_price is None:
        prior = bars[bars["datetime"] <= confirm_dt].tail(1)
        if not prior.empty and float(prior["close"].iloc[0]) > 0:
            ref_price = float(prior["close"].iloc[0])
            source = "confirm_prior"
    if ref_price is None:
        ref_price = float(bars["close"].iloc[0])
        source = "first_bar"
    scale = entry_price / ref_price if ref_price > 0 else 1.0
    out = bars.copy()
    for col in ["open", "high", "low", "close"]:
        out[f"adj_{col}"] = out[col] * scale
    return out, float(scale), source


def _replay(row: pd.Series) -> dict:
    code = str(row["code"])
    entry_date = pd.Timestamp(row["entry_date"]).normalize()
    exit_date = pd.Timestamp(row["policy_exit_date"]).normalize()
    confirm_dt = pd.Timestamp(row.get("confirm_datetime")) if pd.notna(row.get("confirm_datetime")) else entry_date
    entry_price = float(row.get("entry_price") or 0.0)
    bars = _load_bars(code, entry_date, exit_date)
    if bars.empty:
        return {
            "entry_date": entry_date.date().isoformat(),
            "policy_exit_date": exit_date.date().isoformat(),
            "code": code,
            "name": row.get("name"),
            "bars": 0,
            "status": "missing_30m",
        }
    bars, scale, scale_source = _scale_bars(bars, entry_price, confirm_dt)
    bars = bars[bars["datetime"] >= confirm_dt].copy()
    if bars.empty:
        return {
            "entry_date": entry_date.date().isoformat(),
            "policy_exit_date": exit_date.date().isoformat(),
            "code": code,
            "name": row.get("name"),
            "bars": 0,
            "status": "no_bar_after_confirm",
        }
    bars["low_ret"] = bars["adj_low"] / entry_price - 1.0
    bars["close_ret"] = bars["adj_close"] / entry_price - 1.0
    bars["high_ret"] = bars["adj_high"] / entry_price - 1.0
    stop_hit = bars[bars["low_ret"] <= -0.05]
    first_stop_dt = pd.NaT if stop_hit.empty else pd.Timestamp(stop_hit.iloc[0]["datetime"])
    after_stop = bars[bars["datetime"] >= first_stop_dt] if pd.notna(first_stop_dt) else pd.DataFrame()
    reclaim = after_stop[after_stop["close_ret"] >= 0.0] if not after_stop.empty else pd.DataFrame()
    strong_reclaim = after_stop[after_stop["close_ret"] >= 0.03] if not after_stop.empty else pd.DataFrame()
    worst_idx = bars["low_ret"].idxmin()
    close_exit = bars[bars["datetime"].dt.normalize() <= exit_date].tail(1)
    return {
        "entry_date": entry_date.date().isoformat(),
        "policy_exit_date": exit_date.date().isoformat(),
        "code": code,
        "name": row.get("name"),
        "g3_market_style": row.get("g3_market_style"),
        "exit_reason_proxy": row.get("exit_reason_proxy"),
        "entry_price": entry_price,
        "confirm_datetime": str(confirm_dt),
        "bars": int(len(bars)),
        "scale": scale,
        "scale_source": scale_source,
        "net_ret": float(row.get("net_ret")),
        "h5": float(row.get("h5")),
        "fwd_ret_3d": float(row.get("fwd_ret_3d")),
        "min_30m_low_ret": float(bars["low_ret"].min()),
        "min_30m_low_datetime": str(pd.Timestamp(bars.loc[worst_idx, "datetime"])),
        "min_30m_close_ret": float(bars["close_ret"].min()),
        "max_30m_close_ret": float(bars["close_ret"].max()),
        "first_stop_datetime": "" if pd.isna(first_stop_dt) else str(first_stop_dt),
        "first_reclaim_close_datetime": "" if reclaim.empty else str(pd.Timestamp(reclaim.iloc[0]["datetime"])),
        "first_strong_reclaim_close_datetime": "" if strong_reclaim.empty else str(pd.Timestamp(strong_reclaim.iloc[0]["datetime"])),
        "exit_bar_close_ret": float(close_exit["close_ret"].iloc[-1]) if not close_exit.empty else None,
        "status": "ok",
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _prepare_base(30.0)
    for col in ["mae_close_1d", "mae_close_2d", "mae_close_3d", "mae_close_5d", "mfe_close_3d", "mfe_close_5d"]:
        base[col] = pd.to_numeric(base.get(col), errors="coerce")
    confirm = _apply_policy(base, "confirm_d3_le0", 30.0)
    focus = confirm.sort_values("mae_close_3d").head(30).copy()
    rows = [_replay(row) for _, row in focus.iterrows()]
    replay = pd.DataFrame(rows)
    replay.to_csv(OUT_DIR / "confirm_d3_worst30_30m_replay.csv", index=False, encoding="utf-8-sig")
    ok = replay[replay["status"].eq("ok")].copy()
    summary = pd.DataFrame(
        [
            {
                "rows": int(len(replay)),
                "ok_rows": int(len(ok)),
                "missing_rows": int((replay["status"] != "ok").sum()),
                "mean_min_30m_low_ret": float(ok["min_30m_low_ret"].mean()) if len(ok) else None,
                "worst_min_30m_low_ret": float(ok["min_30m_low_ret"].min()) if len(ok) else None,
                "low_le_m8_rate": float((ok["min_30m_low_ret"] <= -0.08).mean()) if len(ok) else None,
                "low_le_m12_rate": float((ok["min_30m_low_ret"] <= -0.12).mean()) if len(ok) else None,
                "reclaim_after_stop_rate": float(ok["first_reclaim_close_datetime"].astype(str).ne("").mean()) if len(ok) else None,
                "strong_reclaim_after_stop_rate": float(ok["first_strong_reclaim_close_datetime"].astype(str).ne("").mean()) if len(ok) else None,
            }
        ]
    )
    summary.to_csv(OUT_DIR / "confirm_d3_worst30_30m_summary.csv", index=False, encoding="utf-8-sig")
    pct_cols = {
        "mean_min_30m_low_ret",
        "worst_min_30m_low_ret",
        "low_le_m8_rate",
        "low_le_m12_rate",
        "reclaim_after_stop_rate",
        "strong_reclaim_after_stop_rate",
        "net_ret",
        "h5",
        "fwd_ret_3d",
        "min_30m_low_ret",
        "min_30m_close_ret",
        "max_30m_close_ret",
        "exit_bar_close_ret",
    }
    lines = [
        "# G3 强势链路 Confirm D3 最差样本 30m 重放 V1",
        "",
        "## 口径",
        "",
        "- 样本：`confirm_d3_le0` 中 `mae_close_3d` 最差的 30 个候选。",
        "- 数据：ClickHouse `kline_minute_30`，从 `confirm_datetime` 到 `policy_exit_date`。",
        "- 价格尺度：用源 `entry_price` 与确认 bar close 做比例校正，避免复权/未复权尺度错配。",
        "- 目标：检查收盘级 MAE 是否低估盘中风险，并观察 stop 后是否曾出现收盘修复。",
        "",
        "## 汇总",
        "",
        _md_table(summary, pct_cols=pct_cols),
        "",
        "## 明细",
        "",
        _md_table(replay, pct_cols=pct_cols),
        "",
        "## 判断",
        "",
        "- 若 `low_le_m12_rate` 明显高，D3 等待期间的盘中痛感不可忽视。",
        "- 若 stop 后仍频繁出现 close reclaim，裸 -5% 全退继续不成立；更合理的是 30m 修复失败确认。",
        "- 下一步应把规则从 D3 收盘代理改成 30m 条件：触发 stop 后，若若干根 30m 不能重新站回入场价/MA5 或继续创新低，则提前退出。",
        "",
    ]
    (OUT_DIR / "confirm_d3_worst30_30m_replay_report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    print({"out_dir": str(OUT_DIR), "summary": summary.to_dict(orient="records")})


if __name__ == "__main__":
    main()
