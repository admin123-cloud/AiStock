# -*- coding: utf-8 -*-
"""R48 host 侧盘中持仓巡读 CLI（schtasks 每 5min 触发 --once）。

用法：
  F:/python/python3.10/python.exe F:/Aistock/app-r48/scripts/ths_holdings_poll.py --once
  --once            单轮巡读（schtasks 模式：窗口守卫 09:35~15:00 + 秒错峰 47s）
  --probe-capture   单面板单次零交互采集（不限时段；验证 PrintWindow/OCR 链）
  --replay-morning  回放对拍：用真实 fills 重建快照序列→diff→inferred→fills 覆盖
  --account acctN   只巡指定账号（默认全部绑定面板）
  --now HH:MM[:SS]  覆盖巡读时刻（错峰等待与守卫按此判定；测试用）
  --out-dir PATH    覆盖 t_tracker 根目录（测试用）
  --no-stagger      跳过 :47s 错峰等待（手动即时巡）

退出码：0=正常（含跳过轮）；1=本轮全部账号 error（告警字段见 poller_state）。
零交易动作（零交互，连查询菜单都不点）；formal 表零写。
"""
from __future__ import annotations

import argparse
import json
import sys
import time as time_mod
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import ths_holdings_poll as poll  # noqa: E402
from services.ths_hotswitch import build_hotswitch_plan  # noqa: E402


def _parse_now(text: Optional[str]) -> datetime:
    if not text:
        return datetime.now()
    fmt = "%H:%M:%S" if len(text.split(":")) == 3 else "%H:%M"
    return datetime.combine(date.today(), datetime.strptime(text, fmt).time())


def _build_plan(account_filter: Optional[str]) -> dict[str, dict[str, Any]]:
    """bridge 面板枚举 × 注册表 → 有序计划 {account: {panel, identity_hint,
    hotswitch?}}（R49c：热切换组 home 在前、末位 restore_after；切换机制
    =ComboBox aid=2322 SelectionItem，注册表 hotkey 仅作绑定标记）。

    fills 导出器同语义：acct1 无绑定面板也入计划走默认 connect；未绑定其他
    账号绝采集。identity_hint 取自规则（默认 acct1=「曾」，R47 取证锚）。"""
    from services.ths_account_bridge import load_panel_registry

    try:
        from services.ths_account_bridge import enumerate_trade_panels
        # include_minimized：最小化面板也入解析（capture 仍 skipped_minimized
        # 让位零交互铁律；状态可辨 panel vs absent）
        live_panels = enumerate_trade_panels(include_minimized=True)
    except Exception:  # 非 host 环境 → 空面板表
        live_panels = []
    return build_hotswitch_plan(load_panel_registry(), live_panels,
                                account_filter=account_filter)


def cmd_once(args: argparse.Namespace) -> int:
    now = _parse_now(args.now)
    ok, reason = poll.should_poll(now)
    if not ok:
        print(json.dumps({"status": "out_of_window", "reason": reason,
                          "ts": now.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False))
        return 0
    if not args.no_stagger:
        wait = poll.stagger_sleep_seconds(datetime.now())
        if wait > 0:
            time_mod.sleep(min(wait, 60.0))
    root = Path(args.out_dir) if args.out_dir else _tracker_root()
    plan = _build_plan(args.account)
    report = poll.run_poll_round(
        root=root, now=datetime.now(), plan=plan,
        capture=lambda account, panel, hint=None: poll.capture_panel_zero_touch(
            account, panel, evidence_dir=poll.snapshots_dir(root, now.date()),
            identity_hint=hint,
            hotswitch=(plan.get(account) or {}).get("hotswitch")))
    print(json.dumps(report, ensure_ascii=False, default=str))
    statuses = [r.get("status") for r in report.get("accounts", {}).values()]
    return 1 if statuses and all(s == "error" for s in statuses) else 0


def cmd_probe(args: argparse.Namespace) -> int:
    """单面板一次零交互采集（任何时候安全：PrintWindow+UIA 只读，不吃焦点）。

    R49：--account acct2 时经热切换读目标账号（Alt+N 身份自证，读毕切回常态位）。"""
    root = Path(args.out_dir) if args.out_dir else _tracker_root()
    plan = _build_plan(args.account or "acct1")
    account, entry = next(iter(plan.items()))
    cap = poll.capture_panel_zero_touch(
        account, entry.get("panel"),
        evidence_dir=poll.snapshots_dir(root, date.today()),
        identity_hint=entry.get("identity_hint"),
        hotswitch=entry.get("hotswitch"))
    safe = {k: cap.get(k) for k in ("status", "note", "error", "ocr_line_count",
                                    "screenshot", "identity_label",
                                    "identity_hint", "switchback_failed")}
    if cap.get("status") == "ok":
        safe["holdings"] = [{k: h.get(k) for k in
                             ("code", "name", "shares", "available_shares",
                              "cost_price", "current_price", "holding_pnl")}
                            for h in cap.get("holdings") or []]
        safe["capital"] = cap.get("capital")
    safe["ocr_lines_preview"] = (cap.get("ocr_lines") or [])[:12]
    print(json.dumps(safe, ensure_ascii=False, default=str))
    return 0 if cap.get("status") == "ok" else 2


def cmd_replay_morning(args: argparse.Namespace) -> int:
    """Owner 上午两笔 inferred 回放对拍（证据生成器，写 replay/ 隔离目录）。

    输入：--fills（R37/R47 fills 协议文件，真实成交）+ --anchor CODE=QTY,...
    （当日开盘前持仓，默认取 broker_state holdings）。
    重建：锚点 → 逐笔 fill 后 +2min 合成快照（price_ref=成交价，标 synthetic）
    → diff_holdings → inferred 行 → override_inferred_with_fills（±10min）
    → 对拍报告（逐笔：inferred vs fill 匹配/价差）。绝写生产 trades_inferred。
    """
    from services import t_trade_coaching as coach

    fills_payload = json.loads(Path(args.fills).read_text(encoding="utf-8"))
    fills_rows = fills_payload.get("fills") if isinstance(fills_payload, dict) \
        else fills_payload
    trade_date = date.fromisoformat(str(fills_payload.get("trade_date")
                                        if isinstance(fills_payload, dict)
                                        else date.today().isoformat()))
    account = str(args.account or "acct1")
    anchor: dict[str, int] = dict(_anchor_holdings())

    day_fills = [f for f in fills_rows
                 if str(f.get("trade_time") or "").startswith(trade_date.isoformat())
                 and (f.get("account") in (None, "", account))]
    day_fills.sort(key=lambda f: str(f.get("trade_time")))

    # 合成快照序列：锚点(09:34) + 每笔后 +2min。
    # T+1 语义（与实盘一致）：买入→shares 增、available 不变；卖出→两者同减。
    anchor_shares = dict(anchor)
    events: list[dict[str, Any]] = [{
        "ts": datetime.combine(trade_date, dtime(9, 34)),
        "shares": dict(anchor_shares), "available": dict(anchor_shares)}]
    shares, available = dict(anchor_shares), dict(anchor_shares)
    from datetime import timedelta
    for f in day_fills:
        ts = datetime.strptime(str(f["trade_time"]), "%Y-%m-%d %H:%M:%S")
        d = int(f["shares"])
        code = str(f["code"])
        if str(f.get("side")).upper() == "BUY":
            shares[code] = int(shares.get(code, 0)) + d
        else:
            shares[code] = int(shares.get(code, 0)) - d
            available[code] = int(available.get(code, 0)) - d
        events.append({"ts": ts + timedelta(minutes=2),
                       "shares": dict(shares), "available": dict(available),
                       "fill": f})

    rows_seq: list[list[dict[str, Any]]] = []
    inferred: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        rows_seq.append([
            {"code": c, "name": "", "shares": q,
             "available_shares": ev["available"].get(c, q),
             "current_price": None}
            for c, q in ev["shares"].items() if q > 0])
        if i == 0:
            continue
        prev_rows, cur_rows = rows_seq[i - 1], rows_seq[i]
        # price_ref 用触发本快照那笔 fill 的真实成交价（标 synthetic）
        fill = ev.get("fill")
        if fill:
            for r in cur_rows:
                if r["code"] == str(fill["code"]):
                    r["current_price"] = float(fill["price"])
        changes = poll.diff_holdings(prev_rows, cur_rows)
        inferred.extend(poll.build_inferred_rows(
            account, ev["ts"], changes,
            f"synthetic_{i - 1:02d}", f"synthetic_{i:02d}"))
    for row in inferred:
        row["source"] = "snapshot_diff_replay(synthetic)"

    overrides = coach.override_inferred_with_fills(inferred, fills_rows)

    out_dir = Path(args.out_dir) if args.out_dir else (
        _tracker_root() / "replay" / f"r48_morning_{trade_date.strftime('%Y%m%d')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema": "ths_holdings_poll_replay/1", "trade_date": trade_date.isoformat(),
        "fills_used": len(day_fills), "anchor": anchor,
        "inferred_rows": [{k: r.get(k) for k in
                           ("ts", "code", "side", "delta_qty", "price_ref",
                            "avail_delta")} for r in inferred],
        "overrides": [{k: r.get(k) for k in
                       ("ts", "code", "side", "delta_qty", "price_ref",
                        "fill_override")} for r in overrides],
        "all_matched": all(
            (r.get("fill_override") or {}).get("status") == "matched"
            for r in overrides),
    }
    out_file = out_dir / "replay_report.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    (out_dir / "inferred.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n"
                for r in overrides), encoding="utf-8")
    print(json.dumps({"all_matched": report["all_matched"],
                      "fills_used": report["fills_used"],
                      "report": str(out_file)}, ensure_ascii=False))
    return 0 if report["all_matched"] else 3


def _anchor_holdings() -> list[tuple[str, int]]:
    """当日开盘前持仓锚点：broker_state.json holdings（权威=15:15 同步产物）。"""
    try:
        from utils.paths import runtime_path
        broker_file = Path(runtime_path("gen3_state_alpha")) / "broker_state.json"
    except Exception:
        broker_file = Path(r"F:\AiStock\data\data\runtime\gen3_state_alpha\broker_state.json")
    payload = json.loads(broker_file.read_text(encoding="utf-8"))
    out = []
    for h in payload.get("holdings") or []:
        code = str(h.get("code") or "").split(".")[0]
        qty = int(h.get("qty") or h.get("shares") or 0)
        if code and qty:
            out.append((code, qty))
    return out


def _tracker_root() -> Path:
    try:
        from utils.paths import runtime_path
        return Path(runtime_path("t_tracker"))
    except Exception:
        return Path(r"F:\AiStock\data\data\runtime\t_tracker")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="R48 盘中持仓巡读（host，零交互）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true",
                      help="单轮巡读（schtasks 模式：守卫+错峰）")
    mode.add_argument("--probe-capture", action="store_true",
                      help="单面板一次零交互采集（验证 PrintWindow/OCR 链）")
    mode.add_argument("--replay-morning", action="store_true",
                      help="回放对拍：fills 重建 inferred→覆盖匹配（写 replay/）")
    parser.add_argument("--account")
    parser.add_argument("--now", help="HH:MM[:SS]（当日时刻，测试用）")
    parser.add_argument("--out-dir")
    parser.add_argument("--no-stagger", action="store_true")
    parser.add_argument("--fills", help="replay-morning 用：fills 协议文件路径")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.once:
        return cmd_once(args)
    if args.probe_capture:
        return cmd_probe(args)
    return cmd_replay_morning(args)


if __name__ == "__main__":
    raise SystemExit(main())
