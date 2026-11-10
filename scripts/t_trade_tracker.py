# -*- coding: utf-8 -*-
"""R37 L1 半自动做T：CLI 入口（周期判定 / 盘中节拍 / 对账 / 回放）。

用法：
  python scripts/t_trade_tracker.py --regime  [--date 2026-11-11]
  python scripts/t_trade_tracker.py --beat     [--now "2026-11-11 10:01:00"]
  python scripts/t_trade_tracker.py --reconcile [--date 2026-11-11] [--fills-file path.json]
  python scripts/t_trade_tracker.py --replay --date 2026-11-10 [--cache cache.json]

零交易零 formal 写；产物全部在 runtime/t_tracker（回放写 replay/ 子目录，不动生产草案）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import t_trade_signal as tts  # noqa: E402
from services.t_trade_signal import BEIJING_TZ, fmt_ts, parse_ts  # noqa: E402

# 回放节拍：与线上 beat cron 同节律（09:36~14:56，每 5min，错峰 +1min）
REPLAY_START = dt_time(9, 36)
REPLAY_END = dt_time(14, 56)
REPLAY_STEP_MIN = 5


def _parse_date(text: Optional[str]) -> date:
    if not text:
        return datetime.now(BEIJING_TZ).date()
    return date.fromisoformat(text)


def _parse_now(text: Optional[str]) -> datetime:
    if not text:
        return datetime.now(BEIJING_TZ).replace(tzinfo=None)
    ts = parse_ts(text)
    if ts is None:
        raise SystemExit(f"--now 无法解析: {text}")
    return ts


def cmd_regime(args: argparse.Namespace) -> int:
    for_date = _parse_date(args.date)
    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    report = tts.run_regime(for_date=for_date, now=now, out_dir=args.out_dir)
    codes = {c: v["regime"] for c, v in (report.get("codes") or {}).items()}
    print(json.dumps({"status": report.get("status"), "date": report.get("date"),
                      "regimes": codes, "errors": report.get("errors", {})},
                     ensure_ascii=False))
    return 0 if report.get("status") in ("ok", "partial") else 1


def cmd_beat(args: argparse.Namespace) -> int:
    now = _parse_now(args.now)
    report = tts.run_beat(now=now, out_dir=args.out_dir)
    print(json.dumps({"status": report.get("status"), "ts": report.get("ts"),
                      "new_draft_ids": report.get("new_draft_ids", []),
                      "suppressed": {c: r.get("suppressed")
                                     for c, r in (report.get("codes") or {}).items()
                                     if isinstance(r, dict) and r.get("suppressed")}},
                     ensure_ascii=False))
    return 0 if report.get("status") == "ok" else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    for_date = _parse_date(args.date)
    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    loader: Optional[Callable[[date], tuple[list[dict], str]]] = None
    if args.fills_file:
        payload = json.loads(Path(args.fills_file).read_text(encoding="utf-8"))
        rows = payload.get("fills") if isinstance(payload, dict) else payload
        loader = lambda _d: (rows, f"file:{Path(args.fills_file).name}")  # noqa: E731
    report = tts.run_reconcile(for_date=for_date, now=now, out_dir=args.out_dir,
                               fills_loader=loader)
    print(json.dumps(report, ensure_ascii=False))
    return 0


def _replay_beat_times(day: date) -> list[datetime]:
    out, cur = [], datetime.combine(day, REPLAY_START)
    end = datetime.combine(day, REPLAY_END)
    while cur <= end:
        out.append(cur)
        cur += timedelta(minutes=REPLAY_STEP_MIN)
    return out


def _load_cache(cache_file: Path) -> dict:
    return json.loads(cache_file.read_text(encoding="utf-8"))


def _fetch_cache(pool_codes: list[str]) -> dict:
    cache: dict[str, Any] = {"fetched_at": fmt_ts(datetime.now(BEIJING_TZ).replace(tzinfo=None))}
    for code in pool_codes:
        cache[code] = {
            "bars5": tts.fetch_east_klines(code, 5),
            "bars15": tts.fetch_east_klines(code, 15),
        }
    return cache


def cmd_replay(args: argparse.Namespace) -> int:
    day = _parse_date(args.date)
    now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
    pool = tts.load_broker_holdings()
    codes = [str(h["code"]).split(".")[0] for h in pool["holdings"]]

    cache_file = Path(args.cache) if args.cache else (
        tts.tracker_dir() / "replay" / f"cache_{day.strftime('%Y%m%d')}.json")
    if cache_file.is_file():
        cache = _load_cache(cache_file)
    else:
        cache = _fetch_cache(codes)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    out_dir = tts.tracker_dir() / "replay" / day.strftime("%Y%m%d")
    regime_loader = lambda d: tts._load_json(tts.regime_file_path(tts.tracker_dir(), d))  # noqa: E731
    bars5_loader = lambda code: (cache.get(code) or {}).get("bars5") or []  # noqa: E731
    bars15_loader = lambda code: (cache.get(code) or {}).get("bars15") or []  # noqa: E731
    snapshot_loader = lambda code: None  # 回放无快照表依赖，价=已收 5m 收价  # noqa: E731

    beats = []
    for beat_now in _replay_beat_times(day):
        report = tts.run_beat(
            now=beat_now, out_dir=out_dir, holdings_loader=lambda: pool,
            regime_loader=regime_loader, bars5_loader=bars5_loader,
            bars15_loader=bars15_loader, snapshot_loader=snapshot_loader,
            write_pending_push=False)
        beats.append({"ts": report.get("ts"), "status": report.get("status"),
                      "new_draft_ids": report.get("new_draft_ids"),
                      "suppressed": {c: r.get("suppressed")
                                     for c, r in (report.get("codes") or {}).items()
                                     if isinstance(r, dict) and r.get("suppressed")}})

    drafts = tts.read_jsonl(tts.drafts_path(out_dir, day))
    summary = {
        "schema": "t_tracker_replay/1", "date": day.isoformat(),
        "generated_at": fmt_ts(now), "beats": len(beats),
        "draft_count": len(drafts),
        "drafts": [{k: d.get(k) for k in ("id", "ts", "code", "side", "subtype",
                                          "px", "qty", "trigger", "regime")}
                   for d in drafts],
        "suppression_timeline": [b for b in beats if b["suppressed"] or b["new_draft_ids"]],
        "cache_file": str(cache_file),
    }
    out_file = tts.tracker_dir() / "replay" / f"{day.strftime('%Y%m%d')}_report.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"draft_count": len(drafts),
                      "draft_ids": [d["id"] for d in drafts],
                      "report": str(out_file)}, ensure_ascii=False))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="R37 L1 半自动做T引擎 CLI")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--regime", action="store_true", help="09:25 周期判定")
    mode.add_argument("--beat", action="store_true", help="盘中信号节拍（单次）")
    mode.add_argument("--reconcile", action="store_true", help="15:10 当日成交对账")
    mode.add_argument("--replay", action="store_true", help="回放验证（写 replay/ 目录）")
    parser.add_argument("--date", help="YYYY-MM-DD（默认今天）")
    parser.add_argument("--now", help="beat 用北京时间 YYYY-MM-DD HH:MM:SS（默认当前）")
    parser.add_argument("--fills-file", help="对账成交文件（{fills:[...]}）")
    parser.add_argument("--cache", help="回放行情缓存文件路径")
    parser.add_argument("--out-dir", help="覆盖 t_tracker 根目录（测试用）")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.regime:
        return cmd_regime(args)
    if args.beat:
        return cmd_beat(args)
    if args.reconcile:
        return cmd_reconcile(args)
    return cmd_replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
