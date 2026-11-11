# -*- coding: utf-8 -*-
"""R47 引擎票池多账号单测：分账号展开 / 草案 account 标注 / 分账号 1/3 限额。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import services.t_trade_signal as tts  # noqa: E402

DAY = datetime(2026, 11, 11)


def _write_state(tmp_path: Path, payload: dict) -> Path:
    f = tmp_path / "broker_state.json"
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return f


LEGACY = {
    "holdings": [{"code": "002015", "name": "协鑫能科", "shares": 4000,
                  "cost_price": 18.005, "current_price": 13.64}],
    "updated_at": "2026-11-11 09:00:00",
    "holding_source": "ths_fills_autosync",
}

DUAL = {
    "holdings": [{"code": "002015", "name": "协鑫能科", "shares": 4000}],
    "accounts": {
        "acct1": {"holdings": [{"code": "002015", "name": "协鑫能科", "shares": 4000}]},
        "acct2": {"holdings": [
            {"code": "002015", "name": "协鑫能科", "shares": 1000},
            {"code": "603881", "name": "数据港", "shares": 3000},
        ]},
    },
    "updated_at": "2026-11-11 09:00:00",
    "holding_source": "ths_fills_autosync",
}


@pytest.fixture
def pool_env(monkeypatch, tmp_path):
    def install(payload: dict):
        target_dir = tmp_path / "gen3_state_alpha"
        target_dir.mkdir(parents=True, exist_ok=True)
        state_file = target_dir / "broker_state.json"
        state_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        import utils.paths as up
        monkeypatch.setattr(up, "runtime_path",
                            lambda *parts: tmp_path.joinpath(*parts))
        return state_file
    return install


def test_pool_legacy_single_account_passthrough(pool_env):
    pool_env(LEGACY)
    pool = tts.load_broker_holdings()
    assert len(pool["holdings"]) == 1
    row = pool["holdings"][0]
    assert row["code"] == "002015" and row["shares"] == 4000
    assert row["account"] == "acct1"  # 数量口径零变化，仅加账号标注


def test_pool_dual_account_expansion(pool_env):
    pool_env(DUAL)
    pool = tts.load_broker_holdings()
    rows = {(r["code"], r["account"]): r["shares"] for r in pool["holdings"]}
    # 同码两账号=两行分别计（不合并）；603881 仅 acct2
    assert rows == {("002015", "acct1"): 4000, ("002015", "acct2"): 1000,
                    ("603881", "acct2"): 3000}
    # 排序稳定：账号序（acct1 前）
    order = [(r["code"], r["account"]) for r in pool["holdings"]]
    assert order.index(("002015", "acct1")) < order.index(("002015", "acct2"))


def test_per_account_lot_cap_split_not_merged():
    # 同码 2000+2000：分账号各 600（合计 1200），合并 4000 会得 1300 —— 必须分开
    assert tts.t_lot_cap(2000) == 600
    assert tts.t_lot_cap(2000) + tts.t_lot_cap(2000) == 1200
    assert tts.t_lot_cap(4000) == 1300  # 合并口径 ≠ 分账号口径，引擎用前者禁止


def _env_for(holding, *, px=13.10):
    """复用 test_t_trade_signal 的触发场景（买带+低RSI→entry 草案）。"""
    sys.path.insert(0, str(PROJECT_ROOT / "tests"))
    from test_t_trade_signal import down_series, info_with_params  # noqa: PLC0415

    now_dt = DAY.replace(hour=10, minute=1)
    closes5 = down_series(20, px)
    n = len(closes5)
    last_ts = now_dt - timedelta(minutes=1)
    first_ts = last_ts - timedelta(minutes=5 * (n - 1))
    bars5 = [{"ts": (first_ts + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M"),
              "close": c, "volume": 1000.0, "amount": c * 100000.0}
             for i, c in enumerate(closes5)]
    bars15 = list(bars5)
    info = info_with_params(holding["code"], "sideways")
    info["params"]["stop"] = 12.90
    return {
        "code": holding["code"], "name": holding.get("name") or "X", "regime_info": info,
        "params": info["params"], "bars5": bars5, "bars15": bars15,
        "px": px, "px_source": "test", "now": now_dt, "state": tts.SignalState(),
        "drafts_today": [], "holding": holding,
        "holdings_updated_at": "2026-11-11 09:00:00",
    }


def test_draft_carries_account_and_per_account_qty():
    holding2 = {"code": "002015", "name": "协鑫能科", "shares": 1000, "account": "acct2"}
    res = tts.evaluate_code(**_env_for(holding2))
    d = res.get("draft")
    assert d, "scenario should fire buy-T entry"
    assert d["account"] == "acct2"
    assert d["qty"] == 300  # t_lot_cap(1000)=300 —— acct2 自己的 1/3
    assert d["precheck"]["pool"]["account"] == "acct2"
    assert d["precheck"]["pool"]["shares"] == 1000


def test_draft_account_defaults_acct1_for_legacy_holding():
    holding = {"code": "002015", "name": "协鑫能科", "shares": 2000}  # 无 account 字段
    res = tts.evaluate_code(**_env_for(holding))
    d = res.get("draft")
    assert d and d["account"] == "acct1" and d["qty"] == 600  # R37 黄金口径不变


def test_res_key_distinguishes_accounts():
    assert tts._res_key("002015", {"account": "acct1"}) == "002015"
    assert tts._res_key("002015", {}) == "002015"
    assert tts._res_key("002015", {"account": "acct2"}) == "002015:acct2"
