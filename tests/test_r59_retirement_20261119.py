# -*- coding: utf-8 -*-
"""R59 retirement: source-structure evidence only, not runtime proof.

Do not import or execute the scheduler.  The source is read only inside tests;
pytest's own configuration, conftest and plugins require separate review before
any collection or execution is authorised.
"""

import ast
from pathlib import Path


_LEGACY_REGISTER = "_register_tdx_postclose_minutes"
_LEGACY_JOB_CONSTANT = "TDX_POSTCLOSE_JOB_ID"
_LEGACY_JOB_ID = "tdx_postclose_minutes"


def _function(tree, name):
    matches = [node for node in tree.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == name]
    return matches[0] if len(matches) == 1 else None


def _serve_has_no_legacy_reference(tree):
    serve = _function(tree, "serve")
    if serve is None:
        return False
    for node in ast.walk(serve):
        if isinstance(node, ast.Name) and node.id in {
            _LEGACY_REGISTER, _LEGACY_JOB_CONSTANT,
        }:
            return False
        if isinstance(node, ast.Attribute) and node.attr in {
            _LEGACY_REGISTER, _LEGACY_JOB_CONSTANT,
        }:
            return False
        if isinstance(node, ast.Constant) and node.value in (
            _LEGACY_REGISTER, _LEGACY_JOB_CONSTANT, _LEGACY_JOB_ID,
        ):
            return False
    return True


def _legacy_entry_is_unconditional_raise(tree):
    entry = _function(tree, _LEGACY_REGISTER)
    if not isinstance(entry, ast.FunctionDef) or entry.decorator_list:
        return False
    args = entry.args
    if args.defaults or any(default is not None for default in args.kw_defaults):
        return False
    if entry.returns is not None or any(
        arg.annotation is not None
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs,
                    *((args.vararg,) if args.vararg else ()),
                    *((args.kwarg,) if args.kwarg else ()))
    ):
        return False
    body = entry.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Raise):
        return False
    raise_stmt = body[0]
    exc = raise_stmt.exc
    return (
        raise_stmt.cause is None
        and isinstance(exc, ast.Call)
        and isinstance(exc.func, ast.Name)
        and exc.func.id == "RuntimeError"
        and len(exc.args) == 1
        and isinstance(exc.args[0], ast.Constant)
        and isinstance(exc.args[0].value, str)
        and bool(exc.args[0].value)
        and not exc.keywords
    )


def _scheduler_tree():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pipeline_scheduler.py"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_serve_does_not_reference_legacy_registration():
    assert _serve_has_no_legacy_reference(_scheduler_tree()), (
        "serve still references the R59 registration or job identity"
    )


def test_direct_legacy_registration_is_unconditionally_fail_closed():
    assert _legacy_entry_is_unconditional_raise(_scheduler_tree()), (
        "legacy registration must contain only an unconditional literal RuntimeError"
    )


def test_serve_structure_controls():
    safe = "def serve():\n    register_postclose_job(scheduler)\n"
    unsafe = (
        "def serve():\n"
        "    register_postclose_job(scheduler)\n"
        "    _register_tdx_postclose_minutes(scheduler)\n"
    )
    assert _serve_has_no_legacy_reference(ast.parse(safe))
    assert not _serve_has_no_legacy_reference(ast.parse(unsafe))
    assert not _serve_has_no_legacy_reference(ast.parse(
        "def serve():\n    scheduler.add_job(job, id='tdx_postclose_minutes')\n"
    ))
    assert not _serve_has_no_legacy_reference(ast.parse("def other():\n    pass\n"))


def test_direct_entry_structure_controls():
    prefix = "def _register_tdx_postclose_minutes(scheduler):\n"
    assert _legacy_entry_is_unconditional_raise(ast.parse(
        prefix + "    \"\"\"Retired.\"\"\"\n    raise RuntimeError('retired')\n"
    ))
    for body in (
        "    pass\n",
        "    if flag:\n        raise RuntimeError('retired')\n",
        "    scheduler.add_job(job)\n    raise RuntimeError('retired')\n",
        "    try:\n        raise RuntimeError('retired')\n    except RuntimeError:\n        pass\n",
        "    raise RuntimeError(side_effect())\n",
    ):
        assert not _legacy_entry_is_unconditional_raise(ast.parse(prefix + body))
    assert not _legacy_entry_is_unconditional_raise(ast.parse(
        "@decorate\n" + prefix + "    raise RuntimeError('retired')\n"
    ))
    assert not _legacy_entry_is_unconditional_raise(ast.parse(
        "def _register_tdx_postclose_minutes(scheduler=side_effect()):\n"
        "    raise RuntimeError('retired')\n"
    ))
    assert not _legacy_entry_is_unconditional_raise(ast.parse("def other():\n    pass\n"))
