# -*- coding: utf-8 -*-
"""AiStock G3 每日 09:40 早盘成交后持仓同步与微信确认简报作业。

职责：
1. 严格交易日门禁（ClickHouse stock.trade_calendar SH 市场），休市静默跳过零打扰；
2. 当日防重保护（runtime/g3_premarket_brief/postopen_pushed_YYYYMMDD.json），同日仅推 1 次；
3. 检查双账号快照（latest_multi.json）是否为今日 09:30 之后的新鲜采集；
   若尚未更新且宿主机桌面桥在线，自动调用 POST /api/accounts/sync（with_fills=true）
   并轮询等待采集与窗口最小化收尾完成（最长 135s，超时不阻塞后续降级汇报）；
4. 调用 POST /api/g3/desk/sync-fills 与 POST /api/g3/watchlist/sync-ths 同步今日成交回执与观察池；
5. 基于最新真实持仓调用 POST /api/g3/premarket-brief/generate 重算并落盘当日简报
   （保留 09:10 隔夜要闻，并在 agent_thesis 头部注入 09:40 实盘持仓/成交确认摘要）；
6. 调用 POST /api/g3/premarket-brief/push-wechat 推送到微信。

安全铁律：
- 全程只读采集 + 局部台账/简报刷新，绝不触发任何实盘下单；
- 非交易日 fail-closed 零动作零打扰。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.g3_profit_loop_job import _is_trading_day  # noqa: E402
from utils.common.paths import runtime_path  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))
BASE_URL = os.getenv("AISTOCK_LOCAL_BASE_URL", "http://127.0.0.1:3000")
LOG_PATH = Path(os.getenv("AISTOCK_LOGS_ROOT", "F:/Aistock/storage/logs")) / "g3_postopen_brief_0940.log"
DEFAULT_SYNC_WAIT_SECONDS = 135.0
DEFAULT_SYNC_POLL_SECONDS = 5.0
FRESH_CUTOFF_TIME = dtime(9, 30, 0)


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


def _http_json(path: str, *, method: str = "GET", body: Optional[dict[str, Any]] = None, timeout: int = 60) -> dict[str, Any]:
    url = f"{BASE_URL.rstrip('/')}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _marker_path(today_ymd: str, brief_dir: Optional[Path] = None) -> Path:
    root = brief_dir or runtime_path("g3_premarket_brief")
    return root / f"postopen_pushed_{today_ymd}.json"


def _parse_iso_bj(raw: Any) -> Optional[datetime]:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BJ_TZ)
        return dt.astimezone(BJ_TZ)
    except Exception:
        return None


def _fmt_bj_hhmm(raw: Any) -> str:
    dt = _parse_iso_bj(raw)
    return dt.strftime("%H:%M") if dt is not None else ""


def is_snapshot_fresh_after_open(
    overview: dict[str, Any],
    *,
    today_bj: datetime,
    account_id: str = "acct1",
    cutoff: dtime = FRESH_CUTOFF_TIME,
) -> tuple[bool, Optional[str]]:
    """判断主力账号快照是否已在今日开盘（09:30）后刷新。"""
    acct = next(
        (a for a in (overview.get("accounts") or []) if str(a.get("account_id")) == account_id),
        None,
    )
    if not isinstance(acct, dict):
        return False, None
    gen_at = str(acct.get("generated_at") or "").strip() or None
    dt = _parse_iso_bj(gen_at)
    if dt is None:
        return False, gen_at
    if dt.date() != today_bj.date():
        return False, gen_at
    return dt.time() >= cutoff, gen_at


def _build_postopen_thesis(
    *,
    now_bj: datetime,
    overview: dict[str, Any],
    fill_sync: dict[str, Any],
    sync_note: str,
    prev_thesis: str,
    account_id: str = "acct1",
) -> str:
    acct = next(
        (a for a in (overview.get("accounts") or []) if str(a.get("account_id")) == account_id),
        None,
    ) or {}
    funds = acct.get("funds") or {}
    holdings = [
        h for h in (acct.get("holdings") or [])
        if float(h.get("quantity") or 0) > 0
    ]
    holding_parts = []
    for h in holdings:
        code = str(h.get("code") or "").split(".")[0].zfill(6)
        name = str(h.get("name") or code)
        qty = int(round(float(h.get("quantity") or 0)))
        holding_parts.append(f"{name}({code}) {qty}股")
    holdings_text = "、".join(holding_parts) if holding_parts else "空仓"
    total_assets = funds.get("total_assets")
    available = funds.get("available")
    assets_str = f"总资产 {float(total_assets):,.2f} / 可用 {float(available):,.2f}" if total_assets is not None and available is not None else "资产待刷新"
    fills_found = int(fill_sync.get("broker_fills_found") or 0)
    imported = int(fill_sync.get("imported") or 0)
    header = (
        f"【{now_bj.strftime('%H:%M')} 早盘持仓确认 · {sync_note}】"
        f"{acct.get('label') or account_id} {assets_str}；"
        f"最新持仓：{holdings_text}；"
        f"今日同花顺成交 {fills_found} 笔（本次新入账 {imported} 笔）。"
        "注：若早盘已完成当日步长调仓，下表剩余调仓留待下个交易日执行，切勿重复超额下单。"
    )
    prev_clean = str(prev_thesis or "").strip()
    if prev_clean and not prev_clean.startswith("【"):
        return f"{header}\n{prev_clean}"
    return header


def _wait_for_sync_done(
    request_id: str,
    *,
    http_fn: Callable[..., dict[str, Any]],
    max_wait_seconds: float,
    poll_seconds: float,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    deadline = time.monotonic() + max(0.0, float(max_wait_seconds))
    while True:
        try:
            ov = http_fn("/api/accounts/overview", method="GET", timeout=20)
            bridge = ov.get("bridge") or {}
            done = bridge.get("latest_completed") or {}
            if str(done.get("request_id") or "") == request_id:
                rc = int(done.get("rc", 1))
                status = str(done.get("status") or ("ok" if rc == 0 else "failed"))
                return rc == 0, f"done:{status}(rc={rc})"
            pending = list(bridge.get("pending_ids") or [])
            active_id = str(bridge.get("active_request_id") or "")
            if request_id not in pending and active_id != request_id and str(bridge.get("state") or "") == "idle":
                return False, "idle_without_result"
        except Exception as exc:
            _log(f"轮询同步状态异常: {exc}")
        if time.monotonic() >= deadline:
            return False, "sync_wait_timeout"
        sleep_fn(max(1.0, float(poll_seconds)))


def run_postopen_brief(
    *,
    force: bool = False,
    dry_run: bool = False,
    skip_sync: bool = False,
    sync_wait_seconds: float = DEFAULT_SYNC_WAIT_SECONDS,
    sync_poll_seconds: float = DEFAULT_SYNC_POLL_SECONDS,
    now: Optional[datetime] = None,
    http_fn: Callable[..., dict[str, Any]] = _http_json,
    trading_day_fn: Callable[[str], tuple[bool, str]] = _is_trading_day,
    sleep_fn: Callable[[float], None] = time.sleep,
    brief_dir: Optional[Path] = None,
) -> int:
    now_bj = (now.astimezone(BJ_TZ) if (now and now.tzinfo) else (now.replace(tzinfo=BJ_TZ) if now else datetime.now(BJ_TZ)))
    today_str = now_bj.strftime("%Y-%m-%d")
    today_ymd = now_bj.strftime("%Y%m%d")

    if not force:
        ok_td, reason = trading_day_fn(today_str)
        if not ok_td:
            _log(f"跳过 09:40 早盘持仓确认简报：{today_str} {reason}")
            return 0
        marker = _marker_path(today_ymd, brief_dir=brief_dir)
        if marker.exists():
            _log(f"今日 ({today_str}) 09:40 早盘确认简报已推送（{marker.name}），幂等跳过")
            return 0

    _log("=== 启动 09:40 早盘成交后持仓同步与微信确认简报作业 ===")

    # 1. 读取当前账户快照，检查是否已在 09:30 后刷新
    overview = http_fn("/api/accounts/overview", method="GET", timeout=20)
    fresh, gen_at = is_snapshot_fresh_after_open(overview, today_bj=now_bj)
    gen_hhmm = _fmt_bj_hhmm(gen_at)
    sync_note = f"快照已更新({gen_hhmm})" if (fresh and gen_hhmm) else "沿用现有快照"

    if not fresh and not skip_sync:
        bridge = overview.get("bridge") or {}
        if bridge.get("online"):
            try:
                enq = http_fn("/api/accounts/sync", method="POST", body={"with_fills": True}, timeout=20)
                req_id = str(enq.get("request_id") or "")
                _log(f"快照偏旧（{gen_at}），已自动触发只读双账号同步: request_id={req_id}")
                if req_id:
                    ok_sync, sync_status = _wait_for_sync_done(
                        req_id,
                        http_fn=http_fn,
                        max_wait_seconds=sync_wait_seconds,
                        poll_seconds=sync_poll_seconds,
                        sleep_fn=sleep_fn,
                    )
                    _log(f"只读同步等待结束: ok={ok_sync} status={sync_status}")
                    overview = http_fn("/api/accounts/overview", method="GET", timeout=20)
                    fresh_after, gen_after = is_snapshot_fresh_after_open(overview, today_bj=now_bj)
                    after_hhmm = _fmt_bj_hhmm(gen_after)
                    if ok_sync and fresh_after:
                        sync_note = f"已实时同步({after_hhmm})"
                    elif fresh_after:
                        sync_note = f"部分同步({after_hhmm})"
                    else:
                        sync_note = f"同步未完成({sync_status})，沿用旧快照"
            except Exception as exc:
                _log(f"自动触发账户同步失败（降级继续）: {exc}")
                sync_note = "同步触发失败，沿用旧快照"
        else:
            _log("宿主机桌面桥离线，跳过自动采集，沿用现有快照继续生成简报")
            sync_note = "桌面桥离线，沿用旧快照"

    # 2. 同步同花顺成交回执与做T观察池
    fill_sync: dict[str, Any] = {}
    try:
        fill_sync = http_fn("/api/g3/desk/sync-fills", method="POST", body={}, timeout=30)
        _log(
            f"成交回执入账完成: found={fill_sync.get('broker_fills_found')} "
            f"imported={fill_sync.get('imported')} skipped={fill_sync.get('idempotent_skipped')}"
        )
    except Exception as exc:
        _log(f"成交回执入账异常（继续生成简报）: {exc}")

    try:
        wl = http_fn("/api/g3/watchlist/sync-ths", method="POST", body={}, timeout=30)
        _log(f"观察池同步完成: summary={wl.get('summary')}")
    except Exception as exc:
        _log(f"观察池同步异常（忽略继续）: {exc}")

    # 3. 读取早盘简报保留隔夜要闻，按最新真实持仓重生成简报
    prev_notes: list[str] = []
    prev_thesis = ""
    try:
        prev_brief = http_fn("/api/g3/premarket-brief", method="GET", timeout=20)
        if str(prev_brief.get("date") or "") == today_str:
            prev_notes = [str(x) for x in (prev_brief.get("overnight_notes") or []) if str(x).strip()]
            prev_thesis = str(prev_brief.get("agent_thesis") or "")
    except Exception:
        pass

    thesis = _build_postopen_thesis(
        now_bj=now_bj,
        overview=overview,
        fill_sync=fill_sync,
        sync_note=sync_note,
        prev_thesis=prev_thesis,
    )

    # 3.5 可选接入本地 AI 决策总线（仅当 Antigravity 会话在线时等待 <=45s 补充深度思考；离线/超时零阻塞自动降级）
    if not dry_run:
        try:
            ai_res = http_fn(
                "/api/g3/ai-bus/ask",
                method="POST",
                body={
                    "kind": "postopen_0940",
                    "source": "g3_postopen_brief_0940",
                    "prompt": "结合当前 09:40 早盘实盘持仓与市场状态，给出 2~3 句精炼的操作提醒与风险把关（不超过 120 字）。",
                    "context": {"sync_note": sync_note, "thesis_preview": thesis[:500]},
                    "wait_timeout_sec": 45.0,
                },
                timeout=55,
            )
            if isinstance(ai_res, dict) and ai_res.get("status") == "completed":
                ai_doc = ai_res.get("result") or {}
                ai_note = str(ai_doc.get("summary") or ai_doc.get("answer") or "").strip()
                if ai_note:
                    thesis = f"{thesis}\n【AI参谋实时研判】{ai_note[:240]}"
                    _log(f"已融合在线 AI 参谋思考结论 (job_id={ai_res.get('job_id')})")
            else:
                _log(f"AI 决策总线未介入（自动降级）: status={(ai_res or {}).get('status')}")
        except Exception as exc:  # noqa: BLE001
            _log(f"AI 决策总线调用跳过（降级继续）: {exc}")

    if dry_run:
        _log(f"[DRY_RUN] 将按最新持仓重生成并推送简报:\n{thesis}")
        return 0

    brief = http_fn(
        "/api/g3/premarket-brief/generate",
        method="POST",
        body={
            "source": "agent_enriched",
            "overnight_notes": prev_notes,
            "agent_thesis": thesis,
        },
        timeout=60,
    )
    ar3s = brief.get("ar3_summary") or {}
    _log(
        f"09:40 最新持仓简报已重生成: date={brief.get('date')} "
        f"actual_pct={(brief.get('market_stance') or {}).get('actual_pct')}% "
        f"sells={ar3s.get('sell_count')} buys={ar3s.get('buy_count')}"
    )

    # 4. 推送至微信并写入当日防重标记
    res = http_fn("/api/g3/premarket-brief/push-wechat", method="POST", body={}, timeout=30)
    _log(f"微信推送结果: ok={res.get('ok')} channel={res.get('channel')} detail={res.get('detail')}")
    if res.get("ok"):
        marker = _marker_path(today_ymd, brief_dir=brief_dir)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "date": today_str,
                    "pushed_at": datetime.now(BJ_TZ).isoformat(),
                    "sync_note": sync_note,
                    "channel": res.get("channel"),
                    "detail": res.get("detail"),
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="AiStock G3 09:40 早盘持仓同步与微信确认简报")
    parser.add_argument("--force", action="store_true", help="跳过交易日门禁与当日防重，强制执行")
    parser.add_argument("--dry-run", action="store_true", help="仅演练同步与汇总，不重生成也不推微信")
    parser.add_argument("--skip-sync", action="store_true", help="跳过自动触发桌面桥同步，直接使用现有快照")
    parser.add_argument("--sync-wait-seconds", type=float, default=DEFAULT_SYNC_WAIT_SECONDS)
    args = parser.parse_args()
    return run_postopen_brief(
        force=args.force,
        dry_run=args.dry_run,
        skip_sync=args.skip_sync,
        sync_wait_seconds=args.sync_wait_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
