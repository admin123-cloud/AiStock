# -*- coding: utf-8 -*-
"""AiStock Phase 3C 宿主机半自动预填守护桥（仅预填、绝不提交、独立于只读桌面桥）。

安全铁律：
1. 强制 mode == "prefill_only" 且 allow_submit is False；任何请求提交/撤单的指令一律拒绝并落盘审计；
2. 消费 F:/Aistock/runtime/g3_prefill_drafts/queue/req_*.json，先经 services/broker/ths_prefill_gate.py
   纯逻辑门禁 + draft_id 幂等去重；
3. 仅调用 host_bridges/l2_order_filler.py::fill_order() 将代码/价格/数量填入同花顺界面并读回核验，
   绝不 Invoke 任何提交按钮；
4. 周期刷新 F:/Aistock/runtime/g3_prefill_drafts/bridge_heartbeat.json 供网页端显示在线状态。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from host_bridges import l2_order_filler  # noqa: E402
from services.broker.ths_prefill_gate import PrefillPolicy, evaluate_prefill_gate  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_ROOT = Path(r"F:\Aistock\runtime\g3_prefill_drafts")
LOG_PATH = Path(r"F:\Aistock\storage\logs\g3_prefill_bridge.log")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("g3_prefill_bridge")


def _atomic_write_json(
    path: Path,
    payload: dict[str, Any],
    *,
    retries: int = 6,
    retry_delay: float = 0.06,
) -> None:
    """原子写 JSON；在 Windows 下对并发读锁导致的瞬时 PermissionError 做短重试。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    last_exc: Optional[OSError] = None
    for attempt in range(max(1, retries)):
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_exc = exc
            if attempt + 1 < max(1, retries):
                time.sleep(retry_delay)
    if last_exc is not None:
        raise last_exc


def write_heartbeat(root: Path, *, state: str = "idle", active_draft_id: Optional[str] = None) -> None:
    _atomic_write_json(
        root / "bridge_heartbeat.json",
        {
            "schema": "g3_prefill_bridge_heartbeat/1",
            "state": state,
            "active_draft_id": active_draft_id,
            "pid": os.getpid(),
            "heartbeat_at": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        },
    )


def _consumed_ids(done_dir: Path) -> list[str]:
    if not done_dir.exists():
        return []
    return [f.stem for f in done_dir.glob("*.json")]


def process_one_prefill_request(
    root: Path,
    *,
    filler_fn: Optional[Callable[..., dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """消费 queue/ 下最早一条预填指令，通过安全门禁后仅预填不提交。"""
    queue_dir = root / "queue"
    done_dir = root / "done"
    if not queue_dir.is_dir():
        return None
    pending = sorted(queue_dir.glob("req_*.json"))
    if not pending:
        return None

    req_path = pending[0]
    try:
        req = json.loads(req_path.read_text(encoding="utf-8"))
    except Exception:
        req = {}

    draft_id = str(req.get("draft_id") or req_path.stem.replace("req_", "")).strip()
    done_dir.mkdir(parents=True, exist_ok=True)
    done_path = done_dir / f"{draft_id}.json"

    now_dt = now or datetime.now(BJ_TZ)
    today_d = now_dt.date()

    # 1. 硬安全校验：绝不允许 submit
    if bool(req.get("allow_submit", False)) or str(req.get("mode") or "").lower() != "prefill_only":
        out = {
            "draft_id": draft_id,
            "status": "rejected_submit_forbidden",
            "ok": False,
            "submitted": False,
            "message": "安全拦截：本桥接仅支持 prefill_only（严禁自动提交）",
            "finished_at": now_dt.isoformat(timespec="seconds"),
        }
        _atomic_write_json(done_path, out)
        req_path.unlink(missing_ok=True)
        return out

    # 2. 通过 ths_prefill_gate 纯逻辑门禁（含单笔上限、100股整手、draft_id 幂等去重）
    acct = str(req.get("account") or "acct1")
    gate_res = evaluate_prefill_gate(
        draft={
            "draft_id": draft_id,
            "account": acct,
            "trade_date": str(req.get("trade_date") or today_d.isoformat()),
            "code": str(req.get("code") or ""),
            "side": str(req.get("side") or "").upper(),
            "qty": req.get("qty"),
            "price": req.get("price"),
        },
        target_account=acct,
        trade_date=today_d,
        guard_snapshot={"ok": True, "owner_active": False},
        identity_snapshot={"account": acct, "fingerprint": f"prefill_bridge:{acct}"},
        consumed_draft_ids=_consumed_ids(done_dir),
        mode="prefill_only",
        allow_submit=False,
        policy=PrefillPolicy(max_order_qty=50_000, max_order_notional=200_000.0, allow_submit=False),
    )

    if not gate_res.get("allowed"):
        out = {
            "draft_id": draft_id,
            "status": "rejected_by_prefill_gate",
            "ok": False,
            "submitted": False,
            "reasons": gate_res.get("reasons") or [],
            "message": f"预填门禁拦截：{','.join(gate_res.get('reasons') or [])}",
            "finished_at": now_dt.isoformat(timespec="seconds"),
        }
        _atomic_write_json(done_path, out)
        req_path.unlink(missing_ok=True)
        return out

    # 3. 执行只填不点提交
    write_heartbeat(root, state="filling", active_draft_id=draft_id)
    ticket = gate_res["ticket"]
    exec_fill = filler_fn or l2_order_filler.fill_order
    try:
        fill_res = exec_fill(
            account=ticket["account"],
            code=ticket["code"],
            price=ticket["price"],
            qty=ticket["qty"],
            side=ticket["side"].lower(),
        )
        ok = bool(
            fill_res.get("success") is True
            or fill_res.get("ok") is True
            or fill_res.get("status") in ("ok", "filled")
        )
        err_detail = (
            fill_res.get("error")
            or fill_res.get("reason")
            or fill_res.get("note")
            or fill_res.get("status")
        )
        out = {
            "draft_id": draft_id,
            "status": "prefilled_awaiting_human_confirm" if ok else str(fill_res.get("status") or "fill_failed"),
            "ok": ok,
            "submitted": False,  # 永不为 True
            "ticket": ticket,
            "filler_result": fill_res,
            "message": (
                f"已预填至同花顺（{ticket['code']} {ticket['side']} {ticket['qty']}股 @ {ticket['price']}），请切换窗口人工核对并点击最终确认"
                if ok
                else f"预填未完成：{err_detail}"
            ),
            "finished_at": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        }
    except Exception as exc:
        out = {
            "draft_id": draft_id,
            "status": "fill_exception",
            "ok": False,
            "submitted": False,
            "ticket": ticket,
            "message": f"预填异常：{exc}",
            "finished_at": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        }

    _atomic_write_json(done_path, out)
    req_path.unlink(missing_ok=True)
    write_heartbeat(root, state="idle", active_draft_id=None)
    logger.info("预填完成 draft_id=%s ok=%s status=%s", draft_id, out["ok"], out["status"])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="AiStock G3 半自动预填守护桥（仅填单绝不提交）")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--poll-interval", type=float, default=1.5)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    logger.info("g3_prefill_bridge 启动（prefill_only 零自动提交），监听目录=%s", root / "queue")

    while True:
        try:
            write_heartbeat(root, state="idle")
            process_one_prefill_request(root)
        except Exception as exc:
            logger.exception("轮询异常: %s", exc)
        if args.once:
            break
        time.sleep(max(0.5, args.poll_interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
