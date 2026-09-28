from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_risk_cool_dynamic_circuit import _run_dynamic  # noqa: E402
from scripts.gen2_compare_prev_low_exit_fills import _load_minute_bars  # noqa: E402


BASE = ROOT / "reports" / "gen2_breakout_buy_point_research"
PROBE = BASE / "breakout_family_intraday_strength_probe"
COMBO = PROBE / "combo_policy_probe"
OUT = COMBO / "wait_1030_fair_price_probe"
SOURCES = {
    "combo_or": COMBO / "sources" / "rtret60_or_breakbox25.parquet",
    "volume5": BASE / "sources" / "volume5_dynamic_stop_cd3_mapped.parquet",
}
WINDOWS = {
    "full": ("2024-07-09", "2026-05-26"),
    "train": ("2024-07-09", "2025-03-31"),
    "valid": ("2025-04-01", "2025-12-31"),
    "blind": ("2026-01-01", "2026-05-26"),
}


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return str(obj)


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _sort_visible(d: pd.DataFrame, mode: str) -> pd.DataFrame:
    if mode == "score":
        return d.sort_values(["entry_date", "v4_score", "v4_rank", "confirm_datetime", "code"], ascending=[True, False, True, True, True])
    if mode == "rank":
        return d.sort_values(["entry_date", "v4_rank", "v4_score", "confirm_datetime", "code"], ascending=[True, True, False, True, True])
    return d.sort_values(["entry_date", "confirm_datetime", "v4_rank", "v4_score", "code"], ascending=[True, True, True, False, True])


def _build_decision_source(source_name: str, source_path: Path, mode: str, decision_time: str = "10:30:00") -> Path:
    d = pd.read_parquet(source_path).copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d["confirm_datetime"] = pd.to_datetime(d["confirm_datetime"], errors="coerce")
    d["confirm_time"] = d["confirm_datetime"].dt.strftime("%H:%M:%S")
    d["v4_rank"] = pd.to_numeric(d["v4_rank"], errors="coerce").fillna(999).astype(int)
    d["v4_score"] = pd.to_numeric(d["v4_score"], errors="coerce").fillna(0.0)
    visible = d[d["confirm_time"] <= decision_time].copy()
    picked = _sort_visible(visible, mode).drop_duplicates(["entry_date"], keep="first").copy()
    if picked.empty:
        raise RuntimeError(f"No picked rows for {source_name} {mode}")

    codes = picked["code"].dropna().astype(str).unique().tolist()
    start = str(picked["entry_date"].min())
    end = str(picked["entry_date"].max())
    bars = _load_minute_bars(codes, start, end, 30)
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["bar_date"] = pd.to_datetime(bars["bar_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    bars["bar_time"] = bars["datetime"].dt.strftime("%H:%M:%S")
    decision_bars = bars[bars["bar_time"] == decision_time].copy()
    price_map = {
        (str(row.code), str(row.bar_date)): float(row.close)
        for row in decision_bars.itertuples(index=False)
        if row.close is not None and np.isfinite(float(row.close))
    }
    decision_dt = pd.to_datetime(picked["entry_date"] + " " + decision_time, errors="coerce")
    picked["fair_entry_price"] = [price_map.get((str(r.code), str(r.entry_date))) for r in picked.itertuples(index=False)]
    picked = picked.dropna(subset=["fair_entry_price"]).copy()
    picked["entry_price"] = pd.to_numeric(picked["fair_entry_price"], errors="coerce")
    picked["confirm_datetime"] = decision_dt.loc[picked.index].dt.strftime("%Y-%m-%d %H:%M:%S")
    picked = picked.drop(columns=[c for c in ["confirm_time", "fair_entry_price"] if c in picked.columns])

    source_dir = OUT / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    out_path = source_dir / f"{source_name}_decision1030_{mode}.parquet"
    picked.to_parquet(out_path, index=False)
    picked.to_csv(out_path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    return out_path


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    configs = [
        ("combo_or", "rank"),
        ("combo_or", "score"),
        ("volume5", "rank"),
    ]
    rows: list[dict[str, Any]] = []
    for source_name, mode in configs:
        decision_source = _build_decision_source(source_name, SOURCES[source_name], mode)
        source_df = pd.read_parquet(decision_source)
        for window, (start, end) in WINDOWS.items():
            run_dir = OUT / "runs" / f"{source_name}_decision1030_{mode}" / window
            summary = _run_dynamic(decision_source, run_dir, "stop_cd3_skip", start, end, sort_mode="trigger_time")
            rows.append(
                {
                    "variant": f"{source_name}_decision1030_{mode}",
                    "window": window,
                    "signals": int(len(source_df)),
                    "trades": summary["trade_count"],
                    "total_return": summary["total_return"],
                    "excess_return": summary["excess_return"],
                    "max_drawdown": summary["max_drawdown"],
                    "win_rate": summary["win_rate"],
                    "avg_trade_return": summary["avg_trade_return"],
                }
            )
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "wait_1030_fair_price_summary.csv", index=False, encoding="utf-8-sig")
    (OUT / "wait_1030_fair_price_summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    lines = [
        "# 10:30 等待成交价公平性测试",
        "",
        "本测试把 10:30 前已经确认的信号按 rank/score 选择，并把成交价改为 10:30 的 30m bar close。",
        "",
        "| variant | window | signals | trades | total | excess | max_dd | win |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['variant']} | {row['window']} | {row['signals']} | {row['trades']} | "
            f"{_pct(row['total_return'])} | {_pct(row['excess_return'])} | {_pct(row['max_drawdown'])} | {_pct(row['win_rate'])} |"
        )
    (OUT / "wait_1030_fair_price_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
