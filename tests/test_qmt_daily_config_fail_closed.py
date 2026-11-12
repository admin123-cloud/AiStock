"""Slice 2B — QMT daily defaults fail-closed (test-only assignment, 2026-11-05).

These are STRUCTURAL/CONFIG CONTAINMENT CHECKS ONLY: they verify that the
implicit ``qmt_xtquant`` default/fallback has been removed from the two
``KlineSyncer`` nodes and that the daily-sync configs no longer enable QMT
or its external fallback. They do NOT establish TDX routing or any
end-to-end data-source replacement.

Scope discipline (per task brief
``20261105_qmt_slice2b_qmt_defaults_test.md``):
- No AiStock modules are imported; the fixed project root is
  ``Path(__file__).parents[1]`` and parsing uses stdlib ``ast`` plus
  ``yaml.safe_load`` only.
- The AST check inspects ONLY ``KlineSyncer.__init__`` and
  ``KlineSyncer.sync_kline_for_stock``; historical QMT strings elsewhere in
  the repository are intentionally not banned here.
- Config assertions print no parsed config objects or unrelated values.
"""

import ast
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parents[1]

QMT_XTQUANT = "qmt_xtquant"


def _semantic_string_literals(function_node: ast.FunctionDef) -> list:
    """Return string literals in a function, excluding its docstring."""
    docstring_ids = set()
    first = function_node.body[0] if function_node.body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        docstring_ids.add(id(first.value))
    return [
        node.value
        for node in ast.walk(function_node)
        if id(node) not in docstring_ids
        and isinstance(node, ast.Constant)
        and isinstance(node.value, str)
    ]


def test_sync_all_klines_no_implicit_qmt_xtquant_default_or_fallback():
    """KlineSyncer.__init__/sync_kline_for_stock must not default to qmt_xtquant.

    Containment check on exactly two AST nodes: no implicit ``qmt_xtquant``
    default (e.g. ``sync_settings.get("preferred_source", "qmt_xtquant")``)
    and no implicit fallback (e.g. ``self.preferred_source or "qmt_xtquant"``).
    """
    script_path = PROJECT_ROOT / "scripts" / "sync_all_klines.py"
    tree = ast.parse(script_path.read_text(encoding="utf-8"), filename=str(script_path))

    syncer = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "KlineSyncer"
        ),
        None,
    )
    assert syncer is not None, "scripts/sync_all_klines.py: class KlineSyncer not found"

    wanted = {"__init__", "sync_kline_for_stock"}
    found = {
        node.name: node
        for node in syncer.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    }
    assert set(found) == wanted, (
        "scripts/sync_all_klines.py: KlineSyncer must define both __init__ and sync_kline_for_stock"
    )

    for name in ("__init__", "sync_kline_for_stock"):
        offenders = sorted(
            {
                value
                for value in _semantic_string_literals(found[name])
                if value.strip().lower() == QMT_XTQUANT
            }
        )
        assert not offenders, (
            "scripts/sync_all_klines.py: KlineSyncer.%s still carries an implicit "
            "qmt_xtquant default/fallback string literal" % name
        )


def test_daily_sync_configs_disable_qmt_xtquant_and_external_fallback():
    """Daily-sync configs must leave qmt_xtquant and external fallback fail-closed.

    Parses ONLY ``config/settings.yaml`` and ``config/datasources.yaml``:
    ``data_sync.preferred_source`` empty/unset, ``external_fallback_enabled``
    exactly false, external fallback periods/providers empty, and both
    ``qmt_xtquant`` ``enabled`` flags exactly false.
    """
    settings = yaml.safe_load(
        (PROJECT_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    )
    datasources = yaml.safe_load(
        (PROJECT_ROOT / "config" / "datasources.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(settings, dict), "config/settings.yaml: top-level document must be a mapping"
    assert isinstance(datasources, dict), "config/datasources.yaml: top-level document must be a mapping"

    data_sync = settings.get("data_sync")
    assert isinstance(data_sync, dict), "config/settings.yaml: data_sync section must be a mapping"

    preferred = data_sync.get("preferred_source")
    assert preferred is None or (isinstance(preferred, str) and not preferred.strip()), (
        "config/settings.yaml: data_sync.preferred_source must be empty/unset"
    )

    assert data_sync.get("external_fallback_enabled") is False, (
        "config/settings.yaml: data_sync.external_fallback_enabled must be exactly false"
    )

    for key in ("external_fallback_periods", "external_fallback_providers"):
        raw = data_sync.get(key)
        assert raw is None or raw == [], (
            "config/settings.yaml: data_sync.%s must be empty" % key
        )

    qmt_sync = data_sync.get("qmt_xtquant")
    assert isinstance(qmt_sync, dict) and qmt_sync.get("enabled") is False, (
        "config/settings.yaml: data_sync.qmt_xtquant.enabled must be exactly false"
    )

    data_sources = datasources.get("data_sources")
    assert isinstance(data_sources, dict), "config/datasources.yaml: data_sources section must be a mapping"

    qmt_source = data_sources.get("qmt_xtquant")
    assert isinstance(qmt_source, dict) and qmt_source.get("enabled") is False, (
        "config/datasources.yaml: data_sources.qmt_xtquant.enabled must be exactly false"
    )
