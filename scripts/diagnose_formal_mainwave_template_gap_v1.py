from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_style_template_strategy_v1 import _add_features, _score_candidates  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


FORMAL = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
OUT_DIR = report_path("formal_mainwave_template_gap_v1")


def _code6(value: object) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[-6:]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    formal = pd.read_csv(FORMAL, encoding="utf-8-sig", low_memory=False)
    formal["entry_date"] = pd.to_datetime(formal["entry_date"], errors="coerce").dt.normalize()
    formal["code6"] = formal["code"].map(_code6)
    codes = sorted(formal["code"].dropna().astype(str).unique())
    start = (formal["entry_date"].min() - pd.Timedelta(days=220)).strftime("%Y-%m-%d")
    end = formal["entry_date"].max().strftime("%Y-%m-%d")
    daily = clickhouse_query_df(
        """
        SELECT code, trade_date, open, high, low, close, volume, amount, change_pct, turnover_rate
        FROM kline_daily
        WHERE code IN ? AND trade_date BETWEEN ? AND ? AND close > 0
        ORDER BY code, trade_date
        """,
        [codes, start, end],
    )
    daily["code6"] = daily["code"].map(_code6)
    daily["code_raw"] = daily["code"].astype(str)
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.normalize()
    for c in ["open", "high", "low", "close", "volume", "amount", "change_pct", "turnover_rate"]:
        daily[c] = pd.to_numeric(daily[c], errors="coerce")
    daily = _add_features(daily, max_hold=20)
    daily["learned_count"] = 0.0
    daily["learned_avg_return"] = 0.0
    scored = _score_candidates(daily, use_learned_sector=False)

    decision_dates: list[str] = []
    for rec in formal.to_dict("records"):
        hist = scored[(scored["code6"].eq(rec["code6"])) & (scored["trade_date"] < rec["entry_date"])]
        if not hist.empty:
            decision_dates.append(hist["trade_date"].max().strftime("%Y-%m-%d"))
    quoted_dates = ",".join("'" + x + "'" for x in sorted(set(decision_dates)))
    global_rank = clickhouse_query_df(
        f"""
        SELECT trade_date, code, amount20,
               rank() OVER (PARTITION BY trade_date ORDER BY amount20)
                 / count() OVER (PARTITION BY trade_date) AS amount20_rank
        FROM (
            SELECT code, trade_date,
                   avg(amount) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS amount20
            FROM (
                SELECT code, trade_date, max(amount) AS amount
                FROM kline_daily
                WHERE trade_date BETWEEN '2019-07-01' AND '{max(decision_dates)}' AND close > 0
                GROUP BY code, trade_date
            )
        )
        WHERE trade_date IN ({quoted_dates})
        """
    )
    global_rank["trade_date"] = pd.to_datetime(global_rank["trade_date"], errors="coerce").dt.normalize()
    global_rank["code6"] = global_rank["code"].map(_code6)
    global_rank_map = global_rank.drop_duplicates(["trade_date", "code6"], keep="last").set_index(["trade_date", "code6"])["amount20_rank"]

    rows = []
    for rec in formal.to_dict("records"):
        hist = scored[(scored["code6"].eq(rec["code6"])) & (scored["trade_date"] < rec["entry_date"])].sort_values("trade_date")
        if hist.empty:
            rows.append({"entry_date": rec["entry_date"], "code": rec["code"], "name": rec.get("name", ""), "diagnosis": "missing_daily_history"})
            continue
        x = hist.iloc[-1]
        amount_rank = float(global_rank_map.get((x["trade_date"], rec["code6"]), np.nan))
        institution = bool(amount_rank >= .70 and x["above_ma20"] and x["above_ma60"] and x["close_to_high60"] >= -.10 and x["limit_up_days20"] <= 2 and x["max_dd20"] >= -.22)
        breakout = bool(amount_rank >= .70 and x["close_to_high60"] >= -.08 and x["amount5_20"] >= 1.10 and x["ret5"] >= .03 and 1.5 <= x["change_pct"] <= 9.3)
        capacity = bool(amount_rank >= .70 and x["ret20"] >= .08 and x["big_up_days20"] >= 2)
        too_late = bool(x["ret20"] >= .75 or x["limit_up_days20"] >= 5)
        rows.append({
            "entry_date": rec["entry_date"].strftime("%Y-%m-%d"), "decision_date": x["trade_date"].strftime("%Y-%m-%d"),
            "code": rec["code"], "name": rec.get("name", ""), "formal_net_ret": rec.get("net_ret"),
            "formal_wave_style_score": rec.get("wave_style_score"), "formal_sector_diffusion_score": rec.get("sector_diffusion_score"),
            "rebuilt_template_label": x["template_label"], "rebuilt_wave_style_score_local_rank_only": x["wave_style_score"],
            "institution_trend_pass": institution, "breakout_acceleration_pass": breakout, "capacity_theme_pass": capacity,
            "too_late": too_late, "global_amount20_rank": amount_rank, "above_ma20": x["above_ma20"], "above_ma60": x["above_ma60"],
            "close_to_high60": x["close_to_high60"], "amount5_20": x["amount5_20"], "ret5": x["ret5"], "ret20": x["ret20"],
            "big_up_days20": x["big_up_days20"], "limit_up_days20": x["limit_up_days20"], "max_dd20": x["max_dd20"],
        })
    result = pd.DataFrame(rows)
    result["template_pass"] = result[["institution_trend_pass", "breakout_acceleration_pass", "capacity_theme_pass"]].any(axis=1) & ~result["too_late"]
    result["diagnosis"] = np.where(result["template_pass"], "covered_by_rebuilt_template", "template_gap")
    result.to_csv(OUT_DIR / "formal26_template_gap_detail.csv", index=False, encoding="utf-8-sig")
    summary = {
        "status": "completed", "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "formal_rows": int(len(result)), "template_pass_rows": int(result["template_pass"].sum()),
        "template_gap_rows": int((~result["template_pass"]).sum()),
        "gap_by_reason": {
            "low_global_capacity_rank": int((~result["template_pass"] & result["global_amount20_rank"].lt(.70)).sum()),
            "not_above_ma60": int((~result["template_pass"] & ~result["above_ma60"]).sum()),
            "not_breakout_acceleration": int((~result["template_pass"] & ~result["breakout_acceleration_pass"]).sum()),
        },
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
