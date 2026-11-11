# -*- coding: utf-8 -*-
"""R45：G3 做T交易页持仓股四项修复——watchlist 存储层 + scoped-write 端点守卫。

任务书：F:/Aistock/data/data/runtime/task_briefs/20261110_ttrade_watchlist_r45.md

三层契约：
  1. services/holding_t_watchlist.py（stdlib-only）：
     - 结构 v2：{version, codes[], items[{code,name,qty,source:manual|ths,added_at,updated_at}], updated_at}；
     - upsert：增（manual）/删（校验先行，fail-closed 不部分生效）；
     - sync_from_ths 合并语义：THS 有→更新 qty/name/source=ths；THS 无而列表有→保留
       绝不删除（原 ths 条目降标 manual）；THS 新→追加；无变化零写；
     - 原子写+备份：temp+os.replace；失败回滚（原文件字节不变、零 temp 残留）；
     - 旧结构 {codes:[...]} 读兼容（读端合成 items，source=manual）。
  2. api/gen3_state_alpha.py：POST /holding-t/watchlist-upsert、POST /holding-t/watchlist-sync-ths
     为唯一受控写通道（只写 holding_tick_watchlist.json）；旧 PUT 处理器保持不动。
  3. AST 静态守卫：两个 POST 处理器调用闭包内——写原语仅 services.holding_t_watchlist
     的受控写函数；零其他文件写/零 formal 表写/零调度器/零 SMTP/零子进程；
     services 模块 import 面锁定 stdlib 白名单（零网络零 DB）。
"""
from __future__ import annotations

import ast
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import holding_t_watchlist as htw  # noqa: E402

NOW = datetime(2026, 11, 11, 10, 30, 0)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


@pytest.fixture()
def store(tmp_path):
    return tmp_path / "holding_t_watchlist" / "holding_tick_watchlist.json"


@pytest.fixture()
def legacy(tmp_path):
    return tmp_path / "holding_tick_watchlist.json"


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 服务层：读/结构

class TestReadCompat:
    def test_legacy_shape_synthesizes_manual_items(self, store, legacy):
        _write_json(legacy, {"codes": ["300054.SZ", "002015.SZ"], "updated_at": "2026-07-30T14:32:59+08:00"})
        payload = htw.read_watchlist(store, legacy_path=legacy)
        assert payload["codes"] == ["300054.SZ", "002015.SZ"]
        assert payload["source"] == "legacy"
        assert payload["updated_at"] == "2026-07-30T14:32:59+08:00"
        items = payload["items"]
        assert [item["code"] for item in items] == ["300054.SZ", "002015.SZ"]
        for item in items:
            assert item["source"] == "manual"
            assert item["qty"] is None
            assert item["name"] == ""
            assert item["added_at"] is None
            assert item["updated_at"] == "2026-07-30T14:32:59+08:00"

    def test_store_preferred_over_legacy(self, store, legacy):
        _write_json(legacy, {"codes": ["600000.SH"], "updated_at": "old"})
        _write_json(store, {
            "version": 2, "codes": ["300054.SZ"],
            "items": [{"code": "300054.SZ", "name": "鼎龙股份", "qty": 100,
                        "source": "ths", "added_at": "a", "updated_at": "b"}],
            "updated_at": "new",
        })
        payload = htw.read_watchlist(store, legacy_path=legacy)
        assert payload["source"] == "store"
        assert payload["codes"] == ["300054.SZ"]
        assert payload["items"][0]["name"] == "鼎龙股份"

    def test_missing_files_return_empty(self, store, legacy):
        payload = htw.read_watchlist(store, legacy_path=legacy)
        assert payload["codes"] == [] and payload["items"] == []
        assert payload["updated_at"] is None


# ---------------------------------------------------------------- 服务层：upsert

class TestUpsert:
    def test_add_new_manual_entry(self, store):
        result = htw.upsert_watchlist(store, add=[{"code": "002015", "qty": 2000}], remove=[], now=NOW)
        assert result["ok"] is True
        assert result["codes"] == ["002015.SZ"]
        item = result["items"][0]
        assert item == {"code": "002015.SZ", "name": "", "qty": 2000, "source": "manual",
                        "added_at": _iso(NOW), "updated_at": _iso(NOW)}
        assert result["diff"] == {"added": ["002015.SZ"], "updated": [], "removed": []}
        on_disk = _read_json(store)
        assert on_disk["codes"] == ["002015.SZ"] and on_disk["items"][0]["source"] == "manual"

    def test_add_existing_updates_qty_and_preserves_added_at(self, store):
        htw.upsert_watchlist(store, add=[{"code": "002015", "qty": 2000}], remove=[], now=NOW)
        later = datetime(2026, 11, 11, 14, 0, 0)
        result = htw.upsert_watchlist(store, add=[{"code": "002015.SZ", "qty": 4000}], remove=[], now=later)
        item = result["items"][0]
        assert item["qty"] == 4000
        assert item["added_at"] == _iso(NOW)
        assert item["updated_at"] == _iso(later)
        assert result["diff"]["added"] == [] and result["diff"]["updated"] == ["002015.SZ"]

    def test_manual_add_on_ths_entry_marks_manual(self, store):
        htw.sync_watchlist_from_ths(
            store, [{"code": "300054", "name": "鼎龙股份", "shares": 100}], synced_at="t0", now=NOW)
        later = datetime(2026, 11, 11, 14, 0, 0)
        result = htw.upsert_watchlist(store, add=[{"code": "300054.SZ", "qty": 300}], remove=[], now=later)
        item = result["items"][0]
        assert item["qty"] == 300 and item["source"] == "manual"

    def test_remove_entry(self, store):
        htw.upsert_watchlist(store, add=[{"code": "002015"}, {"code": "300054"}], remove=[], now=NOW)
        result = htw.upsert_watchlist(store, add=[], remove=["300054.SZ"], now=NOW)
        assert result["codes"] == ["002015.SZ"]
        assert result["diff"]["removed"] == ["300054.SZ"]

    def test_remove_then_readd_in_one_call(self, store):
        htw.upsert_watchlist(store, add=[{"code": "002015", "qty": 100}], remove=[], now=NOW)
        later = datetime(2026, 11, 11, 14, 0, 0)
        result = htw.upsert_watchlist(store, add=[{"code": "002015", "qty": 500}], remove=["002015.SZ"], now=later)
        assert result["codes"] == ["002015.SZ"]
        assert result["items"][0]["qty"] == 500 and result["items"][0]["added_at"] == _iso(later)

    def test_invalid_code_rejected_without_write(self, store):
        with pytest.raises(ValueError):
            htw.upsert_watchlist(store, add=[{"code": "hello"}], remove=[], now=NOW)
        assert not store.exists()

    def test_bad_qty_rejected_without_write(self, store):
        htw.upsert_watchlist(store, add=[{"code": "002015", "qty": 100}], remove=[], now=NOW)
        before = store.read_bytes()
        with pytest.raises(ValueError):
            htw.upsert_watchlist(store, add=[{"code": "300054", "qty": -5}], remove=[], now=NOW)
        with pytest.raises(ValueError):
            htw.upsert_watchlist(store, add=[{"code": "300054", "qty": 1.5}], remove=[], now=NOW)
        assert store.read_bytes() == before

    def test_cap_100(self, store):
        adds = [{"code": f"{i:06d}"} for i in range(100)]
        htw.upsert_watchlist(store, add=adds, remove=[], now=NOW)
        with pytest.raises(ValueError):
            htw.upsert_watchlist(store, add=[{"code": "100001"}], remove=[], now=NOW)

    def test_name_lookup_fills_name_only_when_empty(self, store):
        htw.upsert_watchlist(store, add=[{"code": "002015"}], remove=[], now=NOW,
                             name_lookup=lambda codes: {"002015.SZ": "协鑫能科"})
        assert result_name(store) == "协鑫能科"
        htw.upsert_watchlist(store, add=[{"code": "002015", "qty": 100}], remove=[], now=NOW,
                             name_lookup=lambda codes: {"002015.SZ": "占位"})
        assert result_name(store) == "协鑫能科"

    def test_no_change_no_write(self, store):
        htw.upsert_watchlist(store, add=[{"code": "002015", "qty": 100}], remove=[], now=NOW)
        first_mtime_ns = store.stat().st_mtime_ns
        result = htw.upsert_watchlist(store, add=[], remove=["999999"], now=datetime(2026, 11, 11, 15, 0, 0))
        assert result["changed"] is False
        assert result["diff"] == {"added": [], "updated": [], "removed": []}
        assert store.stat().st_mtime_ns == first_mtime_ns

    def test_add_from_legacy_migrates_to_store_and_keeps_legacy(self, store, legacy):
        _write_json(legacy, {"codes": ["603881.SH"], "updated_at": "2026-07-30T14:32:59+08:00"})
        legacy_bytes = legacy.read_bytes()
        result = htw.upsert_watchlist(store, add=[{"code": "002015"}], remove=[], now=NOW, legacy_path=legacy)
        assert result["codes"] == ["603881.SH", "002015.SZ"]
        assert store.exists() and _read_json(store)["version"] == 2
        assert legacy.read_bytes() == legacy_bytes, "旧文件保持不动"


def result_name(store: Path) -> str:
    return _read_json(store)["items"][0]["name"]


# ---------------------------------------------------------------- 服务层：THS 同步合并

class TestSyncThs:
    def HOLDINGS(self):
        return [
            {"code": "002015", "name": "协鑫能科", "shares": 4000},
            {"code": "300054", "name": "鼎龙股份", "shares": 100},
        ]

    def test_full_merge_matrix(self, store):
        htw.upsert_watchlist(store, add=[{"code": "002015", "qty": 2000},   # THS 有 → 更新
                                         {"code": "300037", "qty": 500}],    # THS 无 → 保留 manual
                              remove=[], now=NOW)
        later = datetime(2026, 11, 11, 15, 20, 0)
        result = htw.sync_watchlist_from_ths(store, self.HOLDINGS(),
                                             synced_at="2026-11-11 15:15:00", now=later)
        assert result["ok"] is True and result["synced_at"] == "2026-11-11 15:15:00"
        by_code = {item["code"]: item for item in result["items"]}
        # THS 有 → 更新 qty/name/source=ths
        assert by_code["002015.SZ"]["qty"] == 4000
        assert by_code["002015.SZ"]["name"] == "协鑫能科"
        assert by_code["002015.SZ"]["source"] == "ths"
        assert by_code["002015.SZ"]["updated_at"] == _iso(later)
        assert by_code["002015.SZ"]["added_at"] == _iso(NOW), "added_at 不随同步改变"
        # THS 无 → 保留不动、绝不删除
        assert by_code["300037.SZ"]["qty"] == 500
        assert by_code["300037.SZ"]["source"] == "manual"
        assert by_code["300037.SZ"]["updated_at"] == _iso(NOW), "保留条目 updated_at 不动"
        # THS 新 → 追加
        assert by_code["300054.SZ"]["source"] == "ths"
        assert by_code["300054.SZ"]["qty"] == 100
        assert by_code["300054.SZ"]["added_at"] == _iso(later)
        assert result["codes"] == ["002015.SZ", "300037.SZ", "300054.SZ"]
        assert result["diff"]["added"] == ["300054.SZ"]
        assert result["diff"]["updated"] == ["002015.SZ"]
        assert result["diff"]["kept"] == ["300037.SZ"]

    def test_ths_dropped_entry_stays_but_flips_to_manual(self, store):
        htw.sync_watchlist_from_ths(store, self.HOLDINGS(), synced_at="t1", now=NOW)
        later = datetime(2026, 11, 11, 15, 20, 0)
        result = htw.sync_watchlist_from_ths(
            store, [{"code": "002015", "name": "协鑫能科", "shares": 4000}],
            synced_at="2026-11-11 15:15:00", now=later)
        by_code = {item["code"]: item for item in result["items"]}
        assert "300054.SZ" in by_code, "THS 已无该持仓也绝不删除"
        assert by_code["300054.SZ"]["source"] == "manual"
        assert by_code["300054.SZ"]["qty"] == 100, "数量保留最后已知值"
        assert set(result["diff"]["kept"]) == {"002015.SZ", "300054.SZ"}

    def test_idempotent_sync_zero_write(self, store):
        htw.sync_watchlist_from_ths(store, self.HOLDINGS(), synced_at="t1", now=NOW)
        mtime = store.stat().st_mtime_ns
        result = htw.sync_watchlist_from_ths(store, self.HOLDINGS(), synced_at="t1", now=datetime(2026, 11, 11, 16, 0, 0))
        assert result["changed"] is False
        assert result["diff"] == {"added": [], "updated": [], "kept": ["002015.SZ", "300054.SZ"]}
        assert store.stat().st_mtime_ns == mtime

    def test_sync_from_legacy_keeps_legacy_entries_manual(self, store, legacy):
        _write_json(legacy, {"codes": ["603881.SH"], "updated_at": "2026-07-30T14:32:59+08:00"})
        result = htw.sync_watchlist_from_ths(store, self.HOLDINGS(), synced_at="t1", now=NOW, legacy_path=legacy)
        by_code = {item["code"]: item for item in result["items"]}
        assert by_code["603881.SH"]["source"] == "manual" and by_code["603881.SH"]["qty"] is None
        assert by_code["002015.SZ"]["source"] == "ths"

    def test_empty_ths_holdings_flips_all_to_manual(self, store):
        htw.sync_watchlist_from_ths(store, self.HOLDINGS(), synced_at="t1", now=NOW)
        result = htw.sync_watchlist_from_ths(store, [], synced_at="2026-11-11 15:15:00", now=datetime(2026, 11, 11, 15, 20, 0))
        assert result["codes"] == ["002015.SZ", "300054.SZ"]
        assert all(item["source"] == "manual" for item in result["items"])


# ---------------------------------------------------------------- 原子性与备份

class TestAtomicWrite:
    def test_failure_rolls_back_and_leaves_no_temp(self, store, monkeypatch):
        htw.upsert_watchlist(store, add=[{"code": "002015", "qty": 100}], remove=[], now=NOW)
        before = store.read_bytes()
        real_replace = os.replace

        def _boom(src, dst, *a, **kw):
            raise OSError("disk full (simulated)")

        monkeypatch.setattr(htw.os, "replace", _boom)
        with pytest.raises(OSError):
            htw.upsert_watchlist(store, add=[{"code": "300054", "qty": 100}], remove=[], now=NOW)
        monkeypatch.setattr(htw.os, "replace", real_replace)
        assert store.read_bytes() == before, "原子失败后原文件字节不变"
        leftovers = [p.name for p in store.parent.iterdir() if p.name != store.name and not p.name.endswith(".bak")]
        assert leftovers == [], f"零 temp 残留: {leftovers}"

    def test_backup_holds_previous_version(self, store):
        htw.upsert_watchlist(store, add=[{"code": "002015", "qty": 100}], remove=[], now=NOW)
        first = _read_json(store)
        htw.upsert_watchlist(store, add=[{"code": "300054", "qty": 100}], remove=[], now=datetime(2026, 11, 11, 15, 0, 0))
        backup = store.with_name(store.name + ".bak")
        assert backup.exists()
        assert _read_json(backup) == first

    def test_first_write_has_no_backup(self, store):
        htw.upsert_watchlist(store, add=[{"code": "002015"}], remove=[], now=NOW)
        assert not store.with_name(store.name + ".bak").exists()


# ---------------------------------------------------------------- API 层（真实模块，隔离路径）

_API_TEST_ENV = {
    "AISTOCK_DATA_ROOT": "isolated-data-root",
    "AISTOCK_REPORTS_ROOT": "isolated-reports-root",
    "AISTOCK_ARTIFACTS_ROOT": "isolated-artifacts-root",
    "AISTOCK_LOGS_ROOT": "isolated-logs-root",
}
_SAVED_API_ENV = {name: os.environ.get(name) for name in _API_TEST_ENV}
for name, subdir in _API_TEST_ENV.items():
    os.environ[name] = str(Path(__file__).resolve().parent / subdir)
try:
    import api.gen3_state_alpha as g3
finally:
    for name, prev in _SAVED_API_ENV.items():
        if prev is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev


@pytest.fixture()
def api_paths(tmp_path, monkeypatch):
    paths = {
        "HOLDING_T_WATCHLIST_STORE_PATH": tmp_path / "holding_t_watchlist" / "holding_tick_watchlist.json",
        "HOLDING_TICK_WATCHLIST_PATH": tmp_path / "holding_tick_watchlist.json",
        "BROKER_STATE_PATH": tmp_path / "broker_state.json",
    }
    originals = {}
    for attr, path in paths.items():
        originals[attr] = getattr(g3, attr)
        monkeypatch.setattr(g3, attr, path)
    yield paths
    for attr, value in originals.items():
        monkeypatch.setattr(g3, attr, value)


class TestApiHandlers:
    def test_upsert_endpoint_contract(self, api_paths, monkeypatch):
        monkeypatch.setattr(g3, "_load_stock_names", lambda codes: {"300054.SZ": "鼎龙股份"})
        payload = g3.post_gen3_holding_t_watchlist_upsert({"add": [{"code": "300054", "qty": 100}], "remove": []})
        assert payload["ok"] is True
        assert payload["order_path_enabled"] is False
        assert payload["codes"] == ["300054.SZ"]
        assert payload["items"][0]["name"] == "鼎龙股份"
        assert payload["diff"]["added"] == ["300054.SZ"]
        assert api_paths["HOLDING_T_WATCHLIST_STORE_PATH"].exists()

    def test_upsert_endpoint_rejects_garbage(self, api_paths):
        payload = g3.post_gen3_holding_t_watchlist_upsert({"add": "not-a-list", "remove": []})
        assert payload["ok"] is False and "message" in payload
        assert not api_paths["HOLDING_T_WATCHLIST_STORE_PATH"].exists()

    def test_sync_endpoint_reads_broker_state(self, api_paths):
        _write_json(api_paths["BROKER_STATE_PATH"], {
            "holdings": [{"code": "002015", "name": "协鑫能科", "shares": 4000}],
            "capital": {}, "updated_at": "2026-11-11 15:15:00",
        })
        g3.post_gen3_holding_t_watchlist_upsert({"add": [{"code": "300037", "qty": 500}], "remove": []})
        payload = g3.post_gen3_holding_t_watchlist_sync_ths()
        assert payload["ok"] is True
        assert payload["synced_at"] == "2026-11-11 15:15:00"
        assert payload["source"] == "broker_state"
        diff = payload["diff"]
        assert diff["added"] == ["002015.SZ"] and diff["kept"] == ["300037.SZ"]
        assert "新增 1 / 更新 0 / 保留 1" in payload["summary"]

    def test_sync_endpoint_failclosed_when_broker_state_missing(self, api_paths):
        payload = g3.post_gen3_holding_t_watchlist_sync_ths()
        assert payload["ok"] is False
        assert not api_paths["HOLDING_T_WATCHLIST_STORE_PATH"].exists()

    def test_get_watchlist_returns_both_shapes(self, api_paths):
        payload = g3._read_holding_tick_watchlist()
        assert payload["codes"] == [] and payload["items"] == []
        g3.post_gen3_holding_t_watchlist_upsert({"add": [{"code": "002015", "qty": 100}], "remove": []})
        payload = g3._read_holding_tick_watchlist()
        assert payload["codes"] == ["002015.SZ"]
        assert payload["items"][0]["qty"] == 100

    def test_legacy_put_handler_untouched(self, api_paths):
        """旧 PUT 处理器保持原语义原路径（预览栈仍封禁；此处只证未被 R45 改坏）。"""
        import asyncio
        payload = asyncio.run(g3.put_gen3_state_alpha_holding_tick_watchlist({"codes": ["600000.SH"]}))
        assert payload["ok"] is True
        assert api_paths["HOLDING_TICK_WATCHLIST_PATH"].exists()
        assert not api_paths["HOLDING_T_WATCHLIST_STORE_PATH"].exists()


# ---------------------------------------------------------------- AST 静态守卫

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_G3_SOURCE = (_PROJECT_ROOT / "api" / "gen3_state_alpha.py").read_text(encoding="utf-8")
_G3_TREE = ast.parse(_G3_SOURCE)
_HTW_SOURCE = (_PROJECT_ROOT / "services" / "holding_t_watchlist.py").read_text(encoding="utf-8")
_HTW_TREE = ast.parse(_HTW_SOURCE)

_R45_WRITE_HANDLERS = ("post_gen3_holding_t_watchlist_upsert", "post_gen3_holding_t_watchlist_sync_ths")

_WRITE_PRIMITIVES = {
    "write_text", "write_bytes", "mkdir", "unlink", "rmtree", "touch", "to_csv",
    "replace", "remove", "rename", "makedirs",
}
_FORBIDDEN_CALL_SUBSTRINGS = (
    "subprocess", "os.system", "os.popen", "add_job", "remove_job", "smtplib",
    "SMTP", "_ensure_scheduler", "clickhouse", "insert", "ALTER", "DROP",
)


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    out: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, node)
    return out


def _called_names(func: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


class TestAstGuards:
    def test_handlers_exist_and_are_plain_sync_def(self):
        funcs = _module_functions(_G3_TREE)
        for name in _R45_WRITE_HANDLERS:
            assert name in funcs, f"缺少处理器 {name}"
            assert isinstance(funcs[name], ast.FunctionDef) and not isinstance(funcs[name], ast.AsyncFunctionDef), \
                f"{name} 必须为普通 def（R44 教训：零 await 的 async def 阻塞事件循环）"

    def test_handler_chain_writes_only_via_holding_t_watchlist_store(self):
        """两个 POST 处理器闭包内的写原语只允许 services.holding_t_watchlist 受控写。"""
        g3_funcs = _module_functions(_G3_TREE)
        htw_funcs = _module_functions(_HTW_TREE)
        allowed_writers = {"_atomic_write_with_backup"}
        seen: set[str] = set()
        stack = [g3_funcs[name] for name in _R45_WRITE_HANDLERS]
        visited = set()
        while stack:
            func = stack.pop()
            if func.name in visited:
                continue
            visited.add(func.name)
            for call in _called_names(func):
                if call in allowed_writers:
                    continue
                if call in _WRITE_PRIMITIVES:
                    seen.add(f"{func.name}:{call}")
                if call in g3_funcs:
                    stack.append(g3_funcs[call])
                if call in htw_funcs:
                    stack.append(htw_funcs[call])
        assert not seen, f"写原语越界（只允许经 _atomic_write_with_backup）: {sorted(seen)}"

    def test_handlers_never_touch_broker_state_write_or_scheduler(self):
        g3_funcs = _module_functions(_G3_TREE)
        for name in _R45_WRITE_HANDLERS + ("_upsert_holding_t_watchlist_payload", "_sync_holding_t_watchlist_from_broker_state"):
            func = g3_funcs.get(name)
            if func is None:
                continue
            calls = _called_names(func)
            assert "_save_broker_state" not in calls, f"{name} 不得写 broker_state"
            for call in calls:
                for bad in _FORBIDDEN_CALL_SUBSTRINGS:
                    assert bad not in call, f"{name} 调用 {call} 命中禁区 {bad}"

    def test_service_module_import_surface_is_stdlib_only(self):
        allowed = {"json", "os", "re", "shutil", "uuid", "datetime", "zoneinfo", "pathlib", "typing", "__future__"}
        for node in _HTW_TREE.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] in allowed, f"services/holding_t_watchlist.py 非法 import: {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root in allowed, f"services/holding_t_watchlist.py 非法 from-import: {node.module}"

    def test_service_module_has_no_network_or_db_tokens(self):
        for bad in ("requests", "httpx", "urllib", "socket", "clickhouse", "psycopg", "sqlite", "subprocess", "smtplib"):
            assert bad not in _HTW_SOURCE, f"services/holding_t_watchlist.py 出现 {bad}"

    def test_service_module_writes_confined_to_store_dir(self):
        """受控写函数只接受显式传入的 store path——不得引用任何全局真实运行时路径。"""
        funcs = _module_functions(_HTW_TREE)
        writer = funcs.get("_atomic_write_with_backup")
        assert writer is not None
        args = [a.arg for a in writer.args.args]
        assert "path" in args and "payload" in args
        for node in ast.walk(_HTW_TREE):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "Path":
                pass  # Path 构造允许（tmp 测试），但不得出现真实根字面量
        for literal in ("F:/", "F:\\", "AISTOCK_DATA_ROOT", "runtime"):
            assert literal not in _HTW_SOURCE, f"services 模块不得硬编码真实路径 {literal}"

    def test_preview_scoped_write_paths_declared(self):
        source = (_PROJECT_ROOT / "api" / "operations_preview.py").read_text(encoding="utf-8")
        assert "_SCOPED_WRITE_PREVIEW_PATHS" in source
        assert "'/api/gen3-state-alpha/holding-t/watchlist-upsert'" in source
        assert "'/api/gen3-state-alpha/holding-t/watchlist-sync-ths'" in source
        assert "frozenset" in source
