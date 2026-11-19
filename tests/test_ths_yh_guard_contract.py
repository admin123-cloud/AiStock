"""Synthetic Owner guard contract; never import or operate the THS client.

Only the three named functions are extracted from the exact source path. Review
those functions again before running if the source has changed: exec with a
restricted namespace is not a security sandbox for untrusted Python source.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


_SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "ths_client.py"
_FUNCTIONS = ("_yh_input_idle_seconds", "_yh_owner_idle_seconds", "_yh_owner_using")


def _guard(input_result, *, last_input_tick=11000, self_tick=0.0,
           now_tick=12000, tick_error=False, tick_error_on_call=None,
           foreground=123, second_tick=None):
    """Build the real function chain with fake Win32 interfaces only."""
    class FakeUser32:
        def __init__(self):
            self.input_calls = 0
            self.foreground_calls = 0
            self.kernel32 = FakeKernel32()

        def GetForegroundWindow(self):
            self.foreground_calls += 1
            if foreground == "raise":
                raise OSError("synthetic GetForegroundWindow failure")
            return foreground

        def GetLastInputInfo(self, info):
            self.input_calls += 1
            if input_result == "raise":
                raise OSError("synthetic GetLastInputInfo failure")
            if not input_result:
                return 0
            info.dwTime = last_input_tick
            return 1

    class FakeKernel32:
        def __init__(self):
            self.tick_calls = 0

        def GetTickCount(self):
            self.tick_calls += 1
            if tick_error or self.tick_calls == tick_error_on_call:
                raise OSError("synthetic GetTickCount failure")
            if self.tick_calls == 2 and second_tick is not None:
                return second_tick
            return now_tick

    class FakeStructure:
        pass

    user32 = FakeUser32()
    windll = SimpleNamespace(user32=user32, kernel32=user32.kernel32)
    fake_ctypes = SimpleNamespace(Structure=FakeStructure, c_uint=int,
                                  sizeof=lambda _value: 8,
                                  byref=lambda value: value, windll=windll)

    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"), filename=str(_SOURCE))
    selected = []
    for name in _FUNCTIONS:
        matches = [node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == name]
        assert len(matches) == 1, f"expected exactly one top-level {name}"
        node = matches[0]
        # Do not evaluate annotations, decorators or default expressions at load.
        assert not node.decorator_list, f"review changed decorators on {name}"
        assert not node.args.defaults and not node.args.kw_defaults, (
            f"review changed defaults on {name}"
        )
        node.returns = None
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            arg.annotation = None
        if node.args.vararg:
            node.args.vararg.annotation = None
        if node.args.kwarg:
            node.args.kwarg.annotation = None
        selected.append(node)

    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    builtins = {"__build_class__": __build_class__, "Exception": Exception,
                "float": float, "int": int, "max": max, "round": round}
    env = {"__builtins__": builtins, "__name__": "synthetic_ths_guard",
           "ctypes": fake_ctypes, "USER32": user32,
           "_YH_LAST_SELF_INJECT_TICK": self_tick,
           "YH_OWNER_ACTIVE_IDLE_S": 90.0}
    exec(compile(module, str(_SOURCE), "exec"), env)
    return env["_yh_owner_using"], user32


@pytest.mark.parametrize("foreground", [0, None, -1, "raise"],
                         ids=["zero", "none", "invalid", "exception"])
def test_unknown_foreground_blocks_action(foreground):
    owner_using, user32 = _guard(True, foreground=foreground)
    assert owner_using(123) is True, "unknown foreground must block action"
    assert user32.foreground_calls == 1


@pytest.mark.parametrize("hwnd", [0, None, -1, "invalid"],
                         ids=["zero", "none", "negative", "non-numeric"])
def test_invalid_target_hwnd_blocks_action(hwnd):
    owner_using, _ = _guard(True)
    assert owner_using(hwnd) is True, "invalid target hwnd must block action"


@pytest.mark.parametrize("self_tick", [0.0, 10000.0], ids=["no-injection", "injected"])
def test_last_input_failure_return_blocks_action(self_tick):
    owner_using, user32 = _guard(False, self_tick=self_tick)
    assert owner_using(123) is True, "failed GetLastInputInfo must block action"
    assert user32.foreground_calls == 1 and user32.input_calls == 1


@pytest.mark.parametrize("self_tick", [0.0, 10000.0], ids=["no-injection", "injected"])
def test_last_input_exception_blocks_action(self_tick):
    owner_using, user32 = _guard("raise", self_tick=self_tick)
    assert owner_using(123) is True, "GetLastInputInfo exception must block action"
    assert user32.foreground_calls == 1 and user32.input_calls == 1


def test_tick_count_exception_blocks_action():
    owner_using, user32 = _guard(True, tick_error=True)
    assert owner_using(123) is True, "GetTickCount exception must block action"
    assert user32.foreground_calls == 1 and user32.input_calls == 1


def test_second_tick_count_exception_after_self_injection_blocks_action():
    owner_using, user32 = _guard(True, last_input_tick=10000, self_tick=10000.0,
                                 now_tick=120000, tick_error_on_call=2)
    result = owner_using(123)
    assert user32.kernel32.tick_calls == 2
    assert user32.foreground_calls == 1 and user32.input_calls == 1
    assert result is True, "second GetTickCount exception must block action"


@pytest.mark.parametrize(
    ("last_input_tick", "now_tick", "self_tick"),
    [
        (2147480000, -2147483648, 0.0),
        (2147480000, -2147483648, 2147480000.0),
        (-10000, 120000, 0.0),
        (4294967000, 100, 0.0),
        (4294967000, 100, 4294967000.0),
        (10000, 4294967296, 0.0),
        (4294967296, 120000, 10000.0),
    ],
    ids=["signed-now", "signed-now-after-injection", "negative-last-input",
         "wrap", "wrap-after-injection", "now-out-of-range", "input-out-of-range"],
)
def test_untrusted_input_clock_blocks_action(last_input_tick, now_tick, self_tick):
    owner_using, _ = _guard(True, last_input_tick=last_input_tick,
                            now_tick=now_tick, self_tick=self_tick)
    assert owner_using(123) is True, "untrusted input clock must block action"


@pytest.mark.parametrize(
    ("now_tick", "second_tick", "self_tick"),
    [(120000, 110000, 10000.0), (120000, -1, 10000.0),
     (120000, 5000, 10000.0)],
    ids=["clock-moves-backward", "second-tick-negative", "second-before-injection"],
)
def test_untrusted_second_tick_blocks_action(now_tick, second_tick, self_tick):
    owner_using, _ = _guard(True, last_input_tick=10000, now_tick=now_tick,
                            second_tick=second_tick, self_tick=self_tick)
    assert owner_using(123) is True, "untrusted second clock sample must block action"


def test_recent_and_old_input_keep_normal_behavior():
    owner_using, user32 = _guard(True, last_input_tick=11000)
    assert owner_using(123) is True
    assert user32.foreground_calls == 1 and user32.input_calls == 1

    owner_using, user32 = _guard(True, last_input_tick=10000, now_tick=120000)
    assert owner_using(123) is False
    assert user32.foreground_calls == 1 and user32.input_calls == 1


def test_recent_input_near_self_injection_blocks_action():
    owner_using, user32 = _guard(True, last_input_tick=10100, self_tick=10000.0)
    assert owner_using(123) is True, "input origin is not proven by injection timing"
    assert user32.foreground_calls == 1 and user32.input_calls == 1


@pytest.mark.parametrize(
    "last_input_tick",
    [10000, 10001, 10299, 10300, 10301],
    ids=["0ms", "1ms", "299ms", "300ms", "301ms"],
)
@pytest.mark.parametrize("now_tick", [11000, 13100], ids=["now-11000", "now-13100"])
def test_recent_input_after_self_injection_boundary_blocks_action(last_input_tick, now_tick):
    owner_using, user32 = _guard(True, last_input_tick=last_input_tick,
                                 self_tick=10000.0, now_tick=now_tick)
    assert owner_using(123) is True, "recent input must block regardless of injection timing"
    assert user32.foreground_calls == 1 and user32.input_calls == 1
