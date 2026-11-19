"""First offline-only slice: one pure simulated order, not a broker integration.

The deliberately narrow AST allowlist is a structural safety check, not a
proof about the rest of ths_client.py or its production wiring.
"""

import ast
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "ths_client.py"
FUNCTION_NAME = "simulate_order_pure"
ARGUMENTS = ("symbol", "side", "quantity", "price")
RESULT_KEYS = {
    "symbol", "side", "quantity", "price", "status", "submitted", "broker_order_id"
}


def _extract_pure_function():
    # Parse the file as data; never import or execute its module-level code.
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    matches = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == FUNCTION_NAME
    ]
    assert matches, f"Missing required top-level function: {FUNCTION_NAME}"
    assert len(matches) == 1, f"Expected exactly one top-level {FUNCTION_NAME}"
    function = matches[0]
    assert type(function) is ast.FunctionDef, "Simulation must be synchronous"
    assert not function.decorator_list, "Decorators could execute external code"
    assert function.returns is None and function.type_comment is None
    assert not getattr(function, "type_params", []), "Type parameters are not allowed"

    arguments = function.args
    assert not arguments.posonlyargs and not arguments.kwonlyargs
    assert arguments.vararg is None and arguments.kwarg is None
    assert not arguments.defaults and not arguments.kw_defaults
    assert tuple(arg.arg for arg in arguments.args) == ARGUMENTS
    assert all(arg.annotation is None and arg.type_comment is None for arg in arguments.args)

    # Only a single literal dictionary return is permitted: no calls, imports,
    # attributes, subscripts, callbacks, paths, or other executable statements.
    assert len(function.body) == 1 and isinstance(function.body[0], ast.Return)
    result = function.body[0].value
    assert isinstance(result, ast.Dict), "Return must be a literal dictionary"
    assert all(isinstance(key, ast.Constant) and type(key.value) is str for key in result.keys)
    keys = [key.value for key in result.keys]
    assert len(keys) == len(RESULT_KEYS) and set(keys) == RESULT_KEYS
    for value in result.values:
        assert (
            isinstance(value, ast.Name)
            and isinstance(value.ctx, ast.Load)
            and value.id in ARGUMENTS
        ) or (
            isinstance(value, ast.Constant)
            and type(value.value) in (str, int, float, bool, type(None))
        ), "Result values must be scalar literals or direct input names"

    isolated_module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = {"__builtins__": {}}
    exec(compile(isolated_module, str(SOURCE), "exec"), namespace)
    return namespace[FUNCTION_NAME]


def test_one_normal_order_is_simulated_without_submission():
    simulate = _extract_pure_function()
    result = simulate(symbol="600000", side="buy", quantity=100, price=10.5)
    assert type(result) is dict
    assert result == {
        "symbol": "600000",
        "side": "buy",
        "quantity": 100,
        "price": 10.5,
        "status": "simulated",
        "submitted": False,
        "broker_order_id": None,
    }
    assert result["submitted"] is False
    assert result["broker_order_id"] is None
