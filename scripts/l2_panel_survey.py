# -*- coding: utf-8 -*-
"""R50 Phase1 L2 委托面板 UIA 结构测绘（零风险勘察，只读铁律）。

任务书 task_briefs/20261111_l2_order_automation_r50.md 第 1/2 项：
窗口锁内打开 xiadan 面板 → UIA 枚举全部控件树（代码框/价格框/数量框/
买卖按钮/撤单/查询Tab/确认弹窗的 AutomationId+Name+坐标+可读写性）
→ runtime/l2_r50/panel_map.json + 截图 + 提交类按钮清单（今晚绝不 Invoke）。

铁律（Phase 1）：
  - 零输入零 Invoke 零点击零交易：本脚本对面板只做 GetCurrent*/PrintWindow
    离屏渲染读取；源码级自审计（_self_audit）禁止任何键盘/鼠标/Invoke
    输入原语出现在本文件；
  - 任何意外弹窗只记录（标题/类名/文本），绝不确认绝不关闭；
  - 前台有人 → skipped_foreground 让位；窗口锁 ths_window.lock 全程持有；
  - 身份 fail-closed：顶栏 OCR「券商-姓名」必须命中绑定账号（ths_panels.json），
    未知身份零触碰；--allow-hotswitch 时才可用 Alt+N 切到目标账号（走
    services/ths_hotswitch 受控路径，OCR 自证）；
  - 勘察毕：面板若原为最小化 → 恢复最小化；全程 journal。

用法（host F:/python/python3.10）：
  python scripts/l2_panel_survey.py                    # 测绘当前面板（须已登录目标账号）
  python scripts/l2_panel_survey.py --allow-hotswitch  # 若在王*美→Alt+1 切曾*再测绘
  python scripts/l2_panel_survey.py --walker raw       # RawView 全量树（默认 ControlView）

退出码：0=测绘完成  2=panel_absent  3=skipped_foreground  4=identity_failclosed
        5=lock_timeout  6=ambiguous_panels
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import ths_account_bridge as bridge  # noqa: E402
from services import ths_holdings_poll as poll  # noqa: E402
from services import ths_hotswitch as hotswitch  # noqa: E402

OUT_DIR = Path(r"F:\Aistock\data\data\runtime\l2_r50")
PANEL_MAP = OUT_DIR / "panel_map.json"
JOURNAL = OUT_DIR / "journal_r50_phase1.md"
PANELS_JSON = Path(r"F:\Aistock\data\data\runtime\gen3_state_alpha\ths_panels.json")
PANEL_MAP_SCHEMA = "l2_panel_map/1"

# 提交/撤单类按钮名令牌（命中即入 submit_buttons，Phase1 绝不 Invoke）
SUBMIT_TOKENS = ("买入", "卖出", "买入委托", "卖出委托", "委托下单", "下单",
                 "提交", "撤单", "撤单", "全撤", "一键", "清空")
MAX_NODES = 5000
MAX_DEPTH = 24


def _ts() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def journal(text: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as fh:
        fh.write(f"- `{_ts()}` {text}\n")


def _write_map(payload: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PANEL_MAP.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(PANEL_MAP)


# ── 源码级自审计：本文件不得含任何输入/激活原语 ──────────────────
# 令牌用相邻字面量拼接，避免审计串出现在本文件源码中造成自命中。
_FORBIDDEN_TOKENS = (
    ".invoke" "(", "invoke_pattern" ".Invoke", ".set_" "value(", ".set_" "focus(",
    "click_" "input", ".click" "(", "type_" "keys", "send_" "keys",
    "Set" "Value", "Set" "Focus", "Mouse" "Click", "keybd_" "event",
    "Send" "Input", "Post" "MessageW", "Send" "MessageW",
)


def _self_audit() -> None:
    """源码扫描：本文件一旦出现任何输入/激活原语立即拒绝运行。"""
    src = Path(__file__).read_text(encoding="utf-8")
    hits = [t for t in _FORBIDDEN_TOKENS if t in src]
    if hits:   # pragma: no cover - 防未来改动引入危险调用
        raise RuntimeError(f"L2 survey self-audit FAILED: {hits}")


# ── UIA 只读枚举（comtypes 原生，零交互：只 GetCurrent*） ─────────

class UiaReader:
    """comtypes UIA 封装（只读）。属性经 GetCurrentPropertyValue/Current* 读取。"""

    def __init__(self) -> None:
        from comtypes.client import CreateObject, GetModule
        GetModule("UIAutomationCore.dll")
        import comtypes.gen.UIAutomationClient as UIAC  # noqa: F401
        self.UIAC = UIAC
        self.uia = CreateObject(UIAC.CUIAutomation,
                                interface=UIAC.IUIAutomation)

    def element_from_handle(self, hwnd: int):
        return self.uia.ElementFromHandle(hwnd)

    def walker(self, raw: bool):
        return self.uia.RawViewWalker if raw else self.uia.ControlViewWalker

    @staticmethod
    def _safe_call(fn, *args):
        """易失控件（tooltip/动画/已销毁）上 walker 调用会 E_POINTER——
        读失败按"无"处理（只读勘察不重试不打扰，如实记录可读部分）。"""
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001 - COMError E_POINTER 等
            return None

    def _prop(self, elem, name: str, default: Any = None) -> Any:
        try:
            return getattr(elem, name)
        except Exception:  # noqa: BLE001 - 单属性失败不阻断整树
            return default

    def read_node(self, elem) -> dict:
        UIAC = self.UIAC
        rect = self._prop(elem, "CurrentBoundingRectangle")
        l = t = r = b = None
        if rect is not None:
            l = getattr(rect, "left", getattr(rect, "l", None))
            t = getattr(rect, "top", getattr(rect, "t", None))
            r = getattr(rect, "right", getattr(rect, "r", None))
            b = getattr(rect, "bottom", getattr(rect, "b", None))
        node: dict[str, Any] = {
            "automation_id": self._prop(elem, "CurrentAutomationId", "") or "",
            "name": (self._prop(elem, "CurrentName", "") or "")[:80],
            "class_name": self._prop(elem, "CurrentClassName", "") or "",
            "control_type": int(self._prop(elem, "CurrentControlType", 0) or 0),
            "localized_type": self._prop(elem, "CurrentLocalizedControlType", "") or "",
            "rect": [int(l) if l is not None else None,
                     int(t) if t is not None else None,
                     int(r) if r is not None else None,
                     int(b) if b is not None else None],
            "enabled": bool(self._prop(elem, "CurrentIsEnabled", False)),
            "offscreen": bool(self._prop(elem, "CurrentIsOffscreen", True)),
            "keyboard_focusable": bool(self._prop(elem, "CurrentIsKeyboardFocusable", False)),
            "runtime_id": [int(x) for x in
                           (self._prop(elem, "CurrentRuntimeId") or [])][:6],
            "patterns": {},
        }
        is_password = bool(self._prop(elem, "CurrentIsPassword", False))
        node["is_password"] = is_password

        def avail(pid) -> bool:
            try:
                return bool(elem.GetCurrentPropertyValue(pid))
            except Exception:  # noqa: BLE001
                return False

        # 可读写性标注（ValuePattern 写入能力=available 且非只读；今晚绝不写）
        try:
            if avail(UIAC.UIA_IsValuePatternAvailablePropertyId):
                unk = elem.GetCurrentPattern(UIAC.UIA_ValuePatternId)
                if unk:
                    vp = unk.QueryInterface(UIAC.IUIAutomationValuePattern)
                    node["patterns"]["value"] = {
                        "read_only": bool(vp.CurrentIsReadOnly),
                        "writable_phase2": not bool(vp.CurrentIsReadOnly),
                        "value": "" if is_password else str(vp.CurrentValue)[:60],
                    }
        except Exception as exc:  # noqa: BLE001
            node["patterns"]["value_error"] = f"{type(exc).__name__}"
        if avail(UIAC.UIA_IsInvokePatternAvailablePropertyId):
            node["patterns"]["invoke"] = {"available": True,
                                          "invoke_forbidden_phase1": True}
        if avail(UIAC.UIA_IsRangeValuePatternAvailablePropertyId):
            node["patterns"]["range_value"] = {"available": True}
        if avail(UIAC.UIA_IsTogglePatternAvailablePropertyId):
            try:
                unk = elem.GetCurrentPattern(UIAC.UIA_TogglePatternId)
                if unk:
                    tp = unk.QueryInterface(UIAC.IUIAutomationTogglePattern)
                    node["patterns"]["toggle"] = {
                        "state": str(tp.CurrentToggleState)}
            except Exception:  # noqa: BLE001
                node["patterns"]["toggle"] = {"available": True}
        if avail(UIAC.UIA_IsSelectionItemPatternAvailablePropertyId):
            try:
                unk = elem.GetCurrentPattern(UIAC.UIA_SelectionItemPatternId)
                if unk:
                    sp = unk.QueryInterface(UIAC.IUIAutomationSelectionItemPattern)
                    node["patterns"]["selection_item"] = {
                        "selected": bool(sp.CurrentIsSelected)}
            except Exception:  # noqa: BLE001
                node["patterns"]["selection_item"] = {"available": True}
        return node

    def walk_tree(self, root_elem, raw: bool = False,
                  max_nodes: int = MAX_NODES, max_depth: int = MAX_DEPTH,
                  progress_cb=None) -> tuple[dict, int]:
        """迭代式深度优先（ControlView 默认），返回 (根节点树, 总节点数)。"""
        walker = self.walker(raw)
        total = 0
        truncated = False

        def build(elem, depth: int) -> dict:
            nonlocal total, truncated
            total += 1
            if total > max_nodes:
                truncated = True
                return {"_truncated": True}
            node = self.read_node(elem)
            if depth < max_depth:
                children = []
                child = self._safe_call(walker.GetFirstChildElement, elem)
                guard = 0
                while child is not None and guard < max_nodes:
                    guard += 1
                    children.append(build(child, depth + 1))
                    nxt = self._safe_call(walker.GetNextSiblingElement, child)
                    child = nxt
                node["children"] = children
            else:
                node["children"] = []
                node["_depth_truncated"] = True
            if progress_cb and total % 200 == 0:
                progress_cb(total)
            return node

        tree = build(root_elem, 0)
        return tree, total


# ── 弹窗记录（同 pid 其他可见顶层窗口，只记录零处理） ─────────────

def enumerate_sibling_dialogs(pid: int, main_hwnd: int) -> list[dict]:
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    out: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd: int, _l: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or int(hwnd) == int(main_hwnd):
            return True
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid:
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, buf, n + 2)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        out.append({"hwnd": hex(hwnd), "title": buf.value,
                    "class": cls.value,
                    "rect": [rect.left, rect.top, rect.right, rect.bottom],
                    "action": "recorded_only_no_confirm"})
        return True

    user32.EnumWindows(_cb, 0)
    return out


def classify_submit_buttons(nodes_iter) -> list[dict]:
    """按名称令牌识别提交/撤单类按钮——今晚绝不 Invoke。"""
    submits = []
    for node, path in nodes_iter:
        name = str(node.get("name") or "")
        if not any(tok in name for tok in SUBMIT_TOKENS):
            continue
        submits.append({
            "path": path,
            "automation_id": node.get("automation_id"),
            "name": name,
            "class_name": node.get("class_name"),
            "control_type": node.get("localized_type"),
            "rect": node.get("rect"),
            "invoke_available": bool((node.get("patterns") or {}).get("invoke")),
            "enabled": node.get("enabled"),
            "invoke_forbidden_phase1": True,
            "note": "Phase1 勘察绝不 Invoke；Phase2+ 须 Owner 批准",
        })
    return submits


def flatten(tree: dict):
    """深度优先展开 (node, path)，path 形如 '0/3/1'（测绘定位用）。"""
    def rec(node: dict, path: str):
        yield node, path
        for i, ch in enumerate(node.get("children") or []):
            if isinstance(ch, dict) and not ch.get("_truncated"):
                yield from rec(ch, f"{path}/{i}")
    yield from rec(tree, "")


def load_panel_rules() -> list[dict]:
    try:
        data = json.loads(PANELS_JSON.read_text(encoding="utf-8"))
        return list(data.get("rules") or [])
    except Exception:  # noqa: BLE001
        return []


def _show_window(hwnd: int, cmd: int) -> None:
    ctypes.windll.user32.ShowWindow(hwnd, cmd)


def _is_iconic(hwnd: int) -> bool:
    return bool(ctypes.windll.user32.IsIconic(hwnd))


class _WinHandle:
    """hotswitch/poll 需要的 window 对象最小面（仅 handle）。"""

    def __init__(self, hwnd: int) -> None:
        self.handle = hwnd


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="R50 Phase1 L2 面板 UIA 零风险勘察")
    ap.add_argument("--account", default="acct1",
                    help="目标账号（默认 acct1 曾*）")
    ap.add_argument("--allow-hotswitch", action="store_true",
                    help="面板在另一已知账号时允许 Alt+N 热切换到目标（默认零按键）")
    ap.add_argument("--walker", choices=["control", "raw"], default="control")
    ap.add_argument("--max-nodes", type=int, default=MAX_NODES)
    args = ap.parse_args(argv)

    _self_audit()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    journal(f"survey start account={args.account} walker={args.walker} "
            f"hotswitch={'on' if args.allow_hotswitch else 'off'} "
            f"(iron_law: 零输入零Invoke零点击零交易)")

    # 1) 面板枚举（include_minimized=True：R49b 最小化面板可解析）
    panels = bridge.enumerate_trade_panels(include_minimized=True)
    journal(f"panels={len(panels)} " +
            "; ".join(f"{p['process']}:{p['title'][:24]}@{p['hwnd']}"
                      for p in panels))
    if not panels:
        payload = {"schema": PANEL_MAP_SCHEMA, "status": "panel_absent",
                   "surveyed_at": _ts(),
                   "note": "全系统无交易面板（EnumWindows 零命中）。需 Owner 登录"
                           "交易客户端后重跑本脚本。fail-closed：绝不自启客户端。"}
        _write_map(payload)
        journal("panel_absent → fail-closed 退出（rc=2），绝不自启交易客户端")
        return 2
    if len(panels) > 1:
        payload = {"schema": PANEL_MAP_SCHEMA, "status": "ambiguous_panels",
                   "surveyed_at": _ts(), "panels": panels,
                   "note": "多面板并存，测绘对象不唯一，fail-closed 待 PM 裁决"}
        _write_map(payload)
        journal(f"ambiguous_panels={len(panels)} → rc=6 fail-closed")
        return 6

    panel = panels[0]
    hwnd = int(str(panel["hwnd"]), 16)
    rules = load_panel_rules()
    rule_by_acct = {str(r.get("account")): r for r in rules}
    target_rule = rule_by_acct.get(args.account) or {}
    target_hint = str(target_rule.get("identity_hint") or "")
    known_hints = [(str(r.get("identity_hint")), str(r.get("account")))
                   for r in rules if r.get("identity_hint")]

    # 2) 窗口锁内完成全部面板侧操作
    try:
        lock_cm = bridge._ths_window_operation_lock(
            timeout_seconds=180, lock_name=bridge.HOTSWITCH_LOCK_NAME)
    except Exception as exc:  # noqa: BLE001
        journal(f"lock_setup_error {exc} → rc=5")
        return 5
    with lock_cm:
        if poll._is_foreground(hwnd):
            payload = {"schema": PANEL_MAP_SCHEMA, "status": "skipped_foreground",
                       "surveyed_at": _ts(), "panel": panel,
                       "note": "面板在前台（疑有人使用）→ 让位，绝不抢焦点"}
            _write_map(payload)
            journal("skipped_foreground → rc=3（零触碰）")
            return 3

        was_minimized = _is_iconic(hwnd)
        if was_minimized:
            _show_window(hwnd, 9)  # SW_RESTORE（PrintWindow 对 iconic 窗空白）
            time.sleep(0.5)
            journal("panel was minimized → restored for survey")

        # 3) 身份 fail-closed（顶栏 OCR 自证）
        try:
            label, ident_png = hotswitch.read_window_identity(
                _WinHandle(hwnd),
                save_png=OUT_DIR / "identity_check.png")
        except Exception as exc:  # noqa: BLE001
            label, ident_png = "", None
            journal(f"identity_read_error {type(exc).__name__}: {exc}")
        journal(f"identity_label={label!r} target_hint={target_hint!r}")
        status_identity = None
        if not target_hint:
            status_identity = "no_target_rule"
        elif target_hint not in label:
            hit = hotswitch._match_known(label, known_hints)
            if hit is None:
                status_identity = "identity_mismatch"
            elif args.allow_hotswitch and rule_by_acct.get(hit, {}).get("hotkey"):
                cfg = {"target_hint": target_hint,
                       "target_hotkey": target_rule.get("hotkey"),
                       "known_hints": known_hints}
                try:
                    # 生产窗口契约（fills_export/poll 同源）：pywinauto uia 包装
                    # 窗满足 hotswitch 服务的完整调用面（焦点/最小化/恢复）；
                    # 按键只出自受控 hotswitch 模块，本文件自审计仍零输入原语。
                    win = bridge._find_window_for_panel(panel)
                    res = hotswitch.ensure_target_account(win, cfg)
                except Exception as exc:  # noqa: BLE001 - 窗口消失/连接失败
                    journal(f"hotswitch_window_error {type(exc).__name__}: {exc}")
                    res = {"status": "switch_failed",
                           "note": "窗口连接/热切换异常，fail-closed 零采集"}
                if res is not None:
                    status_identity = res.get("status", "switch_failed")
                else:
                    label2, _ = hotswitch.read_window_identity(
                        _WinHandle(hwnd), save_png=OUT_DIR / "identity_after_switch.png")
                    journal(f"hotswitch ok → identity={label2!r}")
                    label = label2
            else:
                status_identity = "not_target_account_no_hotswitch"

        if status_identity is not None:
            if was_minimized:
                _show_window(hwnd, 6)  # SW_MINIMIZE 复原
                journal("restored to minimized (identity fail-closed)")
            payload = {"schema": PANEL_MAP_SCHEMA, "status": status_identity,
                       "surveyed_at": _ts(), "panel": panel,
                       "identity_label": label, "target_hint": target_hint,
                       "note": "身份 fail-closed：零 UIA 采集零触碰"}
            _write_map(payload)
            journal(f"{status_identity} → rc=4（零触碰，身份标签 {label!r}）")
            return 4

        # 4) PrintWindow 截图证据
        shot = OUT_DIR / f"panel_full_{datetime.now().strftime('%H%M%S')}.png"
        try:
            poll.print_window_image(hwnd).save(shot)
            journal(f"screenshot {shot.name}")
        except Exception as exc:  # noqa: BLE001
            shot = None
            journal(f"screenshot_error {type(exc).__name__}: {exc}")

        # 5) UIA 全树测绘（零交互：只 GetCurrent*）
        try:
            reader = UiaReader()
            root = reader.element_from_handle(hwnd)
            tree, total = reader.walk_tree(
                root, raw=(args.walker == "raw"), max_nodes=args.max_nodes,
                progress_cb=lambda n: journal(f"walk progress {n} nodes"))
        except Exception as exc:  # noqa: BLE001
            if was_minimized:
                _show_window(hwnd, 6)
            payload = {"schema": PANEL_MAP_SCHEMA, "status": "uia_walk_error",
                       "surveyed_at": _ts(), "panel": panel,
                       "error": f"{type(exc).__name__}: {exc}"}
            _write_map(payload)
            journal(f"uia_walk_error {type(exc).__name__}: {exc} → rc=5")
            return 5

        # 6) 提交类按钮清单 + 弹窗记录
        submits = classify_submit_buttons(flatten(tree))
        dialogs = enumerate_sibling_dialogs(int(panel["pid"]), hwnd)
        if dialogs:
            journal(f"sibling_dialogs recorded (no confirm): "
                    + "; ".join(d["title"][:20] for d in dialogs))

        # 7) 复原最小化
        restored_min = False
        if was_minimized:
            _show_window(hwnd, 6)  # SW_MINIMIZE
            restored_min = True
            time.sleep(0.2)
            journal("restored to minimized after survey")

        payload = {
            "schema": PANEL_MAP_SCHEMA, "status": "ok",
            "surveyed_at": _ts(),
            "panel": panel,
            "identity_label": label, "account": args.account,
            "walker": args.walker, "node_count": total,
            "was_minimized": was_minimized, "restored_to_minimized": restored_min,
            "iron_law": {"zero_input": True, "zero_invoke": True,
                         "zero_click": True, "zero_trade": True,
                         "submit_buttons_never_invoke_phase1": True},
            "tree": tree,
            "submit_buttons": submits,
            "sibling_dialogs": dialogs,
            "screenshots": [shot.name] if shot else [],
            "next_phase_note": "Phase2 填单演练须 Owner 在场另派任务书；"
                               "本测绘绝不构成任何填单/提交授权",
        }
        _write_map(payload)
        journal(f"survey ok nodes={total} submits={len(submits)} "
                f"dialogs={len(dialogs)} → panel_map.json")
        return 0


if __name__ == "__main__":
    sys.exit(main())
