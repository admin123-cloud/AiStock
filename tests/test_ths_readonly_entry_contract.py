# -*- coding: utf-8 -*-
"""真实 read-account 入口接线的隔离契约；本切片只编写，待 PM 批准 RED。

边界：仅 stdlib；仅读取 SOURCE 文本，不 import 项目，不执行模块初始化、
build_parser、真实 adapter/collector/verdict/锁。A10/B12 仍是独立验收，
这里不复制、更改或替代其规则；尤其不把 fake 的完整裁决当作真实 A/B GREEN。
所有账户、窗口、持仓、成交和日期均为合成数据。禁止 GUI/网络/DB/runtime。

被测物只允许 AST 摘取 run_read_account、cmd_read_account，以及可选的
新纯函数 _yh_readonly_report。后者只能从真实 run_read_account 直接调用，
不能作为脱离入口的独立被测替身；不允许继续递归扩张 helper allowlist。
所有外部依赖均注入下面的内存 fake，旧 collector/锁/报告不提供 fallback。
生产 _yh_collect_with_adapter 不摘取：这里的同名 spy 是调用边界的见证人，
回放固定协议动作并返回预置证据，不重新实现 B 的身份/守卫裁决算法。

可落地的最小新增 seam（生产实现由下一切片负责，本文件不提供生产 stub）：
* _yh_readonly_lock(*, timeout_seconds) -> context manager；__enter__ 返回严格
  bool acquired；False 为锁忙；__exit__ 不吞异常。生产锁不触 runtime 文件，
  必须与相应 GUI 自动化共享互斥域；进程局部伪锁不算真实互斥的验收证据。
* _YhReadonlyAdapter(*, happ_exe)：无自动启动/F12/键鼠/抢焦点/切号/文件截图/
  远程视觉。六方法沿用 B：guard、identity、select_query、read_funds、
  read_holdings、read_fills。构造本身必须在已取得锁之后，锁持有至 collector
  返回（包括其最后独立 identity/guard）及异常退出。真实平台实现另行审核。
* _yh_collect_with_adapter(adapter, expected_account, *, with_fills=False)：
  已存在的生产采集器；run 的默认路径必须直接调用，不能仅定义却不使用。
* 可选 _yh_readonly_report(evidence, *, with_fills=False) -> dict：纯报告组装，
  调用现有 yh_single_snapshot/yh_fills_verdict；不采集、不获锁、不落盘。
  它的完整/partial/拒绝输出只能通过真实 run/cmd 路由接受本测试。
* account=None：锁内 guard/identity/guard 取得非空 account+fingerprint 后，
  将此 account 作为 collector 的 expected_account；无法确认时 fail-closed。
  allow_switch、旧等待参数等 API 形状保留，allow_switch 绝不触发切号。
* report_root=None 默认仅返回 JSON 可序列化 dict；cmd 仅打印 JSON 并返回 rc。
  本切片不授权显式持久化。安全拒绝 rc4，异常 rc 非零，撤销全部数据及覆盖；
  完整数据 ok/rc0；已归属但字段/查询日期不足 partial/rc0，保留已知原始行。

两项 structural 测试明确不是行为验收：一项检查真实 CLI 绑定/default 调用
及 adapter 的具体协议表面，一项检查模块初始化 services/utils/runtime 引用。
它们不运行初始化，也不能证明 Win32/UIA 或真实锁的安全性。动态导入/反射
变体不在这个有限结构契约的证明范围；新实现扩大依赖必须重新做安全审批。

建议批准后唯一 runner（不 discovery、不 pytest、不项目 import）：
F:/python/python3.10/python.exe -I -S -B F:/Aistock/app/tests/test_ths_readonly_entry_contract.py -v
执行前另核批准的 SOURCE/本文件 SHA 和 HEAD；本文件不在模块加载时触发测试。
"""

import ast
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "ths_client.py"
BASELINE_HEAD = "132fe6683ba62205964eb7e73b9eb829cfa62e89"
BASELINE_SOURCE_SHA256 = "a965392fb5d81df320ab8685dfdc428ebefc5d264cb2ceb834976445feb48e29"
EXTRACTED = ("run_read_account", "cmd_read_account", "_yh_readonly_report")
ACCOUNT = "SYNTHETIC_ACCOUNT_A"
OTHER_ACCOUNT = "SYNTHETIC_ACCOUNT_B"
EXE = "SYNTHETIC_HAPP_EXE_NOT_A_PATH"
PAYLOAD_MARKER = "SYNTHETIC_PAYLOAD_MUST_BE_REVOKED"
FAULT_TEXT = "SYNTHETIC_PRIVATE_EXCEPTION_MUST_NOT_ESCAPE"
PROTOCOL = {"guard", "identity", "select_query", "read_funds", "read_holdings", "read_fills"}
FORBIDDEN = {
    "_yh_collect_single_snapshot", "run_read_account_switchable", "run_switch_account",
    "_yh_acquire_gui_lock", "_yh_release_gui_lock", "_runtime_dir", "runtime_path",
    "_tracker_root", "_write_report", "yh_account_state_snapshot",
    "_yh_ensure_main", "_yh_ensure_panel", "_yh_capture_window", "_yh_read_image",
    "_yh_click_window", "_yh_uia_select_tree_item", "_yh_read_funds",
    "bridge", "poll", "hotswitch", "services", "utils", "USER32", "KERNEL32",
    "requests", "urllib", "socket", "subprocess", "ctypes", "open", "eval", "exec",
    "compile", "__import__", "globals", "locals", "vars", "setattr", "delattr",
    "keybd_event", "mouse_event", "SendInput", "SetForegroundWindow", "set_focus",
    "click_input", "type_keys", "capture_as_image", "save", "write_text", "write_bytes",
    "mkdir", "unlink", "Popen", "urlopen", "remote_vision", "OUT_DIR", "PANEL_MAP",
    "JOURNAL",
}
DATA_ATTRS = {
    "get", "items", "keys", "values", "update", "setdefault", "copy", "clear",
    "append", "extend", "pop", "startswith", "endswith", "strip", "join", "lower",
}
ARGS_ATTRS = {
    "account", "happ_exe", "no_switch", "menu_config", "menu_wait_sec", "settle_sec",
    "f12_wait", "main_wait", "fills", "report_root", "lock_timeout",
}
SAFE_BUILTINS = {
    "str": str, "int": int, "float": float, "bool": bool, "len": len,
    "list": list, "dict": dict, "tuple": tuple, "set": set, "frozenset": frozenset,
    "isinstance": isinstance, "all": all, "any": any, "sorted": sorted,
    "enumerate": enumerate, "range": range, "zip": zip, "min": min, "max": max,
    "sum": sum, "Exception": Exception, "RuntimeError": RuntimeError,
    "ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError,
}


def _source_tree():
    # 唯一真实业务文件读取；不扫描目录、不读取 runtime 或任何配置。
    return ast.parse(SOURCE.read_text(encoding="utf-8-sig"), filename=str(SOURCE))


def _one(tree, name, kind=ast.FunctionDef):
    found = [n for n in tree.body if isinstance(n, kind) and n.name == name]
    assert len(found) == 1, "缺失/重复入口契约定义：" + name
    return found[0]


def _direct_calls(node):
    return {n.func.id for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def _references(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def _prepare_function(original, globals_allowed):
    """受限 AST 装载，不执行原模块；这是有限能力隔离，不宣称通用沙箱。"""
    fn = copy.deepcopy(original)
    for n in ast.walk(fn):
        if isinstance(n, (ast.FunctionDef, ast.Lambda)):
            assert not getattr(n, "decorator_list", []), "禁止执行 decorator"
            assert not getattr(n, "type_params", []), "禁止执行 type parameter"
            if isinstance(n, ast.FunctionDef):
                n.returns = None
            for d in n.args.defaults + [v for v in n.args.kw_defaults if v is not None]:
                if isinstance(d, ast.Name) and d.id == "YUANHANG_EXE_DEFAULT":
                    continue
                try:
                    ast.literal_eval(d)
                except (ValueError, TypeError):
                    raise AssertionError("默认参数仅允许字面量/合成 exe 常量") from None
        if isinstance(n, ast.arg):
            n.annotation = None
        if isinstance(n, ast.AnnAssign):
            n.annotation = ast.Constant(value=None)
    nodes = list(ast.walk(fn))
    local = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    local |= {n.arg for n in nodes if isinstance(n, ast.arg)}
    local |= {n.name for n in nodes if isinstance(n, ast.FunctionDef)}
    local |= {n.name for n in nodes if isinstance(n, ast.ExceptHandler) and n.name}
    banned_nodes = (ast.Import, ast.ImportFrom, ast.ClassDef, ast.Global, ast.Nonlocal,
                    ast.AsyncFunctionDef, ast.AsyncWith, ast.Await, ast.Yield, ast.YieldFrom)
    for n in nodes:
        assert not isinstance(n, banned_nodes), "摘取入口含未审作用域/导入"
        if isinstance(n, ast.Name):
            assert not n.id.startswith("__"), "禁止反射名称"
            assert n.id not in FORBIDDEN, "旧默认链/副作用依赖禁止：" + n.id
            assert n.id in local or n.id in globals_allowed, "未审依赖：" + n.id
        if isinstance(n, ast.Attribute):
            assert isinstance(n.ctx, ast.Load), "禁止属性写入"
            assert n.attr in DATA_ATTRS | PROTOCOL | ARGS_ATTRS | {
                "now", "isoformat", "utc", "dumps"}, "未审属性：" + n.attr
        if isinstance(n, ast.Call):
            assert isinstance(n.func, (ast.Name, ast.Attribute)), "禁止动态调用表达式"
            if isinstance(n.func, ast.Name) and n.func.id == "getattr":
                assert (len(n.args) in (2, 3) and isinstance(n.args[0], ast.Name)
                        and n.args[0].id == "args" and isinstance(n.args[1], ast.Constant)
                        and n.args[1].value in ARGS_ATTRS and not n.keywords), "getattr 仅允许 CLI 参数"
        if isinstance(n, ast.With):
            assert len(n.items) == 1, "仅允许单一只读锁上下文"
            cm = n.items[0].context_expr
            assert (isinstance(cm, ast.Call) and isinstance(cm.func, ast.Name)
                    and cm.func.id == "_yh_readonly_lock"), "仅允许已注入只读锁"
    if fn.name == "_yh_readonly_report":
        assert not (_references(fn) & {"_YhReadonlyAdapter", "_yh_readonly_lock",
                                       "_yh_collect_with_adapter", "run_read_account",
                                       "cmd_read_account", "print", "Path"}), "报告 helper 必须纯组装"
    return fn


def _load_entry(harness):
    tree = _source_tree()
    # 缺失契约必须是断言 RED，不能 NameError 或导入阶段崩溃。
    run = _one(tree, "run_read_account")
    cmd = _one(tree, "cmd_read_account")
    report_defs = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                   and n.name == "_yh_readonly_report"]
    assert len(report_defs) <= 1, "纯报告 helper 重复"
    if report_defs:
        assert "_yh_readonly_report" in _direct_calls(run), "禁止悬空纯报告 helper"
    namespace = harness.namespace()
    allowed = set(namespace) | set(SAFE_BUILTINS) | set(EXTRACTED)
    prepared = [_prepare_function(n, allowed) for n in [run, cmd] + report_defs]
    unit = ast.fix_missing_locations(ast.Module(body=prepared, type_ignores=[]))
    exec(compile(unit, "<isolated-real-read-account-entry>", "exec"), namespace)
    return namespace


class SyntheticFailure(Exception):
    pass


class SafetyViolation(BaseException):
    """不允许被生产 except Exception 吞掉的合成隔离违规。"""


class _FrozenInstant:
    def isoformat(self):
        return "2042-04-06T00:00:00+00:00"


class _FrozenClock:
    @staticmethod
    def now(*args, **kwargs):
        return _FrozenInstant()


class _FakeLock:
    def __init__(self, h):
        self.h = h

    def __enter__(self):
        self.h.event("lock:enter")
        self.h.held = not self.h.busy
        return self.h.held

    def __exit__(self, exc_type, exc_value, tb):
        self.h.events.append("lock:exit")
        self.h.held = False
        return False


class _FakeAdapter:
    def __init__(self, h):
        self.h = h

    def _event(self, label):
        if not self.h.held:
            raise SafetyViolation("adapter 调用越过锁：" + label)
        self.h.event(label)

    def guard(self):
        self._event("guard")
        return copy.deepcopy(self.h.guard_value)

    def identity(self):
        self._event("identity")
        return copy.deepcopy(self.h.identity_value)

    def select_query(self, page):
        if page not in ("资金股票", "历史成交"):
            raise SafetyViolation("非只读查询页")
        self._event("select:" + page)
        return {"ok": True, "selected": True, "page_confirmed": True, "page": page}

    def read_funds(self):
        self._event("read_funds")
        return copy.deepcopy(self.h.evidence["funds"])

    def read_holdings(self):
        self._event("read_holdings")
        return copy.deepcopy(self.h.evidence["holdings"])

    def read_fills(self):
        self._event("read_fills")
        return copy.deepcopy(self.h.evidence["fills"])

    def __getattr__(self, name):
        raise SafetyViolation("禁止非协议访问：" + name)


def _evidence():
    row = {"date": "20420406", "time": "10:12:34", "side": "buy", "code": "990001",
           "quantity": 2, "price": 3.0, "amount": 6.0}
    return {
        "expected_account": ACCOUNT, "identity_before": ACCOUNT, "identity_after": ACCOUNT,
        "stock_page_confirmed": True, "abort_status": None,
        "funds": {"balance": 17.0, "total_assets": 41.0},
        "funds_labels": {"balance": "资金余额", "total_assets": "总资产"},
        "funds_as_of": "2042-04-06", "funds_coverage_complete": True,
        "funds_evidence": {"fixture_tag": PAYLOAD_MARKER},
        "holdings": [{"code": "990001", "quantity": 7}], "headers": ["code", "quantity"],
        "holdings_as_of": "2042-04-06", "holdings_evidence": {"fixture_tag": PAYLOAD_MARKER},
        "coverage_complete": True, "empty_confirmed": False,
        "coverage_evidence": {"rows_complete": True, "columns_complete": True, "pages_complete": True},
        "fills": {
            "attempted": True, "rows": [row, copy.deepcopy(row)],
            "header_columns": ["date", "time", "side_raw", "code", "quantity", "price", "amount"],
            "date_range": {"start": "2042-04-05", "end": "2042-04-06"},
            "date_range_source": "query_controls_readback", "coverage_complete": True,
            "coverage_evidence": {"fixture_tag": PAYLOAD_MARKER}, "empty_confirmed": False,
            "count_total": 2, "row_count_read": 2, "page_current": 1, "page_total": 1,
            "next_page_present": False, "summary": {"present": False},
        },
    }


class Harness:
    """只暴露有限的合成依赖；没有文件句柄、模块对象或真实系统服务。"""
    def __init__(self):
        self.events = []
        self.held = False
        self.busy = False
        self.fault = None
        self.adapter = None
        self.collect_calls = []
        self.snapshot_calls = []
        self.fills_calls = []
        self.last_collected = None
        self.script = None
        self.stdout = ""
        self.evidence = _evidence()
        self.identity_value = {"account": ACCOUNT, "fingerprint": "SYNTHETIC_WINDOW_A"}
        self.guard_value = {"ok": True, "owner_active": False}
        self.snapshot_reason = "ok"
        self.fills_reason = "ok"
        self.fills_status = "ok"

    def event(self, label):
        self.events.append(label)
        if self.fault == label:
            raise SyntheticFailure(FAULT_TEXT)

    def lock(self, *, timeout_seconds):
        if self.held or timeout_seconds != 10.0:
            raise SafetyViolation("只读锁参数/重复持锁不符合契约")
        self.events.append("lock:create")
        return _FakeLock(self)

    def factory(self, *, happ_exe):
        if not self.held or happ_exe != EXE or self.adapter is not None:
            raise SafetyViolation("adapter 必须锁内按合成 exe 构造一次")
        self.event("adapter:create")
        self.adapter = _FakeAdapter(self)
        return self.adapter

    def collect(self, adapter, expected_account, *, with_fills=False):
        if not self.held or adapter is not self.adapter or expected_account != ACCOUNT:
            raise SafetyViolation("入口没有把已锁定 adapter/正确期望账号交给 collector")
        self.collect_calls.append((expected_account, with_fills))
        self.event("collect:start")
        script = ["guard", "identity", "guard", "select:资金股票", "read_funds", "read_holdings"]
        if with_fills:
            script += ["select:历史成交", "read_fills"]
        script += ["identity", "guard"]
        for op in self.script if self.script is not None else script:
            if op.startswith("select:"):
                adapter.select_query(op.split(":", 1)[1])
            else:
                getattr(adapter, op)()
        self.event("collect:return")
        ev = copy.deepcopy(self.evidence)
        if not with_fills:
            ev["fills"] = {"attempted": False, "rows": [], "reason": "fills_not_requested",
                           "coverage_complete": False}
        self.last_collected = copy.deepcopy(ev)
        return ev

    def snapshot(self, evidence):
        self.event("snapshot")
        self.snapshot_calls.append(copy.deepcopy(evidence))
        # 不实施 A 裁决；只要求真实报告层未改造已知字段，返回该场景预置裁决。
        for key in ("funds", "holdings", "headers", "identity_before", "identity_after"):
            if evidence.get(key) != self.last_collected.get(key):
                raise SafetyViolation("报告层篡改 snapshot 输入：" + key)
        ok = self.snapshot_reason == "ok"
        return {"status": "ok" if ok else "unavailable", "rc": 0 if ok else 4,
                "reason": self.snapshot_reason, "account": ACCOUNT,
                "funds": copy.deepcopy(evidence["funds"]) if ok else None,
                "holdings": copy.deepcopy(evidence["holdings"]) if ok else None}

    def fills_verdict(self, evidence):
        self.event("fills_verdict")
        self.fills_calls.append(copy.deepcopy(evidence))
        if evidence != self.last_collected["fills"]:
            raise SafetyViolation("报告层篡改 fills 输入/推造日期/字段")
        attempted = evidence.get("attempted") is True
        ok = attempted and self.fills_status == "ok"
        return {"status": self.fills_status if attempted else "unavailable",
                "reason": self.fills_reason if attempted else "fills_not_attempted",
                "coverage_complete": ok, "date_range_proven": ok,
                "row_count": len(evidence.get("rows", []))}

    def emit(self, *values, sep=" ", end="\n"):
        if not all(isinstance(value, str) for value in values):
            raise SafetyViolation("CLI 必须打印序列化 JSON")
        self.stdout += sep.join(values) + end

    @staticmethod
    def fake_path(value):
        if value != EXE:
            raise SafetyViolation("禁止接触真实路径")
        return value

    @staticmethod
    def args_getattr(obj, name, default=None):
        if type(obj) is not SimpleNamespace or name not in ARGS_ATTRS:
            raise SafetyViolation("getattr 只能读取合成 CLI 参数")
        return getattr(obj, name, default)

    def namespace(self):
        return {
            "__builtins__": dict(SAFE_BUILTINS), "SCHEMA": "ths_client/1",
            "YUANHANG_EXE_DEFAULT": EXE, "datetime": _FrozenClock,
            "timezone": SimpleNamespace(utc="SYNTHETIC_UTC"),
            "ThsXiadanError": SyntheticFailure, "Path": self.fake_path,
            "json": SimpleNamespace(dumps=json.dumps), "print": self.emit,
            "getattr": self.args_getattr, "_yh_readonly_lock": self.lock,
            "_YhReadonlyAdapter": self.factory, "_yh_collect_with_adapter": self.collect,
            "yh_single_snapshot": self.snapshot, "yh_fills_verdict": self.fills_verdict,
        }


class ReadonlyEntryContractTests(unittest.TestCase):
    def invoke(self, h, **kwargs):
        ns = _load_entry(h)
        params = {"account": ACCOUNT}
        params.update(kwargs)
        report = ns["run_read_account"](**params)
        self.assertIsInstance(report, dict)
        self.assertEqual(json.loads(json.dumps(report, ensure_ascii=False, allow_nan=False)), report)
        self.assertEqual(report.get("command"), "read-account")
        self.assertIs(report.get("credentials_touched"), False)
        self.assertFalse(h.held, "返回后必须已释放锁")
        for key in ("report_path", "capture_path", "captures", "switch"):
            self.assertFalse(report.get(key), "默认调用不得生成持久化/切号产物：" + key)
        return report

    def assert_revoked(self, report):
        self.assertNotIn(report.get("status"), ("ok", "partial", None))
        self.assertNotEqual(report.get("rc"), 0)
        self.assertFalse(report.get("account"), "拒绝后不得继续归属账号")
        for key in ("funds", "holdings", "funds_labels", "holdings_headers", "funds_read_ok",
                    "funds_evidence", "holdings_evidence", "coverage_evidence", "empty_confirmed"):
            self.assertFalse(report.get(key), "未撤销：" + key)
        coverage = report.get("coverage", {})
        self.assertIs(coverage.get("holdings_complete"), False)
        self.assertIs(coverage.get("fills_complete"), False)
        fills = report.get("fills", {})
        self.assertNotEqual(fills.get("status"), "ok")
        for key in ("rows", "header_columns", "date_range", "date_tokens", "summary",
                    "observed_record_date_extent", "coverage_complete", "coverage_evidence",
                    "empty_confirmed", "row_count", "row_count_read", "date_range_proven"):
            self.assertFalse(fills.get(key), "未撤销 fills：" + key)
        self.assertNotEqual(report.get("snapshot_decision", {}).get("status"), "ok")
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(PAYLOAD_MARKER, serialized)
        self.assertNotIn(FAULT_TEXT, serialized, "异常文本可能携带账户数据")

    def test_01_structure_real_default_route_not_dangling_or_legacy(self):
        tree = _source_tree()
        run = _one(tree, "run_read_account")
        cmd = _one(tree, "cmd_read_account")
        self.assertTrue({"_YhReadonlyAdapter", "_yh_readonly_lock", "_yh_collect_with_adapter"}
                        <= _direct_calls(run), "真实默认入口必须接到新锁/adapter/现有 collector")
        self.assertIn("run_read_account", _direct_calls(cmd))
        _one(tree, "_yh_collect_with_adapter")
        _one(tree, "_yh_readonly_lock")
        adapter = _one(tree, "_YhReadonlyAdapter", ast.ClassDef)
        methods = {n.name: n for n in adapter.body if isinstance(n, ast.FunctionDef)}
        self.assertTrue(PROTOCOL <= methods.keys(), "真实 adapter 必须具体提供六方法")
        for name in sorted(PROTOCOL):
            method = methods[name]
            self.assertTrue(any(isinstance(n, ast.Call) for n in ast.walk(method)),
                            "协议方法不能仅 pass/raise/返回固定 unavailable：" + name)
            self.assertTrue(any(isinstance(n, ast.Return) for n in ast.walk(method)),
                            "协议方法必须存在数据返回路径：" + name)
        scoped = [run, cmd, adapter] + [n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "_yh_readonly_report"]
        violations = sorted(set().union(*(_references(n) & FORBIDDEN for n in scoped)))
        self.assertEqual(violations, [], "真实默认链引用旧 collector/副作用")
        parser = _one(tree, "build_parser")
        block = []
        in_read = False
        for statement in parser.body:
            additions = [n for n in ast.walk(statement) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute) and n.func.attr == "add_parser"]
            if additions:
                if in_read:
                    break
                in_read = any(n.args and isinstance(n.args[0], ast.Constant)
                              and n.args[0].value == "read-account" for n in additions)
            if in_read:
                block.append(statement)
        bindings = [k.value.id for stmt in block for n in ast.walk(stmt)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "set_defaults" for k in n.keywords
                    if k.arg == "func" and isinstance(k.value, ast.Name)]
        self.assertEqual(bindings, ["cmd_read_account"], "真实 argparse 路由不得分叉")
        main = _one(tree, "main")
        self.assertIn("build_parser", _direct_calls(main))
        self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and isinstance(n.func.value, ast.Name) and n.func.value.id == "args"
                            and n.func.attr == "func" and len(n.args) == 1
                            and isinstance(n.args[0], ast.Name) and n.args[0].id == "args"
                            for n in ast.walk(main)), "main 必须分派已解析的真实子命令")

    def test_02_structure_module_initialization_no_services_utils_runtime(self):
        tree = _source_tree()
        violations = []
        runtime_calls = {"_runtime_dir", "_tracker_root", "runtime_path", "_write_report",
                         "_yh_acquire_gui_lock", "_yh_release_gui_lock"}
        # 仅扫描初始化执行的语法；方法/函数体不在 import 时执行，但其默认参数会。
        class InitVisitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                for item in node.decorator_list + node.args.defaults + [
                        x for x in node.args.kw_defaults if x is not None]:
                    self.visit(item)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_If(self, node):
                is_main = (isinstance(node.test, ast.Compare)
                           and isinstance(node.test.left, ast.Name)
                           and node.test.left.id == "__name__"
                           and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq)
                           and len(node.test.comparators) == 1
                           and isinstance(node.test.comparators[0], ast.Constant)
                           and node.test.comparators[0].value == "__main__")
                if is_main:
                    for child in node.orelse:
                        self.visit(child)
                else:
                    self.generic_visit(node)

            def visit_Import(self, node):
                for alias in node.names:
                    if alias.name.split(".")[0] in {"services", "utils"}:
                        violations.append((node.lineno, "eager_import:" + alias.name))

            def visit_ImportFrom(self, node):
                if (node.module or "").split(".")[0] in {"services", "utils"}:
                    violations.append((node.lineno, "eager_import:" + node.module))

            def visit_Call(self, node):
                name = node.func.id if isinstance(node.func, ast.Name) else (
                    node.func.attr if isinstance(node.func, ast.Attribute) else "")
                if name in runtime_calls or name in {"__import__", "import_module"}:
                    violations.append((node.lineno, "initialization_call:" + name))
                if name in {"read_text", "read_bytes", "open", "mkdir", "glob", "rglob",
                            "exists", "stat", "write_text", "write_bytes"} and (
                        _references(node) & {"OUT_DIR", "PANEL_MAP", "JOURNAL"}
                        or any(isinstance(x, ast.Constant) and isinstance(x.value, str)
                               and "runtime" in x.value.lower() for x in ast.walk(node))):
                    violations.append((node.lineno, "runtime_io:" + name))
                self.generic_visit(node)
        InitVisitor().visit(tree)
        # 单纯 Path('...runtime...') 路径字面量不是读取，不能伪造此处的行为证据。
        self.assertEqual(violations, [], "结构门禁；不代表真实初始化已安全执行")

    def test_03_default_entry_collects_returns_json_without_files(self):
        h = Harness()
        report = self.invoke(h)
        self.assertEqual((report["status"], report["rc"]), ("ok", 0))
        self.assertEqual(report["account"], ACCOUNT)
        self.assertEqual(report["funds"], h.evidence["funds"])
        self.assertEqual(report["holdings"], h.evidence["holdings"])
        self.assertEqual(h.collect_calls, [(ACCOUNT, False)])
        self.assertEqual(len(h.snapshot_calls), 1)
        self.assertNotIn("select:历史成交", h.events)
        self.assertNotIn("read_fills", h.events)
        self.assertFalse(report["fills"].get("attempted"))
        self.assertFalse(report["coverage"]["fills_complete"])

    def test_04_cmd_calls_real_run_and_prints_one_json(self):
        for fills in (False, True):
            with self.subTest(fills=fills):
                h = Harness()
                ns = _load_entry(h)
                args = SimpleNamespace(account=ACCOUNT, happ_exe=EXE, no_switch=False,
                                       menu_config=None, menu_wait_sec=1.5, settle_sec=3.0,
                                       f12_wait=8.0, main_wait=20.0, report_root=None,
                                       fills=fills, lock_timeout=10.0)
                rc = ns["cmd_read_account"](args)
                report = json.loads(h.stdout)
                self.assertEqual((rc, report["rc"], report["status"]), (0, 0, "ok"))
                self.assertEqual(report["funds"], h.evidence["funds"])
                self.assertEqual(h.collect_calls, [(ACCOUNT, fills)])
                self.assertFalse(h.held)
                self.assertEqual(h.events.count("lock:exit"), 1)
                for key in ("report_path", "capture_path", "captures", "switch"):
                    self.assertFalse(report.get(key))

    def test_05_lock_covers_construction_all_collection_and_final_identity(self):
        h = Harness()
        self.invoke(h, with_fills=True)
        self.assertEqual(h.events[:3], ["lock:create", "lock:enter", "adapter:create"])
        self.assertEqual(h.events.count("lock:enter"), 1)
        self.assertEqual(h.events.count("lock:exit"), 1)
        end = h.events.index("lock:exit")
        for event in ("adapter:create", "collect:start", "select:资金股票", "read_funds",
                      "read_holdings", "select:历史成交", "read_fills", "collect:return"):
            self.assertLess(h.events.index(event), end)
        final_identity = max(i for i, event in enumerate(h.events) if event == "identity")
        self.assertLess(h.events.index("read_fills"), final_identity)
        self.assertLess(final_identity, h.events.index("collect:return"))
        self.assertEqual(h.events[final_identity + 1], "guard")

    def test_06_busy_lock_has_zero_adapter_or_collection(self):
        h = Harness()
        h.busy = True
        report = self.invoke(h, with_fills=True)
        self.assertEqual((report["status"], report["rc"]), ("lock_busy", 4))
        self.assert_revoked(report)
        self.assertEqual(h.events, ["lock:create", "lock:enter", "lock:exit"])
        self.assertIsNone(h.adapter)
        self.assertEqual(h.collect_calls, [])

    def test_07_exceptions_revoke_entire_report_release_and_never_fallback(self):
        for fault in ("lock:enter", "adapter:create", "collect:return", "snapshot", "fills_verdict"):
            with self.subTest(fault=fault):
                h = Harness()
                h.fault = fault
                report = self.invoke(h, with_fills=True)
                self.assert_revoked(report)
                self.assertIn(fault, h.events)
                self.assertEqual(h.events.count("lock:exit"), 0 if fault == "lock:enter" else 1)
                if fault in ("lock:enter", "adapter:create"):
                    self.assertEqual(h.collect_calls, [])
                else:
                    self.assertEqual(len(h.collect_calls), 1, "异常不得重试/回退旧 collector")

    def test_08_identity_rejection_revokes_even_stale_previously_complete_payload(self):
        for mode in ("initial_wrong", "initial_unknown", "late_changed", "late_unreadable",
                     "fills_changed", "fills_unreadable", "collector_abort"):
            with self.subTest(mode=mode):
                h = Harness()
                if mode.startswith("initial"):
                    h.script = ["guard", "identity", "guard"]
                    h.evidence["identity_before"] = OTHER_ACCOUNT if mode == "initial_wrong" else None
                    h.evidence["identity_after"] = h.evidence["identity_before"]
                    h.evidence["abort_status"] = "wrong_account_fail_closed" if mode == "initial_wrong" else "identity_unreadable_fail_closed"
                elif mode.startswith("late"):
                    h.evidence["identity_after"] = OTHER_ACCOUNT if mode == "late_changed" else None
                    h.snapshot_reason = "identity_changed" if mode == "late_changed" else "identity_after_unverified"
                elif mode.startswith("fills"):
                    h.evidence["fills"]["identity_reject"] = True if mode == "fills_changed" else "unreadable"
                    h.fills_status, h.fills_reason = "unavailable", "fills_identity_unverified"
                else:
                    h.evidence["abort_status"] = "adapter_exception_fail_closed"
                report = self.invoke(h, with_fills=True)
                self.assertEqual(report["rc"], 4)
                self.assert_revoked(report)
                self.assertEqual(h.events.count("lock:exit"), 1)
                self.assertEqual(len(h.collect_calls), 1)
                if mode.startswith("initial"):
                    self.assertNotIn("read_funds", h.events)

    def test_09_complete_fills_ok_and_duplicates_preserved(self):
        h = Harness()
        report = self.invoke(h, with_fills=True)
        self.assertEqual((report["status"], report["rc"]), ("ok", 0))
        self.assertEqual(report["funds"], h.evidence["funds"])
        self.assertEqual(report["holdings"], h.evidence["holdings"])
        self.assertEqual(report["fills"]["rows"], h.evidence["fills"]["rows"])
        self.assertEqual(report["fills"]["row_count"], 2)
        self.assertEqual(report["fills"]["date_range"], h.evidence["fills"]["date_range"])
        self.assertEqual(report["fills"]["status"], "ok")
        self.assertIs(report["coverage"]["holdings_complete"], True)
        self.assertIs(report["coverage"]["fills_complete"], True)
        self.assertEqual(h.collect_calls, [(ACCOUNT, True)])
        self.assertEqual((len(h.snapshot_calls), len(h.fills_calls)), (1, 1))

    def test_10_missing_fields_and_query_dates_remain_partial_with_known_rows(self):
        for missing in ("funds", "holdings", "fill_field", "query_dates"):
            with self.subTest(missing=missing):
                h = Harness()
                if missing == "funds":
                    del h.evidence["funds"]["total_assets"]
                    h.evidence["funds_coverage_complete"] = False
                    h.evidence["coverage_complete"] = False
                    h.snapshot_reason = "funds_incomplete"
                elif missing == "holdings":
                    h.evidence["holdings"][0]["quantity"] = None
                    h.evidence["coverage_complete"] = False
                    h.snapshot_reason = "holdings_rows_invalid"
                else:
                    h.fills_status = "partial"
                    h.evidence["fills"]["coverage_complete"] = False
                    if missing == "fill_field":
                        h.evidence["fills"]["rows"][0]["time"] = None
                        h.fills_reason = "fills_row_invalid"
                    else:
                        h.evidence["fills"].update(date_range=None, date_range_source=None)
                        h.fills_reason = "date_range_unproven"
                report = self.invoke(h, with_fills=True)
                self.assertEqual((report["status"], report["rc"]), ("partial", 0))
                self.assertEqual(report["account"], ACCOUNT)
                self.assertEqual(report["funds"], h.evidence["funds"])
                self.assertEqual(report["holdings"], h.evidence["holdings"])
                self.assertEqual(report["fills"]["rows"], h.evidence["fills"]["rows"])
                self.assertEqual(report["fills"]["row_count"], 2)
                self.assertEqual((len(h.snapshot_calls), len(h.fills_calls)), (1, 1))
                if missing in ("funds", "holdings"):
                    self.assertIs(report["coverage"]["holdings_complete"], False)
                else:
                    self.assertIs(report["coverage"]["fills_complete"], False)
                    self.assertEqual(report["fills"]["status"], "partial")
                if missing == "query_dates":
                    self.assertIsNone(report["fills"].get("date_range"))
                    self.assertFalse(report["fills"].get("date_range_proven"))

    def test_11_allow_switch_compatibility_never_switches_wrong_account(self):
        for allow_switch in (False, True):
            with self.subTest(allow_switch=allow_switch):
                h = Harness()
                h.identity_value["account"] = OTHER_ACCOUNT
                h.script = ["guard", "identity", "guard"]
                h.evidence.update(identity_before=OTHER_ACCOUNT, identity_after=OTHER_ACCOUNT,
                                  abort_status="wrong_account_fail_closed")
                report = self.invoke(h, allow_switch=allow_switch)
                self.assertEqual((report["status"], report["rc"]), ("wrong_account_fail_closed", 4))
                self.assert_revoked(report)
                self.assertEqual(h.collect_calls, [(ACCOUNT, False)])
                self.assertFalse(any(e.startswith("select:") or e.startswith("read_") for e in h.events))

    def test_12_account_omitted_is_guarded_under_lock_before_collector(self):
        for identity in ({"account": ACCOUNT, "fingerprint": "SYNTHETIC_WINDOW_A"}, None):
            with self.subTest(readable=identity is not None):
                h = Harness()
                h.identity_value = identity
                report = self.invoke(h, account=None)
                self.assertEqual(h.events[:3], ["lock:create", "lock:enter", "adapter:create"])
                first_identity = h.events.index("identity")
                self.assertEqual(h.events[first_identity - 1:first_identity + 2],
                                 ["guard", "identity", "guard"])
                if identity is None:
                    self.assert_revoked(report)
                    self.assertEqual(report["rc"], 4)
                    self.assertEqual(h.collect_calls, [])
                else:
                    self.assertEqual((report["status"], report["account"]), ("ok", ACCOUNT))
                    self.assertLess(first_identity, h.events.index("collect:start"))
                    self.assertEqual(h.collect_calls, [(ACCOUNT, False)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
