# -*- coding: utf-8 -*-
"""R48 reconcile×coaching 集成单测：summary.coaching 字段、ledger coaching 文件、
trades_inferred fills 精确覆盖回写（±10min）、幂等、多账号分流。

fixture：2026-11-11 实况形态（上午两笔 + 10:21 后至草案 + sideways 周期 +
hard_stop 12.90 契约）。fetch_east_day_hl 打桩零网络。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scripts.t_trade_tracker as ttt  # noqa: E402
from services import ths_holdings_poll as poll  # noqa: E402
from services import t_trade_signal as tts  # noqa: E402

TODAY = date(2026, 11, 11)
STAMP = "20261111"

BUY_1004 = {"trade_time": "2026-11-11 10:04:31", "side": "BUY", "code": "002015",
            "name": "协鑫能科", "shares": 2000, "price": 13.33,
            "amount": 26660.0, "source": "ths_today_trades_ocr"}
SELL_1112 = {"trade_time": "2026-11-11 11:12:47", "side": "SELL", "code": "002015",
             "name": "协鑫能科", "shares": 2000, "price": 13.30,
             "amount": 26600.0, "source": "ths_today_trades_ocr"}
DRAFT = {"schema": "t_tracker_beat/1", "id": "T20261111_002015_buy_01",
         "ts": "2026-11-11 10:21:00", "code": "002015", "side": "buy-T",
         "subtype": "entry", "px": 13.2, "qty": 1300,
         "trigger": {"kind": "buy_band", "band": [13.0, 13.2]}}
REGIME_DOC = {"schema": "t_tracker_regime/1", "date": TODAY.isoformat(),
              "codes": {"002015": {"regime": "sideways"}}}
CONTRACT_DOC = {"schema": "stop_contract/1", "rules": [
    {"code": "002015", "hard_stop": 12.90,
     "rally_trim": {"price": 13.85, "qty_per_wave": 1000}}]}

INFERRED_ROWS = [
    {"schema": poll.SCHEMA_INFERRED, "ts": "2026-11-11 10:10:47",
     "account": "acct1", "code": "002015", "side": "buy",
     "delta_qty": 2000, "avail_delta": 0, "price_ref": 13.33,
     "source": "snapshot_diff", "fill_override": None},
    {"schema": poll.SCHEMA_INFERRED, "ts": "2026-11-11 11:20:47",
     "account": "acct1", "code": "002015", "side": "sell",
     "delta_qty": -2000, "avail_delta": -2000, "price_ref": 13.30,
     "source": "snapshot_diff", "fill_override": None},
]


def _make_root(tmp_path: Path, *, fills=(BUY_1004, SELL_1112),
               inferred=INFERRED_ROWS, contract=True) -> Path:
    root = tmp_path / "t_tracker"
    (root / "fills").mkdir(parents=True)
    (root / "fills" / f"{STAMP}.json").write_text(json.dumps(
        {"schema": "ths_fills_export/1", "status": "ok",
         "trade_date": TODAY.isoformat(), "captured_at":
         "2026-11-11 15:05:00", "fills": list(fills)}), encoding="utf-8")
    (root / "drafts").mkdir(parents=True)
    (root / "drafts" / f"{STAMP}.jsonl").write_text(
        json.dumps(DRAFT, ensure_ascii=False) + "\n", encoding="utf-8")
    (root / f"regime_{STAMP}.json").write_text(
        json.dumps(REGIME_DOC, ensure_ascii=False), encoding="utf-8")
    if contract:
        (root / "contract_stop.json").write_text(
            json.dumps(CONTRACT_DOC, ensure_ascii=False), encoding="utf-8")
    if inferred:
        poll._append_jsonl(poll.inferred_path(root, TODAY),
                           [dict(r) for r in inferred])
    return root


@pytest.fixture(autouse=True)
def _no_day_hl_network(monkeypatch):
    monkeypatch.setattr(tts, "fetch_east_day_hl",
                        lambda code, d: {"low": 13.10, "high": 13.62}
                        if code == "002015" else None)


def test_day_hl_parser_fields_and_guard():
    # fields2=f51,f54,f55 = 日期,高,低（R48a：曾把量/额当高低价）
    assert tts._parse_day_hl_klines(
        ["2026-11-10,13.84,13.20", "2026-11-11,13.62,13.10"],
        "2026-11-11") == {"high": 13.62, "low": 13.10}
    assert tts._parse_day_hl_klines(
        ["2026-11-11,473702,646351900.0"], "2026-11-11") == {
        "high": 473702.0, "low": 646351900.0}  # 错源行也会如实返回——
    # 消费侧 sanity：>1000 的"价格"必然是量/额错列，契约核对按不可用处理
    hl = tts._parse_day_hl_klines(["2026-11-11,473702,646351900.0"],
                                  "2026-11-11")
    sane = hl and 0 < hl["low"] < 10000 and 0 < hl["high"] < 10000
    assert sane is not True
    assert tts._parse_day_hl_klines(["2026-11-10,13.84,13.20"], "2026-11-11") is None


def test_reconcile_writes_coaching_and_overrides_inferred(tmp_path):
    root = _make_root(tmp_path)
    ttt.reconcile_all_accounts(root=root, for_date=TODAY)
    summary = json.loads((root / "ledger" / f"{STAMP}_summary.json")
                         .read_text("utf-8"))

    # ① summary.coaching（PM 15:12 直推面四字段 + 机器字段）
    coaching = summary["coaching"]
    for key in ("纪律评分", "跟单率", "最差一笔归因", "明日提醒"):
        assert key in coaching
    assert coaching["fills_scored"] == 2
    assert coaching["knife_catch_count"] == 0
    # 10:04 买被引擎配到 10:21 草案（±40min 同带）但草案后至 → 带外；
    # 11:12 卖无草案 → 带外；契约未触发（low 13.10 > 12.90，high < 13.85）
    assert coaching["纪律评分"] == 80 and coaching["跟单率"] == 0.0
    assert all(t["signal"] == "带外" for t in coaching["trades"])
    assert "带外" in coaching["明日提醒"]

    # ② ledger/<stamp>_coaching.json 详档
    detail = json.loads((root / "ledger" / f"{STAMP}_coaching.json")
                        .read_text("utf-8"))
    assert detail["schema"] == "t_tracker_coaching/1"
    assert detail["day_hl_used"]["002015"]["low"] == 13.10

    # ③ trades_inferred 被 fills 精确覆盖回写（两笔 ±10min 内匹配）
    rows = poll.read_jsonl(poll.inferred_path(root, TODAY))
    assert [(r["fill_override"]["status"],
             r["fill_override"]["sum_shares"],
             r["fill_override"]["price_weighted"]) for r in rows] == \
        [("matched", 2000, 13.33), ("matched", 2000, 13.30)]

    # ④ 幂等：重跑不重复覆盖、不重复扣分、summary 稳定
    poll._append_jsonl(poll.inferred_path(root, TODAY), [])  # no-op
    ttt.reconcile_all_accounts(root=root, for_date=TODAY)
    rows2 = poll.read_jsonl(poll.inferred_path(root, TODAY))
    assert rows2 == rows
    summary2 = json.loads((root / "ledger" / f"{STAMP}_summary.json")
                          .read_text("utf-8"))
    assert summary2["coaching"]["纪律评分"] == summary["coaching"]["纪律评分"]


def test_reconcile_inferred_unmatched_kept_honest(tmp_path):
    # 快照在 15:00 后人工减仓（无对应 fills）→ unmatched 如实保留
    extra = dict(INFERRED_ROWS[1], ts="2026-11-11 14:50:00")
    root = _make_root(tmp_path, inferred=[INFERRED_ROWS[0], extra])
    ttt.reconcile_all_accounts(root=root, for_date=TODAY)
    rows = poll.read_jsonl(poll.inferred_path(root, TODAY))
    assert rows[1]["fill_override"]["status"] == "unmatched"
    summary = json.loads((root / "ledger" / f"{STAMP}_summary.json")
                         .read_text("utf-8"))
    unmatched = summary["coaching"]["inferred_unmatched_by_fills"]
    assert unmatched and unmatched[0]["ts"] == "2026-11-11 14:50:00"


def test_reconcile_account_scoping_no_cross_account_override(tmp_path):
    # acct2 的 inferred 行不被 acct1 的 fills 覆盖
    acct2_row = dict(INFERRED_ROWS[0], account="acct2",
                     ts="2026-11-11 10:05:31")
    root = _make_root(tmp_path, inferred=[INFERRED_ROWS[0], acct2_row])
    ttt.reconcile_all_accounts(root=root, for_date=TODAY)
    rows = poll.read_jsonl(poll.inferred_path(root, TODAY))
    by_acct = {r["account"]: r for r in rows}
    assert by_acct["acct1"]["fill_override"]["status"] == "matched"
    assert by_acct["acct2"]["fill_override"] is None  # acct2 无 fills 文件→不触碰


def test_reconcile_coaching_failure_does_not_break_ledger(tmp_path):
    root = _make_root(tmp_path)
    (root / f"regime_{STAMP}.json").write_text("{ 坏 json", encoding="utf-8")
    ttt.reconcile_all_accounts(root=root, for_date=TODAY)
    assert (root / "ledger" / f"{STAMP}.jsonl").is_file()
    summary = json.loads((root / "ledger" / f"{STAMP}_summary.json")
                         .read_text("utf-8"))
    assert "coaching" in summary or "coaching_error" in summary
