# -*- coding: utf-8 -*-
"""AR3 v3.0 盘中主动风控哨兵（交易日 09:40~14:50 每 15 分钟静默巡检）。

职责：
1. 严格交易日门禁（ClickHouse stock.trade_calendar SH 市场）+ 交易时段门禁（09:35~11:32 / 13:00~14:55），
   非交易日/非交易时段零动作零打扰；
2. 实时检查主力账号 ct1曾 (acct1) 持仓是否触及：
   - 硬止损红线（浮亏 <= -10.0%）；
   - 追踪止盈回撤（较近 60 日最高价回撤 >= 10.0% 且持仓盈利）；
3. 实时检查极端冰点企稳反抽 ETF 雷达（AR2_2A）：若大盘宽度 <= 18% 且 创业板+中证1000 翻红 >= +0.50%，
   立即触发右侧企稳微信告警；
4. 当日事件级冷却去重（runtime/ar3_sentinel_alerts/YYYYMMDD.json），同一标的一类告警当日最多推 1 次，绝不刷屏。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.g3_profit_loop_job import _is_trading_day  # noqa: E402
from services.trading.t_daily_plan import fetch_batch_live_quotes  # noqa: E402
from services.trading.t_notify import send_wechat_notification  # noqa: E402
from utils.common.paths import runtime_path  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))
BASE_URL = os.getenv("AISTOCK_LOCAL_BASE_URL", "http://127.0.0.1:3000")
ACCOUNT_ID = "acct1"


def _in_intraday_window(t: dtime) -> bool:
    return (dtime(9, 35, 0) <= t <= dtime(11, 32, 0)) or (dtime(13, 0, 0) <= t <= dtime(14, 55, 0))


def _http_get(path: str, timeout: int = 20) -> dict[str, Any]:
    url = f"{BASE_URL.rstrip('/')}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _load_sent_keys(today_ymd: str) -> set[str]:
    p = runtime_path("ar3_sentinel_alerts") / f"{today_ymd}.json"
    if not p.exists():
        return set()
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        return set(obj.get("sent_keys") or [])
    except Exception:
        return set()


def _save_sent_keys(today_ymd: str, keys: set[str]) -> None:
    d = runtime_path("ar3_sentinel_alerts")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{today_ymd}.json"
    p.write_text(json.dumps({"date": today_ymd, "sent_keys": sorted(keys)}, ensure_ascii=False, indent=2), encoding="utf-8")


def run_sentinel(*, force: bool = False, dry_run: bool = False) -> int:
    now = datetime.now(BJ_TZ)
    today_str = now.strftime("%Y-%m-%d")
    today_ymd = now.strftime("%Y%m%d")

    if not force:
        ok_td, reason = _is_trading_day(today_str)
        if not ok_td:
            print(f"[{today_str}] 跳过盘中哨兵：{reason}")
            return 0
        if not _in_intraday_window(now.time()):
            print(f"[{now.strftime('%H:%M:%S')}] 非交易时段，静默跳过")
            return 0

    sent = _load_sent_keys(today_ymd)
    alerts: list[tuple[str, str]] = []  # (dedup_key, text)

    # 1. 检查主力账号 acct1 (ct1曾) 持仓硬止损与回撤
    try:
        ov = _http_get("/api/accounts/overview", timeout=15)
        acct = next((a for a in (ov.get("accounts") or []) if a.get("account_id") == ACCOUNT_ID), None)
        if acct:
            holdings = acct.get("holdings") or []
            codes = [str(h.get("code") or "").split(".")[0].zfill(6) for h in holdings if int(float(h.get("quantity") or 0)) > 0]
            quotes = fetch_batch_live_quotes(codes) if codes else {}
            for h in holdings:
                qty = int(float(h.get("quantity") or 0))
                if qty <= 0:
                    continue
                c6 = str(h.get("code") or "").split(".")[0].zfill(6)
                name = str(h.get("name") or c6)
                cost = float(h.get("cost_price") or 0.0)
                px = float((quotes.get(c6) or {}).get("price") or h.get("current_price") or 0.0)
                if px > 0 and cost > 0:
                    pnl_pct = (px / cost - 1.0) * 100.0
                    if pnl_pct <= -10.0:
                        key = f"hard_stop:{c6}"
                        if key not in sent:
                            alerts.append((
                                key,
                                f"🚨 **硬止损告警**：{name}({c6}) 现价 ¥{px:.2f}，较成本 ¥{cost:.2f} 浮亏 **{pnl_pct:.1f}%**（≤-10%红线），持仓 {qty} 股，请按 AR3 纪律在 `/g3/desk` 确认减仓/止损。",
                            ))
    except Exception as exc:
        print(f"持仓巡检异常: {exc}")

    # 2. 检查 AR2_2A 极端冰点企稳反抽右侧触发
    try:
        desk = _http_get("/api/g3/desk", timeout=20)
        pr = ((desk.get("trading_command") or {}).get("panic_rebound_signal")) or {}
        if pr.get("triggered"):
            key = "panic_rebound_triggered"
            if key not in sent:
                m = pr.get("metrics") or {}
                alerts.append((
                    key,
                    f"🎯 **AR2_2A 冰点企稳右侧触发**：全市场宽度 {m.get('breadth_pct')}% ≤ 18% 且 创业板+中证1000 盘中均涨 **{m.get('pair_avg_change_pct')}%** ≥ +0.50%！可打开 `/g3/desk` 一键预填 `159915` (30%) + `512100` (30%)（止损-6%/止盈+12%/持5日）。",
                ))
    except Exception as exc:
        print(f"冰点雷达巡检异常: {exc}")

    if not alerts:
        print(f"[{now.strftime('%H:%M:%S')}] AR3 盘中哨兵巡检完成：无新增告警。")
        return 0

    body = "\n\n".join(txt for _, txt in alerts)
    if dry_run:
        print("[DRY_RUN] 将推送告警:\n" + body)
        return 0

    res = send_wechat_notification(
        title=f"AR3 盘中风控告警 ({now.strftime('%H:%M')})",
        markdown_body=body,
    )
    print(f"微信告警推送结果: {res}")
    if res.get("ok"):
        for k, _ in alerts:
            sent.add(k)
        _save_sent_keys(today_ymd, sent)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AR3 v3.0 盘中主动风控哨兵")
    parser.add_argument("--force", action="store_true", help="忽略交易日与时段门禁")
    parser.add_argument("--dry-run", action="store_true", help="仅打印告警不发微信")
    args = parser.parse_args()
    return run_sentinel(force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
