# -*- coding: utf-8 -*-
"""AiStock G3 盈利闭环定时作业脚本（供 Windows 计划任务无人值守调用）。

模式：
  - --mode premarket  (每个工作日 09:15)：交易日门禁 -> 生成当日盘前主线简报 -> 推送微信
  - --mode postclose  (每个工作日 17:45)：交易日门禁 -> 刷新主升候选池 -> 导出 export_latest.json -> 刷新归因与交易教练

铁律：
  - 默认严格校验 ClickHouse stock.trade_calendar（SH 市场 is_trading=1），非交易日（周末/节假日）静默跳过、零微信打扰；
  - 传入 --force 可跳过交易日门禁用于手工演练；
  - 仅调用线上本地 API（http://127.0.0.1:3000），零数据库直接写、零自动下单。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJ_TZ = timezone(timedelta(hours=8))
BASE_URL = os.getenv("AISTOCK_LOCAL_BASE_URL", "http://127.0.0.1:3000")
LOG_PATH = Path(os.getenv("AISTOCK_LOGS_ROOT", "F:/Aistock/storage/logs")) / "g3_profit_loop.log"
EXPORT_PATH = Path(os.getenv("AISTOCK_DATA_ROOT", "F:/Aistock")) / "runtime" / "g3_ai_screening" / "export_latest.json"


def _log(msg: str) -> None:
    ts = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _is_trading_day(today_str: str) -> tuple[bool, str]:
    """严格查 ClickHouse stock.trade_calendar；查询异常时 fail-closed 返回 False。"""
    try:
        import clickhouse_connect

        client = clickhouse_connect.get_client(
            host=os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1"),
            port=int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123")),
            username=os.getenv("AISTOCK_CLICKHOUSE_USER", "default"),
            password=os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", ""),
            database=os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock"),
            connect_timeout=5,
            send_receive_timeout=10,
        )
        try:
            res = client.query(
                "SELECT is_trading FROM stock.trade_calendar WHERE market = 'SH' AND trade_date = toDate({d:String}) LIMIT 1",
                parameters={"d": today_str},
            )
            rows = res.result_rows or []
        finally:
            client.close()
        if not rows:
            return False, f"trade_calendar 无 {today_str} 记录（fail-closed 跳过）"
        ok = int(rows[0][0]) == 1
        return ok, "交易日" if ok else "非交易日（休市）"
    except Exception as exc:
        return False, f"交易日历查询异常（fail-closed 跳过）: {exc}"


def _http_json(path: str, *, method: str = "GET", body: dict | None = None, timeout: int = 60) -> dict:
    url = f"{BASE_URL.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_premarket(push: bool) -> int:
    _log("=== 启动盘前 AR3 v3.0 终极战报 & 精确买卖单作业 (09:10) ===")
    brief = _http_json("/api/g3/premarket-brief/generate", method="POST", body={}, timeout=60)
    stance = brief.get("market_stance") or {}
    ar3s = brief.get("ar3_summary") or {}
    _log(
        f"AR3 盘前终极战报已生成: date={brief.get('date')} stance={stance.get('regime_label')} "
        f"acct={ar3s.get('account_label')} sells={ar3s.get('sell_count')} buys={ar3s.get('buy_count')}"
    )
    if push:
        # 已合并为唯一权威 AR3 v3.0 盘前终极战报（含精确股数买卖单），不再连发两份旧版简报造成打架
        res = _http_json("/api/g3/premarket-brief/push-wechat", method="POST", body={}, timeout=30)
        _log(f"AR3 v3.0 盘前终极战报微信推送完成: ok={res.get('ok')} channel={res.get('channel')} detail={res.get('detail')}")
    return 0


def run_postclose() -> int:
    _log("=== 启动盘后盈利闭环作业 (17:45 · 含 AR3 v3.0 数据喂料与 Shadow 对账) ===")
    # 1. 自动同步 AR3 独立表：每日刷新 14 只跨资产 ETF，每周五同步增量财报/业绩预告/除权分红
    try:
        is_fri = datetime.now(BJ_TZ).weekday() == 4
        sync_q = f"/api/g3/mining/ar3/sync?sync_etf=true&sync_financials={'true' if is_fri else 'false'}"
        sr = _http_json(sync_q, method="POST", body={}, timeout=180)
        _log(f"AR3 数据自动喂料完成 (sync_financials={is_fri}): status={sr.get('status')}")
    except Exception as exc:
        _log(f"WARN AR3 数据自动喂料失败（继续后续步骤）: {exc}")

    # 2. 自动把当日同花顺回执入账，并运行 AR3 Shadow Ledger 对账与预编译明日调仓预案
    try:
        fills_res = _http_json("/api/g3/desk/sync-fills", method="POST", body={}, timeout=30)
        _log(f"成交回执自动入账: {fills_res.get('status') or fills_res}")
    except Exception as exc:
        _log(f"WARN 回执入账跳过: {exc}")

    try:
        rec = _http_json("/api/g3/mining/ar3/shadow-reconcile?account_id=acct1&days=20", method="GET", timeout=30)
        _log(
            f"AR3 Shadow 对账 (acct1): tracked={rec.get('days_tracked')}d "
            f"completion={rec.get('overall_completion_pct')}% avg_slip={rec.get('avg_slippage_bps')}bps"
        )
        plan = _http_json("/api/g3/mining/ar3/rebalance-plan?account_id=acct1&smooth_step=true&persist=true", method="GET", timeout=60)
        _log(
            f"AR3 明日调仓预案已固化: sells={len(plan.get('sell_orders') or [])} "
            f"buys={len(plan.get('buy_orders') or [])} regime={plan.get('regime')}"
        )
    except Exception as exc:
        _log(f"WARN AR3 对账/预案生成异常: {exc}")

    # 3. 导出 AR3-F (F2A + F3A) 实时候选池与 ct1曾 真实持仓供 17:45 AI 联网深度排雷（彻底淘汰旧主升票）
    ar3_radar = _http_json("/api/g3/mining/ar3/radar?force_refresh=true", method="GET", timeout=90)
    gate = _http_json("/api/g3/market-gate", method="GET", timeout=30)
    desk = _http_json("/api/g3/desk", method="GET", timeout=30)
    sr_data = ar3_radar.get("stock_radar") or {}
    ar3_cands: list[dict] = []
    seen_codes: set[str] = set()
    for bucket in (sr_data.get("exec_queue") or [], sr_data.get("f2a_candidates") or [], sr_data.get("f3a_candidates") or []):
        for c in bucket:
            ts = str(c.get("ts_code") or c.get("code") or "")
            if not ts or ts in seen_codes:
                continue
            seen_codes.add(ts)
            ar3_cands.append({
                "code": ts,
                "name": c.get("name"),
                "industry": c.get("industry"),
                "score": c.get("score"),
                "source_engine": c.get("source_engine"),
                "source_label": c.get("source_label"),
                "notice_date": c.get("notice_date"),
                "net_profit_yoy_pct": c.get("net_profit_yoy_pct"),
                "revenue_yoy_pct": c.get("revenue_yoy_pct"),
                "roe_pct": c.get("roe_pct"),
            })
    EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    export_payload = {
        "schema": "ar3f_ai_screening_export/2",
        "exported_at": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        "decision_date": sr_data.get("latest_date"),
        "entry_date": sr_data.get("latest_date"),
        "market_gate": {
            "regime": gate.get("regime"),
            "regime_label": gate.get("regime_label"),
            "allow_new_mainwave": gate.get("allow_new_mainwave"),
            "target_position_cap_pct": gate.get("target_position_cap_pct"),
            "actual_pct": (gate.get("r51_waterline") or {}).get("actual_pct"),
            "reasons": gate.get("reasons"),
        },
        "ar3_etf_targets": (ar3_radar.get("etf_radar") or {}).get("targets") or [],
        "desk_holdings": [
            {
                "code": c.get("code"),
                "name": c.get("name"),
                "total_shares": c.get("total_shares"),
                "regime": c.get("regime"),
                "in_holdings": c.get("in_holdings"),
            }
            for c in (desk.get("cards") or [])
        ],
        "candidates": ar3_cands,
    }
    EXPORT_PATH.write_text(json.dumps(export_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"AR3-F (F2A+F3A) 候选池已刷新并导出: date={sr_data.get('latest_date')} count={len(ar3_cands)} -> {EXPORT_PATH}")

    attr = _http_json("/api/g3/review/attribution?days=30", method="GET", timeout=30)
    s = attr.get("summary") or {}
    _log(
        f"四维归因已刷新: coverage={s.get('coverage_days')}d total={s.get('total_pnl')} "
        f"beta={s.get('beta_pnl')} alpha={s.get('alpha_pnl')} t={s.get('t_trade_pnl')}"
    )

    coach = _http_json("/api/g3/review/coach?days=14", method="GET", timeout=30)
    _log(
        f"AI交易教练体检已刷新: grade={coach.get('grade')} bleeding={len(coach.get('bleeding_points') or [])} "
        f"red_lines={coach.get('next_week_red_lines')}"
    )

    fe = _http_json("/api/g3/review/forward-eval?etf_days=60", method="GET", timeout=60)
    es = (fe.get("defense_etf_shadow") or {}).get("summary") or {}
    _log(
        f"前向验真与ETF影子已固化: ai_samples={(fe.get('ai_screening_forward') or {}).get('samples_total')} "
        f"etf_cum={es.get('cum_ret_pct')}% vs hs300={es.get('bench_cum_ret_pct')}% mdd_saved={es.get('drawdown_saved_pct')}pp"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AiStock G3 盈利闭环定时任务")
    parser.add_argument("--mode", required=True, choices=["premarket", "postclose"])
    parser.add_argument("--no-push", action="store_true", help="premarket 模式下仅生成简报不推微信")
    parser.add_argument("--force", action="store_true", help="跳过交易日门禁（仅限手工测试）")
    args = parser.parse_args()

    today_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    if not args.force:
        is_td, reason = _is_trading_day(today_str)
        if not is_td:
            _log(f"跳过 {args.mode} 作业：{today_str} {reason}")
            return 0

    try:
        if args.mode == "premarket":
            return run_premarket(push=not args.no_push)
        return run_postclose()
    except Exception as exc:
        _log(f"ERROR [{args.mode}]: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
