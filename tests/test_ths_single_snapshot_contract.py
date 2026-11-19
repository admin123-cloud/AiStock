"""Test-first contract for the single-account read-account vertical slice.

SAFETY: no project imports, GUI, account, network, DB, runtime or report access.
Only this test file and scripts/ths_client.py are read. The decision function
alone is compiled after a restrictive AST check, with synthetic builtin data
and limited builtins. This is not a general-purpose security sandbox. No route
or collector is executed; these tests do not approve real collection.

Proposed seam: run_read_account passes collected evidence to yh_single_snapshot,
where the collector obtains evidence without switching, and yh_single_snapshot
is a side-effect-free decision function. Its evidence is a dict with keys
expected_account, identity_before, identity_after, stock_page_confirmed,
ocr_available, headers, coverage_complete, empty_confirmed, funds, holdings.
Identity tokens are canonical synthetic labels, never broker account numbers.
Required headers: code, quantity. Each holding must have a six-digit code and a
nonnegative integer quantity. Funds must include balance and total_assets.
Success returns status=ok, rc=0, account, funds and holdings. An unproven
snapshot returns non-ok, nonzero rc, funds=None and holdings=None. Empty
holdings are valid only when explicitly confirmed with complete coverage.
The existing allow_switch keyword remains compatible but the new automatic
single-account collection must not actively switch accounts. Explicit switching
and historical chains are not targeted for removal. Static checks cannot prove
execution, control-flow dominance or the absence of indirect switching; actual
route wiring, collector evidence quality and side effects need separate dynamic
verification under explicit authorization.
"""

import ast
import copy

from pathlib import Path
from typing import Iterator, TypeGuard

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "ths_client.py"
HELPER = "yh_single_snapshot"
COLLECTOR = "_yh_collect_single_snapshot"


def _tree():
    return ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))


def _function(tree, name):
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(nodes) == 1, f"contract requires exactly one top-level {name}"
    return nodes[0]


def _local_nodes(fn: ast.FunctionDef) -> Iterator[ast.AST]:
    """Walk the route body but do not mistake deferred nested bodies for calls."""
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


# An intentionally small Python subset: no imports, attribute introspection,
# mutation through methods, nested functions, decorators or arbitrary calls.
_ALLOWED_NODES = {
    ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.AnnAssign,
    ast.If, ast.For, ast.Break, ast.Continue, ast.Pass, ast.Expr,
    ast.Name, ast.Load, ast.Store, ast.Constant, ast.Dict, ast.List, ast.Tuple,
    ast.Set, ast.Subscript, ast.Slice, ast.IfExp, ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not, ast.USub, ast.UAdd, ast.BinOp, ast.Add, ast.Sub,
    ast.Mult, ast.Div, ast.Mod, ast.Compare, ast.Eq, ast.NotEq, ast.Is,
    ast.IsNot, ast.In, ast.NotIn, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Call, ast.keyword, ast.Attribute, ast.ListComp, ast.SetComp,
    ast.DictComp, ast.GeneratorExp, ast.comprehension,
}
_SAFE_BUILTINS = {"len": len, "isinstance": isinstance, "all": all, "any": any,
                  "str": str, "int": int, "float": float, "bool": bool,
                  "list": list, "dict": dict, "set": set, "tuple": tuple,
                  "sorted": sorted, "sum": sum}


def _load_decision(tree):
    fn = copy.deepcopy(_function(tree, HELPER))
    assert not fn.decorator_list and not fn.args.defaults and not fn.args.kw_defaults, (
        "decision must have no decorators or defaults")
    assert len(fn.args.args) == 1 and not fn.args.posonlyargs
    assert not fn.args.kwonlyargs and not fn.args.vararg and not fn.args.kwarg
    # Annotations are not needed for this synthetic execution and may evaluate
    # arbitrary expressions; remove them before AST validation and compilation.
    fn.returns = None
    fn.args.args[0].annotation = None
    evidence_name = fn.args.args[0].arg
    assert evidence_name not in _SAFE_BUILTINS and not evidence_name.startswith("__")
    for node in ast.walk(fn):
        assert type(node) in _ALLOWED_NODES, f"unsafe decision syntax: {type(node).__name__}"
        if isinstance(node, ast.FunctionDef):
            assert node is fn, "nested functions and their defaults/decorators are forbidden"
        if isinstance(node, ast.Name):
            assert not node.id.startswith("__"), "dunder name forbidden"
            if isinstance(node.ctx, ast.Store):
                assert node.id != evidence_name and node.id not in _SAFE_BUILTINS, (
                    "cannot rebind evidence or allowed builtins")
        if isinstance(node, ast.Subscript):
            assert isinstance(node.ctx, ast.Load), "no subscript mutation"
        if isinstance(node, ast.Attribute):
            assert (node.attr == "get" and isinstance(node.ctx, ast.Load)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == evidence_name), (
                "only get on the un-rebound synthetic evidence dict is permitted")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id in _SAFE_BUILTINS, "non-allowlisted call"
            else:
                assert (isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                        and not node.keywords and 1 <= len(node.args) <= 2), (
                            "only evidence.get(key[, default]) calls allowed")
            assert all(k.arg is not None for k in node.keywords), "no ** expansion"
    env = {"__builtins__": _SAFE_BUILTINS.copy()}
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    exec(compile(module, "<safe-single-snapshot>", "exec"), env)
    return env[HELPER]


def _invoke_decision(evidence):
    """Execute only the restricted pure decision, never the route."""
    return _load_decision(_tree())(copy.deepcopy(evidence))


def _complete():
    return {"expected_account": "acct1", "identity_before": "acct1",
            "identity_after": "acct1", "stock_page_confirmed": True,
            "ocr_available": True, "headers": ["code", "quantity"],
            "coverage_complete": True, "empty_confirmed": False,
            "funds": {"balance": 123.0, "total_assets": 456.0},
            "holdings": [{"code": "600001", "quantity": 100}]}


def _assert_unavailable(result):
    assert result["status"] != "ok"
    assert result["rc"] != 0
    assert result.get("funds") is None
    assert result.get("holdings") is None, "unknown must not be represented as an empty position"


# Functional assertions: decision only, using synthetic evidence.
def test_complete_synthetic_evidence_produces_usable_snapshot():
    evidence = _complete()
    result = _invoke_decision(evidence)
    assert result["status"] == "ok" and result["rc"] == 0
    assert result["account"] == "acct1"
    assert result["funds"] == evidence["funds"]
    assert result["holdings"] == evidence["holdings"]


@pytest.mark.parametrize("field,value", [
    ("identity_before", None), ("identity_after", None),
    ("identity_before", "acct2"), ("identity_after", "acct2"),
    ("expected_account", None),
    ("stock_page_confirmed", False), ("stock_page_confirmed", None),
    ("ocr_available", False), ("ocr_available", None),
    ("headers", []), ("headers", ["code"]), ("headers", None),
    ("coverage_complete", False), ("coverage_complete", None),
    ("funds", {}), ("funds", {"balance": 123.0}),
    ("holdings", None),
])
def test_unproven_evidence_is_not_success_or_empty_position(field, value):
    evidence = _complete()
    evidence[field] = value
    _assert_unavailable(_invoke_decision(evidence))


@pytest.mark.parametrize("rows", [
    [],
    [{"code": "600001"}],
    [{"code": "not-a-code", "quantity": 100}],
    [{"code": "600001", "quantity": -1}],
    [{"code": "600001", "quantity": 100}, {"code": "broken"}],
])
def test_unconfirmed_empty_or_incomplete_rows_fail_closed(rows):
    evidence = _complete()
    evidence["holdings"] = rows
    _assert_unavailable(_invoke_decision(evidence))


def test_proven_empty_position_is_distinguished_from_unavailable():
    evidence = _complete()
    evidence.update(holdings=[], empty_confirmed=True)
    result = _invoke_decision(evidence)
    assert result["status"] == "ok" and result["rc"] == 0
    assert result["holdings"] == [] and result["funds"] == evidence["funds"]


@pytest.mark.parametrize("field,value", [
    ("coverage_complete", False), ("coverage_complete", None),
    ("identity_after", "acct2"), ("stock_page_confirmed", False),
    ("ocr_available", False), ("headers", ["code"]),
])
def test_empty_confirmation_cannot_replace_missing_evidence(field, value):
    evidence = _complete()
    evidence.update(holdings=[], empty_confirmed=True)
    evidence[field] = value
    _assert_unavailable(_invoke_decision(evidence))


@pytest.mark.parametrize("source", [
    "def yh_single_snapshot(e):\n def inner(x=danger()):\n  return x\n return {}",
    "def yh_single_snapshot(e):\n @danger()\n def inner():\n  return {}\n return {}",
    "@danger()\ndef yh_single_snapshot(e):\n return {}",
    "def yh_single_snapshot(e=danger()):\n return {}",
    "def yh_single_snapshot(e):\n x = {}\n return x.get('a')",
    "def yh_single_snapshot(e):\n e = {}\n return e.get('a')",
    "def yh_single_snapshot(e):\n len = e.get\n return len('a')",
])
def test_restricted_loader_rejects_unsafe_synthetic_shapes(source):
    # These definitions are parsed only. Rejection must occur before compile/exec.
    with pytest.raises(AssertionError):
        _load_decision(ast.parse(source))


# Structural assertions: static evidence of routing, not a claim that the
# real collector is safe, complete, or free from indirect switching.
def test_structure_cli_targets_actual_read_account_route():
    tree = _tree()
    cmd = _function(tree, "cmd_read_account")
    calls = [n for n in ast.walk(cmd) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "run_read_account"]
    assert len(calls) == 1, "CLI handler must invoke run_read_account exactly once"
    parser = _function(tree, "build_parser")
    body = parser.body
    selected = False
    bound = False
    for stmt in body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == "p"
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Attribute)
                and stmt.value.func.attr == "add_parser"):
            selected = bool(stmt.value.args and isinstance(stmt.value.args[0], ast.Constant)
                            and stmt.value.args[0].value == "read-account")
        if (selected and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Attribute)
                and isinstance(stmt.value.func.value, ast.Name)
                and stmt.value.func.value.id == "p"
                and stmt.value.func.attr == "set_defaults"):
            bound = any(k.arg == "func" and isinstance(k.value, ast.Name)
                        and k.value.id == "cmd_read_account" for k in stmt.value.keywords)
            if bound:
                break
    assert bound, "read-account parser must bind cmd_read_account"


def _named_call(node: ast.AST | None, name: str) -> TypeGuard[ast.Call]:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == name)


def test_structure_route_calls_decision_with_collected_evidence():
    """Require V2 readonly adapter route call/data-flow pattern."""
    route = _function(_tree(), "run_read_account")
    nodes = list(_local_nodes(route))
    decisions = [n for n in nodes if _named_call(n, "_yh_readonly_report")]
    collectors = [n for n in nodes if _named_call(n, "_yh_collect_with_adapter")]
    assert decisions and collectors, "route must call _yh_collect_with_adapter and _yh_readonly_report"
    assigned = {target.id: n.value for n in nodes if isinstance(n, ast.Assign)
                and _named_call(n.value, "_yh_collect_with_adapter") for target in n.targets
                if isinstance(target, ast.Name)}
    assert any(
        len(call.args) == 1
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id in assigned
        for call in decisions
    ), "_yh_readonly_report must receive collector evidence"


def test_structure_preserves_legacy_allow_switch_keyword_signature():
    route = _function(_tree(), "run_read_account")
    defaults = dict(zip((arg.arg for arg in route.args.kwonlyargs), route.args.kw_defaults))
    assert "allow_switch" in defaults
    assert isinstance(defaults["allow_switch"], ast.Constant)
    assert defaults["allow_switch"].value is False


def test_structure_new_path_does_not_directly_reference_switch_or_legacy_chains():
    tree = _tree()
    prohibited = {"run_switch_account", "cmd_switch_account", "switch_account",
                  "run_export", "run_export_multi", "run_daily", "cmd_daily",
                  "cmd_export_fills", "cmd_poll_holdings", "cmd_poll_once"}
    for name in ("run_read_account", COLLECTOR):
        fn = _function(tree, name)
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        assert not ((names | attrs) & prohibited), f"{name} references a prohibited entrypoint"
        # API compatibility does not enable automatic switching on this path.
        assert not any(isinstance(n, ast.keyword) and n.arg in {"allow_switch", "switch"}
                       for n in ast.walk(fn)), f"{name} must not forward switch options"



