# -*- coding: utf-8 -*-
"""WTS session-state contract tests for the readonly Yuanhang adapter.

This file is intentionally test-only and is not executed in this task.  It
loads a narrow AST slice from the real source and supplies only in-memory
Win32/ctypes/os fakes.  It does not import the project or real ctypes, and it
does not use the older regression loader.
"""
import ast
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "ths_client.py"
SOURCE_SHA256 = (
    "89061e83e4c80aa8897f7fc8e09bbfba109f947e9670867162d5f1eae292adb9"
)
BASELINE_D_SHA256 = (
    "9f36321e790bf414338a7e91853f44e2d621e068f9baea57a4bfd789a97ab286"
)
BASELINE_HEAD = "132fe6683ba62205964eb7e73b9eb829cfa62e89"

HELPER = "_yh_ro_session_state"
EXTRACT_FUNCTIONS = (
    "_yh_ro_session_id",
    "_yh_ro_desktop_state",
    HELPER,
)
EXTRACT_METHODS = {
    "__init__": "adapter_init",
    "_guard": "adapter__guard",
    "guard": "adapter_guard",
}
EXTRACT_CONSTANTS = ("_YH_RO_OWNER_IDLE_MS", "_YH_RO_QUERY_PAGES")


class SyntheticThsError(Exception):
    """Replacement for the production fail-closed exception."""


class _FakeScalar:
    def __init__(self, value=None):
        self.value = value

    def __bool__(self):
        return self.value is not None


class _FakeUnicodeBuffer:
    def __init__(self, size):
        self.value = ""
        self.size = int(size)


class _FakeApi:
    """Callable Win32 export which accepts argtypes/restype assignment."""

    def __init__(self, fn):
        self._fn = fn
        self.argtypes = None
        self.restype = None

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


class _FakeWts:
    """One-query WTS fake; every scenario is isolated to one instance."""

    def __init__(self, *, result=True, state=0, length=4, nonnull=True):
        self.result = bool(result)
        self.state = state
        self.length = int(length)
        self.nonnull = bool(nonnull)
        self.query_calls = []
        self.freed = []
        self.returned_buffer = None
        self.WTSQuerySessionInformationW = _FakeApi(self._query)
        self.WTSFreeMemory = _FakeApi(self._free)

    def _query(self, server, session_id, info_class, buffer_out, length_out):
        is_null = server is None or server == 0 or (
            hasattr(server, "value") and server.value in (None, 0)
        )
        self.query_calls.append((None if is_null else server,
                                 int(session_id), int(info_class)))
        if not self.result:
            return 0
        length_out.value = self.length
        if self.nonnull:
            self.returned_buffer = buffer_out
            buffer_out.value = _FakeScalar(self.state)
        else:
            self.returned_buffer = None
            buffer_out.value = None
        return 1

    def _same_buffer(self, pointer):
        if self.returned_buffer is None:
            raw = pointer.value if hasattr(pointer, "value") else pointer
            return raw in (None, 0)
        payload = self.returned_buffer.value
        return (pointer is self.returned_buffer
                or pointer is payload
                or getattr(pointer, "value", None) is payload)

    def _free(self, pointer):
        if not self._same_buffer(pointer):
            raise AssertionError("WTSFreeMemory pointer is not the returned buffer")
        if self.returned_buffer is not None and self.freed:
            raise AssertionError("WTSFreeMemory called twice for one buffer")
        if self.returned_buffer is None:
            return 0
        self.freed.append(pointer)
        return 0


class _FakeKernel32:
    def __init__(self, session_id):
        self.session_id = int(session_id)
        self.ProcessIdToSessionId = _FakeApi(self._process_id_to_session)
        self.GetCurrentThreadId = _FakeApi(lambda: 777)

    def _process_id_to_session(self, pid, out_sid):
        out_sid.value = self.session_id
        return 1


class _FakeUser32:
    def __init__(self, *, thread_name="Default"):
        self.thread_name = thread_name
        self.closed = []
        self.OpenInputDesktop = _FakeApi(lambda flags, inherit, access: "INPUT")
        self.CloseDesktop = _FakeApi(self._close)
        self.GetThreadDesktop = _FakeApi(lambda thread_id: "THREAD")
        self.GetUserObjectInformationW = _FakeApi(self._desktop_name)

    def _close(self, handle):
        self.closed.append(handle)
        return 1

    def _desktop_name(self, handle, index, buf, size, needed):
        buf.value = "Default" if handle == "INPUT" else self.thread_name
        needed.value = len(buf.value) + 1
        return 1


class _FakeCtypes:
    """Small ctypes-shaped object; no real ctypes symbols are imported."""

    def __init__(self, *, session_id=23, wts=None, thread_name="Default"):
        self.wts = wts or _FakeWts()
        self.windll = SimpleNamespace(
            kernel32=_FakeKernel32(session_id),
            user32=_FakeUser32(thread_name=thread_name),
            wtsapi32=self.wts,
        )
        self.c_void_p = _FakeScalar
        self.c_int = _FakeScalar

    @staticmethod
    def POINTER(ctype):
        return ("POINTER", ctype)

    @staticmethod
    def byref(value):
        return value

    @staticmethod
    def cast(value, pointer_type):
        raw = value.value if hasattr(value, "value") else value
        if isinstance(raw, _FakeScalar):
            return SimpleNamespace(contents=raw)
        return SimpleNamespace(contents=_FakeScalar(raw))

    @staticmethod
    def sizeof(value):
        return 4

    @staticmethod
    def create_unicode_buffer(size):
        return _FakeUnicodeBuffer(size)


class _FakeWintypes:
    DWORD = _FakeScalar
    BOOL = int


def _tree():
    return ast.parse(SOURCE.read_text(encoding="utf-8-sig"), filename=str(SOURCE))


def _literal(node):
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_literal(item) for item in node.elts)
    return False


def _prepare(node, allowed_names, label):
    """Strip annotations and reject imports/decorators/top-level dependencies."""
    node = copy.deepcopy(node)
    for item in ast.walk(node):
        if isinstance(item, (ast.FunctionDef, ast.Lambda)):
            assert not getattr(item, "decorator_list", []), label + ": decorator"
            if isinstance(item, ast.FunctionDef):
                item.returns = None
                for default in list(item.args.defaults) + [
                    value for value in item.args.kw_defaults if value is not None
                ]:
                    if isinstance(default, ast.Constant):
                        continue
                    if isinstance(default, ast.Name) and default.id in allowed_names:
                        continue
                    raise AssertionError(label + ": unapproved default argument")
                for arg in list(item.args.posonlyargs) + list(item.args.args):
                    arg.annotation = None
                for arg in item.args.kwonlyargs:
                    arg.annotation = None
                if item.args.vararg:
                    item.args.vararg.annotation = None
                if item.args.kwarg:
                    item.args.kwarg.annotation = None
        if isinstance(item, ast.arg):
            item.annotation = None
        if isinstance(item, ast.AnnAssign):
            item.annotation = ast.Constant(value=None)
        if isinstance(item, (ast.Import, ast.ImportFrom)):
            raise AssertionError(label + ": imports are forbidden in the AST slice")

    local_names = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
            local_names.add(item.id)
        elif isinstance(item, ast.arg):
            local_names.add(item.arg)
        elif isinstance(item, ast.FunctionDef):
            local_names.add(item.name)
        elif isinstance(item, ast.ExceptHandler) and item.name:
            local_names.add(item.name)

    allowed_attrs = {
        "windll", "kernel32", "user32", "wtsapi32", "ProcessIdToSessionId",
        "GetCurrentThreadId", "OpenInputDesktop", "CloseDesktop",
        "GetThreadDesktop", "GetUserObjectInformationW",
        "WTSQuerySessionInformationW", "WTSFreeMemory", "argtypes", "restype",
        "c_void_p", "c_int", "POINTER", "byref", "cast", "sizeof", "DWORD",
        "BOOL", "getpid", "_guard", "contents",
        "create_unicode_buffer", "value", "get", "items", "append",
        "input_desktop", "thread_desktop", "session_id", "idle_ms",
        "last_input_tick", "foreground_hwnd", "xiadan", "pid", "ok",
        "owner_active", "reason", "detail", "_happ_exe", "_chain",
        "_hwnd", "_initial_tick", "_initial_foreground", "_baseline_windows",
        "_panel_rect", "_page", "_identity_values",
    }
    safe_names = {
        "ctypes", "wintypes", "os", "ThsXiadanError", "YUANHANG_EXE_DEFAULT",
        "_yh_ro_session_id", "_yh_ro_session_state", "_yh_ro_desktop_state",
        "_yh_ro_chain_snapshot", "_yh_ro_chain_equal", "_yh_ro_input_state",
        "_yh_ro_find_query_window", "_yh_ro_visible_windows", "_yh_ro_page_state",
        "_yh_ro_identity_values", "_yh_ro_window_state", "_yh_ro_child_dialogs",
        "_YH_RO_OWNER_IDLE_MS", "_YH_RO_QUERY_PAGES", "int", "str", "dict",
        "len", "isinstance", "Exception", "True", "False", "None",
    }
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load):
            assert item.id in local_names or item.id in safe_names or item.id in allowed_names, \
                label + ": unapproved name " + item.id
        if isinstance(item, ast.Attribute) and item.attr not in allowed_attrs:
            raise AssertionError(label + ": unapproved attribute " + item.attr)
    return node


def _load(*, wts=None, session_id=23, thread_name="Default",
          desktop_state_override=None):
    """Compile only the selected real definitions with synthetic globals."""
    tree = _tree()
    functions = {}
    constants = {}
    methods = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in EXTRACT_FUNCTIONS:
            functions[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in EXTRACT_CONSTANTS:
                    constants[target.id] = node
    classes = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_YhReadonlyAdapter"
    ]
    assert len(classes) == 1, "_YhReadonlyAdapter class missing or duplicated"
    for node in classes[0].body:
        if isinstance(node, ast.FunctionDef) and node.name in EXTRACT_METHODS:
            methods[node.name] = node

    # Do not turn a missing new helper into a loader error: its tests must RED
    # with an explicit assertIn while the old desktop/init/guard slice loads.
    for name in ("_yh_ro_session_id", "_yh_ro_desktop_state"):
        assert name in functions, "required existing function missing: " + name
    assert set(methods) == set(EXTRACT_METHODS), "adapter method slice incomplete"
    assert set(constants) == set(EXTRACT_CONSTANTS), "required constants missing"

    fake_wts = wts or _FakeWts()
    fake_ctypes = _FakeCtypes(
        session_id=session_id, wts=fake_wts, thread_name=thread_name
    )
    chain = {
        "session_id": int(session_id),
        "happ": {"pid": 11, "ppid": 1, "image": "SYNTH/happ.exe"},
        "xiadan": {"pid": 22, "ppid": 11, "image": "SYNTH/xiadan.exe"},
        "plus": {"pid": 33, "ppid": 22, "image": "SYNTH/plus.exe"},
    }
    ns = {
        "__builtins__": {
            "int": int, "str": str, "dict": dict, "len": len,
            "isinstance": isinstance, "Exception": Exception,
        },
        "ctypes": fake_ctypes,
        "wintypes": _FakeWintypes,
        "os": SimpleNamespace(getpid=lambda: 4242),
        "ThsXiadanError": SyntheticThsError,
        "YUANHANG_EXE_DEFAULT": "SYNTH_HAPP.EXE",
        "_yh_ro_chain_snapshot": lambda happ_exe: copy.deepcopy(chain),
        "_yh_ro_chain_equal": lambda left, right: left == right,
        "_yh_ro_input_state": lambda: {
            "idle_ms": 120000, "last_input_tick": 88,
            "foreground_hwnd": 100,
        },
        "_yh_ro_find_query_window": lambda pid: (100, [0, 0, 800, 600]),
        "_yh_ro_visible_windows": lambda current_chain: {100},
        "_yh_ro_page_state": lambda hwnd: {
            "selected_page": "资金股票", "page_marker_confirmed": True,
        },
        "_yh_ro_identity_values": lambda hwnd: {"2322": "SYNTH_ACCOUNT"},
        "_yh_ro_window_state": lambda hwnd, pid: True,
        "_yh_ro_child_dialogs": lambda hwnd: [],
        "_YH_RO_OWNER_IDLE_MS": 90000,
        "_YH_RO_QUERY_PAGES": ("资金股票", "历史成交"),
        "_fake_wts": fake_wts,
    }
    body = []
    for name in EXTRACT_CONSTANTS:
        node = constants[name]
        assert _literal(node.value), "constant is not literal: " + name
        body.append(copy.deepcopy(node))
    allowed_names = set(ns) | set(EXTRACT_FUNCTIONS) | set(EXTRACT_METHODS.values())
    for name in EXTRACT_FUNCTIONS:
        if name in functions:
            body.append(_prepare(functions[name], allowed_names, name))
    for original, loaded_name in EXTRACT_METHODS.items():
        method = _prepare(methods[original], allowed_names, original)
        method.name = loaded_name
        body.append(method)
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "<isolated-ths-session-contract>", "exec"), ns)
    if desktop_state_override is not None:
        ns["_yh_ro_desktop_state"] = desktop_state_override
    return ns


class ReadonlySessionContractTests(unittest.TestCase):
    def _require_helper(self, ns):
        self.assertIn(
            HELPER,
            ns,
            "RED: missing _yh_ro_session_state(session_id) helper; "
            "the session-state contract is not implemented",
        )
        return ns[HELPER]

    def _adapter_self(self):
        return SimpleNamespace(
            _happ_exe="SYNTH_HAPP.EXE",
            _chain={
                "session_id": 23,
                "happ": {"pid": 11, "ppid": 1, "image": "SYNTH/happ.exe"},
                "xiadan": {"pid": 22, "ppid": 11, "image": "SYNTH/xiadan.exe"},
                "plus": {"pid": 33, "ppid": 22, "image": "SYNTH/plus.exe"},
            },
            _hwnd=100,
            _baseline_windows={100},
            _initial_tick=88,
            _initial_foreground=100,
        )

    def test_01_new_session_helper_exists_with_clear_red(self):
        """The new helper is an explicit RED contract, not a loader failure."""
        ns = _load()
        self.assertIn(
            HELPER, ns,
            "RED: SOURCE lacks _yh_ro_session_state(session_id); "
            "desktop/init/guard session-state wiring cannot be verified",
        )

    def test_02_active_queries_connect_state_8_and_releases_once(self):
        """Active=0 is accepted only after a single NULL/sid/8 WTS query."""
        wts = _FakeWts(result=True, state=0, length=4, nonnull=True)
        ns = _load(wts=wts, session_id=23)
        helper = self._require_helper(ns)
        if helper is None:
            return
        self.assertEqual(helper(37), "WTSActive")
        self.assertEqual(wts.query_calls, [(None, 37, 8)])
        self.assertEqual(len(wts.freed), 1, "non-null WTS buffer must be freed once")
        self.assertTrue(wts._same_buffer(wts.freed[0]),
                        "WTSFreeMemory must receive the returned buffer")

    def test_03_legal_non_active_state_can_name_or_reject_but_is_not_active(self):
        """A legal disconnected state must never become WTSActive."""
        wts = _FakeWts(result=True, state=4, length=4, nonnull=True)
        ns = _load(wts=wts, session_id=23)
        helper = self._require_helper(ns)
        if helper is None:
            return
        try:
            result = helper(23)
        except SyntheticThsError:
            result = None
        self.assertNotEqual(result, "WTSActive")
        self.assertEqual(wts.query_calls, [(None, 23, 8)])
        self.assertEqual(len(wts.freed), 1)

    def test_04_failure_empty_short_and_illegal_payloads_reject_and_free_safely(self):
        """Error/empty/short/illegal payloads fail closed with one free at most."""
        scenarios = (
            ("query_failure", dict(result=False, state=0, length=0, nonnull=False), 0),
            ("empty_buffer", dict(result=True, state=0, length=4, nonnull=False), 0),
            ("short_buffer", dict(result=True, state=0, length=2, nonnull=True), 1),
            ("illegal_state", dict(result=True, state=99, length=4, nonnull=True), 1),
        )
        for label, kwargs, expected_frees in scenarios:
            with self.subTest(scenario=label):
                wts = _FakeWts(**kwargs)
                ns = _load(wts=wts, session_id=23)
                helper = self._require_helper(ns)
                if helper is None:
                    continue
                with self.assertRaises(
                    SyntheticThsError,
                    msg=label + " must be rejected by the session helper",
                ):
                    helper(23)
                self.assertEqual(wts.query_calls, [(None, 23, 8)])
                self.assertEqual(len(wts.freed), expected_frees)

    def test_05_desktop_state_reports_session_state_for_the_same_session(self):
        """The real desktop helper must expose WTS state beside the same sid."""
        wts = _FakeWts(result=True, state=0, length=4, nonnull=True)
        ns = _load(wts=wts, session_id=23)
        state = ns["_yh_ro_desktop_state"]()
        self.assertEqual(state["input_desktop"], "Default")
        self.assertEqual(state["thread_desktop"], "Default")
        self.assertEqual(state["session_id"], 23)
        self.assertIn("session_state", state)
        self.assertEqual(state["session_state"], "WTSActive")
        self.assertEqual(wts.query_calls, [(None, 23, 8)])

    def test_06_constructor_rejects_missing_session_status(self):
        """Binding must not pass when desktop evidence lacks session_state."""
        ns = _load(
            wts=_FakeWts(result=True, state=0, length=4, nonnull=True),
            desktop_state_override=lambda: {
                "input_desktop": "Default", "thread_desktop": "Default",
                "session_id": 23,
            },
        )
        with self.assertRaises(
            SyntheticThsError,
            msg="constructor must reject missing WTS session-state evidence",
        ):
            ns["adapter_init"](SimpleNamespace(), happ_exe="SYNTH_HAPP.EXE")

    def test_07_constructor_rejects_disconnected_session(self):
        """A real WTSDisconnected desktop state is never bindable."""
        ns = _load(wts=_FakeWts(result=True, state=4, length=4, nonnull=True))
        with self.assertRaises(
            SyntheticThsError,
            msg="constructor must reject WTSDisconnected",
        ):
            ns["adapter_init"](SimpleNamespace(), happ_exe="SYNTH_HAPP.EXE")

    def test_08_constructor_rejects_thread_desktop_drift(self):
        """Default input desktop is insufficient when the thread desktop drifts."""
        ns = _load(
            wts=_FakeWts(result=True, state=0, length=4, nonnull=True),
            thread_name="SYNTH_OTHER_DESKTOP",
        )
        with self.assertRaises(
            SyntheticThsError,
            msg="constructor must reject input/thread desktop mismatch",
        ):
            ns["adapter_init"](SimpleNamespace(), happ_exe="SYNTH_HAPP.EXE")

    def test_09_guard_rejects_missing_disconnected_or_thread_drift_and_accepts_active(self):
        """Guard has three negative session/desktop cases and one active control."""
        scenarios = (
            (
                "missing_status",
                _FakeWts(result=True, state=0, length=4, nonnull=True),
                "Default", False,
                lambda: {
                    "input_desktop": "Default", "thread_desktop": "Default",
                    "session_id": 23,
                },
            ),
            ("disconnected", _FakeWts(result=True, state=4, length=4, nonnull=True),
             "Default", False, None),
            ("thread_drift", _FakeWts(result=True, state=0, length=4, nonnull=True),
             "OTHER", False, None),
            ("active_control", _FakeWts(result=True, state=0, length=4, nonnull=True),
             "Default", True, None),
        )
        for label, wts, thread_name, should_pass, desktop_override in scenarios:
            with self.subTest(scenario=label):
                ns = _load(
                    wts=wts,
                    thread_name=thread_name,
                    desktop_state_override=desktop_override,
                )
                result = ns["adapter__guard"](self._adapter_self())
                self.assertIs(
                    result.get("ok"), should_pass,
                    label + ": unexpected guard result " + repr(result),
                )

    def test_10_constructor_accepts_active_session_positive_control(self):
        """A valid Active session must let the real constructor bind."""
        wts = _FakeWts(result=True, state=0, length=4, nonnull=True)
        ns = _load(wts=wts, session_id=23)
        holder = SimpleNamespace()
        ns["adapter_init"](holder, happ_exe="SYNTH_HAPP.EXE")
        self.assertEqual(holder._hwnd, 100)
        self.assertEqual(wts.query_calls, [(None, 23, 8)])
        self.assertEqual(len(wts.freed), 1)
        self.assertTrue(wts._same_buffer(wts.freed[0]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
