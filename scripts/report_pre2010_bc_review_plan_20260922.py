"""Read-only classification and action plan for the remaining B/C queue."""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path


RUN = os.getenv("AISTOCK_BC_REVIEW_RUN", "purchased_history_review_20260922_a_grade_auto_b_c_manual")
OUT = runtime_path("quality_plan", "pre2010_bc_review_plan_20260922")

SPECS = {
    "15m": ("stock.purchased_native_15m_candidate_user_policy_20260921_day_audit", "candidate_bars=16 AND (native_complete=1 OR five_complete=1)"),
    "30m": ("stock.purchased_native_30m_candidate_user_policy_20260921_day_audit", "candidate_bars=8"),
    "60m": ("stock.purchased_native_60m_candidate_user_policy_20260921_day_audit", "candidate_bars=4"),
}

ACTION = {
    "B_near_tolerance": "人工确认后，优先用Harry/WStock/购买5m重聚合复核；确认通过后重新走A级门禁",
    "C_no_daily": "补充可信日线快照或外部日线证据；没有日线证据不落库",
    "C_structure": "重新读取年度/月度ZIP并检查交易时段；仍不完整则保留人工审核",
    "C_price": "核对复权口径、代码生命周期和价格异常bar；禁止用日线均摊分钟价格",
    "C_volume": "核对手/股单位、重复bar和异常单根；只允许有单位证据的归一化",
    "C_amount": "核对元/千元/万元单位和金额字段；无单位证据不换算",
    "daily_missing_ohlc": "用完整5m聚合生成日线候选，并以购买快照/可信外部日线人工确认",
    "daily_volume_diff": "人工确认量单位；确认后仅修日线量，不联动覆盖分钟成交量",
    "source_5m_incomplete": "优先查Harry/WStock/购买1m备用源；替代源仍不完整则留审",
    "source_5m_invalid": "修复或排除无有效时间戳文件，保留原始文件证据并人工确认",
}


def main() -> int:
    c = clickhouse_client()
    OUT.mkdir(parents=True, exist_ok=True)
    detail = []
    for period, (audit, structure) in SPECS.items():
        q = f"""
            SELECT a.code, a.trade_date, a.source_year,
                multiIf(a.daily_exists=1 AND {structure}
                    AND abs(a.price_relative_diff)<=0.02
                    AND abs(a.volume_relative_diff)<=0.15
                    AND abs(a.amount_relative_diff)<=0.25, 'B', 'C') AS grade,
                multiIf(a.daily_exists=1 AND {structure}
                    AND abs(a.price_relative_diff)<=0.02
                    AND abs(a.volume_relative_diff)<=0.15
                    AND abs(a.amount_relative_diff)<=0.25, 'B_near_tolerance',
                    a.daily_exists=0, 'C_no_daily',
                    NOT ({structure}), 'C_structure',
                    abs(a.price_relative_diff)>0.02, 'C_price',
                    abs(a.volume_relative_diff)>0.15, 'C_volume',
                    abs(a.amount_relative_diff)>0.25, 'C_amount', 'C_other') AS category,
                a.candidate_bars, a.daily_exists, a.price_relative_diff,
                a.volume_relative_diff, a.amount_relative_diff
            FROM {audit} a INNER JOIN
                (SELECT code, trade_date FROM stock.purchased_history_data_review_items
                 WHERE run_id='{RUN}' AND period='{period}') q
            ON a.code=q.code AND a.trade_date=q.trade_date
        """
        for row in c.query(q).result_rows:
            code, day, year, grade, category, bars, daily_exists, p, v, amt = row
            detail.append({"code": str(code), "trade_date": str(day), "year": int(year),
                           "period": period, "grade": str(grade), "category": str(category),
                           "candidate_bars": int(bars), "daily_exists": int(daily_exists),
                           "price_relative_diff": float(p), "volume_relative_diff": float(v),
                           "amount_relative_diff": float(amt), "action": ACTION[str(category)]})

    # Daily/source rows have no frequency audit row; retain queue evidence verbatim.
    q = f"""SELECT code,trade_date,period,issue_type,actual_count,expected_count,reason
        FROM stock.purchased_history_data_review_items WHERE run_id='{RUN}' AND period IN ('1d','5m')"""
    for code, day, period, issue, actual, expected, reason in c.query(q).result_rows:
        if issue == "purchased_daily_missing_ohlc_2010":
            category, grade, action = "daily_missing_ohlc", "C", ACTION["daily_missing_ohlc"]
        elif issue == "purchased_daily_volume_diff_2010":
            category, grade, action = "daily_volume_diff", "B", ACTION["daily_volume_diff"]
        elif issue == "source_5m_incomplete_bars":
            category, grade, action = "source_5m_incomplete", "C", ACTION["source_5m_incomplete"]
        else:
            category, grade, action = "source_5m_invalid", "C", ACTION["source_5m_invalid"]
        detail.append({"code": str(code), "trade_date": str(day), "year": int(str(day)[:4]),
                       "period": str(period), "grade": grade, "category": category,
                       "candidate_bars": int(actual), "daily_exists": 0,
                       "price_relative_diff": None, "volume_relative_diff": None,
                       "amount_relative_diff": None, "action": action,
                       "issue_type": str(issue), "expected_count": int(expected),
                       "reason": str(reason)})

    summary = Counter((x["grade"], x["category"]) for x in detail)
    by_period = Counter(x["period"] for x in detail)
    by_year = Counter((x["year"], x["grade"]) for x in detail)
    code_days = defaultdict(set)
    for x in detail:
        code_days[(x["code"], x["trade_date"])].add(x["period"])
    report = {
        "run_id": RUN,
        "generated_at": datetime.now().astimezone().isoformat(),
        "total_rows": len(detail),
        "total_unique_code_days": len(code_days),
        "frequency_union_code_days": len({k for k, ps in code_days.items() if ps & {"15m", "30m", "60m"}}),
        "rows_by_period": dict(by_period),
        "grade_category_counts": {f"{g}:{c}": n for (g, c), n in sorted(summary.items())},
        "grade_counts": dict(Counter(x["grade"] for x in detail)),
        "year_grade_counts": {f"{y}:{g}": n for (y, g), n in sorted(by_year.items())},
        "priority": [
            {"rank": 1, "scope": "B_near_tolerance", "count": summary[("B", "B_near_tolerance")], "next": ACTION["B_near_tolerance"]},
            {"rank": 2, "scope": "daily_missing_ohlc", "count": summary[("C", "daily_missing_ohlc")], "next": ACTION["daily_missing_ohlc"]},
            {"rank": 3, "scope": "C_no_daily/C_volume", "count": summary[("C", "C_no_daily")] + summary[("C", "C_volume")], "next": "先做日线补证据，再做单位/异常量复核"},
            {"rank": 4, "scope": "source_5m_incomplete", "count": summary[("C", "source_5m_incomplete")], "next": ACTION["source_5m_incomplete"]},
            {"rank": 5, "scope": "C_price/C_structure/C_amount", "count": summary[("C", "C_price")] + summary[("C", "C_structure")] + summary[("C", "C_amount")], "next": "按代码日人工抽样确认，禁止批量自动覆盖"},
        ],
        "policy": {"A_auto_write": True, "B_manual_review": True, "C_manual_review": True},
    }
    (OUT / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = ["code", "trade_date", "year", "period", "grade", "category", "candidate_bars", "daily_exists", "price_relative_diff", "volume_relative_diff", "amount_relative_diff", "action", "issue_type", "expected_count", "reason"]
    with (OUT / "detail.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(detail)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
