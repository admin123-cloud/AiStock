"""Research-only market-state audit for the recovered G3 mainwave contract.

It replays the already-confirmed 30-minute tickets through the same two-slot
competition model.  It never reads runtime candidates and cannot create orders.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.backtest_g3_recalled_mainwave_contract_v1 as replay
from utils.paths import report_path


SOURCE_ROOT = report_path("g3_recalled_mainwave_contract_v1")
OUT_DIR = report_path("g3_mainwave_market_state_audit_v1")
SEGMENTS = (
    ("20200101", "20201231"), ("20210101", "20210630"), ("20210701", "20211231"),
    ("20220101", "20220630"), ("20220701", "20221231"), ("20230101", "20230630"),
    ("20230701", "20231231"), ("20240101", "20240630"), ("20240701", "20241231"),
    ("20250101", "20250630"), ("20250701", "20251231"), ("20260101", "20260630"),
)
WINDOWS = {
    "train_2020_2023": ("2020-01-01", "2023-12-31"),
    "validation_2024_2025": ("2024-01-01", "2025-12-31"),
    "blind_2026": ("2026-01-01", "2026-06-30"),
    "post_2024_09": ("2024-09-24", "2026-06-30"),
    "full": ("2020-01-01", "2026-06-30"),
}
MIN_RETENTION = 0.65
MIN_BLIND_TRADES = 10


def _tickets() -> pd.DataFrame:
    parts = []
    for start, end in SEGMENTS:
        path = SOURCE_ROOT / f"{start}_{end}_breakout_score88_sector2_stop10%_top10" / "m30_confirmed_tickets.csv"
        if not path.exists():
            raise FileNotFoundError(path)
        parts.append(pd.read_csv(path, encoding="utf-8-sig"))
    tickets = pd.concat(parts, ignore_index=True)
    tickets["entry_date"] = pd.to_datetime(tickets["entry_date"], errors="coerce").dt.normalize()
    tickets["entry_datetime"] = pd.to_datetime(tickets["entry_datetime"], errors="coerce")
    tickets = tickets.dropna(subset=["entry_date", "entry_datetime"]).drop_duplicates(
        subset=["code_raw", "entry_datetime"], keep="last"
    )
    return tickets.sort_values(["entry_datetime", "daily_rank", "rank_key"], ascending=[True, True, False])


def _metrics(trades: pd.DataFrame) -> dict:
    summary = replay._summary(trades)
    _, funded = replay._two_slot_funded_curve(trades)
    return {**summary, **funded, "hard_stop_rate": float((trades["exit_reason"] == "hard_stop").mean()) if len(trades) else None}


def _window_rows(name: str, entered: pd.DataFrame, baseline: pd.DataFrame) -> list[dict]:
    rows = []
    for window, (start, end) in WINDOWS.items():
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        base = baseline[baseline["entry_date_ts"].between(start_ts, end_ts)]
        trades = entered[entered["entry_date_ts"].between(start_ts, end_ts)]
        result = _metrics(trades)
        rows.append({
            "variant": name, "window": window, "trades": len(trades), "baseline_trades": len(base),
            "trade_retention": len(trades) / len(base) if len(base) else None, **result,
        })
    return rows


def _assess(rows: pd.DataFrame, base_rows: pd.DataFrame) -> dict:
    pivot = rows.set_index("window")
    coverage_ok = all(float(pivot.loc[w, "trade_retention"]) >= MIN_RETENTION for w in WINDOWS if w != "post_2024_09")
    blind_trades = int(pivot.loc["blind_2026", "trades"])
    base = base_rows.set_index("window")
    valid = pivot.loc["validation_2024_2025"]
    blind = pivot.loc["blind_2026"]
    increment_ok = (
        float(valid["avg_ret"]) > float(base.loc["validation_2024_2025", "avg_ret"])
        and float(blind["avg_ret"]) > float(base.loc["blind_2026", "avg_ret"])
    )
    status = "eligible_for_two_slot_full_replay"
    if blind_trades < MIN_BLIND_TRADES:
        status = "reject_insufficient_blind_sample"
    elif not coverage_ok:
        status = "reject_trade_count_overfit"
    elif not increment_ok:
        status = "reject_no_out_of_sample_increment"
    return {"variant": str(pivot.iloc[0]["variant"]), "status": status, "blind_trades": blind_trades,
            "coverage_ok": coverage_ok, "out_of_sample_increment_ok": increment_ok}


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tickets = _tickets()
    below_ma20 = pd.to_numeric(tickets["index_close"], errors="coerce") < pd.to_numeric(tickets["index_ma20"], errors="coerce")
    mom60_positive = pd.to_numeric(tickets["index_mom60"], errors="coerce") > 0
    variants = {
        "base": pd.Series(True, index=tickets.index),
        "index_above_ma20": ~below_ma20,
        "mom60_nonpositive": ~mom60_positive,
        # A broad veto: do not chase a positive-60d rebound while the index is
        # still below MA20.  This is deliberately a state definition, not an MA
        # parameter sweep.
        "veto_weak_rebound": ~(below_ma20 & mom60_positive),
    }
    all_rows, trades_out = [], {}
    baseline = None
    for name, mask in variants.items():
        entered, skipped = replay._two_slot(tickets[mask].copy())
        if name == "base":
            baseline = entered.copy()
        assert baseline is not None
        all_rows.extend(_window_rows(name, entered, baseline))
        trades_out[name] = entered
    metrics = pd.DataFrame(all_rows)
    base_rows = metrics[metrics["variant"].eq("base")].copy()
    assessments = [
        _assess(metrics[metrics["variant"].eq(name)].copy(), base_rows)
        for name in variants if name != "base"
    ]
    metrics.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    for name, trades in trades_out.items():
        trades.to_csv(OUT_DIR / f"{name}_two_slot_trades.csv", index=False, encoding="utf-8-sig")
    payload = {
        "status": "completed", "research_only": True, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "12 chronological base-contract segment m30_confirmed_tickets.csv files",
        "contract": "same 30m-confirmed tickets; market-state filter before identical two-slot competition",
        "anti_overfit_contract": {"min_retention": MIN_RETENTION, "min_blind_trades": MIN_BLIND_TRADES},
        "assessments": assessments,
        "limitations": "OHLC proxy and reconstructed tickets only; no tick slippage, price-limit fill model, or byte-for-byte historical runtime scanner replay.",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# G3机构主升市场状态审计 v1", "", "- 仅研究：不修改运行合同、影子账本或下单链路。",
              "- 统一对已完成30分钟确认的票据做市场状态过滤，再用相同二槽竞争重放。", "",
              "## 窗口结果", "", metrics.to_markdown(index=False), "", "## 判定", ""]
    for item in assessments:
        lines.append(f"- `{item['variant']}`：`{item['status']}`；盲测 {item['blind_trades']} 笔；保留率合格={item['coverage_ok']}；样本外增量合格={item['out_of_sample_increment_ok']}。")
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
