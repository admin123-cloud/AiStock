"""R43 封禁 GET 批量净化守卫（35 条：①refresh6 / ②调度器status10 / ③缺件13 / ④写外呼6）。

静态 AST 守卫：对每条净化后的 GET 处理器，沿「函数体内 Call 节点」做跨模块
传递闭包（装饰器与参数默认值表达式不参与——它们在 import 期求值一次，不在
请求路径上），断言闭包内零：
  - subprocess 执行（subprocess.run/Popen/check_*、os.system/popen 及全部 _run_* runner）
  - 调度器变更（add_job/remove_job/modify_job/pause/resume/start/shutdown、
    configure_*_scheduler / _ensure_*_scheduler）
  - 文件写（write_text/mkdir/unlink/rmtree/touch/to_csv、os.replace/remove/
    rename/makedirs 及已知 _save_*/_write_json/_append_* 写助手）
  - SMTP 外呼（smtplib.*、_smtp_login_probe/_open_smtp_server/_send_*_email）
  - 写禁区字符串（qmtmini、.write_probe）

行为守卫按批次启用（ACTIVE_CATEGORIES）：每批 commit 前%；四批全绿后为全集。
判定与证据行号见 runtime/full_readface_allowlist_r30/AUDIT_TABLE.md（39 条封禁
表）与 R43 交付报告。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_DIR = PROJECT_ROOT / "api"

# 参与闭包解析的模块（R30 审计同口径：api 四 router + services/utils 一跳名索引）
_CLOSURE_MODULES = [
    "gen3_shadow",
    "gen3_state_alpha",
    "trading",
    "system_config",
]
_EXTRA_SCAN_DIRS = [PROJECT_ROOT / "services", PROJECT_ROOT / "utils"]

# ---------------------------------------------------------------- 路由清单
# (module, full_path, category)——category ∈ {"1","2","3","4"} 对应任务书①②③④。
R43_PURIFIED_ROUTES = [
    # ①类 refresh 触发（6）
    ("gen3_shadow", "/api/gen3-shadow/guarded", "1"),
    ("gen3_shadow", "/api/gen3-shadow/live", "1"),
    ("gen3_shadow", "/api/gen3-shadow/range-filtered", "1"),
    ("gen3_shadow", "/api/gen3-shadow/state-router-current", "1"),
    ("gen3_shadow", "/api/gen3-shadow/v4-strong-offense", "1"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/current", "1"),
    # ②类 调度器改写 status（10，含④重叠的 gen2/workflow/status 按②处理）
    ("gen3_state_alpha", "/api/gen3-state-alpha/broker/sync/status", "2"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/daily-trend-exit-monitor/status", "2"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/observation-scheduler/status", "2"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/observation-snapshots", "2"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/shadow-exit-monitor/status", "2"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/shadow-monitor/status", "2"),
    ("trading", "/api/trading/gen2/shadow-monitor/status", "2"),
    ("trading", "/api/trading/gen2/strategy-refresh/status", "2"),
    ("trading", "/api/trading/gen2/workflow/status", "2"),
    ("trading", "/api/trading/v4/manual-holdings/monitor/status", "2"),
    # ③类 缺件自动跑（13）
    ("gen3_state_alpha", "/api/gen3-state-alpha/broker/holdings/post-sync-acceptance", "3"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/live-action-console", "3"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/live-blocker-evidence-board", "3"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/live-launch-decision", "3"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/live-launch-packet", "3"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/live-launch-readiness-audit", "3"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/live-learning-ledger", "3"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/premarket-control", "3"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/strategy-tuning-axis-board", "3"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/strategy-tuning-completion-audit", "3"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/strategy-tuning-replay-session", "3"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/strategy-tuning-replay-suggestions", "3"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/strategy-tuning-review-queue", "3"),
    # ④类 GET 内写/外呼（6）
    ("gen3_state_alpha", "/api/gen3-state-alpha/historical-trades", "4"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/replacement-assessment", "4"),
    ("gen3_state_alpha", "/api/gen3-state-alpha/workflow/status", "4"),
    ("system_config", "/api/system/tdx-gateway/diagnostics", "4"),
    ("trading", "/api/trading/gen2/daily-trade-ticket", "4"),
    ("trading", "/api/trading/gen2/daily-trade-ticket/execution", "4"),
]

# 分批推进：每批 commit 时其类别测试必须全绿（①→②→③→④）。
ACTIVE_CATEGORIES = {"1", "2"}

assert len(R43_PURIFIED_ROUTES) == 35, "R43 范围必须是 35 条（39 封禁 − #14 R42 − ⑤类 sync-ths 3 条）"

# ---------------------------------------------------------------- AST 索引


def _iter_module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    out: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, node)
    return out


def _router_prefix(tree: ast.Module) -> str:
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            f = node.value.func
            is_router_ctor = (isinstance(f, ast.Name) and f.id == "APIRouter") or (
                isinstance(f, ast.Attribute) and f.attr == "APIRouter"
            )
            if is_router_ctor:
                for kw in node.value.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        return str(kw.value.value)
                return ""
    return ""


def _route_targets(tree: ast.Module) -> dict[str, str]:
    """route_path -> handler function name（仅 GET 装饰器）。"""
    out: dict[str, str] = {}
    prefix = _router_prefix(tree)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "get"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                out["/api" + prefix + str(dec.args[0].value)] = node.name
    return out


class _Index:
    def __init__(self) -> None:
        self.functions: dict[str, list[ast.AST]] = {}
        self.routes: dict[str, tuple[str, str]] = {}  # path -> (module, func)
        for name in _CLOSURE_MODULES:
            path = API_DIR / f"{name}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for fname, fnode in _iter_module_functions(tree).items():
                self.functions.setdefault(fname, []).append(fnode)
            for route, handler in _route_targets(tree).items():
                self.routes[route] = (name, handler)
        for extra_dir in _EXTRA_SCAN_DIRS:
            if not extra_dir.exists():
                continue
            for py in extra_dir.glob("*.py"):
                try:
                    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
                except SyntaxError:
                    continue
                for fname, fnode in _iter_module_functions(tree).items():
                    self.functions.setdefault(fname, []).append(fnode)


_INDEX = _Index()

# ---------------------------------------------------------------- 风险判定

BANNED_CALL_NAMES = {
    # subprocess / runner（GET 链绝不允许出现，包括 run-on-missing 网关）
    "_run_daily_update",
    "_run_guarded_update",
    "_run_range_filtered_update",
    "_run_state_router_current_update",
    "_run_v4_strong_offense_update",
    "_run_state_router_refresh",
    "_run_realtime_readiness_review_once",
    "_run_live_launch_packet_once",
    "_read_or_run_realtime_readiness_review",
    "_read_or_run_live_launch_packet",
    "_run_refresh_task",
    "_run_broker_sync_once",
    "_run_shadow_exit_monitor_once",
    "_run_shadow_entry_monitor_once",
    "_run_gen2_live_shadow_update_sync",
    # 调度器配置/启动
    "configure_broker_sync_scheduler",
    "configure_shadow_monitor_scheduler",
    "configure_shadow_exit_monitor_scheduler",
    "configure_daily_trend_exit_monitor_scheduler",
    "configure_observation_scheduler",
    "_ensure_monitor_scheduler",
    "_ensure_v4_monitor_scheduler",
    "_configure_v4_monitor_scheduler",
    "_configure_gen2_strategy_refresh_scheduler",
    "_configure_gen2_shadow_buy_monitor_scheduler",
    "init_v4_manual_holdings_monitor_scheduler_from_config",
    # 文件写助手
    "_write_json",
    "_save_json_file",
    "_save_monitor_state",
    "_save_exit_monitor_state",
    "_save_daily_trend_exit_monitor_state",
    "_save_observation_scheduler_state",
    "_save_broker_sync_state",
    "_save_broker_state",
    "_save_gen2_shadow_monitor_state",
    "_save_gen2_strategy_refresh_state",
    "_save_v4_monitor_state",
    "_save_gen2_daily_trade_ticket",
    "_save_gen2_daily_execution_ledger",
    "_save_observation_snapshots",
    "_save_live_launch_review_snapshots",
    "_save_g3_live_action_console_step_review",
    "_append_monitor_event",
    "_append_premarket_action_attempt",
    "_record_broker_post_sync_execution_evidence",
    "_reconcile_official_rebuild_state_if_needed",
    # SMTP 外呼
    "_smtp_login_probe",
    "_open_smtp_server",
    "_send_shadow_email",
    "_send_monitor_email",
    "_send_gen2_blocker_email",
    "_send_gen2_shadow_buy_email",
    "_build_live_launch_review_snapshot",
}

BANNED_ATTR_CALLS = {
    "add_job",
    "remove_job",
    "modify_job",
    "pause_job",
    "resume_job",
    "shutdown",
    "start",
    "write_text",
    "write_bytes",
    "mkdir",
    "unlink",
    "rmtree",
    "touch",
    "to_csv",
    "to_excel",
    "sendmail",
    "login",
    "starttls",
    "Popen",
    "check_call",
    "check_output",
}

# 仅当接收者为该模块名时才判写（str.replace/list.remove 等 benign 同名方法不误伤）
_BANNED_OS_ATTRS = {"replace", "remove", "rename", "makedirs", "system", "popen", "run"}

FORBIDDEN_SUBSTRINGS = ("qmtmini", ".write_probe")


def _attr_chain(node: ast.AST) -> list[str]:
    """把 Attribute/Name 链拆成 ['os','replace'] / ['subprocess','run'] / ['x','y','add_job']。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return list(reversed(parts))


def _closure(entry_func: ast.AST, index: _Index, max_depth: int = 25) -> set[str]:
    """函数体内 Call 节点的传递闭包（按 `module.func` / `func` 名返回）。"""
    seen_funcs: set[int] = set()
    calls: set[str] = set()
    stack: list[ast.AST] = [entry_func]
    depth_of: dict[int, int] = {id(entry_func): 0}
    while stack:
        fn = stack.pop()
        if id(fn) in seen_funcs:
            continue
        seen_funcs.add(id(fn))
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            chain = _attr_chain(node.func)
            if not chain:
                continue
            if len(chain) == 1:
                fname = chain[0]
                calls.add(fname)
                if depth_of[id(fn)] < max_depth:
                    for target in index.functions.get(fname, []):
                        if id(target) not in seen_funcs:
                            depth_of[id(target)] = depth_of[id(fn)] + 1
                            stack.append(target)
            else:
                receiver, attr = chain[0], chain[-1]
                calls.add(f"{receiver}.{attr}")
                if receiver == "subprocess":
                    calls.add(f"__subprocess__.{attr}")
                if receiver == "smtplib":
                    calls.add(f"__smtplib__.{attr}")
    return calls


def _closure_strings(entry_func: ast.AST) -> list[str]:
    return [
        str(node.value)
        for node in ast.walk(entry_func)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _violation_report(calls: set[str]) -> list[str]:
    bad: list[str] = []
    for name in sorted(calls):
        if name in BANNED_CALL_NAMES:
            bad.append(f"banned call: {name}")
            continue
        if name.startswith("__subprocess__.") or name.startswith("__smtplib__."):
            bad.append(f"banned module call: {name}")
            continue
        if "." not in name:
            continue
        receiver, attr = name.split(".", 1)
        if attr in BANNED_ATTR_CALLS:
            bad.append(f"banned attr call: {name}")
        elif attr in _BANNED_OS_ATTRS and receiver in {"os", "shutil", "subprocess"}:
            bad.append(f"banned os-level call: {name}")
    return bad


# ---------------------------------------------------------------- 守卫测试


def _guard_ids():
    return [
        f"{cat}-{module}-{path}"
        for module, path, cat in R43_PURIFIED_ROUTES
        if cat in ACTIVE_CATEGORIES
    ]


@pytest.mark.parametrize("route_id", _guard_ids())
def test_r43_get_closure_is_read_only(route_id: str) -> None:
    category, module, path = route_id.split("-", 2)
    entry = _INDEX.routes.get(path)
    assert entry is not None, f"route not found: {path}"
    handler = _INDEX.functions.get(entry[1], [])
    assert handler, f"handler def not found: {entry[1]}"
    violations: list[str] = []
    for fnode in handler:
        calls = _closure(fnode, _INDEX)
        violations.extend(_violation_report(calls))
    assert not violations, f"{path} GET 链仍含写/跑/外呼: {violations}"


@pytest.mark.parametrize("route_id", _guard_ids())
def test_r43_get_closure_no_forbidden_zone_strings(route_id: str) -> None:
    category, module, path = route_id.split("-", 2)
    entry = _INDEX.routes.get(path)
    handler = _INDEX.functions.get(entry[1], [])
    hits: list[str] = []
    for fnode in handler:
        for text in _closure_strings(fnode):
            low = text.lower()
            if any(sub.lower() in low for sub in FORBIDDEN_SUBSTRINGS):
                hits.append(text[:120])
    assert not hits, f"{path} GET 链触达写禁区字符串: {hits}"


# ---------------------------------------------------------------- 行为守卫（①）


def _call_async(coro):
    import asyncio

    return asyncio.run(coro)


def test_r43_cat1_gen3_shadow_refresh_true_never_runs(monkeypatch) -> None:
    import api.gen3_shadow as gs

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on regression
        raise AssertionError("GET must not run update subprocess")

    for runner in (
        "_run_daily_update",
        "_run_guarded_update",
        "_run_range_filtered_update",
        "_run_v4_strong_offense_update",
        "_run_state_router_current_update",
    ):
        monkeypatch.setattr(gs, runner, _boom)
    monkeypatch.setattr(gs.subprocess, "run", _boom)

    live = _call_async(gs.get_gen3_shadow_live(as_of=None, refresh=True, allow_after_close=True, limit=20))
    assert live["ok"] is True and live["order_path_enabled"] is False
    assert live["refresh"]["ran"] is False and live["refresh"]["ignored"] is True

    guarded = _call_async(gs.get_gen3_guarded_shadow(as_of="2026-11-10", refresh=True, limit=20))
    assert guarded["refresh"]["ran"] is False and guarded["refresh"]["ignored"] is True

    ranged = _call_async(gs.get_gen3_range_filtered_shadow(as_of=None, refresh=True, limit=20))
    assert ranged["refresh"]["ran"] is False and ranged["refresh"]["ignored"] is True

    v4 = _call_async(gs.get_gen3_v4_strong_offense_shadow(as_of=None, refresh=True, limit=20))
    assert v4["refresh"]["ran"] is False and v4["refresh"]["ignored"] is True

    src = _call_async(gs.get_gen3_state_router_current_shadow(entry_date="2026-11-10", refresh=True, limit=30))
    assert src["refresh"]["ran"] is False and src["refresh"]["ignored"] is True


def test_r43_cat1_gen3_state_alpha_current_refresh_true_never_runs(monkeypatch) -> None:
    import api.gen3_state_alpha as gsa

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on regression
        raise AssertionError("GET /current must not trigger state-router refresh")

    monkeypatch.setattr(gsa, "_run_state_router_refresh", _boom)
    payload = _call_async(gsa.get_gen3_state_alpha_current(limit=30, refresh=True, entry_date="2026-11-10"))
    assert payload["ok"] is True and payload["order_path_enabled"] is False
    assert payload["refresh"]["ran"] is False and payload["refresh"]["ignored"] is True
    # schema 兼容：旧版键全部保留
    for key in ("ran", "ok", "returncode"):
        assert key in payload["refresh"]


def test_r43_cat1_refresh_false_payload_shape_unchanged() -> None:
    import api.gen3_shadow as gs

    live = _call_async(gs.get_gen3_shadow_live(as_of=None, refresh=False, allow_after_close=False, limit=20))
    assert live["refresh"] == {"ran": False, "ok": None, "returncode": None}
    assert {"ok", "mode", "diagnosis", "summary", "candidates", "blocked", "recent_signal_dates", "refresh", "artifacts"} <= set(live)


# ---------------------------------------------------------------- 行为守卫（②）


class _FakeJob:
    def __init__(self, job_id: str) -> None:
        self.id = job_id
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        self.next_run_time = _dt.now() + _td(minutes=30)


class _RecordingScheduler:
    """假 APScheduler：允许 get_job；一切变更/启动调用都被记录为 mutation。"""

    running = True

    def __init__(self, jobs: dict[str, object] | None = None) -> None:
        self.jobs = jobs or {}
        self.mutations: list[str] = []

    def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    def add_job(self, *args, **kwargs):
        self.mutations.append("add_job")
        return None

    def remove_job(self, *args, **kwargs):
        self.mutations.append("remove_job")
        return None

    def start(self, *args, **kwargs):
        self.mutations.append("start")
        return None

    def shutdown(self, *args, **kwargs):
        self.mutations.append("shutdown")
        return None


def test_r43_cat2_gen3_status_gets_never_mutate_scheduler(monkeypatch) -> None:
    import api.gen3_state_alpha as gsa

    fake = _RecordingScheduler(
        {
            gsa._monitor_job_id: _FakeJob(gsa._monitor_job_id),
            gsa._exit_monitor_job_id: _FakeJob(gsa._exit_monitor_job_id),
            gsa._daily_trend_exit_monitor_job_id: _FakeJob(gsa._daily_trend_exit_monitor_job_id),
            gsa._observation_job_id: _FakeJob(gsa._observation_job_id),
            gsa._observation_retry_job_id: _FakeJob(gsa._observation_retry_job_id),
            gsa._broker_sync_job_id: _FakeJob(gsa._broker_sync_job_id),
        }
    )
    monkeypatch.setattr(gsa, "_monitor_scheduler", fake)
    monkeypatch.setattr(gsa, "_startup_schedulers_enabled", lambda: True)

    monitor = _call_async(gsa.get_gen3_state_alpha_shadow_monitor_status(include_workflow=True))
    assert monitor["ok"] is True and monitor["monitor"]["scheduler_enabled"] is True
    assert monitor["monitor"]["scheduler_wired"] is True
    assert "workflow" in monitor and monitor["workflow"]["monitor"]["scheduler_enabled"] is True
    assert "state_alpha_runtime_dir" in [c.get("name") for c in monitor["workflow"]["pipeline_checks"]]

    broker = _call_async(gsa.get_gen3_state_alpha_broker_sync_status())
    assert broker["scheduler"]["wired"] is True

    exit_monitor = _call_async(gsa.get_gen3_state_alpha_shadow_exit_monitor_status())
    assert exit_monitor["monitor"]["scheduler_enabled"] is True

    daily = _call_async(gsa.get_gen3_state_alpha_daily_trend_exit_monitor_status())
    assert daily["monitor"]["scheduler_enabled"] is True

    obs = _call_async(gsa.get_gen3_state_alpha_observation_scheduler_status())
    assert obs["scheduler"]["scheduler_enabled"] is True
    assert obs["scheduler"]["job_ids"] == [gsa._observation_job_id, gsa._observation_retry_job_id]

    snaps = _call_async(gsa.get_gen3_state_alpha_observation_snapshots(limit=5))
    assert snaps["ok"] is True and snaps["scheduler"]["scheduler_enabled"] is True

    assert fake.mutations == [], f"GET 链出现调度器变更: {fake.mutations}"


def test_r43_cat2_gen3_status_disabled_branch_schema(monkeypatch) -> None:
    import api.gen3_state_alpha as gsa

    fake = _RecordingScheduler()
    monkeypatch.setattr(gsa, "_monitor_scheduler", fake)
    monkeypatch.setattr(gsa, "_startup_schedulers_enabled", lambda: False)

    payload = _call_async(gsa.get_gen3_state_alpha_shadow_monitor_status())
    assert payload["monitor"]["scheduler_enabled"] is False
    assert payload["monitor"]["scheduler_wired"] is False
    assert payload["monitor"]["scheduler_disabled_reason"].startswith("AISTOCK_STARTUP_SCHEDULERS")
    assert fake.mutations == []


def test_r43_cat2_gen3_readonly_keys_match_configure(monkeypatch) -> None:
    import api.gen3_state_alpha as gsa

    fake = _RecordingScheduler({gsa._monitor_job_id: _FakeJob(gsa._monitor_job_id)})
    monkeypatch.setattr(gsa, "_monitor_scheduler", fake)
    monkeypatch.setattr(gsa, "_startup_schedulers_enabled", lambda: True)

    configured = gsa.configure_shadow_monitor_scheduler()
    mutated = [m for m in fake.mutations]
    fake.mutations.clear()
    readonly = gsa._scheduler_state_readonly(gsa._load_monitor_state(), gsa._monitor_job_id)
    assert set(readonly) == set(configured), f"键集漂移: readonly={set(readonly) ^ set(configured)}"
    assert fake.mutations == [] and mutated  # configure 本身应有变更动作（对照有效性）


def test_r43_cat2_trading_status_gets_never_mutate_scheduler(monkeypatch) -> None:
    import api.trading as tr

    fake = _RecordingScheduler(
        {
            tr._v4_monitor_job_id: _FakeJob(tr._v4_monitor_job_id),
            tr._gen2_shadow_monitor_job_id: _FakeJob(tr._gen2_shadow_monitor_job_id),
            tr._gen2_strategy_refresh_job_id: _FakeJob(tr._gen2_strategy_refresh_job_id),
        }
    )
    monkeypatch.setattr(tr, "_v4_monitor_scheduler", fake)
    monkeypatch.setattr(tr, "_startup_schedulers_enabled", lambda: True)

    shadow = tr.get_gen2_shadow_buy_monitor_status()
    assert shadow["scheduler_enabled"] is True and shadow["scheduler_wired"] is True
    assert shadow["next_run_time"] is not None

    refresh = tr.get_gen2_strategy_refresh_status()
    assert refresh["scheduler_enabled"] is True and "current_bar_slot" in refresh

    v4 = tr.get_v4_manual_holdings_monitor_status()
    assert v4["scheduler_enabled"] is True

    assert fake.mutations == [], f"GET 链出现调度器变更: {fake.mutations}"


def test_r43_cat2_trading_readonly_keys_match_configure(monkeypatch) -> None:
    import api.trading as tr

    fake = _RecordingScheduler({tr._v4_monitor_job_id: _FakeJob(tr._v4_monitor_job_id)})
    monkeypatch.setattr(tr, "_v4_monitor_scheduler", fake)
    monkeypatch.setattr(tr, "_startup_schedulers_enabled", lambda: True)

    configured = tr._configure_v4_monitor_scheduler()
    fake.mutations.clear()
    readonly = tr._v4_monitor_status_readonly()
    assert set(readonly) == set(configured) | {"scheduler_enabled", "scheduler_wired"}
    assert fake.mutations == []


def test_r43_cat2_notification_status_no_smtp_probe(monkeypatch) -> None:
    import api.trading as tr

    def _boom(*args, **kwargs):  # pragma: no cover - regression tripwire
        raise AssertionError("smtp login probe must not run from status chain")

    monkeypatch.setattr(tr, "_smtp_login_probe", _boom)
    monkeypatch.setattr(
        tr,
        "_load_smtp_config_for_monitor",
        lambda *a, **k: {"to_emails": ["a@b.c"], "server": "smtp.example", "port": 465},
    )
    check = tr._notification_config_status()
    assert check["ok"] is True and check["send_mode"] == "config_only"


def test_r43_cat2_directory_writable_status_never_creates(tmp_path) -> None:
    import api.trading as tr

    missing = tmp_path / "never_created"
    check = tr._directory_writable_status("strategy", "shadow_live_output_dir", missing)
    assert check["ok"] is False and not missing.exists()

    existing = tmp_path / "existing"
    existing.mkdir()
    check2 = tr._directory_writable_status("strategy", "shadow_live_output_dir", existing)
    assert check2["ok"] is True


def test_r43_cat2_gen3_directory_writable_status_never_writes(tmp_path) -> None:
    import api.gen3_state_alpha as gsa

    missing = tmp_path / "never_created_dir"
    check = gsa._directory_writable_status("x", missing)
    assert check["ok"] is False and not missing.exists()
    assert ".write_probe" not in str(list(tmp_path.iterdir()))
