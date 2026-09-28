from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "reports" / "gen3_live_payload_v1" / "g3_live_payload.csv"
DEFAULT_OUT = ROOT / "reports" / "gen3_shadow_live_entry_v1"


DISPLAY_COLUMNS = [
    "entry_date",
    "confirm_datetime",
    "code",
    "name",
    "profile",
    "strategy_id",
    "route",
    "route_label",
    "chain",
    "g3_chain",
    "entry_price",
    "confirm_rule",
    "policy",
    "shadow_action",
    "auto_order_allowed",
    "block_reason",
]


def _parse_as_of(value: str | None) -> pd.Timestamp:
    if value:
        return pd.Timestamp(value)
    return pd.Timestamp(datetime.now())


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_无数据_"
    return df.to_markdown(index=False)


def _safe_cols(df: pd.DataFrame, cols: list[str]) -> list[str]:
    return [c for c in cols if c in df.columns]


def main() -> None:
    parser = argparse.ArgumentParser(description="G3 shadow live entry from live-safe payload.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--as-of", default=None, help="Decision datetime, e.g. 2026-06-01 10:30:00.")
    parser.add_argument("--allow-after-close", action="store_true", help="Allow replaying after 15:00; still no auto order.")
    args = parser.parse_args()

    input_path = Path(args.input)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    as_of = _parse_as_of(args.as_of)
    as_of_date = as_of.normalize()
    df = pd.read_csv(input_path, low_memory=False)
    df["entry_date_dt"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.normalize()
    df["confirm_dt"] = pd.to_datetime(df["confirm_datetime"], errors="coerce")

    df["shadow_action"] = "observe_only"
    df["auto_order_allowed"] = False
    df["block_reason"] = ""

    status_ok = df["payload_status"].astype(str).eq("live_safe_core")
    date_ok = df["entry_date_dt"].eq(as_of_date)
    confirm_ok = df["confirm_dt"].notna() & (df["confirm_dt"] <= as_of)
    after_close = as_of.time() > datetime.strptime("15:00:00", "%H:%M:%S").time()
    time_window_ok = True if args.allow_after_close else not after_close

    candidates = df[status_ok & date_ok & confirm_ok].copy()
    if not time_window_ok:
        candidates["block_reason"] = "after_close_replay_only"
    else:
        candidates["block_reason"] = "auto_order_disabled_shadow_only"

    blocked = df[date_ok & ~status_ok].copy()
    if not blocked.empty:
        blocked["block_reason"] = "payload_status_not_live_safe_core"
    pending = df[status_ok & date_ok & ~confirm_ok].copy()
    if not pending.empty:
        pending["block_reason"] = "confirm_datetime_after_as_of_or_missing"

    latest_entry_date = df["entry_date_dt"].max()
    recent_dates = (
        df.groupby("entry_date_dt")
        .size()
        .reset_index(name="rows")
        .sort_values("entry_date_dt", ascending=False)
        .head(10)
    )
    recent_dates["entry_date_dt"] = recent_dates["entry_date_dt"].dt.strftime("%Y-%m-%d")

    candidates_out = candidates[_safe_cols(candidates, DISPLAY_COLUMNS)].copy()
    blocked_out = pd.concat([blocked, pending], ignore_index=True)
    blocked_out = blocked_out[_safe_cols(blocked_out, DISPLAY_COLUMNS)].copy() if not blocked_out.empty else pd.DataFrame(columns=_safe_cols(df, DISPLAY_COLUMNS))

    candidates_out.to_csv(out / "g3_shadow_live_candidates.csv", index=False, encoding="utf-8-sig")
    blocked_out.to_csv(out / "g3_shadow_live_blocked.csv", index=False, encoding="utf-8-sig")
    recent_dates.to_csv(out / "g3_shadow_live_recent_signal_dates.csv", index=False, encoding="utf-8-sig")

    summary = pd.DataFrame(
        [
            {
                "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
                "as_of_date": as_of_date.strftime("%Y-%m-%d"),
                "input_rows": len(df),
                "today_payload_rows": int(date_ok.sum()),
                "display_candidates": len(candidates_out),
                "blocked_rows": len(blocked_out),
                "auto_order_allowed_rows": int(candidates["auto_order_allowed"].sum()) if not candidates.empty else 0,
                "latest_entry_date": "" if pd.isna(latest_entry_date) else latest_entry_date.strftime("%Y-%m-%d"),
                "after_close": bool(after_close),
                "allow_after_close": bool(args.allow_after_close),
            }
        ]
    )
    summary.to_csv(out / "g3_shadow_live_summary.csv", index=False, encoding="utf-8-sig")

    report = f"""# G3 影子实盘入口 V1

生成时间：2026-06-01

## 输入

`{input_path}`

## 决策时间

- as_of：`{as_of.strftime('%Y-%m-%d %H:%M:%S')}`
- as_of_date：`{as_of_date.strftime('%Y-%m-%d')}`

## 核心规则

- 只读取 `payload_status = live_safe_core` 的记录。
- 只展示 `entry_date = as_of_date` 且 `confirm_datetime <= as_of` 的记录。
- 所有记录强制 `auto_order_allowed = false`。
- 这一步只做影子观察入口，不接真实下单。

## 摘要

{_md_table(summary)}

## 当前可展示观察候选

{_md_table(candidates_out)}

## 当前被阻断或未到确认时间

{_md_table(blocked_out)}

## 最近有历史信号的日期

{_md_table(recent_dates)}

## 判断

如果 `display_candidates = 0`，说明当前日期没有经过 live-safe payload 和确认时间双重检查的 G3 观察候选。不能因为历史研究文件里有旧信号，就在今天展示或提示买入。

如果有候选，也只能作为 `observe_only`，不能自动下单。后续要进入自动下单，必须额外完成：

- D-1/盘中代理字段证明。
- 当日分钟线新鲜度检查。
- 涨跌停/停牌可成交检查。
- 资金和仓位约束。
- 通知真实发送确认。
"""
    (out / "g3_shadow_live_entry_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(
        {
            "out_dir": str(out),
            "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
            "display_candidates": len(candidates_out),
            "blocked_rows": len(blocked_out),
            "latest_entry_date": "" if pd.isna(latest_entry_date) else latest_entry_date.strftime("%Y-%m-%d"),
            "auto_order_allowed_rows": int(candidates["auto_order_allowed"].sum()) if not candidates.empty else 0,
        }
    )


if __name__ == "__main__":
    main()
