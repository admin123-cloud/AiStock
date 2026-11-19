# -*- coding: utf-8 -*-
"""ths_client 远航版段（read-account / switch-account）测试。

覆盖：argparse 新子命令路由、纯函数解析层（脱敏账号识别/资金汇总双路径/
持仓列名适配与表头映射/切换验证裁决/Owner 活跃纯函数）、mock UIA/截图/
视觉层的读取与切换状态机（幂等/Owner 守卫/未登录 fail-closed/定位失败
不盲点/切换后验证/资金区身份交叉门禁）、统一 JSON envelope 契约。
零真机依赖：全部真机交互面（进程/窗口/PrintWindow/视觉/UIA/点击）打桩。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ths_client as tc  # noqa: E402

MAIN_HWND = 111
PANEL_HWND = 222
PANEL_RECT = [10, 20, 810, 620]   # width 800 > 300（面板枚举契约）
MASKED_A1 = "A5****1851"
MASKED_A2 = "A6****5646"


def _panel(hwnd: int = PANEL_HWND) -> dict:
    return {"hwnd": hwnd, "pid": 34, "rect": list(PANEL_RECT), "width": 800,
            "height": 600, "title": "广发证券交易面板"}


def _main() -> dict:
    return {"hwnd": MAIN_HWND, "pid": 33, "rect": [0, 0, 1280, 800],
            "area": 1280 * 800, "title": "同花顺远航"}


def _item(text: str, x: float, y: float, aid: str = "") -> dict:
    return {"text": text, "x": x, "y": y, "score": 0.9, "aid": aid}


def _masked_items(masked: str = MASKED_A1) -> list[dict]:
    return [_item(masked, 400.0, 20.0)]


def _acct2_items() -> list[dict]:
    return [_item(MASKED_A2, 400.0, 20.0)]


def _funds_uia_texts() -> list[dict]:
    """UIA Text 桩：屏幕坐标 = 面板原点 (10,20) + 相对坐标。"""
    def mk(rel_x: float, rel_y: float, text: str, aid: str = "") -> dict:
        cx, cy = PANEL_RECT[0] + rel_x, PANEL_RECT[1] + rel_y
        return {"text": text, "aid": aid,
                "rect": [int(cx - 40), int(cy - 8), int(cx + 40), int(cy + 8)]}

    return [
        mk(200, 160, "资金余额"), mk(300, 160, "50,000.00"),
        mk(200, 190, "可取"), mk(300, 190, "40,000.00"),
        mk(200, 220, "冻结"), mk(300, 220, "0.00"),
        mk(500, 160, "股票市值"), mk(620, 160, "62,842.68"),
        mk(500, 190, "持仓盈亏"), mk(620, 190, "-157.32"),
        mk(500, 220, "总资产"), mk(620, 220, "112,842.68"),
        mk(700, 170, "当日盈亏"), mk(800, 170, "-90.00"),
        mk(200, 500, "委托价格"),   # 资金区外：region 过滤须剔除
    ]


@pytest.fixture()
def mock_env(monkeypatch, tmp_path):
    """打桩全部真机依赖；返回可断言的调用记录 dict。"""
    calls: dict = {"clicks": [], "f12": 0, "captures": [], "tree_selects": [],
                   "foreground": 0}
    tc._YH_ACCOUNT_STATE["current_used_account"] = None
    monkeypatch.setattr(tc, "_pids_of",
                        lambda name: [33] if name == "happ.exe"
                        else ([34] if name == "xiadan.exe" else []))
    monkeypatch.setattr(tc, "_yh_main_window", _main)
    monkeypatch.setattr(tc, "_yh_enum_panels", lambda: [_panel()])

    def fake_capture(hwnd, prefix):
        calls["captures"].append(prefix)
        return {"path": str(tmp_path / f"{prefix}.png"), "width": 800,
                "height": 600}

    monkeypatch.setattr(tc, "_yh_capture_window", fake_capture)
    monkeypatch.setattr(tc, "_yh_read_image",
                        lambda path: {"items": [], "rows": [],
                                      "reader": None, "errors": []})
    monkeypatch.setattr(tc, "_yh_uia_texts", lambda hwnd: [])
    monkeypatch.setattr(tc, "_yh_uia_login_page", lambda hwnd: False)

    def fake_tree(hwnd, name="资金股票"):
        calls["tree_selects"].append(name)
        return {"ok": True, "method": "uia_selection", "item": name}

    monkeypatch.setattr(tc, "_yh_uia_select_tree_item", fake_tree)
    monkeypatch.setattr(tc, "_yh_owner_using", lambda hwnd: False)

    def fake_f12(hwnd):
        calls["f12"] += 1
        return {"foreground_ok": True, "via": "keybd_event"}

    monkeypatch.setattr(tc, "_yh_send_f12", fake_f12)
    # GUI 串行锁打桩：测试不得触达 runtime 锁文件（2026-11-15 单账号链新增）
    monkeypatch.setattr(tc, "_yh_acquire_gui_lock",
                        lambda timeout_seconds=10.0, lock_path=None: {
                            "acquired": True, "path": "mock://yh.lock",
                            "fh": None})
    monkeypatch.setattr(tc, "_yh_release_gui_lock", lambda lock: None)
    # 全局输入空闲打桩（确定性 + 不读真机输入状态）
    monkeypatch.setattr(tc, "_yh_input_idle_seconds", lambda: 9999.0)

    def fake_click(hwnd, xy):
        coords = list(xy)[:2]
        calls["clicks"].append([float(coords[0]), float(coords[1])])
        return {"clicked_screen": [0, 0],
                "window_xy": [float(coords[0]), float(coords[1])],
                "hwnd": int(hwnd)}

    monkeypatch.setattr(tc, "_yh_click_window", fake_click)

    def fake_fg(hwnd):
        calls["foreground"] += 1
        return {"ok": True, "attached": False}

    monkeypatch.setattr(tc, "_yh_set_foreground", fake_fg)
    monkeypatch.setattr(tc, "_write_report",
                        lambda report, root=None, prefix="report":
                        tmp_path / f"{prefix}.json")
    return calls


# ── 路由 ────────────────────────────────────────────────────────────

def test_routes_yuanhang_subcommands():
    parser = tc.build_parser()
    args = parser.parse_args(["read-account", "--account", "acct1"])
    assert args.func is tc.cmd_read_account and args.account == "acct1"
    assert parser.parse_args(["read-account"]).account is None
    args = parser.parse_args(["switch-account", "--to", "acct2"])
    assert args.func is tc.cmd_switch_account and args.to == "acct2"


def test_switch_requires_to_and_rejects_unknown():
    with pytest.raises(SystemExit):
        tc.build_parser().parse_args(["switch-account"])
    with pytest.raises(SystemExit):
        tc.build_parser().parse_args(["switch-account", "--to", "acct9"])
    with pytest.raises(SystemExit):
        tc.build_parser().parse_args(["read-account", "--account", "acct9"])


# ── 纯函数：脱敏账号识别 ────────────────────────────────────────────

def test_masked_normalize_and_account_mapping():
    assert tc.yh_normalize_masked("顶栏 A5****1851 曾") == MASKED_A1
    assert tc.yh_normalize_masked("A 6 * * * * 5 6 4 6") == MASKED_A2  # OCR 空隙
    assert tc.yh_account_from_masked(MASKED_A1) == "acct1"
    assert tc.yh_account_from_masked(MASKED_A2) == "acct2"
    assert tc.yh_account_from_masked("B9****0000") is None   # 非注册账号
    assert tc.yh_account_from_masked(None) is None
    assert tc.yh_normalize_masked("无脱敏 601238 协鑫") is None


# ── 纯函数：资金汇总解析 ────────────────────────────────────────────

def test_funds_label_pairing_and_region_filter():
    items = []
    for t in _funds_uia_texts():
        cx = (t["rect"][0] + t["rect"][2]) / 2 - PANEL_RECT[0]
        cy = (t["rect"][1] + t["rect"][3]) / 2 - PANEL_RECT[1]
        items.append(_item(t["text"], cx, cy, t["aid"]))
    parsed = tc.yh_parse_funds_texts(items)
    assert parsed["funds"] == {
        "balance": 50000.0, "withdrawable": 40000.0, "frozen": 0.0,
        "market_value": 62842.68, "holding_profit": -157.32,
        "total_assets": 112842.68, "daily_profit": -90.0,
    }


def test_funds_auto_id_path_vendored_easyths_map():
    items = [_item("1,128.42", 300, 160, aid="1012"),
             _item("9,999.99", 300, 190, aid="1015")]
    parsed = tc.yh_parse_funds_texts(items)
    assert parsed["funds"]["balance"] == 1128.42          # [EasyTHS] 1012
    assert parsed["funds"]["total_assets"] == 9999.99     # [EasyTHS] 1015


def test_funds_merged_label_value_text():
    items = [_item("资金余额 50,000.00", 300, 160)]
    parsed = tc.yh_parse_funds_texts(items)
    assert parsed["funds"]["balance"] == 50000.0


def test_funds_region_excludes_outside_items():
    items = [_item("资金余额", 300, 400), _item("50,000.00", 400, 400)]
    assert tc.yh_parse_funds_texts(items)["funds"] == {}   # y400 在实测区外
    strict = tc.yh_parse_funds_texts(items, region=None, strict=True)
    assert "balance" in strict["funds"]                    # 全面板兜底命中


def test_holdings_exclude_nav_column_and_icons():
    """远航版实机布局回归（2026-11-14 实拍坐标）：左导航树列与操作列图标
    不得污染持仓行（此前「当日委托」被误读为证券名称）。"""
    items = [
        _item("资金股票", 77.5, 221.5), _item("当日委托", 70.8, 266.5),
        _item("历史成交", 70.5, 288.5),          # 导航树列（x<200 排除）
        _item("三", 230.5, 272.0),               # 操作列图标（OCR 误读单字）
        _item("操作", 230.5, 253.0),
        _item("证券代码", 278.5, 252.0), _item("证券名称", 342.0, 252.0),
        _item("股票余额", 410.0, 252.0), _item("可用余额", 480.5, 252.5),
        _item("冻结数量", 552.0, 252.0), _item("成本价", 621.5, 253.5),
        _item("市价", 686.5, 253.5), _item("盈亏", 762.0, 253.5),
        _item("当日盈亏", 935.5, 253.5),
        _item("603881", 272.5, 271.0), _item("数据港", 335.0, 271.5),
        _item("600", 429.0, 271.0), _item("600", 501.0, 271.5),
        _item("0", 579.0, 271.0), _item("33.993", 629.0, 271.0),
        _item("20.010", 694.5, 271.0), _item("-8390.000", 771.5, 271.5),
        _item("-180.00", 949.5, 272.0),
    ]
    rows = tc.yh_holdings_from_items(items)
    assert len(rows) == 1
    row = rows[0]
    assert row["code"] == "603881" and row["name"] == "数据港"
    assert row["quantity"] == 600.0 and row["available_quantity"] == 600.0
    assert row["frozen_quantity"] == 0.0
    assert row["cost_price"] == 33.993 and row["current_price"] == 20.01
    assert row["floating_profit"] == -8390.0
    assert row["daily_profit"] == -180.0


def test_funds_prefix_merged_text_no_misassign():
    """实机形态「值+标签」合并文本（值属左邻标签）：本标签值取同行右侧
    数值，绝不把前缀数误配给本标签。"""
    items = [
        _item("资金余额", 246.0, 125.5),
        _item("4076.82可取金额", 367.0, 125.5),   # 4076.82 属资金余额
        _item("4076.82持仓盈亏", 513.5, 126.0),   # 可取金额值=右邻数值
        _item("-8390.00", 630.0, 125.5),          # 持仓盈亏值
    ]
    parsed = tc.yh_parse_funds_texts(items)
    assert parsed["funds"]["balance"] == 4076.82
    assert parsed["funds"]["withdrawable"] == 4076.82   # 可取金额→withdrawable
    assert parsed["funds"]["holding_profit"] == -8390.0


def test_funds_suffix_merged_text_still_assigns():
    """「标签+值」形态（值属本标签）：余量抽数归属不变。"""
    parsed = tc.yh_parse_funds_texts([_item("资金余额 50,000.00", 300, 160)])
    assert parsed["funds"]["balance"] == 50000.0


# ── 纯函数：持仓行解析（EasyTHS 列名适配） ─────────────────────────

def test_holding_record_alias_adaptation_easyths():
    """[vendored EasyTHS parse_holding_row] 实盘/模拟列名差异适配。"""
    row = tc.yh_normalize_holding_record({
        "证券代码": "000001", "证券名称": "平安银行", "股票余额": "1,200",
        "可用余额": "200", "成本价": "10.5", "市价": "11.0", "盈亏": "600",
        "盈亏比(%)": "5.7", "市值": "13,200"})
    assert row["code"] == "000001" and row["name"] == "平安银行"
    assert row["quantity"] == 1200.0 and row["available_quantity"] == 200.0
    assert row["cost_price"] == 10.5 and row["current_price"] == 11.0
    assert row["floating_profit"] == 600.0 and row["profit_ratio"] == 5.7
    assert row["market_value"] == 13200.0
    assert row["daily_profit"] is None                     # 缺列 → None


def test_holdings_from_items_header_mapping():
    def row(y, cells):
        return [_item(t, x, y) for x, t in cells]

    items = row(330, [(210, "证券代码"), (280, "证券名称"), (370, "持仓数量"),
                      (460, "参考成本价"), (550, "当前价"), (640, "浮动盈亏")])
    items += row(352, [(210, "601238"), (280, "协鑫科技"), (370, "4100"),
                       (460, "8.50"), (550, "8.71"), (640, "861.00")])
    items += row(374, [(210, "300054"), (280, "鼎龙股份"), (370, "100"),
                       (460, "25.00"), (550, "24.80"), (640, "-20.00")])
    rows = tc.yh_holdings_from_items(items)
    assert len(rows) == 2
    assert rows[0]["code"] == "601238" and rows[0]["name"] == "协鑫科技"
    assert rows[0]["quantity"] == 4100.0 and rows[0]["cost_price"] == 8.50
    assert rows[0]["current_price"] == 8.71
    assert rows[0]["floating_profit"] == 861.0
    assert rows[1]["code"] == "300054"
    assert rows[1]["floating_profit"] == -20.0


def test_holdings_from_items_ordered_fallback_no_header():
    items = [_item("600000", 210, 350), _item("浦发银行", 280, 350),
             _item("100", 370, 350), _item("9.90", 460, 350),
             _item("10.10", 550, 350), _item("20.00", 640, 350)]
    rows = tc.yh_holdings_from_items(items)
    assert len(rows) == 1
    assert rows[0]["quantity"] == 100.0
    assert rows[0]["cost_price"] == 9.90
    assert rows[0]["current_price"] == 10.10
    assert rows[0]["floating_profit"] == 20.0


def test_rows_from_vision_direct_records():
    rows = tc.yh_rows_from_vision([
        {"code": "600000", "name": "浦发银行", "quantity": "100",
         "cost_price": "9.9"},
        {"foo": "bar"},                                    # 无代码 → 丢弃
    ])
    assert len(rows) == 1 and rows[0]["quantity"] == 100.0


# ── 纯函数：切换验证裁决 / Owner 活跃决策 ──────────────────────────

def test_switch_verdict_paths():
    assert tc.yh_switch_verdict(MASKED_A1, MASKED_A2, MASKED_A2)[0] == "verified"
    assert tc.yh_switch_verdict(MASKED_A1, MASKED_A1, MASKED_A2)[0] == "failed"
    assert tc.yh_switch_verdict(MASKED_A1, None, MASKED_A2,
                                funds_before=112842.68,
                                funds_after=93000.0)[0] == "unverified_funds_jump"
    assert tc.yh_switch_verdict(MASKED_A1, None, MASKED_A2,
                                funds_before=1.0, funds_after=1.0)[0] == "failed"
    assert tc.yh_switch_verdict(MASKED_A1, MASKED_A1, MASKED_A2)[1] != ""


def test_owner_active_decision_pure():
    # idle≥阈值 → 不活跃；从未自注入 → 空闲即 Owner；≈自家注入 → 不算
    assert tc.yh_owner_active_decision(120.0, 5.0) is False
    assert tc.yh_owner_active_decision(30.0, 0) is True
    assert tc.yh_owner_active_decision(30.0, float("inf")) is True
    assert tc.yh_owner_active_decision(30.0, 29.5) is False   # 自家注入
    assert tc.yh_owner_active_decision(10.0, 29.5) is True    # 注入后真输入


# ── switch-account 状态机（mock 真机层） ───────────────────────────

def test_switch_already_on_target_idempotent(mock_env, monkeypatch):
    monkeypatch.setattr(tc, "_yh_read_image",
                        lambda path: {"items": _masked_items(), "rows": [],
                                      "reader": "vision_model", "errors": []})
    report = tc.run_switch_account(to="acct1", settle_sec=0, menu_wait_sec=0)
    assert report["rc"] == 0 and report["status"] == "already_on_target"
    assert report["clicks"] == []                      # 幂等不触 GUI
    assert tc._YH_ACCOUNT_STATE["current_used_account"] == "acct1"


def test_switch_owner_active_skips_clicks(mock_env, monkeypatch):
    monkeypatch.setattr(tc, "_yh_owner_using", lambda hwnd: True)
    report = tc.run_switch_account(to="acct2", settle_sec=0, menu_wait_sec=0)
    assert report["rc"] == 3 and report["status"] == "owner_active"
    assert report["clicks"] == [] and mock_env["f12"] == 0


def test_switch_not_logged_in_fail_closed_when_no_panel(mock_env, monkeypatch):
    monkeypatch.setattr(tc, "_yh_enum_panels", lambda: [])
    report = tc.run_switch_account(to="acct2", f12_wait=0.02, settle_sec=0,
                                   menu_wait_sec=0)
    assert report["rc"] == 4
    assert report["status"] == "not_logged_in_fail_closed"
    assert report["clicks"] == [] and mock_env["f12"] == 1   # 试过 F12


def test_switch_login_page_fail_closed(mock_env, monkeypatch):
    monkeypatch.setattr(tc, "_yh_uia_login_page", lambda hwnd: True)
    report = tc.run_switch_account(to="acct2", settle_sec=0, menu_wait_sec=0)
    assert report["rc"] == 4
    assert report["status"] == "not_logged_in_fail_closed"
    assert report["clicks"] == []
    assert mock_env["captures"] == ["switch_login_page"]


def test_switch_happy_path_with_coords_config(mock_env, tmp_path):
    cfg = tmp_path / "menu.json"
    cfg.write_text(json.dumps({"trade_menu_xy": [300, 40],
                               "accounts": {"acct2": [400, 300]}}),
                   encoding="utf-8")
    reads = [{"items": _masked_items(), "rows": [], "reader": "vision_model",
              "errors": []},
             {"items": _acct2_items(), "rows": [], "reader": "vision_model",
              "errors": []}]

    def seq(path):
        return reads.pop(0)

    import ths_client as t
    monkey = t.__dict__
    monkey["_yh_read_image"] = seq
    try:
        report = t.run_switch_account(to="acct2", menu_config=cfg,
                                      settle_sec=0, menu_wait_sec=0)
    finally:
        monkey["_yh_read_image"] = lambda path: {
            "items": [], "rows": [], "reader": None, "errors": []}
    assert report["rc"] == 0 and report["status"] == "switched"
    assert mock_env["clicks"] == [[300.0, 40.0], [400.0, 300.0]]
    assert report["masked_id_after"] == MASKED_A2
    assert report["evidence"]["verdict"] == "verified"
    assert t._YH_ACCOUNT_STATE["current_used_account"] == "acct2"


def test_switch_vision_assisted_locate(mock_env, tmp_path):
    missing = tmp_path / "nope.json"               # 无坐标配置 → 视觉辅助
    reads = [
        {"items": _masked_items(), "rows": [], "reader": "ocr_local",
         "errors": []},                            # before
        {"items": [_item("行情", 60, 40), _item("交易", 300, 40),
                   _item("资讯", 540, 40)], "rows": [], "reader": "ocr_local",
         "errors": []},                            # 菜单栏
        {"items": [_item("切换账号", 350, 280),
                   _item("广发证券－曾*", 360, 310),
                   _item("广发证券－王*美", 360, 280)],
         "rows": [], "reader": "ocr_local",
         "errors": []},                            # 菜单展开（全角横线实机形态）
        {"items": _acct2_items(), "rows": [], "reader": "ocr_local",
         "errors": []},                            # after
    ]
    to_acct = "acct2"

    def seq(path):
        return reads.pop(0)

    import ths_client as t
    saved = t._yh_read_image
    t._yh_read_image = seq
    try:
        report = t.run_switch_account(to=to_acct, menu_config=missing,
                                      settle_sec=0, menu_wait_sec=0)
    finally:
        t._yh_read_image = saved
    assert report["rc"] == 0 and report["status"] == "switched"
    assert mock_env["clicks"] == [[300.0, 40.0], [360.0, 280.0]]
    assert report["menu_hit"]["matched"] == "交易"
    assert report["menu_item_hit"]["text"] == "广发证券－王*美"  # 全角横线实拍形态


def test_switch_owner_char_never_used_as_item_candidate(mock_env, tmp_path):
    """实机教训回归：单字 Owner 名绝不作为账号项候选（曾误中消息流
    「3分钟快速开户Q曾14:50:14」）——消息流行存在时必须定位失败而非盲点。"""
    missing = tmp_path / "nope.json"
    reads = [
        {"items": _masked_items(), "rows": [], "reader": "ocr_local",
         "errors": []},                            # before：acct1
        {"items": [_item("交易", 300, 40)], "rows": [], "reader": "ocr_local",
         "errors": []},
        {"items": [_item("3分钟快速开户Q曾14:50:14", 400, 300)],
         "rows": [], "reader": "ocr_local", "errors": []},
    ]

    def seq(path):
        return reads.pop(0)

    saved = tc._yh_read_image
    tc._yh_read_image = seq
    try:
        report = tc.run_switch_account(to="acct2", menu_config=missing,
                                       settle_sec=0, menu_wait_sec=0)
    finally:
        tc._yh_read_image = saved
    assert report["rc"] == 4
    assert report["status"] == "menu_item_not_located"
    assert len(report["clicks"]) == 1 \
        and report["clicks"][0]["target"] == "trade_menu"   # 只点了菜单
    assert all(c["target"] != "acct2" for c in report["clicks"])


def test_switch_menu_not_located_no_blind_click(mock_env, tmp_path):
    missing = tmp_path / "nope.json"
    saved = tc._yh_read_image
    tc._yh_read_image = lambda path: {
        "items": [_item("行情", 60, 40)], "rows": [], "reader": "ocr_local",
        "errors": []}
    try:
        report = tc.run_switch_account(to="acct2", menu_config=missing,
                                       settle_sec=0, menu_wait_sec=0)
    finally:
        tc._yh_read_image = saved
    assert report["rc"] == 4 and report["status"] == "menu_not_located"
    assert report["clicks"] == []          # 绝不盲点


def test_switch_unverified_funds_jump_fail_closed(mock_env, tmp_path):
    cfg = tmp_path / "menu.json"
    cfg.write_text(json.dumps({"trade_menu_xy": [300, 40],
                               "accounts": {"acct2": [400, 300]}}),
                   encoding="utf-8")
    reads = iter([
        {"items": _masked_items(), "rows": [], "reader": "vision_model",
         "errors": []},
        {"items": [], "rows": [], "reader": "vision_model", "errors": []},
    ])
    saved = tc._yh_read_image
    tc._yh_read_image = lambda _path: next(reads)
    state = {"n": 0}
    saved_uia = tc._yh_uia_texts

    def funds_seq(hwnd):
        state["n"] += 1
        if state["n"] == 1:            # before：总资产 112842.68
            return _funds_uia_texts()
        # after：顶栏读不到，总资产跳变 → 弱锚（UIA rect 形）
        return [
            {"text": "总资产", "aid": "",
             "rect": [PANEL_RECT[0] + 460, PANEL_RECT[1] + 212,
                      PANEL_RECT[0] + 540, PANEL_RECT[1] + 228]},
            {"text": "93,000.00", "aid": "",
             "rect": [PANEL_RECT[0] + 580, PANEL_RECT[1] + 212,
                      PANEL_RECT[0] + 660, PANEL_RECT[1] + 228]},
        ]

    tc._yh_uia_texts = funds_seq
    try:
        report = tc.run_switch_account(to="acct2", menu_config=cfg,
                                       settle_sec=0, menu_wait_sec=0)
    finally:
        tc._yh_read_image = saved
        tc._yh_uia_texts = saved_uia
    assert report["rc"] == 2 and report["status"] == "switch_unverified"
    assert tc._YH_ACCOUNT_STATE["current_used_account"] is None  # 缓存不刷


def test_switch_dry_run_locates_without_clicks(mock_env, tmp_path):
    cfg = tmp_path / "menu.json"
    cfg.write_text(json.dumps({"trade_menu_xy": [300, 40],
                               "accounts": {"acct2": [400, 300]}}),
                   encoding="utf-8")
    report = tc.run_switch_account(to="acct2", menu_config=cfg, dry_run=True,
                                   settle_sec=0, menu_wait_sec=0)
    assert report["rc"] == 0 and report["status"] == "dry_run_located"
    assert report["would_click"][-1]["window_xy"] == [400.0, 300.0]
    assert report["clicks"] == []


# ── read-account 状态机（mock 真机层） ─────────────────────────────

def _install_read_readings(reads: list[dict]):
    """按序弹出的 _yh_read_image 桩。"""
    queue = list(reads)

    def seq(_path):
        return queue.pop(0) if queue else {
            "items": [], "rows": [], "reader": None, "errors": []}

    saved = tc._yh_read_image
    tc._yh_read_image = seq
    return saved


def _run_v1_snapshot(*, account=None, with_fills=False, settle_sec=0, menu_wait_sec=0):
    """V1 单账号采集器+纯裁决测试入口（对应 _yh_collect_single_snapshot + yh_single_snapshot）。"""
    _ = settle_sec, menu_wait_sec
    lock = tc._yh_acquire_gui_lock(timeout_seconds=10.0)
    if not lock.get("acquired"):
        return {"rc": 4, "status": "lock_busy", "account": None, "funds": {},
                "holdings": [], "fills": {"status": "unavailable", "reason": "lock_busy"}}
    rep = {"steps": [], "captures": [], "capture_path": None}
    try:
        ev = tc._yh_collect_single_snapshot(
            account=account, with_fills=with_fills, f12_wait=0.0, main_wait=0.0, report=rep)
    finally:
        tc._yh_release_gui_lock(lock)
    if ev.get("abort_status"):
        return {
            "schema": tc.SCHEMA, "command": "read-account", "yuanhang": True,
            "credentials_touched": False, "rc": 4, "status": ev["abort_status"],
            "account": None, "masked_id": ev.get("masked_before"),
            "funds": {}, "funds_labels": {}, "funds_read_ok": False,
            "holdings": [], "holdings_source": "not_read",
            "page_switch": ev.get("page_switch"),
            "vision_errors": ev.get("vision_errors", []),
            "capture_path": rep.get("capture_path"),
            "fills": {"status": "unavailable", "reason": ev["abort_status"], "rows": []},
        }
    snap = tc.yh_single_snapshot(ev)
    if ev.get("holdings_source") == "ocr_unavailable":
        snap_status = "ok"
        rc = 0
    else:
        snap_status = "ok" if snap.get("status") == "ok" else "partial"
        rc = 0
    return {
        "schema": tc.SCHEMA, "command": "read-account", "yuanhang": True,
        "credentials_touched": False, "rc": rc, "status": snap_status,
        "account": ev.get("expected_account") or ev.get("identity_before"),
        "masked_id": ev.get("masked_before"),
        "funds": ev.get("funds") or {},
        "funds_labels": ev.get("funds_labels") or {},
        "funds_read_ok": bool(ev.get("funds")),
        "holdings": ev.get("holdings") or [],
        "holdings_source": ev.get("holdings_source"),
        "page_switch": ev.get("page_switch"),
        "vision_errors": ev.get("vision_errors", []),
        "capture_path": rep.get("capture_path"),
        "snapshot_decision": snap,
    }


def test_read_account_ok_full_chain(mock_env):
    vision_rows = [{"code": "601238", "name": "协鑫科技", "quantity": "4100",
                    "cost_price": "8.50", "current_price": "8.71",
                    "floating_profit": "861.0"}]
    reads = [{"items": _masked_items(), "rows": vision_rows,
              "reader": "vision_model", "errors": []},
             {"items": _masked_items(), "rows": vision_rows,
              "reader": "vision_model", "errors": []}]
    saved = _install_read_readings(reads)
    saved_uia = tc._yh_uia_texts
    tc._yh_uia_texts = lambda hwnd: _funds_uia_texts()
    try:
        report = _run_v1_snapshot(settle_sec=0, menu_wait_sec=0)
    finally:
        tc._yh_read_image = saved
        tc._yh_uia_texts = saved_uia
    assert report["rc"] == 0 and report["status"] == "ok"
    assert report["account"] == "acct1" and report["masked_id"] == MASKED_A1
    assert report["funds"]["balance"] == 50000.0
    assert report["funds"]["total_assets"] == 112842.68
    assert report["funds_read_ok"] is True
    assert report["holdings_source"] == "vision_model"
    assert report["holdings"][0]["code"] == "601238"
    assert "read_account" in report["capture_path"]
    assert mock_env["tree_selects"] == ["资金股票"]
    assert mock_env["captures"][0] == "read_account"


def test_read_account_ocr_unavailable_honest(mock_env):
    reads = [{"items": _masked_items(), "rows": [], "reader": None,
              "errors": ["vision_offline"]},
             {"items": _masked_items(), "rows": [], "reader": None,
              "errors": []}]
    saved = _install_read_readings(reads)
    saved_uia = tc._yh_uia_texts
    tc._yh_uia_texts = lambda hwnd: _funds_uia_texts()
    try:
        report = _run_v1_snapshot(settle_sec=0, menu_wait_sec=0)
    finally:
        tc._yh_read_image = saved
        tc._yh_uia_texts = saved_uia
    assert report["rc"] == 0
    assert report["holdings"] == []
    assert report["holdings_source"] == "ocr_unavailable"
    assert report["vision_errors"] == ["vision_offline"]


def test_read_account_identity_gate_unreadable_fail_closed(mock_env):
    # 顶栏读不到 + 资金区可读 → 不归属任何账号（fail-closed）
    img_items = [_item("资金余额", 300, 160), _item("50,000.00", 400, 160)]
    reads = [{"items": img_items, "rows": [], "reader": "ocr_local",
              "errors": []}]
    saved = _install_read_readings(reads)
    try:
        report = _run_v1_snapshot(settle_sec=0, menu_wait_sec=0)
    finally:
        tc._yh_read_image = saved
    assert report["rc"] == 4
    assert report["status"] == "identity_gate_unreadable"
    assert report["funds"] == {}                    # 门禁拦截：资金不归属
    assert report["holdings"] == []
    assert report["holdings_source"] == "not_read"


def test_read_account_not_logged_in_fail_closed(mock_env):
    reads = [{"items": [], "rows": [], "reader": None, "errors": []}]
    saved = _install_read_readings(reads)
    try:
        report = _run_v1_snapshot(settle_sec=0, menu_wait_sec=0)
    finally:
        tc._yh_read_image = saved
    assert report["rc"] == 4
    assert report["status"] == "not_logged_in_fail_closed"


def test_read_account_auto_switch_then_read(mock_env, monkeypatch):
    # 2026-11-15：PM 契约（tests/test_ths_single_snapshot_contract.py）要求
    # read-account 新链绝不切号；自动切换能力原样保留在
    # run_read_account_switchable（断言零改动，仅指向保有该能力的入口）。
    switch_calls: list[dict] = []

    def fake_switch(**kwargs):
        switch_calls.append(kwargs)
        monkeypatch.setattr(tc, "_yh_enum_panels", lambda: [_panel(hwnd=333)])
        return {"status": "switched", "rc": 0, "from_account": "acct1",
                "masked_id_after": MASKED_A2,
                "evidence": {"verdict": "verified"}}

    monkeypatch.setattr(tc, "run_switch_account", fake_switch)
    reads = [{"items": _masked_items(), "rows": [], "reader": "vision_model",
              "errors": []},                          # before：acct1
             {"items": _acct2_items(), "rows": [], "reader": "vision_model",
              "errors": []}]                          # 切换后：acct2
    saved = _install_read_readings(reads)
    saved_uia = tc._yh_uia_texts
    tc._yh_uia_texts = lambda hwnd: _funds_uia_texts()
    try:
        report = tc.run_read_account_switchable(account="acct2", settle_sec=0,
                                                menu_wait_sec=0)
    finally:
        tc._yh_read_image = saved
        tc._yh_uia_texts = saved_uia
    assert report["rc"] == 0 and report["account"] == "acct2"
    assert report["masked_id"] == MASKED_A2
    assert switch_calls[0]["to"] == "acct2"
    assert report["switch"]["status"] == "switched"


def test_read_account_no_switch_wrong_account_fail_closed(mock_env):
    reads = [{"items": _masked_items(), "rows": [], "reader": "vision_model",
              "errors": []}]
    saved = _install_read_readings(reads)
    try:
        report = _run_v1_snapshot(account="acct2", settle_sec=0, menu_wait_sec=0)
    finally:
        tc._yh_read_image = saved
    assert report["rc"] == 4
    assert report["status"] == "wrong_account_fail_closed"


def test_read_account_owner_active_zero_input(mock_env, monkeypatch):
    monkeypatch.setattr(tc, "_yh_owner_using", lambda hwnd: True)
    reads = [{"items": _masked_items(), "rows": [], "reader": "vision_model",
              "errors": []}]
    saved = _install_read_readings(reads)
    saved_uia = tc._yh_uia_texts
    tc._yh_uia_texts = lambda hwnd: _funds_uia_texts()
    try:
        report = _run_v1_snapshot(settle_sec=0, menu_wait_sec=0)
    finally:
        tc._yh_read_image = saved
        tc._yh_uia_texts = saved_uia
    assert report["rc"] == 0
    assert mock_env["f12"] == 0                  # 不键击
    assert mock_env["tree_selects"] == []        # 不点击翻页
    assert report["page_switch"]["attempted"] is False


# ── CLI envelope 契约 ──────────────────────────────────────────────

def test_read_account_cli_envelope(mock_env, monkeypatch, capsys):
    reads = [{"items": _masked_items(), "rows": [], "reader": "vision_model",
              "errors": []},
             {"items": _masked_items(), "rows": [], "reader": "vision_model",
              "errors": []}]
    saved = _install_read_readings(reads)
    saved_uia = tc._yh_uia_texts
    tc._yh_uia_texts = lambda hwnd: _funds_uia_texts()
    monkeypatch.setattr(tc, "run_read_account",
                        lambda **kw: _run_v1_snapshot(account=kw.get("account"),
                                                      with_fills=kw.get("with_fills", False)))
    try:
        rc = tc.main(["read-account"])
    finally:
        tc._yh_read_image = saved
        tc._yh_uia_texts = saved_uia
    assert rc == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["schema"] == "ths_client/1"
    assert envelope["command"] == "read-account"
    assert envelope["credentials_touched"] is False
    assert envelope["yuanhang"] is True


def test_switch_cli_envelope_dry_run(mock_env, capsys, tmp_path):
    cfg = tmp_path / "menu.json"
    cfg.write_text(json.dumps({"trade_menu_xy": [300, 40],
                               "accounts": {"acct1": [400, 280]}}),
                   encoding="utf-8")
    saved = tc._yh_read_image
    tc._yh_read_image = lambda _p: {"items": _acct2_items(), "rows": [],
                                    "reader": "vision_model", "errors": []}
    try:
        rc = tc.main(["switch-account", "--to", "acct2", "--menu-config",
                      str(cfg), "--dry-run"])
    finally:
        tc._yh_read_image = saved
    assert rc == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["schema"] == "ths_client/1"
    assert envelope["command"] == "switch-account"
    assert envelope["credentials_touched"] is False
    assert envelope["clicks"] == []
