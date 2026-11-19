# -*- coding: utf-8 -*-
"""L2 做T真实成交闭环端到端测试（#23-#26：正T开平/部分核销、反T配对、账号/日期隔离、幂等重放）。"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.l2_fill_closure import (  # noqa: E402
    normalize_fill_record,
    reconcile_account_fills_closure,
    sync_fills_to_signal_state,
)
from services.t_trade_signal import SignalState, is_fill_backed_open_t, load_state  # noqa: E402

TD = date(2026, 11, 19)


class TestL2FillClosureStateMachine:
    def test_forward_t_buy_partial_sell_full_close_and_idempotent_replay(self):
        state = SignalState({})
        fills = [
            # 兼容 THS 远航 OCR 导出格式（中文操作、date+time、deal_id）
            {
                "account": "acct1",
                "code": "300054.SZ",
                "operation": "证券买入",
                "volume": 1000,
                "avg_price": 20.0,
                "deal_id": "F-BUY-1",
                "date": "20261119",
                "time": "09:45:10",
            },
            # 部分卖出 400 股 → 剩余 600 股保持 fill_backed=True
            {
                "account": "acct1",
                "code": "300054.SZ",
                "operation": "证券卖出",
                "volume": 400,
                "avg_price": 20.5,
                "deal_id": "F-SELL-1",
                "date": "2026-11-19",
                "time": "10:15:00",
            },
        ]
        rep1 = sync_fills_to_signal_state(state, fills, target_account="acct1", trade_date=TD)
        assert rep1["applied_buys"] == 1
        assert rep1["applied_sells"] == 1
        assert rep1["open_t_codes"] == ["300054.SZ"]
        ot = state.open_t("300054.SZ")
        assert is_fill_backed_open_t(ot)
        assert ot["qty"] == 600
        assert ot["trimmed"] is True
        assert len(rep1["closed_pairs"]) == 1
        assert rep1["closed_pairs"][0]["direction"] == "forward_T"
        assert rep1["closed_pairs"][0]["gross_pnl"] == pytest.approx(200.0)

        # 重复回放同一批 fills → 100% 幂等，不重复扣减或翻倍
        rep_dup = sync_fills_to_signal_state(state, fills, target_account="acct1", trade_date=TD)
        assert rep_dup["applied_buys"] == 0 and rep_dup["applied_sells"] == 0
        assert rep_dup["idempotent_skipped"] == 2
        assert state.open_t("300054.SZ")["qty"] == 600

        # 全量核销剩余 600 股 → open_t 清零
        rep2 = sync_fills_to_signal_state(
            state,
            [
                {
                    "account": "acct1",
                    "code": "300054.SZ",
                    "side": "SELL",
                    "qty": 600,
                    "px": 20.8,
                    "fill_id": "F-SELL-2",
                    "fill_ts": "2026-11-19 14:10:00",
                }
            ],
            target_account="acct1",
            trade_date=TD,
        )
        assert state.open_t("300054.SZ") is None
        assert rep2["open_t_codes"] == []
        assert len(rep2["closed_pairs"]) == 2
        assert rep2["closed_pairs"][1]["gross_pnl"] == pytest.approx(480.0)

    def test_reverse_t_sell_then_buy_does_not_fabricate_forward_open_t(self):
        state = SignalState({})
        # 先卖 500 股（反T开腿）→ 不写 open_t
        rep1 = sync_fills_to_signal_state(
            state,
            [
                {
                    "account": "acct1",
                    "code": "002015.SZ",
                    "side": "SELL",
                    "qty": 500,
                    "px": 15.0,
                    "fill_id": "R-SELL-1",
                    "fill_ts": "2026-11-19 09:50:00",
                }
            ],
            target_account="acct1",
            trade_date=TD,
        )
        assert state.open_t("002015.SZ") is None
        assert rep1["reverse_t_sells"] == 1
        assert rep1["reverse_t_open_codes"] == ["002015.SZ"]

        # 回补 800 股：500 股结案反T（盈利 (15.0-14.5)*500=250），净多 300 股开正T open_t
        rep2 = sync_fills_to_signal_state(
            state,
            [
                {
                    "account": "acct1",
                    "code": "002015.SZ",
                    "side": "BUY",
                    "qty": 800,
                    "px": 14.5,
                    "fill_id": "R-BUY-1",
                    "fill_ts": "2026-11-19 13:20:00",
                }
            ],
            target_account="acct1",
            trade_date=TD,
        )
        assert rep2["reverse_t_open_codes"] == []
        assert len(rep2["closed_pairs"]) == 1
        assert rep2["closed_pairs"][0]["direction"] == "reverse_T"
        assert rep2["closed_pairs"][0]["matched_qty"] == 500
        assert rep2["closed_pairs"][0]["gross_pnl"] == pytest.approx(250.0)
        ot = state.open_t("002015.SZ")
        assert is_fill_backed_open_t(ot) and ot["qty"] == 300

    def test_account_and_date_isolation_and_legacy_advice_not_treated_as_fill(self, tmp_path):
        state = SignalState({})
        # 注入旧建议伪 open_t（无 fill_backed）
        state.set_open_t("603881.SH", {"draft_id": "adv-1", "entry_px": 30.0, "qty": 200, "ts": "2026-11-19 09:40:00"})

        rep = sync_fills_to_signal_state(
            state,
            [
                # 他账号成交 → 严禁混账
                {"account": "acct2", "code": "603881.SH", "side": "BUY", "qty": 200, "px": 30.0, "fill_id": "X1", "fill_ts": "2026-11-19 09:45:00"},
                # 跨日成交 → 拒绝
                {"account": "acct1", "code": "603881.SH", "side": "BUY", "qty": 200, "px": 30.0, "fill_id": "X2", "fill_ts": "2026-11-18 09:45:00"},
                # 缺 fill_id → 拒绝
                {"account": "acct1", "code": "603881.SH", "side": "BUY", "qty": 200, "px": 30.0, "fill_id": "", "fill_ts": "2026-11-19 09:45:00"},
                # 针对旧建议伪 open_t 的卖出 → 不伪造正T核销，转记反T卖腿
                {"account": "acct1", "code": "603881.SH", "side": "SELL", "qty": 200, "px": 31.0, "fill_id": "S1", "fill_ts": "2026-11-19 10:00:00"},
            ],
            target_account="acct1",
            trade_date=TD,
        )
        reasons = {r["reason"] for r in rep["rejected"]}
        assert reasons == {"account_mismatch", "date_mismatch", "missing_fill_id"}
        assert rep["closed_pairs"] == []
        assert rep["reverse_t_open_codes"] == ["603881.SH"]

        # 端到端落盘与 dry_run 验证
        dry = reconcile_account_fills_closure(
            root=tmp_path,
            for_date=TD,
            target_account="acct1",
            fills=[{"account": "acct1", "code": "300054.SZ", "side": "BUY", "qty": 100, "px": 20.0, "fill_id": "D1", "fill_ts": "2026-11-19 10:00:00"}],
            dry_run=True,
        )
        assert dry["dry_run"] is True and dry["applied_buys"] == 1
        assert load_state(tmp_path, TD).open_t("300054.SZ") is None

        applied = reconcile_account_fills_closure(
            root=tmp_path,
            for_date=TD,
            target_account="acct1",
            fills=[{"account": "acct1", "code": "300054.SZ", "side": "BUY", "qty": 100, "px": 20.0, "fill_id": "D1", "fill_ts": "2026-11-19 10:00:00"}],
            dry_run=False,
        )
        assert applied["dry_run"] is False
        assert is_fill_backed_open_t(load_state(tmp_path, TD).open_t("300054.SZ"))

    def test_cli_sync_fills_dry_run_apply_and_missing_file(self, tmp_path, capsys):
        import json
        from scripts import t_trade_tracker as cli

        # 1) 缺失成交文件 fail-closed 返回 2
        rc_missing = cli.main(["--sync-fills", "--date", "2026-11-19", "--account", "acct1", "--out-dir", str(tmp_path)])
        assert rc_missing == 2
        err_out = json.loads(capsys.readouterr().out)
        assert err_out["status"] == "missing_fills_file"

        # 2) 准备单账号成交文件
        fills_dir = tmp_path / "fills"
        fills_dir.mkdir(parents=True, exist_ok=True)
        (fills_dir / "20261119_acct1.json").write_text(
            json.dumps({
                "fills": [
                    {"account": "acct1", "code": "300054.SZ", "side": "BUY", "qty": 300, "px": 20.0, "fill_id": "C1", "fill_ts": "2026-11-19 09:40:00"},
                    {"account": "acct1", "code": "300054.SZ", "side": "SELL", "qty": 100, "px": 20.6, "fill_id": "C2", "fill_ts": "2026-11-19 10:10:00"},
                ]
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        # 3) 默认 dry-run 零落盘
        rc_dry = cli.main(["--sync-fills", "--date", "2026-11-19", "--account", "acct1", "--out-dir", str(tmp_path)])
        assert rc_dry == 0
        dry_out = json.loads(capsys.readouterr().out)
        assert dry_out["status"] == "ok" and dry_out["dry_run"] is True
        assert dry_out["open_t_codes"] == ["300054.SZ"]
        assert load_state(tmp_path, TD).open_t("300054.SZ") is None

        # 4) 显式 --apply 落盘，再重复执行 100% 幂等
        rc_apply = cli.main(["--sync-fills", "--date", "2026-11-19", "--account", "acct1", "--out-dir", str(tmp_path), "--apply"])
        assert rc_apply == 0
        apply_out = json.loads(capsys.readouterr().out)
        assert apply_out["dry_run"] is False and apply_out["applied_buys"] == 1 and apply_out["applied_sells"] == 1
        assert load_state(tmp_path, TD).open_t("300054.SZ")["qty"] == 200

        rc_replay = cli.main(["--sync-fills", "--date", "2026-11-19", "--account", "acct1", "--out-dir", str(tmp_path), "--apply"])
        assert rc_replay == 0
        replay_out = json.loads(capsys.readouterr().out)
        assert replay_out["idempotent_skipped"] == 2
        assert load_state(tmp_path, TD).open_t("300054.SZ")["qty"] == 200
