# -*- coding: utf-8 -*-
"""R55（2026-11-11）：涨停生态聚合更新 CLI（盘中追加 / 盘后终算）。

调用方：scripts/pipeline_scheduler.py
  - intraday 节拍：快照采集成功后追加 `--intraday`（realtime 键）；
  - finalize 节拍：工作日 16:40 `--finalize`（final 键，日线终算覆盖）。

产物：AISTOCK_DATA_ROOT/runtime/market_mood/YYYYMMDD.json（运营层，非 formal）。
退出码：0=成功 / 1=无数据或失败（fail-closed，不造数）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.market_mood import (  # noqa: E402
    compute_final_mood,
    compute_intraday_mood,
    write_certified_final_payload,
    write_day_payload,
)
from utils.logger import get_logger  # noqa: E402

logger = get_logger("market_mood_update")

__version__ = "1.1.0"


def run_intraday(out_root: Optional[Path] = None,
                 now: Optional[datetime] = None) -> dict:
    from zoneinfo import ZoneInfo
    from services.intraday_phase_gate import is_before_postclose_cutoff

    now_bj = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if not is_before_postclose_cutoff(now_bj):
        return {"status": "blocked", "reason": "after_1505_cutoff"}

    mood = compute_intraday_mood(now=now_bj)
    if mood is None:
        return {"status": "failure", "reason": "intraday_data_unavailable"}
    day = mood["date"].replace("-", "")
    write_day_payload(day, realtime=mood, out_root=out_root)
    logger.info(
        "[market_mood] intraday %s as_of=%s limit_up=%d seal_rate=%s",
        mood["date"], mood.get("as_of"), mood.get("limit_up_count", 0),
        mood.get("seal_rate"),
    )
    return {
        "status": "success", "mode": "intraday", "date": mood["date"],
        "as_of": mood.get("as_of"),
        "limit_up_count": mood.get("limit_up_count"),
        "seal_rate": mood.get("seal_rate"),
    }


def run_certified_finalize(
    trade_date: str,
    *,
    control_root: Optional[Path] = None,
    out_root: Optional[Path] = None,
    trusted_issuers: tuple[str, ...] = ("postclose_orchestrator",),
    now: Optional[datetime] = None,
    require_certificate: bool = True,
) -> dict:
    """S10 认证事件驱动终算：必须验证当日 ``CERTIFIED`` 发布证书且绑定摘要，重复触发幂等。"""
    from datetime import date as _date
    from services.postclose_publication import PublicationStore

    target_d = _date.fromisoformat(str(trade_date)[:10])
    store = PublicationStore(control_root)
    view = store.read_view(requested_date=target_d, trusted_issuers=trusted_issuers, now=now)
    if require_certificate and (not view.ready or view.status != "CERTIFIED" or not view.certificate_digest):
        return {
            "status": "blocked",
            "mode": "final",
            "date": target_d.isoformat(),
            "reason": f"postclose_not_certified:{view.reason}",
        }

    mood = compute_final_mood(target_d.isoformat())
    if mood is None:
        return {"status": "failure", "reason": "final_data_unavailable", "date": target_d.isoformat()}
    day = mood["date"].replace("-", "")
    if view.certificate_digest and view.generation >= 1:
        write_res = write_certified_final_payload(
            day,
            final=mood,
            certificate_digest=view.certificate_digest,
            generation=view.generation,
            certified_at=now,
            out_root=out_root,
        )
    else:
        write_day_payload(day, final=mood, out_root=out_root)
        write_res = {"status": "written", "generation": 0, "certificate_digest": None}
    return {
        "status": "success",
        "mode": "final",
        "date": mood["date"],
        "write_status": write_res["status"],
        "generation": write_res.get("generation"),
        "certificate_digest": write_res.get("certificate_digest"),
        "limit_up_count": mood.get("limit_up_count"),
        "ever_limit_up": mood.get("ever_limit_up"),
        "broke_count": mood.get("broke_count"),
        "max_streak": mood.get("max_streak"),
    }


def run_finalize(trade_date: Optional[str] = None,
                 out_root: Optional[Path] = None) -> dict:
    mood = compute_final_mood(trade_date)
    if mood is None:
        return {"status": "failure", "reason": "final_data_unavailable",
                "date": trade_date}
    day = mood["date"].replace("-", "")
    write_day_payload(day, final=mood, out_root=out_root)
    logger.info(
        "[market_mood] final %s limit_up=%d ever=%d broke=%d max_streak=%d",
        mood["date"], mood.get("limit_up_count", 0),
        mood.get("ever_limit_up", 0), mood.get("broke_count", 0),
        mood.get("max_streak", 0),
    )
    return {
        "status": "success", "mode": "final", "date": mood["date"],
        "limit_up_count": mood.get("limit_up_count"),
        "ever_limit_up": mood.get("ever_limit_up"),
        "broke_count": mood.get("broke_count"),
        "max_streak": mood.get("max_streak"),
    }


def exit_code(report: dict) -> int:
    return 0 if report.get("status") == "success" else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="AiStock 涨停生态聚合更新（R55）：盘中 realtime / 盘后 final "
                    "写 runtime/market_mood/YYYYMMDD.json（运营层，非 formal 表）。")
    parser.add_argument("--intraday", action="store_true",
                        help="盘中模式：用最新快照计算 realtime 键")
    parser.add_argument("--finalize", action="store_true",
                        help="盘后模式：kline_daily 终算 final 键")
    parser.add_argument("--date", default=None,
                        help="finalize 目标交易日（YYYY-MM-DD，默认=日线最新）")
    parser.add_argument("--out-root", default=None,
                        help="输出根目录（默认 AISTOCK_DATA_ROOT/runtime；测试注入用）")
    parser.add_argument("--require-certified", action="store_true",
                        help="S10 门禁：仅当日 postclose_control 呈 CERTIFIED 证书有效时才执行终算")
    parser.add_argument("--control-root", default=None,
                        help="S9/S10 postclose_control 控制目录（默认 runtime/postclose_control）")
    args = parser.parse_args(argv)

    out_root = Path(args.out_root) if args.out_root else None
    control_root = Path(args.control_root) if args.control_root else None

    if args.intraday and args.finalize:
        parser.error("--intraday 与 --finalize 互斥")
    if not args.intraday and not args.finalize:
        parser.error("必须指定 --intraday 或 --finalize")

    if args.intraday:
        report = run_intraday(out_root=out_root)
    elif args.require_certified:
        target = args.date or datetime.now().strftime("%Y-%m-%d")
        report = run_certified_finalize(
            target, control_root=control_root, out_root=out_root, require_certificate=True
        )
    else:
        report = run_finalize(trade_date=args.date, out_root=out_root)
    print(json.dumps(report, ensure_ascii=False))
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
