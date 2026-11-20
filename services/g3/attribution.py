# -*- coding: utf-8 -*-
"""G3 Phase 1B：四维收益归因引擎（Attribution Dashboard）。

将真实双账号收益拆解为四笔账，帮助交易员看清“钱从哪来、亏在哪一环”：
1. 大盘 Beta 贡献（beta_pnl）：持仓市值 × 基准指数（默认沪深300 000300.SH）当日涨跌幅
2. 选股 Alpha 贡献（alpha_pnl）：总盈亏 − 大盘 Beta 贡献 − 做T 净增厚
3. 做T 滚动贡献（t_trade_pnl）：实盘对账 ledger 与手工做T登记台账的扣费净盈亏
4. 执行偏离与纪律诊断（execution_friction）：纪律评分、跟单率、违约/未执行止损事件及估算拖延风险

铁律：
- 绝不虚构缺失数据；某日缺少双账号快照或基准收益时，显式返回 null 并在 data_notes 中说明。
- 止损未执行估算损耗仅作为“风控警示估算”单独展示，不混入已实现盈亏总账。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_BENCHMARK_CODE = "000300.SH"
DEFAULT_BENCHMARK_NAME = "沪深300"


def _safe_float(v: Any) -> Optional[float]:
    if v in (None, "", "-"):
        return None
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _read_json_safe(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _normalize_date_str(raw: str) -> Optional[str]:
    s = str(raw or "").strip()
    m = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", s)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


# ---------------------------------------------------------------------------
# 1. 双账号每日资产与持仓快照聚合
# ---------------------------------------------------------------------------

def _extract_multi_snapshot_summary(doc: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """从 ths_read_account 的 multi JSON 中提取双账号汇总资产、持仓市值、当日盈亏与个股持仓。"""
    accounts = doc.get("accounts")
    if not isinstance(accounts, Mapping):
        return None

    total_assets = 0.0
    total_mv = 0.0
    total_daily_pnl = 0.0
    has_daily_pnl = False
    has_assets = False
    shares_by_code: dict[str, int] = {}
    ready_accounts = 0

    for _, acct_blk in accounts.items():
        if not isinstance(acct_blk, Mapping):
            continue
        ra = acct_blk.get("read_account")
        if not isinstance(ra, Mapping):
            continue
        funds = ra.get("funds") if isinstance(ra.get("funds"), Mapping) else {}
        ta = _safe_float(funds.get("total_assets"))
        mv = _safe_float(funds.get("market_value"))
        if ta is not None and ta > 0:
            total_assets += ta
            has_assets = True
            ready_accounts += 1
        if mv is not None and mv >= 0:
            total_mv += mv

        holdings = ra.get("holdings")
        if isinstance(holdings, list):
            for h in holdings:
                if not isinstance(h, Mapping):
                    continue
                code = str(h.get("code") or "").split(".")[0].zfill(6)
                qty = int(float(h.get("quantity") or h.get("shares") or h.get("qty") or 0))
                if code and code != "000000" and qty > 0:
                    shares_by_code[code] = shares_by_code.get(code, 0) + qty
                dp = _safe_float(h.get("daily_profit"))
                if dp is not None:
                    total_daily_pnl += dp
                    has_daily_pnl = True

    if not has_assets:
        return None

    gen_at = str(doc.get("generated_at") or "")
    trade_date = _normalize_date_str(gen_at)
    if not trade_date:
        return None

    return {
        "date": trade_date,
        "generated_at": gen_at,
        "ready_accounts": ready_accounts,
        "total_assets": round(total_assets, 2),
        "market_value": round(total_mv, 2),
        "position_ratio": round(total_mv / total_assets, 4) if total_assets > 0 else None,
        "daily_profit": round(total_daily_pnl, 2) if has_daily_pnl else None,
        "shares_by_code": shares_by_code,
    }


def _load_daily_account_snapshots(ths_dir: Path, cutoff_date: str) -> dict[str, dict[str, Any]]:
    """按交易日聚合最晚一条有效双账号快照（优先高账号就绪数，同就绪数取最晚时间戳）。"""
    by_date: dict[str, dict[str, Any]] = {}
    if not ths_dir.exists():
        return by_date

    candidates = list(ths_dir.glob("read_multi_*.json"))
    latest_multi = ths_dir / "latest_multi.json"
    if latest_multi.exists():
        candidates.append(latest_multi)

    for f in candidates:
        doc = _read_json_safe(f)
        if not isinstance(doc, Mapping):
            continue
        snap = _extract_multi_snapshot_summary(doc)
        if not snap:
            continue
        d = snap["date"]
        if d < cutoff_date:
            continue
        prev = by_date.get(d)
        if prev is None or (
            (snap["ready_accounts"], snap["generated_at"]) > (prev["ready_accounts"], prev["generated_at"])
        ):
            by_date[d] = snap

    return by_date


# ---------------------------------------------------------------------------
# 2. 做T 台账与执行纪律读取
# ---------------------------------------------------------------------------

def _load_daily_ledger_and_coaching(tracker_dir: Path, desk_plan_dir: Path, cutoff_date: str) -> dict[str, dict[str, Any]]:
    """合并 t_tracker/ledger/*_summary.json、*_coaching.json 以及手工做T执行台账。"""
    by_date: dict[str, dict[str, Any]] = {}

    ledger_dir = tracker_dir / "ledger"
    if ledger_dir.exists():
        for f in sorted(ledger_dir.glob("*_summary.json")):
            doc = _read_json_safe(f)
            if not isinstance(doc, Mapping):
                continue
            d = _normalize_date_str(str(doc.get("date") or f.stem))
            if not d or d < cutoff_date:
                continue
            coaching = doc.get("coaching") if isinstance(doc.get("coaching"), Mapping) else {}
            by_date[d] = {
                "date": d,
                "fills_source": doc.get("fills_source"),
                "t_round_trips": int(doc.get("t_round_trips") or 0),
                "net_pnl": _safe_float(doc.get("net_pnl")) or 0.0,
                "gross_pnl": _safe_float(doc.get("gross_pnl")) or 0.0,
                "fees": _safe_float(doc.get("fees")) or 0.0,
                "discipline_rate": _safe_float(doc.get("discipline_rate")),
                "discipline_score": _safe_float(coaching.get("纪律评分")),
                "off_plan_count": int(doc.get("off_plan_count") or 0),
                "unmatched_drafts": len(doc.get("unmatched_draft_ids") or []),
                "violations_total": int(coaching.get("violations_total") or 0),
                "contract_events": list(coaching.get("contract") or []),
                "coaching_reminder": coaching.get("明日提醒"),
                "source": "ledger_summary",
            }

    # 补充手工登记的做T执行台账（g3_desk_plans/executions_YYYYMMDD.json）
    if desk_plan_dir.exists():
        for f in sorted(desk_plan_dir.glob("executions_*.json")):
            doc = _read_json_safe(f)
            rows = doc.get("executions") if isinstance(doc, Mapping) else (doc if isinstance(doc, list) else [])
            if not isinstance(rows, list) or not rows:
                continue
            d = _normalize_date_str(f.stem)
            if not d or d < cutoff_date:
                continue
            # 计算手工台账已配对做T净收益
            by_code: dict[str, dict[str, float]] = {}
            for r in rows:
                if not isinstance(r, Mapping):
                    continue
                code = str(r.get("code") or "")
                side = str(r.get("side") or "").upper()
                px = _safe_float(r.get("price")) or 0.0
                sh = _safe_float(r.get("shares")) or 0.0
                if not code or px <= 0 or sh <= 0:
                    continue
                slot = by_code.setdefault(code, {"b_sh": 0.0, "b_amt": 0.0, "s_sh": 0.0, "s_amt": 0.0})
                if side == "BUY":
                    slot["b_sh"] += sh
                    slot["b_amt"] += px * sh
                elif side == "SELL":
                    slot["s_sh"] += sh
                    slot["s_amt"] += px * sh
            manual_pnl = 0.0
            manual_rounds = 0
            for slot in by_code.values():
                matched = min(slot["b_sh"], slot["s_sh"])
                if matched > 0:
                    avg_b = slot["b_amt"] / slot["b_sh"]
                    avg_s = slot["s_amt"] / slot["s_sh"]
                    manual_pnl += (avg_s - avg_b) * matched
                    manual_rounds += 1
            if manual_rounds > 0:
                entry = by_date.setdefault(
                    d,
                    {
                        "date": d,
                        "fills_source": "manual_desk",
                        "t_round_trips": 0,
                        "net_pnl": 0.0,
                        "gross_pnl": 0.0,
                        "fees": 0.0,
                        "discipline_rate": None,
                        "discipline_score": None,
                        "off_plan_count": 0,
                        "unmatched_drafts": 0,
                        "violations_total": 0,
                        "contract_events": [],
                        "coaching_reminder": None,
                        "source": "manual_desk",
                    },
                )
                # 仅当 ledger 无自动成交对账时采用手工台账
                if entry["t_round_trips"] == 0:
                    entry["t_round_trips"] = manual_rounds
                    entry["net_pnl"] = round(manual_pnl, 2)
                    entry["source"] = "manual_desk"

    return by_date


# ---------------------------------------------------------------------------
# 3. 基准指数日涨跌幅获取（ClickHouse，fail-safe）
# ---------------------------------------------------------------------------

def _fetch_benchmark_returns_from_ch(
    start_date: str,
    end_date: str,
    benchmark_code: str = DEFAULT_BENCHMARK_CODE,
) -> dict[str, float]:
    """返回 {YYYY-MM-DD: 日涨跌幅小数（如 0.0125 表示 +1.25%）}。"""
    try:
        import clickhouse_connect

        client = clickhouse_connect.get_client(
            host=os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1"),
            port=int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123")),
            username=os.getenv("AISTOCK_CLICKHOUSE_USER", "default"),
            password=os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", ""),
            database=os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock"),
            connect_timeout=5,
            send_receive_timeout=15,
        )
        try:
            res = client.query(
                """
                SELECT trade_date, anyLast(close) AS close
                FROM stock.kline_daily
                WHERE code = {code:String}
                  AND trade_date >= toDate({s:String}) - 15
                  AND trade_date <= toDate({e:String})
                GROUP BY trade_date
                ORDER BY trade_date ASC
                """,
                parameters={"code": benchmark_code, "s": start_date, "e": end_date},
            )
            rows = res.result_rows or []
        finally:
            client.close()

        out: dict[str, float] = {}
        prev_close: Optional[float] = None
        for dt_val, close_val in rows:
            c = _safe_float(close_val)
            if c is None or c <= 0:
                continue
            d_str = str(dt_val)[:10]
            if prev_close is not None and prev_close > 0:
                out[d_str] = round((c - prev_close) / prev_close, 6)
            prev_close = c
        return out
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 4. 四维收益归因主计算入口
# ---------------------------------------------------------------------------

def build_four_dim_attribution(
    *,
    days: int = 30,
    benchmark_code: str = DEFAULT_BENCHMARK_CODE,
    benchmark_name: str = DEFAULT_BENCHMARK_NAME,
    ths_dir: Optional[Path] = None,
    tracker_dir: Optional[Path] = None,
    desk_plan_dir: Optional[Path] = None,
    benchmark_fetcher: Optional[Callable[[str, str, str], dict[str, float]]] = None,
    today_date: Optional[str] = None,
) -> dict[str, Any]:
    """计算近 N 自然日的四维收益归因报表（大盘 Beta / 选股 Alpha / 做T 增厚 / 执行偏离）。"""
    now_bj = datetime.now(BJ_TZ)
    end_str = today_date or now_bj.strftime("%Y-%m-%d")
    end_dt = datetime.strptime(end_str, "%Y-%m-%d").date()
    cutoff_str = (end_dt - timedelta(days=max(1, days))).strftime("%Y-%m-%d")

    ths_root = ths_dir or runtime_path("ths_read_account")
    trk_root = tracker_dir or runtime_path("t_tracker")
    plan_root = desk_plan_dir or runtime_path("g3_desk_plans")

    acct_by_date = _load_daily_account_snapshots(ths_root, cutoff_str)
    ledger_by_date = _load_daily_ledger_and_coaching(trk_root, plan_root, cutoff_str)

    all_dates = sorted(set(acct_by_date.keys()) | set(ledger_by_date.keys()))
    bench_fetcher = benchmark_fetcher or _fetch_benchmark_returns_from_ch
    bench_returns = (
        bench_fetcher(all_dates[0], all_dates[-1], benchmark_code)
        if all_dates
        else {}
    )

    daily_rows: list[dict[str, Any]] = []
    friction_events: list[dict[str, Any]] = []

    sum_total_pnl = 0.0
    sum_beta_pnl = 0.0
    sum_alpha_pnl = 0.0
    sum_t_pnl = 0.0
    sum_est_stop_drag = 0.0
    has_any_total_pnl = False
    has_any_beta_pnl = False
    has_any_alpha_pnl = False

    discipline_scores: list[float] = []
    total_violations = 0
    total_t_rounds = 0
    total_unmatched_drafts = 0

    for d in all_dates:
        acct = acct_by_date.get(d)
        ldg = ledger_by_date.get(d)
        bench_ret = bench_returns.get(d)

        total_pnl = acct["daily_profit"] if acct else None
        mv = acct["market_value"] if acct else None
        pos_ratio = acct["position_ratio"] if acct else None
        t_pnl = round(float(ldg["net_pnl"]), 2) if ldg else 0.0
        t_rounds = int(ldg["t_round_trips"]) if ldg else 0

        beta_pnl = None
        if mv is not None and bench_ret is not None:
            beta_pnl = round(mv * bench_ret, 2)

        alpha_pnl = None
        if total_pnl is not None and beta_pnl is not None:
            alpha_pnl = round(total_pnl - beta_pnl - t_pnl, 2)

        if total_pnl is not None:
            sum_total_pnl += total_pnl
            has_any_total_pnl = True
        if beta_pnl is not None:
            sum_beta_pnl += beta_pnl
            has_any_beta_pnl = True
        if alpha_pnl is not None:
            sum_alpha_pnl += alpha_pnl
            has_any_alpha_pnl = True
        sum_t_pnl += t_pnl
        total_t_rounds += t_rounds

        disc_score = ldg.get("discipline_score") if ldg else None
        if disc_score is not None:
            discipline_scores.append(float(disc_score))
        violations = int(ldg.get("violations_total") or 0) if ldg else 0
        total_violations += violations
        unmatched = int(ldg.get("unmatched_drafts") or 0) if ldg else 0
        total_unmatched_drafts += unmatched

        # 提取当日违约/止损未执行事件，并结合当日持仓股数估算潜在拖延风险
        day_est_drag = 0.0
        shares_map = acct.get("shares_by_code") if acct else {}
        for ev in (ldg.get("contract_events") or [] if ldg else []):
            if not isinstance(ev, Mapping):
                continue
            code = str(ev.get("code") or "").split(".")[0].zfill(6)
            hard_stop = _safe_float(ev.get("hard_stop"))
            day_low = _safe_float(ev.get("day_low"))
            held_sh = int(shares_map.get(code) or 0)
            est_loss = None
            if hard_stop is not None and day_low is not None and hard_stop > day_low and held_sh > 0:
                est_loss = round((hard_stop - day_low) * held_sh, 2)
                day_est_drag += est_loss
            friction_events.append({
                "date": d,
                "type": ev.get("type") or "contract_violation",
                "code": code,
                "label": ev.get("label") or "",
                "held_shares": held_sh or None,
                "estimated_drag_pnl": -est_loss if est_loss is not None else None,
            })
        sum_est_stop_drag += day_est_drag

        daily_rows.append({
            "date": d,
            "total_assets": acct["total_assets"] if acct else None,
            "market_value": mv,
            "position_pct": round(pos_ratio * 100, 1) if pos_ratio is not None else None,
            "benchmark_ret_pct": round(bench_ret * 100, 2) if bench_ret is not None else None,
            "total_pnl": total_pnl,
            "beta_pnl": beta_pnl,
            "alpha_pnl": alpha_pnl,
            "t_trade_pnl": t_pnl,
            "t_round_trips": t_rounds,
            "discipline_score": disc_score,
            "violations_total": violations,
            "unmatched_drafts": unmatched,
            "estimated_stop_drag": round(-day_est_drag, 2) if day_est_drag > 0 else 0.0,
            "has_account_snapshot": acct is not None,
            "has_ledger": ldg is not None,
        })

    # 倒序返回给前端（最新日期在前）
    daily_rows.reverse()
    friction_events.reverse()

    avg_discipline = round(sum(discipline_scores) / len(discipline_scores), 1) if discipline_scores else None

    data_notes: list[str] = [
        f"基准指数：{benchmark_name}（{benchmark_code}）；Beta = 当日持仓市值 × 基准日涨跌幅，Alpha = 总盈亏 − Beta − 做T净额。",
        f"窗口覆盖：近 {days} 自然日内共采集到 {len(acct_by_date)} 天双账号资产快照、{len(ledger_by_date)} 天做T对账/纪律记录。",
    ]
    if len(acct_by_date) < 5:
        data_notes.append("双账号历史快照天数尚少，随每日 17:30 自动同步持续累积后，周/月度 Beta 与 Alpha 归因将更具统计显著性。")
    if total_violations > 0:
        data_notes.append("“风控拖延估算”仅基于止损契约未执行时的 (止损价−日内低点)×持仓股数 估算警示，不计入实际总盈亏。")

    return {
        "schema": "g3_four_dim_attribution/1",
        "as_of": now_bj.isoformat(timespec="seconds"),
        "window_days": days,
        "from_date": cutoff_str,
        "to_date": end_str,
        "benchmark": {
            "code": benchmark_code,
            "name": benchmark_name,
            "matched_days": len(bench_returns),
        },
        "summary": {
            "coverage_days": len(all_dates),
            "account_snapshot_days": len(acct_by_date),
            "ledger_days": len(ledger_by_date),
            "total_pnl": round(sum_total_pnl, 2) if has_any_total_pnl else None,
            "beta_pnl": round(sum_beta_pnl, 2) if has_any_beta_pnl else None,
            "alpha_pnl": round(sum_alpha_pnl, 2) if has_any_alpha_pnl else None,
            "t_trade_pnl": round(sum_t_pnl, 2),
            "t_round_trips": total_t_rounds,
            "avg_discipline_score": avg_discipline,
            "violations_total": total_violations,
            "unmatched_drafts_total": total_unmatched_drafts,
            "estimated_stop_drag_pnl": round(-sum_est_stop_drag, 2) if sum_est_stop_drag > 0 else 0.0,
        },
        "friction_events": friction_events[:30],
        "daily_rows": daily_rows,
        "data_notes": data_notes,
    }
