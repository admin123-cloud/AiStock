"""Slice 2B.1 — preferred_source explicit-null test (test-only, 2026-11-05).

STRUCTURAL CHECK ONLY: verifies that the single ``self.preferred_source``
assignment in ``KlineSyncer.__init__`` coalesces
``sync_settings.get("preferred_source")`` with an empty-string fallback
BEFORE the ``str(...).strip().lower()`` normalization chain, so an explicit
YAML ``null`` cannot be stringified into the literal source name ``"none"``.

Accepted shape (the minimal safe expression from the task brief)::

    self.preferred_source = str(
        sync_settings.get("preferred_source") or ""
    ).strip().lower()

The ``or`` coalescing is pinned to exactly two operands: operand 0 the
``sync_settings.get("preferred_source")`` call, operand 1 the
empty-string constant ``""``. A reversed ``"" or
sync_settings.get("preferred_source")`` does not coalesce an explicit
``None`` (it yields ``None`` and ``str(None) == "none"``) and is rejected,
as are extra ``or`` operands and missing/renamed ``get`` calls. The pre-fix
expression ``str(sync_settings.get("preferred_source", "")).strip().lower()``
stringified an explicit ``None`` as ``"none"`` via ``str(None)``; the
current required shape coalesces the ``get`` result with ``or ""`` before
``str()``.

Scope discipline (per task brief
``20261105_qmt_slice2b1_preferred_source_null_test.md``):
- No AiStock modules are imported and no application source is executed,
  compiled, or evaluated beyond stdlib ``ast`` parsing; the fixed project
  root is ``Path(__file__).parents[1]`` and the only file read is
  ``scripts/sync_all_klines.py``.
- The AST check inspects ONLY ``KlineSyncer.__init__``'s single
  ``self.preferred_source`` assignment; it is structural evidence for this
  normalization contract only, not for source routing or end-to-end
  behaviour.
- Assertions are fixed; no parsed config objects or unrelated values are
  printed.
"""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "sync_all_klines.py"


def _is_preferred_source_get(node: ast.AST) -> bool:
    """True only for the bare ``sync_settings.get("preferred_source")`` call
    shape (no ``...`` positional defaults or implied keyword defaults).
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sync_settings"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "preferred_source"
        and not node.keywords
    )


def _is_empty_string(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == ""


def _preferred_source_value() -> ast.expr:
    """Return the value of the single direct top-level ``self.preferred_source``
    assignment in ``KlineSyncer.__init__``.

    The descendant scan first counts every syntactic write/delete target
    (``ast.Store``/``ast.Del``) for ``self.preferred_source`` — simple/nested
    ``Assign``, ``AnnAssign``, ``AugAssign``, destructuring targets, and
    ``del`` — and requires exactly one. The value is then extracted only from
    a direct top-level ``Assign`` or valued ``AnnAssign`` whose target is
    exactly ``self.preferred_source``; the scan is syntactic only and does
    not detect reflective ``setattr``/``__dict__`` mutation.
    """
    tree = ast.parse(
        SCRIPT_PATH.read_text(encoding="utf-8"), filename=str(SCRIPT_PATH)
    )

    syncer = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "KlineSyncer"
        ),
        None,
    )
    assert syncer is not None, "scripts/sync_all_klines.py: class KlineSyncer not found"

    init = next(
        (
            node
            for node in syncer.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        ),
        None,
    )
    assert init is not None, "scripts/sync_all_klines.py: KlineSyncer.__init__ not found"

    write_targets = [
        node
        for node in ast.walk(init)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr == "preferred_source"
        and isinstance(node.ctx, (ast.Store, ast.Del))
    ]
    assert len(write_targets) == 1, (
        "scripts/sync_all_klines.py: KlineSyncer.__init__ must contain exactly "
        "one syntactic write/delete target for self.preferred_source — "
        "simple/nested Assign, AnnAssign, AugAssign, destructuring target, "
        "or del (found %d)" % len(write_targets)
    )

    values = []
    for stmt in init.body:
        if isinstance(stmt, ast.Assign):
            candidates = [(target, stmt.value) for target in stmt.targets]
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            candidates = [(stmt.target, stmt.value)]
        else:
            candidates = []
        for target, value in candidates:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr == "preferred_source"
            ):
                values.append(value)

    assert len(values) == 1, (
        "scripts/sync_all_klines.py: KlineSyncer.__init__ must contain exactly "
        "one direct top-level assignment to self.preferred_source (Assign or "
        "valued AnnAssign with target exactly self.preferred_source; found %d)"
        % len(values)
    )
    return values[0]


def test_preferred_source_null_coalesced_before_str_normalization():
    """Explicit null must be coalesced to "" before str().strip().lower().

    ``str(sync_settings.get("preferred_source", ""))`` stringifies an explicit
    YAML ``null`` into the nonempty source name ``"none"``; the get() result
    must instead be coalesced with an empty-string fallback (``or ""``)
    before ``str(...)`` is applied.
    """
    value = _preferred_source_value()

    assert (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "lower"
    ), "scripts/sync_all_klines.py: self.preferred_source must end with .lower()"

    strip = value.func.value
    assert (
        isinstance(strip, ast.Call)
        and isinstance(strip.func, ast.Attribute)
        and strip.func.attr == "strip"
    ), "scripts/sync_all_klines.py: .lower() must wrap a .strip() call"

    to_str = strip.func.value
    assert (
        isinstance(to_str, ast.Call)
        and isinstance(to_str.func, ast.Name)
        and to_str.func.id == "str"
    ), "scripts/sync_all_klines.py: normalization must begin with str(<coalesced value>)"

    assert len(to_str.args) == 1 and not to_str.keywords, (
        "scripts/sync_all_klines.py: str() must take exactly one positional "
        "argument"
    )
    coalesced = to_str.args[0]

    assert isinstance(coalesced, ast.BoolOp) and isinstance(coalesced.op, ast.Or), (
        'scripts/sync_all_klines.py: sync_settings.get("preferred_source") '
        'must be coalesced with an empty-string fallback via `or` BEFORE '
        'str(), e.g. str(sync_settings.get("preferred_source") or "")'
    )
    assert len(coalesced.values) == 2, (
        "scripts/sync_all_klines.py: the `or` coalescing must have exactly "
        'two operands — sync_settings.get("preferred_source") and "" '
        "(found %d)" % len(coalesced.values)
    )
    assert _is_preferred_source_get(coalesced.values[0]), (
        'scripts/sync_all_klines.py: `or` operand 0 must be the '
        'sync_settings.get("preferred_source") call itself; a leading '
        '"" would let an explicit None pass through as str(None) == "none"'
    )
    assert _is_empty_string(coalesced.values[1]), (
        'scripts/sync_all_klines.py: `or` operand 1 must be the empty-string '
        'constant "" so an explicit None from get() coalesces to ""'
    )


def test_is_preferred_source_get_rejects_positional_default():
    """get() with an extra positional default must not match the helper.

    ``_is_preferred_source_get`` must only accept the bare
    ``sync_settings.get("preferred_source")`` call shape. A second positional
    argument (a truthy default such as a source name) could bypass the
    empty-string ``or ""`` coalescing when the key is missing, so the helper
    must reject it. The snippet is hardcoded AST input only.
    """
    node = ast.parse(
        'sync_settings.get("preferred_source", "sentinel-red-2b1")', mode="eval"
    ).body
    assert not _is_preferred_source_get(node), (
        '_is_preferred_source_get must not accept '
        'sync_settings.get("preferred_source", <default>): a truthy default '
        'would bypass the or-"" empty-string fallback when the key is missing'
    )


def test_is_preferred_source_get_rejects_keyword_default():
    """get() with a ``default=`` keyword must not match the helper.

    Same false-green as the positional case: a truthy keyword default could
    bypass the empty-string coalescing when the key is missing, so the
    helper must reject ``default=`` too. The snippet is hardcoded AST input
    only.
    """
    node = ast.parse(
        'sync_settings.get("preferred_source", default="sentinel-red-2b1")',
        mode="eval",
    ).body
    assert not _is_preferred_source_get(node), (
        '_is_preferred_source_get must not accept '
        'sync_settings.get("preferred_source", default=<value>): a truthy '
        'keyword default would bypass the or-"" empty-string fallback when '
        'the key is missing'
    )
