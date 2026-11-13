# -*- coding: utf-8 -*-
"""ths_client 统一入口 CLI 测试：子命令路由 + 各模式 mock（零真机依赖）。

覆盖：argparse 六子命令路由与缺参/未知命令拒绝、login/export-fills/
poll-holdings/survey/status/daily 各模式 mock 行为、统一 JSON 报告风格
（schema ths_client/1 + credentials_touched:false）、export-fills 对
ths_fills_daily.py 看门狗 grep 的 stdout 兼容、四个旧脚本薄 wrapper 的
CLI 转发与 DEPRECATED 标注、凭证只读长度位的源码契约。
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ths_client as tc  # noqa: E402
import test_ths_xiadan_client as harness  # noqa: E402  复用已验收 FakeWindows 桩

FAST_FLAGS = ["--launch-wait", "0.02", "--direct-wait", "0.02",
              "--window-wait", "0.02", "--login-timeout", "0.3"]


def _envelope(capsys) -> dict:
    out = capsys.readouterr().out
    try:
        return json.loads(out)
    except ValueError:
        return json.loads(out.strip().splitlines()[-1])


# ── 路由 ────────────────────────────────────────────────────────────

def test_routes_all_subcommands():
    parser = tc.build_parser()
    assert parser.parse_args(["login"]).func is tc.cmd_login
    assert parser.parse_args(["export-fills", "--once"]).func is tc.cmd_export_fills
    args = parser.parse_args(["poll-holdings", "--once", "--now", "10:00",
                              "--no-stagger"])
    assert args.func is tc.cmd_poll_holdings and args.once and args.no_stagger
    assert parser.parse_args(["survey", "--walker", "raw"]).func is tc.cmd_survey
    assert parser.parse_args(["status"]).func is tc.cmd_status
    assert parser.parse_args(["daily", "--force"]).func is tc.cmd_daily


def test_unknown_or_missing_command_rejected():
    with pytest.raises(SystemExit):
        tc.build_parser().parse_args(["nope"])
    with pytest.raises(SystemExit):
        tc.build_parser().parse_args([])


def test_services_reused_not_copied():
    """统一入口必须复用 services 层（单一事实源），不复制其逻辑。"""
    assert tc.bridge.__name__ == "services.ths_account_bridge"
    assert tc.poll.__name__ == "services.ths_holdings_poll"
    assert tc.hotswitch.__name__ == "services.ths_hotswitch"


# ── login ───────────────────────────────────────────────────────────

def test_login_subcommand_end_to_end_fake_windows(monkeypatch, tmp_path, capsys):
    fake, pids = harness.FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    harness.install(monkeypatch, fake, pids)
    harness._main_window(fake)
    monkeypatch.setattr(tc, "_write_report",
                        lambda r, root=None, prefix="report": tmp_path / "x.json")

    rc = tc.main(["login", *FAST_FLAGS])

    assert rc == 0
    report = _envelope(capsys)
    assert report["schema"] == tc.SCHEMA == "ths_client/1"
    assert report["command"] == "login"
    assert report["status"] == "logged_in"
    assert report["login_state"] == "logged_in"
    assert report["credentials_touched"] is False


def test_login_need_manual_login_rc1(monkeypatch, tmp_path, capsys):
    fake, pids = harness.FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    harness.install(monkeypatch, fake, pids)
    harness._login_box(fake, password_len=0)  # 空密码：fail-closed
    monkeypatch.setattr(tc, "_write_report",
                        lambda *a, **k: tmp_path / "x.json")

    rc = tc.main(["login", *FAST_FLAGS])

    assert rc == 1
    report = _envelope(capsys)
    assert report["status"] == "need_manual_login"
    assert report["credentials_touched"] is False


# ── export-fills ────────────────────────────────────────────────────

def _capture_ok(_ev, **_kw):
    return {
        "today_lines": [
            "成交时间\t证券代码\t证券名称\t操作\t成交数量\t成交均价\t成交金额\t合同编号",
            "10:00:00\t002015\t协鑫能科\t证券买入\t100\t13.50\t1350.00\t1350\t12345",
        ],
        "history_rows": [],
        "history_header_seen": True,
        "window_state": {},
    }


def _patch_plan(monkeypatch, capture):
    monkeypatch.setattr(tc, "build_capture_plan",
                        lambda live, registry, account_filter=None: {
                            "acct1": {"panel": None, "identity_hint": None}})
    monkeypatch.setattr(tc, "_capture_ths_views", capture)
    # 不触真机窗口枚举（EnumWindows 在 git-bash 子进程下有死锁前科）
    monkeypatch.setattr(tc.bridge, "enumerate_trade_panels",
                        lambda include_minimized=False: [])


def test_export_fills_ok_envelope(tmp_path, capsys, monkeypatch):
    _patch_plan(monkeypatch, _capture_ok)

    rc = tc.main(["export-fills", "--date", "2026-11-10",
                  "--out-dir", str(tmp_path)])

    assert rc == 0
    env = _envelope(capsys)
    assert env["schema"] == "ths_client/1" and env["command"] == "export-fills"
    assert env["status"] == "ok" and env["credentials_touched"] is False
    assert env["fills_export"]["accounts"]["acct1"]["status"] == "ok"
    assert (tmp_path / "fills" / "20261110.json").is_file()
    payload = json.loads((tmp_path / "fills" / "20261110.json")
                         .read_text(encoding="utf-8"))
    assert payload["schema"] == "ths_fills_export/1"  # 文件产物协议不变
    assert payload["fills"] and payload["fills"][0]["code"] == "002015"


def test_export_fills_unavailable_watchdog_compat(tmp_path, capsys, monkeypatch):
    """rc=2 + stdout 含 '\"status\": \"unavailable\"'（ths_fills_daily.py 告警 grep 兼容）。"""
    def boom(_ev, **_kw):
        raise RuntimeError("THS_UNREACHABLE")

    _patch_plan(monkeypatch, boom)

    rc = tc.main(["export-fills", "--date", "2026-11-10",
                  "--out-dir", str(tmp_path)])

    assert rc == 2
    out = capsys.readouterr().out
    assert '"status": "unavailable"' in out
    marker = json.loads((tmp_path / "fills" / "20261110.unavailable.json")
                        .read_text(encoding="utf-8"))
    assert marker["schema"] == "ths_fills_export_unavailable/1"
    assert not (tmp_path / "fills" / "20261110.json").exists()


# ── poll-holdings ───────────────────────────────────────────────────

def test_poll_holdings_out_of_window(monkeypatch, capsys):
    monkeypatch.setattr(tc.poll, "should_poll", lambda now: (False, "weekend"))

    rc = tc.main(["poll-holdings", "--once", "--now", "10:00"])

    assert rc == 0
    env = _envelope(capsys)
    assert env["command"] == "poll-holdings"
    assert env["status"] == "out_of_window" and env["reason"] == "weekend"
    assert env["credentials_touched"] is False


def test_poll_holdings_once_round_empty_plan(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(tc.poll, "should_poll", lambda now: (True, "in_window"))
    monkeypatch.setattr(tc, "_build_plan", lambda account_filter: {})

    rc = tc.main(["poll-holdings", "--once", "--now", "10:00",
                  "--out-dir", str(tmp_path), "--no-stagger"])

    assert rc == 0
    env = _envelope(capsys)
    assert env["command"] == "poll-holdings" and env["status"] == "ok"
    assert env["credentials_touched"] is False
    assert env["poll"]["schema"] == "ths_holdings_poll_round/1"
    assert env["poll"]["accounts"] == {}


def test_poll_holdings_replay_requires_fills(monkeypatch, capsys):
    rc = tc.main(["poll-holdings", "--replay-morning"])
    assert rc == 2
    env = _envelope(capsys)
    assert env["status"] == "error" and "--fills" in env["reason"]


# ── survey ──────────────────────────────────────────────────────────

def test_survey_panel_absent_via_dispatcher(monkeypatch, tmp_path, capsys):
    out = tmp_path / "l2"
    monkeypatch.setattr(tc, "OUT_DIR", out)
    monkeypatch.setattr(tc, "PANEL_MAP", out / "panel_map.json")
    monkeypatch.setattr(tc, "JOURNAL", out / "journal.md")
    monkeypatch.setattr(tc.bridge, "enumerate_trade_panels",
                        lambda include_minimized=False: [])

    assert tc.main(["survey"]) == 2

    env = _envelope(capsys)
    assert env["command"] == "survey" and env["status"] == "panel_absent"
    assert env["credentials_touched"] is False
    data = json.loads((out / "panel_map.json").read_text(encoding="utf-8"))
    assert data["schema"] == tc.PANEL_MAP_SCHEMA == "l2_panel_map/1"
    assert data["status"] == "panel_absent"


def test_survey_self_audit_still_guards_survey_zone(tmp_path, monkeypatch):
    """自审计仍能抓到勘察段外泄的输入原语（合并文件不放松禁令）。"""
    src = Path(tc.__file__).read_text(encoding="utf-8")
    bad = tmp_path / "ths_client_bad.py"
    bad.write_text(src + "\nx." + "invoke" + "(1)\n", encoding="utf-8")
    monkeypatch.setattr(tc, "__file__", str(bad))
    with pytest.raises(RuntimeError, match="self-audit FAILED"):
        tc._self_audit()


# ── status ──────────────────────────────────────────────────────────

def test_status_aggregates_readonly(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(tc, "_pids_of", lambda name: {
        "hexin.exe": [11], "xiadan.exe": [22]}.get(name, []))
    monkeypatch.setattr(tc.bridge, "enumerate_trade_panels",
                        lambda include_minimized=False: [
                            {"pid": 22, "process": "xiadan.exe",
                             "title": "网上股票交易系统5.0", "hwnd": "0x1"}])
    monkeypatch.setattr(tc, "_poll_state",
                        lambda: ("main", [], {"hwnd": 1,
                                              "title": "网上股票交易系统5.0"}))
    monkeypatch.setattr(tc, "_tracker_root", lambda out_dir=None: tmp_path)
    monkeypatch.setattr(tc, "_runtime_dir",
                        lambda name: tmp_path / name)
    panels_json = tmp_path / "ths_panels.json"
    panels_json.write_text(json.dumps({
        "schema": "ths_panels/1",
        "rules": [{"account": "acct1", "identity_hint": "曾", "hotkey": "1"}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(tc.bridge, "load_panel_registry",
                        lambda: tc.bridge.PanelRegistry(panels_json))
    fills = tmp_path / "fills"
    fills.mkdir(parents=True)
    (fills / "20261113.json").write_text(json.dumps({
        "schema": "ths_fills_export/1", "status": "ok",
        "trade_date": "2026-11-13", "captured_at": "2026-11-13 15:05:01",
        "fills": [{"code": "002015"}]}), encoding="utf-8")
    (tmp_path / "broker_state_sync_alerts.jsonl").write_text(
        json.dumps({"ts": "2026-11-13 15:15", "reason": "fills_missing"}) + "\n",
        encoding="utf-8")

    rc = tc.main(["status"])

    assert rc == 0
    env = _envelope(capsys)
    assert env["schema"] == "ths_client/1" and env["command"] == "status"
    assert env["credentials_touched"] is False
    detail = env["detail"]
    assert detail["processes"]["hexin.exe"] == [11]
    assert detail["processes"]["xiadan.exe"] == [22]
    assert detail["login_state"]["state"] == "main"
    assert detail["panels"][0]["pid"] == 22
    acct1 = detail["accounts"]["acct1"]
    assert acct1["identity_hint"] == "曾"
    assert acct1["panel_bound"] is True
    assert acct1["latest_fills"]["status"] == "ok"
    assert acct1["latest_fills"]["fills_total"] == 1
    assert detail["recent_alerts"]["broker_state_sync"][0]["reason"] \
        == "fills_missing"


# ── daily ───────────────────────────────────────────────────────────

def test_daily_login_blocked_skips_export(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(tc, "run", lambda **_kw: {
        "schema": tc.SCHEMA, "status": "need_manual_login",
        "login_state": "need_manual_login"})
    monkeypatch.setattr(tc, "_write_report",
                        lambda r, root=None, prefix="report": tmp_path / "x.json")

    def _fail_export(**_kw):  # pragma: no cover - login 未就绪绝不导出
        pytest.fail("login 未就绪绝不导出")

    monkeypatch.setattr(tc, "_execute_export_fills", _fail_export)

    rc = tc.main(["daily", "--out-dir", str(tmp_path)])

    assert rc == 1
    env = _envelope(capsys)
    assert env["status"] == "login_need_manual_login"
    assert env["export_skipped"] == "not_logged_in"
    assert env["credentials_touched"] is False


def test_daily_success_chain(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(tc, "run", lambda **_kw: {
        "schema": tc.SCHEMA, "status": "logged_in",
        "login_state": "logged_in", "xiadan_pids": [200]})
    written: dict[str, dict] = {}

    def fake_write(report, root=None, prefix="report"):
        written[prefix] = dict(report)
        return tmp_path / f"{prefix}.json"

    monkeypatch.setattr(tc, "_write_report", fake_write)
    monkeypatch.setattr(tc, "_execute_export_fills", lambda **kw: {
        "rc": 0, "trade_date": kw["for_date"].isoformat(),
        "accounts": {"acct1": {"status": "ok", "fills_total": 2,
                               "fills_file": str(tmp_path / "fills" / "x.json")}}})

    rc = tc.main(["daily", "--out-dir", str(tmp_path), "--force"])

    assert rc == 0
    env = _envelope(capsys)
    assert env["status"] == "ok"
    assert env["summary"]["acct1"]["fills_total"] == 2
    assert env["steps"] == [{"step": "login", "status": "logged_in"},
                            {"step": "export_fills", "rc": 0}]
    assert written["daily"]["schema"] == "ths_client/1"
    assert written["daily"]["credentials_touched"] is False


# ── 旧脚本薄 wrapper ────────────────────────────────────────────────

WRAPPERS = [
    ("ths_xiadan_client", ["login"], ["--dry-run"]),
    ("ths_fills_export", ["export-fills"], ["--once"]),
    ("ths_holdings_poll", ["poll-holdings"], ["--once"]),
    ("l2_panel_survey", ["survey"], ["--account", "acct2"]),
]


@pytest.mark.parametrize("name,sub,extra", WRAPPERS)
def test_legacy_wrappers_forward_and_marked_deprecated(monkeypatch, name, sub,
                                                       extra):
    mod = importlib.import_module(name)
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert "DEPRECATED" in src and "ths_client" in src  # 头部指向新入口
    calls: dict[str, list] = {}

    def fake_main(argv):
        calls["argv"] = list(argv)
        return 0

    monkeypatch.setattr(tc, "main", fake_main)
    assert mod.main(extra) == 0
    assert calls["argv"] == [*sub, *extra]


# ── 源码契约（凭证只读长度位，收拢后全文件仍成立）──────────────────

def test_source_contract_credentials_and_password_readonly():
    source = Path(tc.__file__).read_text(encoding="utf-8")
    assert 'credentials_touched": False' in source
    assert "WM_GETTEXTLENGTH" in source
    # 密码框只允许长度位查询；不得出现取文本/写文本消息
    assert source.count("WM_GETTEXT") == source.count("WM_GETTEXTLENGTH")
    assert "WM_SETTEXT" not in source
    assert "EM_SETSEL" not in source
    assert "GetDlgItemText" not in source
