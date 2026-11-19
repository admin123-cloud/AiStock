# -*- coding: utf-8 -*-
"""THS readonly adapter 六缺陷回归（任务书 2026-11-15：ths_adapter_regression）。

本切片只编写隔离回归测试，不运行（RED 须经 PM 批准后由 runner 执行）。
不修改生产脚本/既有测试/docs；不 commit/push；不触 GUI/网络/DB/runtime/
凭证。全部账户/窗口/持仓/成交/日期/控件值均为合成数据；所有 Win32/COM
依赖一律内存 fake，绝不接触真实系统服务。

冻结基线（追溯用，不做运行时硬门禁；由批准的 runner 在 RED 前核）：
  HEAD  132fe6683ba62205964eb7e73b9eb829cfa62e89
  scripts/ths_client.py sha256
        e0d1176a7b0c150c0ddcbc8852bbe68ce84749f2791711904bf3cb175272efef

被测物 = 真实 SOURCE 的 AST 摘取（仅文本读取 scripts/ths_client.py，
不 import 项目、不执行模块初始化、不用 pytest）：
  顶层函数 allowlist：
    _yh_ro_uia_scan / _yh_ro_query_date_range / _yh_ro_child_dialogs /
    _yh_ro_desktop_state / _yh_ro_chain_equal / yh_fills_page_evidence /
    yh_fills_verdict / _yh_readonly_report / _yh_collect_with_adapter /
    _yh_uia_texts
  adapter 方法 allowlist（自 _YhReadonlyAdapter 类体摘取，改名装载）：
    __init__→adapter_init, _guard→adapter__guard, guard→adapter_guard,
    read_holdings→adapter_read_holdings, read_fills→adapter_read_fills
  常量 allowlist（真实字面量/re.compile 原样摘取）：
    SCHEMA / _YH_RO_DATE_VALUE_RE / _YH_RO_CREDENTIAL_AIDS / _YH_RO_UIA_CT /
    _YH_RO_QUERY_PAGES / _YH_RO_OWNER_IDLE_MS / _YH_FILLS_COUNT_RE /
    _YH_FILLS_PAGE_RE / _YH_FILLS_NEXT_TOKENS / _YH_FILLS_ORDER_TOKENS /
    _YH_FILLS_TRADE_TOKENS / _YH_FILLS_NUMERIC_FIELDS
  其余依赖注入白名单（_base_fakes / _load overrides，全部合成）：
    ThsXiadanError(合成异常) USER32 ctypes wintypes os re date datetime
    timezone YUANHANG_EXE_DEFAULT YH_*_COLUMN_ALIASES _yh_ro_uia
    _yh_ro_chain_snapshot _yh_ro_session_id _yh_ro_input_state
    _yh_ro_find_query_window _yh_ro_visible_windows _yh_ro_page_state
    _yh_ro_identity_values _yh_ro_window_state _yh_ro_grid_items
    _yh_ro_query_date_range _class_name yh_holdings_from_items
    yh_fills_from_items _yh_ro_header_band yh_fills_summary_evidence
    yh_fills_summary_reconciled yh_single_snapshot
  （_yh_ro_desktop_state / _yh_ro_child_dialogs 摘取真实版；需要替身的
    用例通过 _load(overrides=...) 在装载后覆盖同名全局——注入缝隙显式可见。）

受限装载说明（非通用沙箱）：loader 只接受节点白名单内的 AST；禁
decorator/Global/Nonlocal/异步/删除/双下划线名称；默认参数仅字面量或
白名单名；import 仅允许 comtypes.client(from: CreateObject/GetModule)与
comtypes.gen.UIAutomationClient(as UIAC)——且仅在 _yh_uia_texts 内出现，
由各 ns 自带的私有严格 __import__ 就地解析：只识别批准的 comtypes
模块名（level=0）与批准 fromlist，返回内存 ModuleType 层级
（client.CreateObject 返回装载参数 uia_backend 注入的 fake UIA 后端；
uia_backend=None 即 comtypes 缺席场景，一律 ImportError）。绝不触真实
sys.modules：不预置、不覆盖、不删除、不恢复任何真实模块或父包属性——
本文件对全局 import 状态零修改。属性读取限白名单、属性写仅限非双下
划线（self 私有槽/argtypes/restype）。overrides 仅允许固定 fake 缝隙键
（OVERRIDABLE_SEAMS），且在真实定义装载完成后应用——不会被同名真实
函数静默覆盖，也绝不替换 builtins/__import__/被测函数本体；每项测试
docstring 标明被测真函数与下层 fake。这是面向本文件的目标性隔离
（defense in depth），不是安全边界；绕过沙箱不是本测试的对抗目标。
局部名解析采用函数级近似（展平收集），可能放过嵌套遮蔽，不影响本
测试目标。

结构断言标注：test_05 对 _yh_ro_desktop_state 输出契约的键检查、以及
装载期 allowlist 断言，均为结构检查，不能替代真实 WTS/UIA 行为验收。

下一切片建议的最小纯 helper（本文件不实现；缺失即 test_15 RED）：
  _yh_ro_bound_date_range(bound_values) -> (range_dict | None, source | None)
    bound_values: [(role, value)] 序列，role ∈ {"start", "end"} 仅为已验证
    身份控件的读回值（此层不读任何 Value，值由绑定层提供）。
    规则：start/end 各恰一个 → 区间；同值 → 单日区间（start==end 合法）；
    反向（end<start）→ (None, None)；多组/角色重复/未知角色 → (None,
    None)；绝不排序修补、绝不推算日期。

RED 预期：15 项测试针对任务书六缺陷的现行实现（缺陷1:test_01；
缺陷2:test_02/03/04；缺陷3:test_05/06；缺陷4:test_07/08；缺陷5:
test_09..12；缺陷6:test_13/14/15）。批准后建议 runner（不 discovery、
不 pytest、不项目 import）：
  F:/python/python3.10/python.exe -I -S -B F:/Aistock/app/tests/test_ths_readonly_adapter_regression.py -v
执行前核 HEAD 与 SOURCE sha256。本文件不在导入时触发测试。
"""
import ast
import copy
import re
import types
import unittest
from datetime import date as _real_date
from pathlib import Path
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "ths_client.py"
BASELINE_HEAD = "132fe6683ba62205964eb7e73b9eb829cfa62e89"
BASELINE_SOURCE_SHA256 = ("e0d1176a7b0c150c0ddcbc8852bbe68ce84749f2"
                          "791711904bf3cb175272efef")

EXE = "SYNTHETIC_HAPP_EXE_NOT_A_PATH"
ACCOUNT = "SYNTHETIC_ACCOUNT_A"
BASE_CHAIN = {
    "session_id": 1,
    "happ": {"pid": 11, "ppid": 1, "image": "SYNTH/happ.exe"},
    "xiadan": {"pid": 22, "ppid": 11, "image": "SYNTH/transaction/xiadan.exe"},
    "plus": {"pid": 33, "ppid": 22,
             "image": "SYNTH/transaction/xiadan-plus/xiadan-plus.exe"},
}
HEADER_ONLY_ITEMS = [
    {"text": t, "x": float(x), "y": 350.0}
    for t, x in (("证券代码", 60), ("证券名称", 120),
                 ("持股数量", 180), ("成本价", 240))
]

# ── loader allowlist ────────────────────────────────────────────────────
ALLOWED_FUNCTIONS = (
    "_yh_ro_uia_scan", "_yh_ro_query_date_range", "_yh_ro_child_dialogs",
    "_yh_ro_desktop_state", "_yh_ro_chain_equal", "yh_fills_page_evidence",
    "yh_fills_verdict", "_yh_readonly_report", "_yh_collect_with_adapter",
    "_yh_uia_texts",
)
METHOD_LOAD = {
    "__init__": "adapter_init", "_guard": "adapter__guard",
    "guard": "adapter_guard", "read_holdings": "adapter_read_holdings",
    "read_fills": "adapter_read_fills",
}
ALLOWED_CONSTANTS = (
    "SCHEMA", "_YH_RO_DATE_VALUE_RE", "_YH_RO_CREDENTIAL_AIDS",
    "_YH_RO_UIA_CT", "_YH_RO_QUERY_PAGES", "_YH_RO_OWNER_IDLE_MS",
    "_YH_FILLS_COUNT_RE", "_YH_FILLS_PAGE_RE", "_YH_FILLS_NEXT_TOKENS",
    "_YH_FILLS_ORDER_TOKENS", "_YH_FILLS_TRADE_TOKENS",
    "_YH_FILLS_NUMERIC_FIELDS",
    "_YH_RO_HISTORY_BUTTON_AIDS", "_YH_RO_QUERY_DONE_AID",
)
OPTIONAL_HELPER = "_yh_ro_bound_date_range"

FORBIDDEN_NAMES = frozenset({
    "eval", "exec", "compile", "open", "input", "breakpoint", "globals",
    "locals", "vars", "setattr", "delattr", "print", "__import__",
})
SAFE_BUILTINS = {
    "str": str, "int": int, "float": float, "bool": bool, "len": len,
    "abs": abs, "min": min, "max": max, "sum": sum, "any": any, "all": all,
    "sorted": sorted, "reversed": reversed, "set": set, "frozenset": frozenset,
    "list": list, "dict": dict, "tuple": tuple, "zip": zip, "repr": repr,
    "isinstance": isinstance, "issubclass": issubclass, "enumerate": enumerate,
    "next": next, "iter": iter, "range": range, "round": round,
    "getattr": getattr, "Exception": Exception, "RuntimeError": RuntimeError,
    "ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError,
    "IndexError": IndexError, "StopIteration": StopIteration,
    "ImportError": ImportError,
}
ATTR_OK = frozenset({
    "get", "items", "keys", "values", "update", "setdefault", "copy", "clear",
    "append", "extend", "pop", "insert", "join", "strip", "lstrip", "rstrip",
    "lower", "upper", "split", "replace", "startswith", "endswith", "find",
    "sort", "group", "groups", "isoformat", "now", "utc", "value", "getpid",
    "fullmatch", "search", "match", "compile", "sub",
    "ElementFromHandle", "FindAll", "GetElement", "Length",
    "CreateTrueCondition", "CreatePropertyCondition", "GetCurrentPattern",
    "QueryInterface", "CurrentValue", "CurrentName", "CurrentClassName",
    "CurrentAutomationId", "CurrentControlType", "CurrentIsPassword",
    "CurrentIsOffscreen", "CurrentIsEnabled", "CurrentNativeWindowHandle",
    "CurrentBoundingRectangle", "left", "top", "right", "bottom",
    "windll", "user32", "kernel32", "IsWindowVisible", "GetDlgCtrlID",
    "EnumChildWindows", "OpenInputDesktop", "CloseDesktop", "GetThreadDesktop",
    "GetUserObjectInformationW", "GetCurrentThreadId", "argtypes", "restype",
    "create_unicode_buffer", "sizeof", "byref", "c_void_p", "DWORD", "BOOL",
    "HWND", "LPARAM", "POINTER", "WINFUNCTYPE",
    "guard", "identity", "select_query", "read_funds", "read_holdings",
    "read_fills", "_happ_exe", "_chain", "_hwnd", "_baseline_windows",
    "_initial_tick", "_initial_foreground", "_identity_values", "_page",
    "_require_page",
    # D15 隔离夹具修复：源码摘取函数实际必需的属性，逐项补齐（不放宽）
    "UIA_ControlTypePropertyId", "UIA_ValuePatternId",
    "IUIAutomationValuePattern", "CUIAutomation", "IUIAutomation",
    "c_int", "_guard",
})
IMPORT_OK = frozenset({"comtypes.gen.UIAutomationClient"})
IMPORT_FROM_OK = {"comtypes.client": frozenset({"CreateObject", "GetModule"})}
NODE_OK = frozenset({
    ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
    ast.AnnAssign, ast.AugAssign, ast.If, ast.For, ast.While, ast.Break,
    ast.Continue, ast.Pass, ast.Expr, ast.Name, ast.Load, ast.Store,
    ast.Constant, ast.Dict, ast.List, ast.Tuple, ast.Set, ast.Subscript,
    ast.Slice, ast.IfExp, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.USub, ast.UAdd, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.FloorDiv, ast.Mod, ast.Pow, ast.Compare, ast.Eq, ast.NotEq, ast.Is,
    ast.IsNot, ast.In, ast.NotIn, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Call,
    ast.keyword, ast.Starred, ast.Attribute, ast.Lambda, ast.ListComp,
    ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension, ast.Try,
    ast.ExceptHandler, ast.Raise, ast.Import, ast.ImportFrom, ast.alias,
    ast.JoinedStr, ast.FormattedValue,
})


class SyntheticThsError(Exception):
    """合成 ThsXiadanError（真实类不摘取；同名注入）。"""


# ── 合成 COM/Win32 fake（全部内存对象，绝不触真实系统） ────────────────
class _FakeFound:
    def __init__(self, getter, length):
        self._getter = getter
        self.Length = int(length)

    def GetElement(self, index):
        return self._getter(index)


class _ScanRoot:
    def __init__(self, found):
        self._found = found

    def FindAll(self, scope, condition):
        return self._found


class _ScanUia:
    def __init__(self, elements):
        self._elements = list(elements)

    def ElementFromHandle(self, hwnd):
        return _ScanRoot(_FakeFound(self._elements.__getitem__,
                                    len(self._elements)))

    def CreateTrueCondition(self):
        return ("true_condition",)

    def CreatePropertyCondition(self, prop, value):
        return ("property_condition", prop, value)


class _ScanEl:
    """逐属性记录访问日志的 UIA 元素 fake。"""

    def __init__(self, eid, spec, journal):
        self._eid, self._spec, self._journal = eid, spec, journal

    def _hit(self, attr):
        self._journal.append((self._eid, attr))
        return self._spec[attr]

    @property
    def CurrentBoundingRectangle(self):
        return self._hit("rect")

    @property
    def CurrentControlType(self):
        return self._hit("ct")

    @property
    def CurrentClassName(self):
        return self._hit("cls")

    @property
    def CurrentAutomationId(self):
        return self._hit("aid")

    @property
    def CurrentName(self):
        return self._hit("name")

    @property
    def CurrentIsPassword(self):
        return self._hit("pwd")

    @property
    def CurrentIsOffscreen(self):
        return self._hit("offscreen")

    @property
    def CurrentIsEnabled(self):
        return self._hit("enabled")

    @property
    def CurrentNativeWindowHandle(self):
        return self._hit("win")


_RECT = SimpleNamespace(left=1, top=2, right=30, bottom=20)


def _scan_elements(journal):
    def el(eid, **spec):
        full = {"rect": _RECT, "ct": 50020, "cls": "SYNTH_CLS", "aid": "",
                "name": "", "pwd": False, "offscreen": False, "enabled": True,
                "win": 0}
        full.update(spec)
        return _ScanEl(eid, full, journal)

    return [
        el("E1_TREEITEM", ct=50024, name="资金股票"),
        el("E2_PWD_EDIT", ct=50004, pwd=True, name="SYNTH_PWD_NEVER_READ"),
        el("E3_DISABLED", ct=50000, enabled=False,
           name="SYNTH_DISABLED_NEVER_READ"),
        el("E4_OFFSCREEN", ct=50020, offscreen=True,
           name="SYNTH_OFFSCREEN_TEXT"),
    ]


class _DialogEl:
    """子对话框元素 fake：fail=True 时三个属性 getter 均抛错——注入点
    在源码 try 块内（真实缺陷被 continue 吞掉的路径）。"""

    def __init__(self, cls="#32770", aid="", offscreen=False, fail=False):
        self._cls, self._aid, self._off = cls, aid, offscreen
        self._fail = fail

    def _read(self, value):
        if self._fail:
            raise SyntheticThsError("SYNTH_DIALOG_READ_FAILURE")
        return value

    @property
    def CurrentIsOffscreen(self):
        return self._read(self._off)

    @property
    def CurrentClassName(self):
        return self._read(self._cls)

    @property
    def CurrentAutomationId(self):
        return self._read(self._aid)


class _ValueHolder:
    def __init__(self, hwnd, value, journal):
        self._hwnd, self._value, self._journal = hwnd, value, journal

    @property
    def CurrentValue(self):
        self._journal.append(("value_read", self._hwnd))
        return self._value


class _DatePattern:
    def __init__(self, hwnd, value, journal):
        self._hwnd, self._value, self._journal = hwnd, value, journal

    def QueryInterface(self, iface):
        return _ValueHolder(self._hwnd, self._value, self._journal)


class _DateEl:
    def __init__(self, hwnd, value, journal):
        self._hwnd, self._value, self._journal = hwnd, value, journal

    @property
    def CurrentIsPassword(self):
        return False

    def GetCurrentPattern(self, pattern_id):
        self._journal.append(("get_pattern", self._hwnd, pattern_id))
        return _DatePattern(self._hwnd, self._value, self._journal)


class _DateUia:
    def __init__(self, values, journal):
        self._values, self._journal = dict(values), journal

    def ElementFromHandle(self, hwnd):
        return _DateEl(hwnd, self._values[int(hwnd)], self._journal)


class _TextEl:
    def __init__(self, name, fail=False):
        self._name, self._fail = name, fail

    @property
    def CurrentBoundingRectangle(self):
        return _RECT

    @property
    def CurrentAutomationId(self):
        return ""

    @property
    def CurrentName(self):
        if self._fail:
            raise RuntimeError("SYNTHETIC_UIA_READ_FAILURE")
        return self._name


UIAC_NS = SimpleNamespace(
    UIA_ControlTypePropertyId=30003, TreeScope_Descendants=4,
    UIA_TextControlTypeId=50020, UIA_ValuePatternId=10042,
    IUIAutomationValuePattern=("SYNTH_IValuePattern",),
    CUIAutomation=("SYNTH_CUIAutomation",), IUIAutomation=("SYNTH_IUIA",))


class _ApiStub:
    """纯内存可调用 Win32 API 桩：允许 argtypes/restype 属性赋值。

    源码惯例是调用前就地声明函数原型（user32.XXX.argtypes = [...]）；
    绑定方法不支持属性赋值，故统一用本包装（不引入真实 ctypes）。
    """

    def __init__(self, fn):
        self._fn = fn
        self.argtypes = None
        self.restype = None

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


class _FakeUser32:
    def __init__(self):
        self.child_hwnds = []
        self.ctrl_ids = {}
        self.IsWindowVisible = _ApiStub(lambda hwnd: True)
        self.GetDlgCtrlID = _ApiStub(
            lambda hwnd: self.ctrl_ids.get(int(hwnd), 7000))
        self.EnumChildWindows = _ApiStub(self._enum_children)
        self.OpenInputDesktop = _ApiStub(
            lambda flags, inherit, access: ("SYNTH_HDESK_INPUT",))
        self.CloseDesktop = _ApiStub(lambda handle: 0)
        self.GetThreadDesktop = _ApiStub(
            lambda thread_id: ("SYNTH_HDESK_THREAD",))
        self.GetUserObjectInformationW = _ApiStub(
            lambda handle, index, buf, length, needed: 1)

    def _enum_children(self, hwnd, callback, lparam):
        for child in list(self.child_hwnds):
            callback(child, 0)
        return True


class _FakeDWORD:
    def __init__(self, *args, **kwargs):
        pass


class _FakeCtypes:
    def __init__(self):
        self.windll = SimpleNamespace(
            user32=_FakeUser32(),
            kernel32=SimpleNamespace(GetCurrentThreadId=lambda: 4242))

    @staticmethod
    def create_unicode_buffer(size):
        return SimpleNamespace(value="Default")

    @staticmethod
    def sizeof(obj):
        return 512

    @staticmethod
    def byref(obj):
        return ("byref", obj)

    @staticmethod
    def c_void_p(value=None):
        return ("c_void_p", value)

    @staticmethod
    def c_int(value=None):
        return ("c_int", value)

    @staticmethod
    def POINTER(ctype):
        return ("SYNTH_POINTER", ctype)

    @staticmethod
    def WINFUNCTYPE(*args):
        return lambda fn: fn


_FAKE_WINTYPES = SimpleNamespace(DWORD=_FakeDWORD, BOOL=1, HWND=2, LPARAM=3)


# ── 私有严格 importer：内存 comtypes 层级；零真实 sys.modules 触碰 ────
_FAKE_IMPORT_MODULES = ("comtypes", "comtypes.client", "comtypes.gen",
                        "comtypes.gen.UIAutomationClient")
_FAKE_IMPORT_FROM_CLIENT_OK = frozenset({"CreateObject", "GetModule"})

# 固定 fake 缝隙键（overrides 白名单）。真实被测函数/常量、builtins、
# __import__ 一律不可覆盖；_yh_ro_desktop_state/_yh_ro_child_dialogs 为文
# 件头声明的两处批准缝隙（本身也在 ALLOWED_FUNCTIONS 中摘取真实版）。
OVERRIDABLE_SEAMS = frozenset({
    "_yh_ro_uia", "_yh_ro_chain_snapshot", "_yh_ro_input_state",
    "_yh_ro_find_query_window", "_yh_ro_visible_windows", "_yh_ro_page_state",
    "_yh_ro_identity_values", "_yh_ro_window_state", "_yh_ro_grid_items",
    "_class_name", "_yh_ro_desktop_state", "_yh_ro_child_dialogs",
    "_yh_ro_header_band",
})


def _fake_comtypes_modules(uia_backend):
    """内存 ModuleType 层级（常量同 UIAC_NS；CreateObject 返回给定后端）。

    层级各层均挂 leaf 名属性：无论解释器对 `import a.b.c as X` 按
    __import__ 顶层回值后取属性、还是按属性链直取 leaf，都解析到同一
    uac 模块。不写入 sys.modules，不触真实父包属性。
    """
    uac = types.ModuleType("comtypes.gen.UIAutomationClient")
    for name, value in vars(UIAC_NS).items():
        setattr(uac, name, value)
    client = types.ModuleType("comtypes.client")
    client.CreateObject = lambda cls, interface=None: uia_backend
    client.GetModule = lambda name: None
    gen = types.ModuleType("comtypes.gen")
    top = types.ModuleType("comtypes")
    top.client, top.gen = client, gen
    gen.UIAutomationClient = uac
    top.UIAutomationClient = uac
    uac.UIAutomationClient = uac
    return {"comtypes": top, "comtypes.client": client,
            "comtypes.gen": gen, "comtypes.gen.UIAutomationClient": uac}


def _make_strict_importer(uia_backend):
    """每个 ns 的私有 __import__（场景选择器）。

    uia_backend=None → comtypes 缺席场景（一律 ImportError，由源码自身
    except 如实降级）；否则仅返回内存 comtypes 层级。level≠0 / 未审模块
    名 / 未审 fromlist → ImportError（fail-closed：绝不委托真实 importer，
    绝不读写真实 sys.modules）。
    """
    modules = (_fake_comtypes_modules(uia_backend)
               if uia_backend is not None else None)

    def _strict_import(name, _globals=None, _locals=None, fromlist=(),
                       level=0):
        if modules is None:
            raise ImportError("comtypes unavailable (SYNTHETIC 缺席场景)")
        if level != 0 or name not in _FAKE_IMPORT_MODULES:
            raise ImportError("isolated loader 拒绝 import %r" % (name,))
        if fromlist:
            if (name == "comtypes.client"
                    and set(fromlist) <= _FAKE_IMPORT_FROM_CLIENT_OK):
                return modules[name]
            raise ImportError(
                "isolated loader 拒绝 from-import %r %r" % (name, tuple(fromlist)))
        return modules["comtypes"]

    return _strict_import


class _FrozenInstant:
    def isoformat(self):
        return "2042-04-06T00:00:00+00:00"


class _FrozenClock:
    @staticmethod
    def now(*args, **kwargs):
        return _FrozenInstant()


def _page_state(hwnd=None):
    return {"selected_page": "资金股票", "page_marker_confirmed": True,
            "grid": {"hwnd": 4242},
            "scroll": {"vert_scrollable": False, "vert_view_size": 100.0,
                       "horz_scrollable": False, "horz_view_size": 100.0}}


def _raising_grid(hwnd):
    raise SyntheticThsError("PrintWindow failed: SYNTHETIC")


def _base_fakes():
    return {
        "ThsXiadanError": SyntheticThsError,
        "USER32": SimpleNamespace(IsWindowVisible=lambda h: True),
        "re": re, "date": _real_date,
        "os": SimpleNamespace(getpid=lambda: 31337),
        "ctypes": _FakeCtypes(), "wintypes": _FAKE_WINTYPES,
        "datetime": _FrozenClock, "timezone": SimpleNamespace(utc="SYNTH_UTC"),
        "YUANHANG_EXE_DEFAULT": EXE,
        "YH_HOLDING_COLUMN_ALIASES": {"证券代码": "code", "证券名称": "name",
                                      "持股数量": "quantity"},
        "YH_FILLS_COLUMN_ALIASES": {"成交日期": "date", "成交时间": "time",
                                    "业务名称": "side_raw", "证券代码": "code",
                                    "成交数量": "quantity", "成交价格": "price",
                                    "成交金额": "amount"},
        "_yh_ro_uia": lambda: (UIAC_NS, _ScanUia([])),
        "_yh_ro_chain_snapshot": lambda happ_exe: copy.deepcopy(BASE_CHAIN),
        "_yh_ro_session_id": lambda pid: 1,
        # D05 结构夹具：真实 WTS 行为由新 session contract 覆盖。
        "_yh_ro_session_state": lambda session_id: "WTSActive",
        "_yh_ro_input_state": lambda: {"idle_ms": 120000,
                                       "last_input_tick": 555,
                                       "foreground_hwnd": 100},
        "_yh_ro_find_query_window": lambda pid: (100, [0, 0, 800, 600]),
        "_yh_ro_visible_windows": lambda chain: {100},
        "_yh_ro_page_state": _page_state,
        "_yh_ro_identity_values": lambda hwnd: {"2322": "SYNTH_ID_ANCHOR"},
        "_yh_ro_window_state": lambda hwnd, pid: True,
        "_yh_ro_grid_items": lambda hwnd: (copy.deepcopy(HEADER_ONLY_ITEMS),
                                           14.0, {"synthetic": True}),
        # 注：_yh_ro_query_date_range 是被摘取的真实函数（ALLOWED_FUNCTIONS），
        # 装载后由真实定义落位；不提供 fake（overrides 白名单亦不含它）。
        "_class_name": lambda hwnd: "SYNTH_UNKNOWN_CLS",
        "yh_holdings_from_items": lambda items, row_delta=14.0, min_code_x=0.0: [],
        "yh_fills_from_items": lambda items, row_delta=14.0, min_code_x=0.0: [],
        "_yh_ro_header_band": lambda items, aliases, row_delta: ["code", "name",
                                                                 "quantity"],
        "yh_fills_summary_evidence": lambda items, row_delta=14.0, min_code_x=0.0:
            {"present": False},
        "yh_fills_summary_reconciled": lambda fev: True,
        "yh_single_snapshot": lambda proof: {"status": "ok"},
    }


# ── 受限 AST 装载（非通用沙箱；说明见文件头） ──────────────────────────
_TREE = None


def _tree():
    global _TREE
    if _TREE is None:
        _TREE = ast.parse(SOURCE.read_text(encoding="utf-8-sig"),
                          filename=str(SOURCE))
    return _TREE


def _const_ok(value):
    if isinstance(value, ast.Constant):
        return True
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return all(_const_ok(e) for e in value.elts)
    if isinstance(value, ast.Dict):
        return all(k is None or _const_ok(k) for k in value.keys) \
            and all(_const_ok(v) for v in value.values)
    if isinstance(value, ast.UnaryOp) and isinstance(value.op,
                                                     (ast.USub, ast.UAdd)):
        return _const_ok(value.operand)
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
            and isinstance(value.func.value, ast.Name)
            and value.func.value.id == "re" and value.func.attr == "compile"
            and not value.keywords):
        return all(isinstance(a, ast.Constant) for a in value.args)
    return False


def _prepare(fn, allowed_names, what):
    assert not fn.decorator_list, what + ": 禁止 decorator"
    assert not getattr(fn, "type_params", []), what + ": 禁止 type 参数"
    fn.returns = None
    for node in ast.walk(fn):
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            assert not getattr(node, "decorator_list", []), what + ": 禁止 decorator"
            assert not getattr(node, "type_params", []), what + ": 禁止 type 参数"
            if isinstance(node, ast.FunctionDef):
                node.returns = None
                for default in list(node.args.defaults) \
                        + [d for d in node.args.kw_defaults if d is not None]:
                    if isinstance(default, ast.Constant):
                        continue
                    if isinstance(default, ast.Name) \
                            and default.id in allowed_names:
                        continue
                    raise AssertionError(what + ": 默认参数仅允许字面量/白名单名")
        if isinstance(node, ast.arg):
            node.annotation = None
        if isinstance(node, ast.AnnAssign):
            node.annotation = ast.Constant(value=None)
    local = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            local.add(node.id)
        elif isinstance(node, ast.arg):
            local.add(node.arg)
        elif isinstance(node, ast.FunctionDef):
            local.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local.add(node.name)
        elif isinstance(node, ast.alias):
            local.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, ast.comprehension):
            pass
    for target in [t for node in ast.walk(fn)
                   if isinstance(node, (ast.comprehension,))
                   for t in ([node.target] if node.target else [])]:
        for n in ast.walk(target):
            if isinstance(n, ast.Name):
                local.add(n.id)
    for node in ast.walk(fn):
        kind = type(node)
        assert kind in NODE_OK, what + ": 未审 AST 节点 " + kind.__name__
        if isinstance(node, ast.Name):
            assert not node.id.startswith("__"), what + ": 禁止双下划线名称"
            assert node.id not in FORBIDDEN_NAMES, what + ": 禁止名称 " + node.id
            if isinstance(node.ctx, ast.Load):
                assert node.id in local or node.id in allowed_names, \
                    what + ": 未审外部依赖 " + node.id
        elif isinstance(node, ast.Attribute):
            if isinstance(node.ctx, ast.Store):
                assert not node.attr.startswith("__"), what + ": 禁止属性写入"
            else:
                assert node.attr in ATTR_OK, what + ": 未审属性 ." + node.attr
        elif isinstance(node, ast.Call):
            assert isinstance(node.func, (ast.Name, ast.Attribute)), \
                what + ": 禁止动态调用表达式"
            for keyword in node.keywords:
                assert keyword.arg is not None, what + ": 禁止 ** 展开"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in IMPORT_OK, what + ": 未审 import " + alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert node.level == 0 and module in IMPORT_FROM_OK, \
                what + ": 未审 from-import " + module
            for alias in node.names:
                assert alias.name in IMPORT_FROM_OK[module], \
                    what + ": 未审 from-import 名 " + alias.name
    return fn


def _load(overrides=None, uia_backend=None):
    """摘取装载：真实 allowlist 函数 + 合成依赖 + 私有严格 importer。

    overrides：仅允许固定 fake 缝隙键（OVERRIDABLE_SEAMS），在真实定义
    装载完成后应用（先装载后覆盖，真实被测函数绝不先被替身顶替）。
    uia_backend：_yh_uia_texts 的 comtypes 场景选择——None=能力缺席
    （importer 一律 ImportError）；给定 fake UIA 后端=正常 COM 场景。
    """
    tree = _tree()
    consts, funcs, methods = {}, {}, {}
    optional = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ALLOWED_CONSTANTS:
                    assert target.id not in consts, "常量重复赋值 " + target.id
                    consts[target.id] = node
        elif isinstance(node, ast.FunctionDef):
            if node.name in ALLOWED_FUNCTIONS:
                assert node.name not in funcs, "顶层重复定义 " + node.name
                funcs[node.name] = node
            elif node.name == OPTIONAL_HELPER:
                assert len(optional) == 0, "helper 重复定义"
                optional.append(node)
    missing = ([n for n in ALLOWED_FUNCTIONS if n not in funcs]
               + [n for n in ALLOWED_CONSTANTS if n not in consts])
    assert not missing, "源码缺少 allowlist 定义（源码漂移？）: %s" % missing
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "_YhReadonlyAdapter"]
    assert len(classes) == 1, "_YhReadonlyAdapter 类缺失/重复"
    for member in classes[0].body:
        if isinstance(member, ast.FunctionDef) and member.name in METHOD_LOAD:
            assert member.name not in methods, "方法重复 " + member.name
            methods[member.name] = member
    assert set(methods) == set(METHOD_LOAD), \
        "adapter 方法缺失: %s" % sorted(set(METHOD_LOAD) - set(methods))
    if overrides:
        bad = sorted(set(overrides) - OVERRIDABLE_SEAMS)
        assert not bad, "overrides 仅允许固定 fake 缝隙键: %s" % bad
    ns = {"__builtins__": dict(SAFE_BUILTINS)}
    # 私有严格 __import__：摘取函数内 import 语句的就地解析器（绝不触
    # 真实 sys.modules；见 _make_strict_importer）。
    ns["__builtins__"]["__import__"] = _make_strict_importer(uia_backend)
    ns.update(_base_fakes())
    allowed_names = (set(ns) | set(ALLOWED_FUNCTIONS) | set(METHOD_LOAD.values())
                     | set(ALLOWED_CONSTANTS) | set(SAFE_BUILTINS))
    body = []
    for name in ALLOWED_CONSTANTS:
        node = copy.deepcopy(consts[name])
        assert _const_ok(node.value), "常量 " + name + " 值超出字面量形态"
        body.append(node)
    for name in ALLOWED_FUNCTIONS:
        body.append(_prepare(copy.deepcopy(funcs[name]), allowed_names, name))
    for original, load_name in METHOD_LOAD.items():
        fn = _prepare(copy.deepcopy(methods[original]), allowed_names, original)
        fn.name = load_name
        body.append(fn)
    for helper in optional:
        body.append(_prepare(copy.deepcopy(helper), allowed_names, OPTIONAL_HELPER))
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "<isolated-readonly-adapter-regression>", "exec"), ns)
    if overrides:  # 装载后应用：真实定义优先落位，注入缝隙显式可见
        ns.update(overrides)
    return ns


# ── 回归测试（现行实现应暴露缺陷 = RED；修复后转 GREEN） ────────────────
class ReadonlyAdapterRegressionTests(unittest.TestCase):

    def test_01_scan_gates_on_controltype_password_before_any_name_read(self):
        """缺陷1：_yh_ro_uia_scan 先读 CurrentName 后读 IsPassword，且对
        密码/禁用控件也读 Name。期望：IsPassword/ControlType 先行；密码与
        禁用元素 Name 读取数为 0（Value 本函数不读，亦不得引入）。"""
        journal = []
        elements = _scan_elements(journal)
        ns = _load({"_yh_ro_uia": lambda: (UIAC_NS, _ScanUia(elements))})
        items = ns["_yh_ro_uia_scan"](4242)[3]
        self.assertEqual(len(items), 4, "对照：真实函数确实遍历了 fake 树")
        for eid in ("E2_PWD_EDIT", "E3_DISABLED"):
            self.assertNotIn((eid, "name"), journal,
                             eid + "：密码/禁用元素绝不读 Name")
        name_idx = [i for i, entry in enumerate(journal)
                    if entry == ("E1_TREEITEM", "name")]
        pwd_idx = [i for i, entry in enumerate(journal)
                   if entry == ("E1_TREEITEM", "pwd")]
        self.assertTrue(name_idx and pwd_idx and pwd_idx[0] < name_idx[0],
                        "IsPassword 必须先于 CurrentName 读取")

    def test_02_constructor_rejects_initially_present_unknown_window(self):
        """缺陷2：构造把既有未知顶层窗纳入 baseline。期望：绑定即存在
        未知顶层窗（如 #32770）必须构造失败（fail-closed）。"""
        ns = _load({"_yh_ro_visible_windows": lambda chain: {100, 777}})
        with self.assertRaises(SyntheticThsError,
                               msg="构造期存在未知顶层窗必须拒绝绑定"):
            ns["adapter_init"](SimpleNamespace(), happ_exe=EXE)
        control = _load()  # 对照：仅已知面板窗 → 构造成功，断言非 fake 破坏
        holder = SimpleNamespace()
        control["adapter_init"](holder, happ_exe=EXE)
        self.assertEqual(holder._hwnd, 100)

    def test_03_guard_rejects_preexisting_unknown_window_in_baseline(self):
        """缺陷2：guard 只比较可见窗集合变化，基线内未知 #32770 可通过。
        期望：即使窗口集合与 baseline 相同，存在未知顶层窗也必须 fail-closed。"""
        ns = _load({"_yh_ro_visible_windows": lambda chain: {100, 777}})
        fake_self = SimpleNamespace(
            _happ_exe=EXE, _chain=copy.deepcopy(BASE_CHAIN), _hwnd=100,
            _baseline_windows={100, 777}, _initial_tick=555,
            _initial_foreground=100)
        guard = ns["adapter__guard"](fake_self)
        self.assertIs(guard.get("ok"), False,
                      "基线内未知窗口必须令守卫拒绝：" + repr(guard.get("reason")))

    def test_04_child_dialogs_read_failure_must_propagate(self):
        """缺陷2：_yh_ro_child_dialogs 的属性读取异常被 continue 吞掉。
        注入点=真正处于源码 try 内的属性 getter（IsOffscreen/ClassName/
        AutomationId），而非 try 外的 GetElement（在 GetElement 抛错属
        假绿）。被测真函数=_yh_ro_child_dialogs；_yh_ro_uia 为 fake 缝隙。
        期望：读取失败必须抛出（fail-closed），绝不静默跳过。"""
        failing = _DialogEl(fail=True)
        normal = _DialogEl()
        found = _FakeFound(lambda i: failing if i == 1 else normal, 2)
        uia = SimpleNamespace(
            ElementFromHandle=lambda h: _ScanRoot(found),
            CreatePropertyCondition=lambda p, v: ("cond", p, v))
        ns = _load({"_yh_ro_uia": lambda: (UIAC_NS, uia)})
        with self.assertRaises(SyntheticThsError,
                               msg="属性读取失败必须传播，不得 continue"):
            ns["_yh_ro_child_dialogs"](4242)
        ok_found = _FakeFound(lambda i: normal, 1)  # 对照：正常读取
        ok_uia = SimpleNamespace(
            ElementFromHandle=lambda h: _ScanRoot(ok_found),
            CreatePropertyCondition=lambda p, v: ("cond", p, v))
        ns_ok = _load({"_yh_ro_uia": lambda: (UIAC_NS, ok_uia)})
        self.assertEqual(len(ns_ok["_yh_ro_child_dialogs"](4242)), 1)

    def test_05_desktop_state_reports_active_session_evidence(self):
        """缺陷3（结构检查，不能替代真实 WTS 行为验收）：_yh_ro_desktop_state
        输出无 WTS 会话状态。期望：输出必须携带会话状态证据且为 Active。"""
        ns = _load()
        state = ns["_yh_ro_desktop_state"]()
        self.assertEqual(state.get("input_desktop"), "Default")
        self.assertEqual(state.get("thread_desktop"), "Default")
        self.assertIn("session_state", state,
                      "桌面态缺会话状态证据（键名契约见报告；等价字段可议）")
        self.assertEqual(state["session_state"], "WTSActive")

    def test_06_guard_compares_thread_desktop_and_active_session(self):
        """缺陷3：guard 未比 thread_desktop、无会话 Active 证明。期望：
        线程桌面漂移或会话非 Active 都必须 fail-closed。被测真函数=
        adapter__guard；_yh_ro_desktop_state 为批准缝隙（overrides 注入
        场景字典），其余依赖 base fakes。"""
        scenarios = (
            ("control_all_clear",
             {"input_desktop": "Default", "thread_desktop": "Default",
              "session_id": 1, "session_state": "WTSActive"}, True),
            ("thread_desktop_mismatch",
             {"input_desktop": "Default",
              "thread_desktop": "SYNTH_SCREENSAVER_DESKTOP",
              "session_id": 1, "session_state": "WTSActive"}, False),
            ("session_disconnected",
             {"input_desktop": "Default", "thread_desktop": "Default",
              "session_id": 1, "session_state": "WTSDisconnected"}, False),
        )
        for label, desktop, should_ok in scenarios:
            with self.subTest(scenario=label):
                ns = _load({"_yh_ro_desktop_state": lambda d=desktop: dict(d)})
                fake_self = SimpleNamespace(
                    _happ_exe=EXE, _chain=copy.deepcopy(BASE_CHAIN), _hwnd=100,
                    _baseline_windows={100}, _initial_tick=555,
                    _initial_foreground=100)
                guard = ns["adapter__guard"](fake_self)
                self.assertIs(guard.get("ok"), should_ok,
                              label + "：" + repr(guard.get("reason")))

    def test_07_holdings_header_only_is_neither_complete_nor_empty(self):
        """缺陷4：无分页文字→pages_complete=True；仅表头无数据行→
        empty_confirmed=True。期望：无可靠总数/分页证据不得 complete，
        读不到行 ≠ 空仓（empty_confirmed 必须 False，行原样保留）。"""
        ns = _load()
        fake_self = SimpleNamespace(_require_page=lambda page: _page_state())
        result = ns["adapter_read_holdings"](fake_self)
        self.assertEqual(result["holdings"], [])      # 对照：行集确为空
        self.assertTrue(result["headers"])            # 对照：表头确已取到
        self.assertIs(result["coverage_evidence"]["pages_complete"], False,
                      "无分页文字不得默认单页完整")
        self.assertIs(result["coverage_complete"], False,
                      "无总数/分页证据不得宣称覆盖完整")
        self.assertIs(result["empty_confirmed"], False,
                      "仅表头无行不得确认空仓")

    def test_08_fills_counter_evidence_downgrades_real_report(self):
        """缺陷4：read_fills 的 coverage_evidence 反证被 yh_fills_verdict
        忽略、readonly_report 升级 complete/ok。期望：显式覆盖反证必须
        优先降级（非 ok、fills_complete=False），已知行全部保留。"""
        ns = _load()
        rows = [
            {"date": "2042-04-05", "time": "10:12:34", "side": "buy",
             "code": "990001", "quantity": 2.0, "price": 3.0, "amount": 6.0,
             "fee": 0.5, "stamp_tax": 0.0, "net_amount": -6.5},
            {"date": "2042-04-06", "time": "11:13:45", "side": "sell",
             "code": "990001", "quantity": -2.0, "price": 3.5, "amount": 7.0,
             "fee": 0.5, "stamp_tax": 0.0, "net_amount": 6.2},
        ]
        fills_fev = {
            "attempted": True, "rows": rows, "row_count_read": 2,
            "count_total": 2, "page_current": 1, "page_total": 1,
            "next_page_present": False, "summary": {"present": False},
            "header_columns": ["date", "time", "side_raw", "code", "quantity",
                               "price", "amount"],
            "date_range": {"start": "2042-04-05", "end": "2042-04-06"},
            "date_range_source": "query_controls_readback",
            # 显式反证：视口纵向/横向/分页覆盖均未确认（如横向滚动存在）
            "coverage_evidence": {"rows_complete": False,
                                  "columns_complete": False,
                                  "pages_complete": False},
        }
        verdict = ns["yh_fills_verdict"](copy.deepcopy(fills_fev))
        self.assertEqual(verdict["row_count"], 2, "对照：真实裁决已接收行集")
        evidence = {
            "expected_account": ACCOUNT, "identity_before": ACCOUNT,
            "identity_after": ACCOUNT, "abort_status": None,
            "funds": {"balance": 17.0, "total_assets": 41.0},
            "funds_labels": {}, "funds_as_of": "2042-04-06",
            "funds_coverage_complete": True, "funds_evidence": {},
            "holdings": [{"code": "990001", "quantity": 7}],
            "headers": ["code", "quantity"], "holdings_as_of": "2042-04-06",
            "holdings_evidence": {"ocr_available": True},
            "coverage_complete": True, "empty_confirmed": False,
            "coverage_evidence": {}, "fills": fills_fev,
        }
        report = ns["_yh_readonly_report"](evidence, with_fills=True)
        self.assertEqual(report["account"], ACCOUNT)   # 对照：链路归属正常
        self.assertNotEqual(report["status"], "ok",
                            "显式覆盖反证必须令整体降级，不得 ok")
        self.assertIs(report["coverage"]["fills_complete"], False,
                      "fills_complete 不得被计数/日期齐全升级")
        self.assertIs(report["fills"]["coverage_complete"], False)
        self.assertEqual(report["fills"]["rows"], rows,
                         "降级也必须保留全部已知 partial 行")

    def test_09_holdings_capture_error_propagates_not_partial(self):
        """缺陷5：read_holdings 把 ThsXiadanError 吞成 ocr_unavailable
        partial。期望：真实读取/采集异常必须向上传播（由 collector 全撤销）。"""
        ns = _load({"_yh_ro_grid_items": _raising_grid})
        fake_self = SimpleNamespace(_require_page=lambda page: _page_state())
        with self.assertRaises(SyntheticThsError,
                               msg="PrintWindow/OCR 失败必须传播，不得返回 partial"):
            ns["adapter_read_holdings"](fake_self)

    def test_10_fills_capture_error_propagates_not_partial(self):
        """缺陷5：read_fills 同样吞 ThsXiadanError 成 base partial 继续。
        期望：采集异常必须传播，绝不变造 partial 证据继续下一页。"""
        ns = _load({"_yh_ro_grid_items": _raising_grid})
        fake_self = SimpleNamespace(_require_page=lambda page: _page_state())
        with self.assertRaises(SyntheticThsError,
                               msg="PrintWindow/OCR 失败必须传播，不得返回 partial"):
            ns["adapter_read_fills"](fake_self)

    def test_11_uia_texts_distinguishes_capability_missing_from_read_error(self):
        """缺陷5：_yh_uia_texts 吞全部异常。期望：comtypes 缺席（能力缺失）
        如实返回 []；真实读取异常（元素枚举中途失败）必须传播。被测真
        函数=_yh_uia_texts；comtypes 场景由装载期私有严格 importer 选择
        （uia_backend 参数），本测试零 sys.modules 读写。"""
        ns_missing = _load()  # 缺席场景：私有 importer 一律 ImportError
        self.assertEqual(ns_missing["_yh_uia_texts"](4242), [],
                         "能力缺失可如实降级为空清单")
        found = _FakeFound(
            lambda i: _TextEl("资金余额") if i == 0 else _TextEl(
                "SYNTH_FAIL", fail=True), 2)
        uia = SimpleNamespace(
            ElementFromHandle=lambda h: _ScanRoot(found),
            CreatePropertyCondition=lambda p, v: ("cond", p, v))
        ns = _load(uia_backend=uia)  # 正常 COM 场景：枚举中途读取失败
        with self.assertRaises(RuntimeError,
                               msg="UIA 读取异常不得吞为空清单"):
            ns["_yh_uia_texts"](4242)

    def test_12_collector_revokes_and_stops_navigation_on_capture_error(self):
        """缺陷5（组合真实 collector + 真实 read_holdings）：采集异常被吞后
        collector 继续导航历史成交并保留资金。期望：捕获失败 → 全链撤销
        （abort、资金清空、fills 未尝试、无后续导航）。"""
        ns = _load({"_yh_ro_grid_items": _raising_grid})
        events = []
        holdings_self = SimpleNamespace(
            _require_page=lambda page: _page_state())

        class CollectorAdapter:
            def guard(self):
                events.append("guard")
                return {"ok": True, "owner_active": False}

            def identity(self):
                events.append("identity")
                return {"account": ACCOUNT, "fingerprint": "SYNTH_FP"}

            def select_query(self, page):
                events.append("select:" + page)
                return {"ok": True, "selected": True,
                        "page_confirmed": True, "page": page}

            def read_funds(self):
                events.append("read_funds")
                return {"funds": {"balance": 17.0, "total_assets": 41.0},
                        "funds_labels": {}, "as_of": "2042-04-06",
                        "coverage_complete": True}

            def read_holdings(self):
                events.append("read_holdings")
                return ns["adapter_read_holdings"](holdings_self)

            def read_fills(self):
                events.append("read_fills")
                return {"attempted": True, "rows": [], "header_columns": [],
                        "date_range": None, "date_range_source": None,
                        "coverage_complete": False, "coverage_evidence": {},
                        "empty_confirmed": False}

        evidence = ns["_yh_collect_with_adapter"](CollectorAdapter(), ACCOUNT,
                                                  with_fills=True)
        self.assertEqual(evidence["expected_account"], ACCOUNT)  # 对照
        self.assertTrue(evidence["abort_status"],
                        "采集异常必须撤销全链（abort_status 非空）")
        self.assertEqual(evidence["funds"], {}, "撤销后不得保留资金数据")
        self.assertEqual(evidence["holdings"], [])
        self.assertIs(evidence["fills"]["attempted"], False,
                      "撤销后 fills 必须未尝试")
        self.assertNotIn("select:历史成交", events, "失败后不得继续导航")
        self.assertNotIn("read_fills", events, "失败后不得读取成交页")

    def test_13_date_range_unbound_controls_zero_value_reads(self):
        """缺陷6：对通用 Edit/ComboBox 仅凭值形态读 Value。期望：角色/父级
        未验证的控件绝不读 Value（读取数 0），结果 (None, None)。"""
        journal = []
        ns = _load({
            "_class_name": lambda h: {7001: "Edit", 7002: "Edit"}.get(
                int(h), "SYNTH_UNKNOWN_CLS"),
            "_yh_ro_uia": lambda: (UIAC_NS, _DateUia(
                {7001: "2026-11-03", 7002: "2026-11-12"}, journal)),
        })
        ctypes_fake = ns["ctypes"]
        ctypes_fake.windll.user32.child_hwnds = [7001, 7002]
        ctypes_fake.windll.user32.ctrl_ids = {7001: 7001, 7002: 7002}
        result = ns["_yh_ro_query_date_range"](8800)
        self.assertEqual([e for e in journal if e[0] == "value_read"], [],
                         "未绑定控件绝不读 Value")
        self.assertEqual(result, (None, None),
                         "未绑定控件值不得构成查询范围证据")

    def test_14_date_range_reversed_and_ambiguous_ranges_rejected(self):
        """缺陷6：sorted(ranges[0]) 把反向区间修成合法、多组歧义任取其一。
        期望：反向区间与多组歧义一律 (None, None)，绝不排序修补/取舍。"""
        cases = (
            ("reversed_single_range",
             {7101: ("ComboBox", "2026-11-12~2026-11-03")}),
            ("ambiguous_two_groups",
             {7201: ("SysDateTimePick32", "2026-11-03~2026-11-10"),
              7202: ("ComboBox", "2026-11-05~2026-11-08")}),
        )
        for label, children in cases:
            with self.subTest(scenario=label):
                journal = []
                names = {hwnd: cls for hwnd, (cls, _v) in children.items()}
                values = {hwnd: value for hwnd, (_c, value) in children.items()}
                ns = _load({
                    "_class_name": lambda h, n=names: n.get(
                        int(h), "SYNTH_UNKNOWN_CLS"),
                    "_yh_ro_uia": lambda v=values, j=journal: (
                        UIAC_NS, _DateUia(v, j)),
                })
                ctypes_fake = ns["ctypes"]
                ctypes_fake.windll.user32.child_hwnds = sorted(children)
                ctypes_fake.windll.user32.ctrl_ids = {
                    int(h): int(h) for h in children}
                result = ns["_yh_ro_query_date_range"](8800)
                self.assertEqual(result, (None, None),
                                 label + "：反向/歧义必须拒绝，不得推算")

    def test_15_bound_date_range_pure_helper_contract(self):
        """缺陷6-接口（下一切片新增小纯 helper；缺失即 RED，接口见文件头）：
        仅绑定角色输入：同日单查询合法、反向拒绝、多组/未知角色拒绝。"""
        ns = _load()
        self.assertIn(OPTIONAL_HELPER, ns,
                      "缺少纯解析入口 " + OPTIONAL_HELPER + "（接口见文件头；"
                      "本测试不要求读任意 Edit，只约束已绑定角色值）")
        if OPTIONAL_HELPER not in ns:
            return
        helper = ns[OPTIONAL_HELPER]
        self.assertEqual(
            helper([("start", "2042-04-06"), ("end", "2042-04-06")]),
            ({"start": "2042-04-06", "end": "2042-04-06"},
             "query_controls_readback"))
        self.assertEqual(
            helper([("start", "2042-04-07"), ("end", "2042-04-06")]),
            (None, None))
        self.assertEqual(
            helper([("start", "2042-04-05"), ("start", "2042-04-06"),
                    ("end", "2042-04-07")]),
            (None, None))
        self.assertEqual(helper([("edit", "2042-04-06")]), (None, None))

    def test_16_holdings_single_page_requires_matching_explicit_count(self):
        """E1b：真实 read_holdings + yh_fills_page_evidence；仅 grid seam
        提供合成表头/分页/计数文本，其余沿用既有内存 fake（行集为空）。
        三场景都有显式单页，不让缺分页掩盖缺 count；正数与空行集冲突
        同样不得 complete/empty，明确零条作为防恒拒绝正对照。"""
        cases = (
            ("missing_count_single_page", (), None, False),
            ("count_conflicts_with_empty_rows", ("共1条",), 1, False),
            ("explicit_zero_count_single_page", ("共0条",), 0, True),
        )
        for label, count_texts, expected_count, should_complete in cases:
            with self.subTest(scenario=label):
                items = copy.deepcopy(HEADER_ONLY_ITEMS)
                items.extend(
                    {"text": text, "x": 60.0, "y": 420.0}
                    for text in ("第1页/共1页",) + count_texts)
                ns = _load({
                    "_yh_ro_grid_items": lambda hwnd, data=items: (
                        copy.deepcopy(data), 14.0, {"synthetic": True}),
                })
                # 用真实 parser 校验夹具，不 mock 分页/计数证据。
                page = ns["yh_fills_page_evidence"](items)
                self.assertEqual((page["page_current"], page["page_total"]),
                                 (1, 1), "每个场景均须解析到明确单页")
                self.assertIs(page["next_page_present"], False)
                self.assertEqual(page["count_total"], expected_count)
                fake_self = SimpleNamespace(
                    _require_page=lambda page: _page_state())
                result = ns["adapter_read_holdings"](fake_self)
                self.assertEqual(result["holdings"], [],
                                 "缺 count/计数冲突也须原样保留已读行集")
                self.assertTrue(result["headers"], "表头已存在，不以缺表头拒绝")
                coverage = result["coverage_evidence"]
                self.assertIs(coverage["rows_complete"], True)
                self.assertIs(coverage["columns_complete"], True)
                self.assertIs(coverage["pages_complete"], True,
                              "显式单页证据不得与未知分页混淆")
                self.assertEqual(
                    (result["coverage_complete"], result["empty_confirmed"]),
                    (should_complete, should_complete),
                    "空行集须有匹配的明确零条证据才可 complete/empty")

    def test_17_uia_texts_import_initialization_runtime_error_propagates(self):
        """E1b：真实 _yh_uia_texts 初始 import 的非缺包异常必须传播。
        只替换新 ns 的私有 importer；不改全局 builtins/sys.modules，
        不委托真实 import。test_11 保留 ImportError 缺包降级对照。"""
        ns = _load()

        def fail_import(name, globals=None, locals=None, fromlist=(), level=0):
            raise RuntimeError("synthetic import initialization failed")

        ns["__builtins__"]["__import__"] = fail_import
        with self.assertRaisesRegex(
                RuntimeError, "^synthetic import initialization failed$"):
            ns["_yh_uia_texts"](4242)

    def test_18_holdings_current_page_beyond_total_is_not_complete_or_empty(self):
        """E1d：真实 read_holdings + 真实 yh_fills_page_evidence；仅覆盖
        grid seam，其余沿用既有内存 fake。共0条、完整表头、横纵覆盖
        均齐全时，第2页/共1页仍是分页反证，不得 complete/empty。"""
        items = copy.deepcopy(HEADER_ONLY_ITEMS)
        items.extend(
            {"text": text, "x": 60.0, "y": 420.0}
            for text in ("第2页/共1页", "共0条"))
        ns = _load({
            "_yh_ro_grid_items": lambda hwnd: (
                copy.deepcopy(items), 14.0, {"synthetic": True}),
        })
        page = ns["yh_fills_page_evidence"](items)
        self.assertEqual((page["page_current"], page["page_total"]), (2, 1),
                         "真实 parser 必须读到越界当前页，不以缺分页拒绝")
        self.assertEqual(page["count_total"], 0)
        self.assertIs(page["next_page_present"], False)
        fake_self = SimpleNamespace(_require_page=lambda page: _page_state())
        result = ns["adapter_read_holdings"](fake_self)
        self.assertEqual(result["holdings"], [])
        self.assertEqual(result["headers"], ["code", "name", "quantity"])
        self.assertIs(result["ocr_available"], True)
        coverage = result["coverage_evidence"]
        self.assertIs(coverage["rows_complete"], True)
        self.assertIs(coverage["columns_complete"], True)
        self.assertEqual(
            (result["coverage_complete"], result["empty_confirmed"]),
            (False, False), "当前页超出总页数不得 complete/empty")
        self.assertIs(coverage["pages_complete"], False,
                      "第2页/共1页不得确认为单页完整")

    def test_19_holdings_missing_quantity_header_is_not_complete_or_empty(self):
        """E1d：真实 read_holdings + 真实分页 parser；grid 提供明确1/1
        共0条，仅底层 header seam 返回场景表头（不替换 adapter）。
        code/name 缺 quantity 必须拒绝，完整表头正对照仍 complete/empty。"""
        items = copy.deepcopy(HEADER_ONLY_ITEMS)
        items.extend(
            {"text": text, "x": 60.0, "y": 420.0}
            for text in ("第1页/共1页", "共0条"))
        cases = (
            ("complete_headers", ("code", "name", "quantity"), True),
            ("missing_quantity_header", ("code", "name"), False),
        )
        for label, headers, should_complete in cases:
            with self.subTest(scenario=label):
                ns = _load({
                    "_yh_ro_grid_items": lambda hwnd: (
                        copy.deepcopy(items), 14.0, {"synthetic": True}),
                    "_yh_ro_header_band": lambda data, aliases, row_delta,
                    cols=headers: list(cols),
                })
                page = ns["yh_fills_page_evidence"](items)
                self.assertEqual(
                    (page["page_current"], page["page_total"]), (1, 1))
                self.assertEqual(page["count_total"], 0)
                self.assertIs(page["next_page_present"], False)
                fake_self = SimpleNamespace(
                    _require_page=lambda page: _page_state())
                result = ns["adapter_read_holdings"](fake_self)
                self.assertEqual(result["holdings"], [])
                self.assertEqual(result["headers"], list(headers),
                                 "真实 adapter 必须收到场景指定的表头")
                self.assertIs(result["ocr_available"], True)
                coverage = result["coverage_evidence"]
                self.assertIs(coverage["rows_complete"], True)
                self.assertIs(coverage["columns_complete"], True)
                self.assertIs(coverage["pages_complete"], True,
                              "不能以缺分页掩盖缺 quantity 表头")
                self.assertEqual(
                    (result["coverage_complete"], result["empty_confirmed"]),
                    (should_complete, should_complete),
                    "仅非空表头不足以确认完整/空仓，必须含 quantity")


if __name__ == "__main__":
    unittest.main(verbosity=2)
