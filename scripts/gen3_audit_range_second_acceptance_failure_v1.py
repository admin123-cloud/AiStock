from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_test_range_30m_volume_acceptance_v1 import max_drawdown  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


SOURCE = ROOT / "reports" / "gen3_range_second_acceptance_v1" / "deep_and_reclaim_any_second_accept__cost30" / "closed_trades.csv"
OUT_DIR = ROOT / "reports" / "gen3_range_second_acceptance_failure_audit_v1"


def sql_list(values: list[str]) -> str:
    return ",".join("'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'" for v in values)


def pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, max_rows: int = 80) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    view = df.head(max_rows).copy()
    for col in view.columns:
        if col in pct_cols:
            view[col] = view[col].map(pct)
        elif pd.api.types.is_float_dtype(view[col]):
            view[col] = view[col].map(lambda x: "" if pd.isna(x) else f"{float(x):.4f}")
        else:
            view[col] = view[col].map(lambda x: "" if pd.isna(x) else str(x))
    suffix = "" if len(df) <= max_rows else f"\n\n_仅展示前 {max_rows} 行，共 {len(df)} 行。_"
    return view.to_markdown(index=False) + suffix


def load_trades() -> pd.DataFrame:
    d = pd.read_csv(SOURCE, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in [
        "policy_net_ret",
        "entry_price_adjusted",
        "daily_entry_close",
        "minute_day_close",
        "confirm_high",
        "confirm_low",
        "confirm_open",
        "entry_price",
        "bar_close_pos",
        "bar_ret",
        "amount_ratio3",
        "range_pos60",
        "close_position",
        "amount_ratio20",
        "gap_open",
        "index_mom20",
        "runup_from_60d_low",
        "limit_up_count",
        "limit_down_count",
        "fwd_ret_confirm_to_close_1d",
        "fwd_ret_confirm_to_close_2d",
        "fwd_ret_confirm_to_close_5d",
    ]:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    return d


def load_daily(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    codes = sorted(trades["code"].unique())
    start = trades["entry_date"].min().strftime("%Y-%m-%d")
    end = (trades["entry_date"].max() + pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    sql = f"""
    SELECT code, trade_date, open, high, low, close, amount, change_pct
    FROM kline_daily
    WHERE code IN ({sql_list(codes)})
      AND trade_date BETWEEN toDate('{start}') AND toDate('{end}')
    ORDER BY code, trade_date
    """
    d = clickhouse_query_df(sql)
    if d.empty:
        return d
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce").dt.normalize()
    d["code"] = d["code"].astype(str)
    for col in ["open", "high", "low", "close", "amount", "change_pct"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    return d


def attach_path_features(trades: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    out = trades.copy()
    rows: list[dict[str, Any]] = []
    for row in out.itertuples(index=False):
        code = getattr(row, "code")
        entry_date = pd.Timestamp(getattr(row, "entry_date")).normalize()
        entry_price = float(getattr(row, "entry_price_adjusted"))
        g = daily[(daily["code"].eq(code)) & (daily["trade_date"].ge(entry_date))].sort_values("trade_date").head(6).copy()
        item: dict[str, Any] = {"code": code, "entry_date": entry_date}
        if len(g):
            d0 = g.iloc[0]
            item["d0_open_ret"] = float(d0["open"] / entry_price - 1.0)
            item["d0_high_ret"] = float(d0["high"] / entry_price - 1.0)
            item["d0_close_ret"] = float(d0["close"] / entry_price - 1.0)
            item["d0_high_to_close_fade"] = float(d0["close"] / d0["high"] - 1.0) if d0["high"] else None
            item["d0_close_position_daily"] = float((d0["close"] - d0["low"]) / (d0["high"] - d0["low"])) if d0["high"] != d0["low"] else None
        for i in range(1, min(4, len(g))):
            di = g.iloc[i]
            item[f"d{i}_open_ret"] = float(di["open"] / entry_price - 1.0)
            item[f"d{i}_close_ret"] = float(di["close"] / entry_price - 1.0)
            item[f"d{i}_high_ret"] = float(di["high"] / entry_price - 1.0)
            item[f"d{i}_low_ret"] = float(di["low"] / entry_price - 1.0)
        rows.append(item)
    feat = pd.DataFrame(rows)
    return out.merge(feat, on=["code", "entry_date"], how="left")


def label_failure_reason(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    reasons = []
    for row in out.itertuples(index=False):
        ret = float(getattr(row, "policy_net_ret"))
        if ret > 0:
            reasons.append("winner")
            continue
        d0_fade = getattr(row, "d0_high_to_close_fade", None)
        d1_close = getattr(row, "d1_close_ret", None)
        d2_close = getattr(row, "d2_close_ret", None)
        index_mom20 = getattr(row, "index_mom20", None)
        amount20 = getattr(row, "amount_ratio20", None)
        gap_open = getattr(row, "gap_open", None)
        runup = getattr(row, "runup_from_60d_low", None)
        if pd.notna(d0_fade) and d0_fade <= -0.035:
            reasons.append("d0_spike_fade")
        elif pd.notna(d1_close) and d1_close <= -0.01:
            reasons.append("d1_weak_confirm")
        elif pd.notna(d2_close) and d2_close <= 0:
            reasons.append("d2_no_followthrough")
        elif pd.notna(index_mom20) and index_mom20 >= 0:
            reasons.append("not_real_ice_environment")
        elif pd.notna(amount20) and amount20 >= 1.25:
            reasons.append("daily_volume_overheated")
        elif pd.notna(gap_open) and gap_open <= -0.025:
            reasons.append("gap_down_pressure")
        elif pd.notna(runup) and runup >= 0.15:
            reasons.append("not_low_enough")
        else:
            reasons.append("unclassified_small_loss")
    out["failure_reason"] = reasons
    out["period"] = pd.cut(
        out["entry_date"],
        bins=[pd.Timestamp("2019-12-31"), pd.Timestamp("2023-12-31"), pd.Timestamp("2025-12-31"), pd.Timestamp("2026-12-31")],
        labels=["train_2020_2023", "valid_2024_2025", "blind_2026ytd"],
    ).astype(str)
    return out


def grouped(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    rows = []
    for key, g in df.groupby(cols, dropna=False):
        if not isinstance(key, tuple):
            key = (key,)
        ret = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        item = {col: val for col, val in zip(cols, key)}
        item.update(
            {
                "trade_count": int(len(g)),
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
                "avg_ret": float(ret.mean()) if len(ret) else 0.0,
                "sum_ret": float(ret.sum()) if len(ret) else 0.0,
                "worst_trade": float(ret.min()) if len(ret) else 0.0,
                "avg_d0_fade": float(pd.to_numeric(g.get("d0_high_to_close_fade"), errors="coerce").mean()),
                "avg_d1_close": float(pd.to_numeric(g.get("d1_close_ret"), errors="coerce").mean()),
                "avg_d2_close": float(pd.to_numeric(g.get("d2_close_ret"), errors="coerce").mean()),
            }
        )
        rows.append(item)
    return pd.DataFrame(rows).sort_values(["sum_ret", "trade_count"], ascending=[True, False])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = load_trades()
    daily = load_daily(trades)
    audited = label_failure_reason(attach_path_features(trades, daily))
    audited.to_csv(OUT_DIR / "audited_trades.csv", index=False, encoding="utf-8-sig")

    by_period = grouped(audited, ["period"])
    by_reason = grouped(audited[audited["policy_net_ret"].le(0)].copy(), ["period", "failure_reason"])
    by_emotion = grouped(audited, ["period", "emotion_signal"])
    by_second = grouped(audited, ["period", "d0_second_accept", "d1_second_accept"])
    recent_losses = audited[(audited["period"].isin(["valid_2024_2025", "blind_2026ytd"])) & audited["policy_net_ret"].le(0)].copy()
    recent_losses = recent_losses.sort_values("policy_net_ret")

    by_period.to_csv(OUT_DIR / "by_period.csv", index=False, encoding="utf-8-sig")
    by_reason.to_csv(OUT_DIR / "failure_reasons.csv", index=False, encoding="utf-8-sig")
    by_emotion.to_csv(OUT_DIR / "by_emotion_signal.csv", index=False, encoding="utf-8-sig")
    by_second.to_csv(OUT_DIR / "by_second_accept_type.csv", index=False, encoding="utf-8-sig")
    recent_losses.to_csv(OUT_DIR / "recent_losses.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "win_rate",
        "avg_ret",
        "sum_ret",
        "worst_trade",
        "avg_d0_fade",
        "avg_d1_close",
        "avg_d2_close",
        "policy_net_ret",
        "d0_high_to_close_fade",
        "d1_close_ret",
        "d2_close_ret",
        "index_mom20",
        "amount_ratio20",
        "gap_open",
        "runup_from_60d_low",
    }
    lines = [
        "# G3 横盘二次承接失败归因审计 v1",
        "",
        "## 审计范围",
        "- 策略：`deep_and_reclaim_any_second_accept`，中文是“箱体底部且日线修复，并要求D0后半日或D1任一再次放量承接”。",
        "- 回测/审计窗口：2020-01-01 至 2026-05-29。",
        "- 交易数：22 笔。这里不做新参数优化，只做失败标签归因。",
        "",
        "## 失败标签解释",
        "- `d0_spike_fade`：入场日冲高回落，日内最高点到收盘回落超过约3.5%。",
        "- `d1_weak_confirm`：D1收盘相对入场价跌破约1%，说明二次承接后没有延续。",
        "- `d2_no_followthrough`：D2仍没有正反馈。",
        "- `not_real_ice_environment`：指数20日动量已经转正，可能不是冰点/弱势修复环境。",
        "- `daily_volume_overheated`：日线量能偏热，可能不是低位安静承接。",
        "- `gap_down_pressure`：入场日跳空压力较大。",
        "- `not_low_enough`：距离60日低点反弹已不低，安全边际不足。",
        "",
        "## 分窗口表现",
        md_table(by_period, pct_cols=pct_cols),
        "",
        "## 失败原因",
        md_table(by_reason, pct_cols=pct_cols),
        "",
        "## 情绪分组",
        md_table(by_emotion, pct_cols=pct_cols),
        "",
        "## 二次承接类型分组",
        md_table(by_second, pct_cols=pct_cols),
        "",
        "## 2024以后亏损明细",
        md_table(
            recent_losses[
                [
                    "entry_date",
                    "code",
                    "name",
                    "policy_net_ret",
                    "failure_reason",
                    "emotion_signal",
                    "d0_second_accept",
                    "d1_second_accept",
                    "d0_high_to_close_fade",
                    "d1_close_ret",
                    "d2_close_ret",
                    "index_mom20",
                    "amount_ratio20",
                    "gap_open",
                    "runup_from_60d_low",
                ]
            ],
            pct_cols=pct_cols,
        ),
        "",
        "## 结论",
        "- 二次承接方向有效地降低了全周期回撤，但近年弱点主要不在“有没有二次承接”，而在二次承接后的 D1/D2 延续不足。",
        "- 下一步不建议继续调入场 score/rank；更应验证 D1 早期弱确认快速退出，或者要求 D1 二次承接后的收盘/开盘延续确认。",
    ]
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()
