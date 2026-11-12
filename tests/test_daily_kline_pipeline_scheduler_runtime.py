"""RED-stage runtime tests for the daily kline pipeline scheduler wiring.

Task brief:
F:/Aistock/data/data/runtime/task_briefs/20261105_daily_kline_pipeline_scheduler_runtime_red.md
Isolation rework brief:
F:/Aistock/data/data/runtime/task_briefs/20261105_daily_kline_pipeline_scheduler_runtime_red_isolation_repair.md

These tests pin the Owner-approved scheduler RUNTIME contract BEFORE the
wiring exists in ``scheduler/scheduler.py`` (TDD RED stage). Slice 3A
already delivered the pure policy (``scheduler.config.daily_kline_pipeline_due``
plus the ``daily_kline_pipeline`` job config); this slice covers the loop.

Runtime contract under test (for the future GREEN slice):

* ``TaskScheduler._create_scheduled_tasks`` registers exactly one asyncio
  task for the daily pipeline loop when
  ``SCHEDULER_CONFIG["jobs"]["daily_kline_pipeline"]["enabled"]`` is true,
  and none when it is false. Registration is gated on that config flag
  only -- the pipeline is not a Task class, so it must not depend on
  ``self.tasks`` entries.
* ``TaskScheduler._poll_daily_kline_pipeline_once(now=None)`` performs one
  poll step. With ``now=None`` it computes an aware Beijing-time clock
  reading itself; with an injected ``now`` it evaluates exactly that
  datetime. It consults the EXISTING ``daily_kline_pipeline_due`` policy
  exactly once per poll, passing the aware ``now`` and the recorded last
  success date.
* A due poll calls the module-level synchronous seam
  ``scheduler.scheduler.run_daily_kline_pipeline`` exactly once, delegated
  via ``asyncio.to_thread`` (i.e. off the event-loop thread), with exactly
  the approved production keyword options and nothing else::

      source="pkg", trade_date=None, crosscheck_sample=20,
      formal_write=True, stage_write=False

  The default seam implementation must import the real pipeline lazily
  (never at module import time). Success state lives on the scheduler
  instance as ``_daily_kline_pipeline_last_success_date`` (a
  ``datetime.date`` in Beijing time, or ``None``).
* Only ``exit_code == 0`` records the Beijing calendar date of the run and
  suppresses further invocations on that same natural day; nonzero exit
  codes and raised exceptions record nothing and leave the job retryable
  on the next poll.
* ``TaskScheduler._schedule_daily_kline_pipeline()`` is the polling loop
  (``while self._running``) and must exit cleanly on cancellation.

Import/collection safety: this module's import/collection phase loads only
the statically verified project modules -- the docstring-only ``scheduler``
package initializer and the pure-stdlib ``scheduler.config``.  No stub is
installed and ``scheduler.scheduler`` is NOT imported at collection time.
All import isolation happens inside the module-scoped
``_scheduler_runtime_module`` fixture, i.e. after collection, before any
test executes, and before ``scheduler.scheduler`` is imported:

* Fail-closed precheck FIRST: in one pass, before any modification of
  ``sys.modules`` or any parent-package attribute, the fixture checks that
  none of the names it must control -- ``utils``, ``utils.logger``,
  ``scheduler.tasks``, ``scripts``, ``scripts.daily_kline_pipeline`` (plus
  any loaded descendants of those names) and ``scheduler.scheduler``
  itself -- is already imported.  Any conflict refuses to run every test
  in this module with the fixed ``test isolation fail-closed:`` label.
  A real pipeline, task, or client module is never kept or tolerated,
  whatever else ``sys.modules`` already contains.
* Stubs are claimed ONLY by object identity (``is``) against module
  objects this module privately holds; no module attribute, name, or
  other publicly settable marker is trusted.
* All five stub modules are fully built first; only then are they
  inserted into ``sys.modules``, and only then is the one parent-package
  attribute Python's import semantics needs (``tasks`` on the real
  ``scheduler`` package, for ``from scheduler import tasks``) snapshotted
  precisely and set.  Any exception during installation or during the
  ``scheduler.scheduler`` import triggers a best-effort rollback of
  EVERY ``sys.modules`` entry and parent attribute this fixture owns:
  each target is identity-checked independently (absent means already
  cleaned), a target that no longer holds this module's own object --
  including a present-but-``None`` ``sys.modules`` value -- is recorded
  as a conflict and preserved untouched, and every remaining owned
  target is still cleaned; only after all attempts are the collected
  conflicts, if any, summarized in ONE fail-closed ``AssertionError``
  that keeps the original import-phase exception as its ``__cause__``.
  The import loader does not leave the parent ``scheduler`` attribute
  behind on a failed import, so the rollback covers everything touched.
* Teardown restores the pre-fixture state the same best-effort way:
  the module-level guard state is always reset first, then the imported
  ``scheduler.scheduler`` entry, every stub entry, and each snapshotted
  parent-package attribute are attempted independently -- entries are
  removed only while they still hold this module's own objects (a
  concurrently replaced entry, or one whose value became ``None``, is a
  conflict and is never overwritten or deleted), parent attributes are
  restored strictly by object identity, and one conflicted target never
  skips the cleanup of the others; all conflicts are reported together
  in a single fail-closed error raised only after every cleanup attempt
  has run.

Why those names: ``scheduler/__init__.py`` is docstring-only and
``scheduler/config.py`` is pure stdlib, so both load for real;
``scheduler/tasks/__init__.py`` would import the six real task modules
(database/config heavy) and ``utils/__init__.py`` would import
database/config/logger/helpers (including loguru sinks that create log
directories), so ``utils``/``utils.logger``/``scheduler.tasks`` get inert
stubs; the real ``scripts/daily_kline_pipeline`` import header reaches
``utils.db_write_lock``, market sources and market policy, so ``scripts``
and ``scripts.daily_kline_pipeline`` get fail-fast stubs in the same
pre-import step.  The ``scripts`` stub carries an empty ``__path__``
(even if its ``sys.modules`` entry is evicted, a submodule lookup can
never fall through to the real on-disk scripts), and the pipeline stub
raises on ANY attribute reach-through (PEP 562), so the real pipeline
file can never be executed by the loader from this test module and the
real pipeline can never become an acceptable object on any branch.  The
runtime-seam monkeypatch in the autouse fixture below is only a SECOND
line of defense on top of this pre-import isolation, never a substitute
for it.
Isolation is proven, not assumed: the autouse fixture asserts before
every test that each isolated ``sys.modules`` name still holds exactly
this module's stub object, and after every test that none was replaced
or removed, that no ``scripts.*`` entry appeared (the stub keys are in
the before-snapshot, so a stub never counts as a real import), and that
the fail-fast stub was never touched.  No task class, DatabaseManager,
config loader, or loguru sink is ever constructed by this module;
schedulers are built via ``__new__`` without the production
initializer. Missing runtime pieces must fail as plain assertion
failures with fixed contract labels -- never as import or collection
errors (the two deliberate exceptions: the fail-closed isolation refusal
above, and a fail-fast stub hit; both abort loudly by design).
These tests use only synthetic in-memory objects and touch no data
files, runtime state, logs, reports, databases, network, or the real
pipeline.
"""

import asyncio
import datetime as dt
import importlib
import sys
import threading
import types

import pytest

import scheduler.config as scheduler_config

# Asia/Shanghai is a fixed +08:00 offset (no DST); a static timezone keeps
# the synthetic inputs independent of tzdata availability.
BEIJING = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")

THU_1615 = dt.datetime(2026, 11, 5, 16, 15, tzinfo=BEIJING)  # Thursday, due boundary
THU_1614 = dt.datetime(2026, 11, 5, 16, 14, tzinfo=BEIJING)  # one minute early
THU_LATE = dt.datetime(2026, 11, 5, 23, 59, tzinfo=BEIJING)  # same Beijing date
FRI_1615 = dt.datetime(2026, 11, 6, 16, 15, tzinfo=BEIJING)  # next natural day
SAT_1615 = dt.datetime(2026, 11, 7, 16, 15, tzinfo=BEIJING)  # Saturday, still due

APPROVED_OPTIONS = {
    "source": "pkg",
    "trade_date": None,
    "crosscheck_sample": 20,
    "formal_write": True,
    "stage_write": False,
}


# ---------------------------------------------------------------------------
# Import isolation (installed by the module fixture BEFORE any test runs
# and BEFORE scheduler.scheduler is imported; nothing runs at collection)
# ---------------------------------------------------------------------------

# The ``scheduler.scheduler`` runtime module under test.  It is imported
# by the ``_scheduler_runtime_module`` fixture with the isolation stubs in
# place and assigned here for the test bodies; it is ``None`` outside a
# run of this module's tests.
scheduler_runtime: "types.ModuleType | None" = None

# Guard state for the ``scripts.daily_kline_pipeline`` pre-import
# isolation.  ``stub`` holds the fail-fast stub module object while this
# module's tests run (the identity anchor for the claims below), and
# ``touched`` records whether any code ever reached the pipeline seam
# through the stub.
_pipeline_guard = {"stub": None, "touched": False}

# Sentinel for "attribute/entry absent" snapshots.
_MISSING = object()

# ``sys.modules`` names this module must control while
# ``scheduler.scheduler`` is imported for its tests.
_STUB_NAMES = (
    "utils",
    "utils.logger",
    "scheduler.tasks",
    "scripts",
    "scripts.daily_kline_pipeline",
)
_RUNTIME_NAME = "scheduler.scheduler"


class _NoopLogger:
    """Inert stand-in for the loguru-based project logger."""

    def __init__(self, *_args, **_kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def critical(self, *args, **kwargs):
        pass


def _build_utils_stub(name):
    return types.ModuleType(name)


def _build_logger_stub(name):
    stub = types.ModuleType(name)
    setattr(stub, "get_logger", lambda *_args, **_kwargs: _NoopLogger())
    return stub


def _build_tasks_stub(name):
    return types.ModuleType(name)


def _build_scripts_stub(name):
    stub = types.ModuleType(name)
    # Empty package path: even if this sys.modules entry is evicted, a
    # submodule lookup under ``scripts`` fails fast instead of ever
    # finding the real F:/Aistock/app/scripts/ files on disk.
    stub.__path__ = []
    return stub


def _build_pipeline_failfast_stub(module_name):
    stub = types.ModuleType(module_name)

    def _fail_fast(name):
        # Any attribute reach-through means some code expected the REAL
        # pipeline module here.  Fail before real pipeline code,
        # databases, network, or formal tables can be touched.
        _pipeline_guard["touched"] = True
        raise AssertionError(
            "contract violation: test path reached the real daily "
            f"kline pipeline seam ({module_name}.{name}); the real "
            "pipeline, databases, network and formal tables must never "
            "be touched from tests"
        )

    stub.__getattr__ = _fail_fast  # PEP 562 module hook
    return stub


def _isolation_conflicts():
    """Return every isolated name (or loaded descendant) already imported.

    This is the one-pass, read-only precheck the fixture runs BEFORE any
    modification: ``scheduler.scheduler`` itself, every stub name, and
    every already-loaded descendant of those names.
    """
    conflicts = []
    for name in (_RUNTIME_NAME,) + _STUB_NAMES:
        if name in sys.modules:
            conflicts.append(name)
        prefix = name + "."
        conflicts.extend(sorted(key for key in sys.modules if key.startswith(prefix)))
    return sorted(set(conflicts))


def _remove_our_sys_modules_entry(name, our_module, registry, conflicts):
    """Restore ``registry[name]`` to its pre-fixture state (absent).

    ``registry`` is the mapping being cleaned (``sys.modules`` for the
    fixture; a plain local dict for the pure unit tests).  Outcomes:

    * key absent -- already at the pre-fixture state, nothing to do
      (distinct from the next case: a missing key is NOT a conflict);
    * value ``is`` ``our_module`` -- this fixture still owns the entry,
      so it is deleted;
    * key present with value ``None`` -- NOT this fixture's object and
      NOT absent: a conflict; the entry is preserved untouched and
      recorded;
    * any other object -- a conflict; preserved untouched and recorded.

    Conflicts are appended to ``conflicts`` instead of being raised, so
    a single conflicted entry can never stop the cleanup of the
    remaining entries; the caller summarizes them once, after every
    attempt has run.
    """
    if name not in registry:
        return
    current = registry[name]
    if current is our_module:
        del registry[name]
        return
    if current is None:
        conflicts.append(
            f"test isolation fail-closed: sys.modules[{name!r}] is "
            "present with value None instead of this test's own object; "
            "a present-but-None entry is not this fixture's and is not "
            "absent, so it is preserved untouched"
        )
        return
    conflicts.append(
        f"test isolation fail-closed: sys.modules[{name!r}] no longer "
        "holds this test's own object; refusing to overwrite or remove "
        "an object owned by another test/session"
    )


def _restore_parent_attr(parent, attr, snapshot, our_object, conflicts):
    """Restore ``parent.attr`` to its pre-fixture value, by identity.

    ``snapshot`` is the original object, or ``_MISSING`` when the
    attribute was absent before this fixture ran; ``our_object`` is what
    this fixture installed there.  Restoration is best-effort and
    identity-gated: the attribute is only written when it still holds
    ``our_object`` (or is absent, in which case putting the snapshotted
    original back overwrites no live object).  An attribute that was
    concurrently replaced by a foreign object is a conflict: it is
    preserved untouched and recorded in ``conflicts`` instead of being
    raised, so one conflicted attribute never stops the restoration of
    the remaining ones.
    """
    current = getattr(parent, attr, _MISSING)
    if current is our_object:
        if snapshot is _MISSING:
            delattr(parent, attr)
        else:
            setattr(parent, attr, snapshot)
        return
    if current is _MISSING:
        if snapshot is _MISSING:
            return  # absent before and absent now: already restored
        # The attribute is currently absent (deleted during the test);
        # putting the snapshotted original back overwrites no live object.
        setattr(parent, attr, snapshot)
        return
    if current is snapshot:
        return  # already at the pre-fixture value
    parent_desc = getattr(parent, "__name__", None)
    if not isinstance(parent_desc, str):
        parent_desc = type(parent).__name__
    conflicts.append(
        f"test isolation fail-closed: attribute {attr!r} of module "
        f"{parent_desc!r} was replaced by an object this test does "
        "not own; refusing to overwrite it"
    )


def _cleanup_conflicts_error(phase, conflicts):
    """Build the single fail-closed error summarizing cleanup conflicts.

    Called only AFTER every best-effort cleanup attempt has run; the
    caller preserves any in-flight exception as this error's ``__cause__``
    (rollback path) or raises it during teardown.  One conflicted target
    never silenced the others: every conflict recorded during the sweep
    appears in this one summary.
    """
    return AssertionError(
        "test isolation fail-closed: "
        + phase
        + " could not restore every sys.modules entry / parent-package "
        "attribute this fixture owned. Every owned target was still "
        "attempted; the conflicted targets below were preserved "
        "untouched (no object owned by another test/session -- including "
        "a present-but-None sys.modules value -- was deleted or "
        "overwritten):\n  - "
        + "\n  - ".join(conflicts)
    )


@pytest.fixture(scope="module")
def _scheduler_runtime_module():
    """Install import-isolation stubs, import ``scheduler.scheduler``
    against them, and restore the pre-fixture state afterwards.

    Ordering guarantees (full contract in the module docstring):

    1. one fail-closed precheck pass over every isolated name, their
       loaded descendants, and ``scheduler.scheduler`` BEFORE anything
       is modified;
    2. all stub modules are built first, then inserted into
       ``sys.modules``; then the single parent-package attribute the
       import semantics need is snapshotted and set; then
       ``scheduler.scheduler`` is imported;
    3. any exception during (2) triggers a best-effort rollback of every
       entry/attribute this fixture owns (identity-checked per target;
       conflicted targets -- foreign object or present-but-None entry --
       are recorded and preserved untouched while the rest are still
       cleaned) and re-raises: unchanged when cleanup was conflict-free,
       otherwise as one aggregated fail-closed error whose ``__cause__``
       is the original import-phase exception, so the session never
       continues half-isolated;
    4. teardown resets the guard state first, then removes the imported
       runtime entry and every stub entry and restores the snapshotted
       parent attributes, each identity-checked and best-effort
       (present-but-None entries and foreign replacements are conflicts
       and are never overwritten), reporting all conflicts together in
       one fail-closed error only after every attempt has run.
    """
    global scheduler_runtime

    conflicts = _isolation_conflicts()
    if conflicts:
        raise RuntimeError(
            "test isolation fail-closed: modules already imported that "
            f"this test must isolate ({', '.join(conflicts)}); importing "
            "scheduler.scheduler now could bind real production modules "
            "or their import chains into the test, and overwriting "
            "sys.modules would corrupt the surrounding test session. "
            "Refusing to run any test; nothing has been modified."
        )

    scheduler_pkg = sys.modules.get("scheduler")
    assert scheduler_pkg is not None, (
        "test isolation fail-closed: the scheduler package must already "
        "be imported (this module imports scheduler.config at collection "
        "time) before the isolation fixture runs"
    )
    attrs_before = {
        # set by this fixture, for ``from scheduler import tasks``
        "tasks": getattr(scheduler_pkg, "tasks", _MISSING),
        # set by the import machinery when scheduler.scheduler loads
        "scheduler": getattr(scheduler_pkg, "scheduler", _MISSING),
    }

    utils_stub = _build_utils_stub("utils")
    logger_stub = _build_logger_stub("utils.logger")
    setattr(utils_stub, "logger", logger_stub)  # keep parent/child consistent
    tasks_stub = _build_tasks_stub("scheduler.tasks")
    scripts_stub = _build_scripts_stub("scripts")
    pipeline_stub = _build_pipeline_failfast_stub("scripts.daily_kline_pipeline")
    setattr(scripts_stub, "daily_kline_pipeline", pipeline_stub)
    stubs = {
        "utils": utils_stub,
        "utils.logger": logger_stub,
        "scheduler.tasks": tasks_stub,
        "scripts": scripts_stub,
        "scripts.daily_kline_pipeline": pipeline_stub,
    }

    inserted = []
    try:
        for name, module in stubs.items():
            sys.modules[name] = module
            inserted.append(name)
        setattr(scheduler_pkg, "tasks", tasks_stub)  # snapshotted above
        runtime_module = importlib.import_module(_RUNTIME_NAME)
    except BaseException as exc:
        # Roll back EVERYTHING this fixture inserted so far, best-effort
        # over all owned targets at once: each ``sys.modules`` entry and
        # parent attribute is identity-checked independently, conflicted
        # targets (foreign object, or a present-but-None entry) are
        # recorded and preserved untouched, and the remaining owned
        # targets are still cleaned.  Only after every attempt are the
        # conflicts summarized: the original import-phase exception is
        # re-raised unchanged when cleanup was conflict-free, and
        # otherwise preserved as the ``__cause__`` of the single
        # aggregated fail-closed error below.  The import loader does
        # not leave the parent ``scheduler`` attribute behind on a
        # failed import, so the entries restored here cover everything
        # this fixture touched.
        conflicts = []
        for name in reversed(inserted):
            _remove_our_sys_modules_entry(name, stubs[name], sys.modules, conflicts)
        _restore_parent_attr(
            scheduler_pkg, "tasks", attrs_before["tasks"], tasks_stub, conflicts
        )
        _restore_parent_attr(
            scheduler_pkg,
            "scheduler",
            attrs_before["scheduler"],
            sys.modules.get(_RUNTIME_NAME),
            conflicts,
        )
        if conflicts:
            raise _cleanup_conflicts_error(
                "rollback after a failed scheduler.scheduler import",
                conflicts,
            ) from exc
        raise

    _pipeline_guard["stub"] = pipeline_stub
    _pipeline_guard["touched"] = False
    scheduler_runtime = runtime_module
    try:
        yield types.SimpleNamespace(
            runtime=runtime_module,
            stubs=dict(stubs),
            pipeline_stub=pipeline_stub,
        )
    finally:
        # Teardown ALWAYS resets the module-level guard state first, then
        # attempts EVERY owned cleanup (identity-checked) even when
        # earlier targets conflict; conflicts are collected and reported
        # together, once, after all attempts, as a single fail-closed
        # error.  A foreign object or a present-but-None entry value is
        # never deleted or overwritten.
        scheduler_runtime = None
        _pipeline_guard["stub"] = None
        _pipeline_guard["touched"] = False
        conflicts = []
        _remove_our_sys_modules_entry(
            _RUNTIME_NAME, runtime_module, sys.modules, conflicts
        )
        for name in reversed(inserted):
            _remove_our_sys_modules_entry(name, stubs[name], sys.modules, conflicts)
        _restore_parent_attr(
            scheduler_pkg,
            "scheduler",
            attrs_before["scheduler"],
            runtime_module,
            conflicts,
        )
        _restore_parent_attr(
            scheduler_pkg, "tasks", attrs_before["tasks"], tasks_stub, conflicts
        )
        if conflicts:
            raise _cleanup_conflicts_error(
                "scheduler runtime fixture teardown", conflicts
            )


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


def _make_scheduler():
    """Build a TaskScheduler without the production initializer and without
    any task class; only the daily-pipeline contract attributes are set."""
    assert scheduler_runtime is not None, (
        "test isolation fail-closed: scheduler_runtime must be imported "
        "by the _scheduler_runtime_module fixture before tests run"
    )
    sched = scheduler_runtime.TaskScheduler.__new__(scheduler_runtime.TaskScheduler)
    sched.tasks = {}
    sched._running = False
    sched._scheduled_tasks = []
    sched._daily_kline_pipeline_last_success_date = None
    return sched


def _require_poll(sched):
    poll = getattr(sched, "_poll_daily_kline_pipeline_once", None)
    assert poll is not None, (
        "contract: TaskScheduler._poll_daily_kline_pipeline_once missing"
    )
    return poll


class _PipelineStub:
    """Recording stand-in for the pipeline seam.

    ``results`` items are exit codes, or exception instances to raise; the
    last item repeats for any further calls.
    """

    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.thread_ids = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.thread_ids.append(threading.get_ident())
        if len(self.results) > 1:
            result = self.results.pop(0)
        else:
            result = self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def _fail_fast_pipeline_seam(monkeypatch, _scheduler_runtime_module):
    """Fail loudly if any test path reaches the real pipeline.

    This is the SECOND line of defense, layered on top of the pre-import
    ``sys.modules`` isolation installed by ``_scheduler_runtime_module``
    (which runs first and actually prevents the real pipeline module from
    ever being loaded by the import machinery).

    Front-loaded identity checks: before each test, every isolated
    ``sys.modules`` name -- including the imported runtime module and the
    fail-fast pipeline stub -- must still hold exactly the object this
    module installed, and the module-level seam is monkeypatched to a
    forbidden callable.  Afterwards this proves the real pipeline module
    was never imported or touched during the test (the stub keys are in
    the before-snapshot, so the stub never counts as a real import).
    """
    iso = _scheduler_runtime_module

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "contract violation: real daily kline pipeline invoked in test"
        )

    monkeypatch.setattr(
        iso.runtime, "run_daily_kline_pipeline", _forbidden, raising=False
    )

    for name, module in iso.stubs.items():
        assert sys.modules.get(name) is module, (
            f"contract violation: isolation stub for {name!r} was "
            "replaced or removed before the test ran"
        )
    assert sys.modules.get(_RUNTIME_NAME) is iso.runtime, (
        "contract violation: the isolated scheduler.scheduler module was "
        "replaced or removed before the test ran"
    )
    assert _pipeline_guard["stub"] is iso.pipeline_stub, (
        "contract violation: the fail-fast pipeline stub installed "
        "before the scheduler.scheduler import must still be in place "
        "before each test"
    )

    before = set(sys.modules)
    yield
    loaded = sorted(set(sys.modules) - before)
    real_pipeline = [name for name in loaded if name.startswith("scripts.")]
    assert not real_pipeline, (
        "contract violation: real pipeline modules imported during test: "
        f"{real_pipeline}"
    )
    current = sys.modules.get("scripts.daily_kline_pipeline")
    assert current is iso.pipeline_stub, (
        "contract violation: real pipeline module must not be "
        "imported during test (the fail-fast stub was replaced or "
        "removed)"
    )
    assert not _pipeline_guard["touched"], (
        "contract violation: test path reached the real daily kline "
        "pipeline seam through the fail-fast stub"
    )


# ---------------------------------------------------------------------------
# 0. Cleanup-robustness pure unit tests (local dict registry + synthetic
#    types.ModuleType / SimpleNamespace only; the real sys.modules, the
#    real pipeline, real data, network, and databases are never touched)
# ---------------------------------------------------------------------------


def test_best_effort_entry_cleanup_preserves_foreign_and_none_and_cleans_all_owned():
    """Conflicted entries are preserved and reported; every OTHER
    fixture-owned entry is still removed, and an absent key is clean.

    The registry is a plain local dict standing in for ``sys.modules``;
    nothing here reads or writes the real ``sys.modules``.
    """
    registry = {}
    ours_a = types.ModuleType("synthetic.ours_a")
    ours_b = types.ModuleType("synthetic.ours_b")
    ours_absent = types.ModuleType("synthetic.already_absent")
    foreign = types.ModuleType("synthetic.foreign")

    registry["synthetic.ours_a"] = ours_a
    # An owned name taken over by a foreign replacement mid-test.
    registry["synthetic.taken_by_foreign"] = foreign
    # A key that is present but holds None (distinct from absent).
    registry["synthetic.none_valued"] = None
    registry["synthetic.ours_b"] = ours_b
    # "synthetic.already_absent" is deliberately NOT inserted.

    conflicts = []
    _remove_our_sys_modules_entry("synthetic.ours_a", ours_a, registry, conflicts)
    _remove_our_sys_modules_entry(
        "synthetic.taken_by_foreign", ours_a, registry, conflicts
    )
    _remove_our_sys_modules_entry(
        "synthetic.none_valued", ours_b, registry, conflicts
    )
    _remove_our_sys_modules_entry("synthetic.ours_b", ours_b, registry, conflicts)
    _remove_our_sys_modules_entry(
        "synthetic.already_absent", ours_absent, registry, conflicts
    )

    assert "synthetic.ours_a" not in registry, (
        "contract: an entry still holding this test's own object must be "
        "removed even when other entries conflict"
    )
    assert "synthetic.ours_b" not in registry, (
        "contract: cleanup of a later owned entry must not be skipped "
        "because an earlier entry conflicted"
    )
    assert registry["synthetic.taken_by_foreign"] is foreign, (
        "contract: a foreign replacement must be preserved, never "
        "overwritten or deleted"
    )
    assert "synthetic.none_valued" in registry and registry["synthetic.none_valued"] is None, (
        "contract: a present-but-None entry must be preserved, never "
        "deleted (it is a conflict, not an absent key)"
    )
    assert len(conflicts) == 2, (
        "contract: exactly the foreign replacement and the "
        "present-but-None entry are reported; an absent key and cleanly "
        "removed owned entries are not conflicts"
    )
    reported = "\n".join(conflicts)
    assert "synthetic.taken_by_foreign" in reported
    assert "synthetic.none_valued" in reported
    assert reported.count("test isolation fail-closed:") == 2

    # The aggregated summary keeps every conflict visible in ONE error.
    error = _cleanup_conflicts_error("synthetic rollback phase", conflicts)
    assert isinstance(error, AssertionError)
    summary = str(error)
    assert "test isolation fail-closed:" in summary
    assert "synthetic rollback phase" in summary
    assert "synthetic.taken_by_foreign" in summary
    assert "synthetic.none_valued" in summary


def test_parent_attr_restore_is_identity_gated_and_reports_conflicts_without_stopping():
    """Parent-attribute restoration on synthetic parents (a real-style
    ``types.ModuleType`` and a ``SimpleNamespace``): ours -> restored,
    absent -> snapshot put back / nothing to do, foreign -> preserved and
    reported while the remaining attributes are still restored."""
    owned = types.ModuleType("synthetic_parent.child_owned")
    original = types.ModuleType("synthetic_parent.child_original")
    foreign = types.ModuleType("synthetic_parent.child_foreign")

    module_parent = types.ModuleType("synthetic_parent")
    ns_parent = types.SimpleNamespace()

    conflicts = []

    # Ours -> the snapshotted pre-fixture value is restored.
    setattr(module_parent, "tasks", owned)
    _restore_parent_attr(module_parent, "tasks", original, owned, conflicts)
    assert module_parent.tasks is original, (
        "contract: an attribute still holding this test's own object "
        "must be restored to the snapshotted pre-fixture value"
    )

    # Ours over an absent-in-snapshot attribute -> deleted (works on a
    # SimpleNamespace parent just as on a module).
    ns_parent.scheduler = owned
    _restore_parent_attr(ns_parent, "scheduler", _MISSING, owned, conflicts)
    assert not hasattr(ns_parent, "scheduler"), (
        "contract: an own object installed over an absent-in-snapshot "
        "attribute must be deleted, on any parent container type"
    )

    # Absent now, snapshot had a value -> the original is put back
    # (overwrites no live object).
    setattr(module_parent, "scheduler", owned)
    delattr(module_parent, "scheduler")
    _restore_parent_attr(module_parent, "scheduler", original, owned, conflicts)
    assert module_parent.scheduler is original, (
        "contract: a deleted attribute whose snapshot held the original "
        "must have the original put back"
    )

    # Absent before and absent now -> already clean, no conflict.
    _restore_parent_attr(module_parent, "never_there", _MISSING, owned, conflicts)
    assert not hasattr(module_parent, "never_there")

    # Foreign replacement -> preserved and reported, and restoration of
    # any further attribute still proceeds.
    setattr(module_parent, "tasks", foreign)
    _restore_parent_attr(module_parent, "tasks", original, owned, conflicts)
    assert module_parent.tasks is foreign, (
        "contract: a foreign replacement must be preserved, never "
        "overwritten"
    )
    ns_parent.tasks = foreign
    _restore_parent_attr(ns_parent, "tasks", original, owned, conflicts)
    assert ns_parent.tasks is foreign, (
        "contract: a foreign replacement on a non-module parent must "
        "also be preserved and reported"
    )

    assert len(conflicts) == 2, (
        "contract: exactly the two foreign replacements are reported; "
        "identity-clean restorations are not conflicts"
    )
    reported = "\n".join(conflicts)
    assert "test isolation fail-closed:" in reported
    assert "tasks" in reported
    # The module parent is named by __name__; the SimpleNamespace parent
    # (no __name__) degrades to its type name instead of crashing.
    assert "synthetic_parent" in reported
    assert "SimpleNamespace" in reported


# ---------------------------------------------------------------------------
# 1. Registration gating
# ---------------------------------------------------------------------------


def test_create_scheduled_tasks_registers_daily_kline_pipeline_when_enabled():
    async def scenario():
        sched = _make_scheduler()
        job = scheduler_config.SCHEDULER_CONFIG["jobs"]["daily_kline_pipeline"]
        assert job.get("enabled") is True, "precondition: job enabled in config"
        await sched._create_scheduled_tasks()
        assert len(sched._scheduled_tasks) == 1, (
            "contract: enabled daily_kline_pipeline must register exactly one "
            "scheduled task (config-gated, independent of self.tasks)"
        )
        for task in sched._scheduled_tasks:
            task.cancel()
        await asyncio.wait_for(
            asyncio.gather(*sched._scheduled_tasks, return_exceptions=True), 5.0
        )

    asyncio.run(scenario())


def test_create_scheduled_tasks_skips_daily_kline_pipeline_when_disabled(monkeypatch):
    async def scenario():
        sched = _make_scheduler()
        await sched._create_scheduled_tasks()
        assert sched._scheduled_tasks == [], (
            "contract: disabled daily_kline_pipeline must not register a task"
        )

    job = scheduler_config.SCHEDULER_CONFIG["jobs"]["daily_kline_pipeline"]
    monkeypatch.setitem(job, "enabled", False)
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 2./3. Due gating, exact approved options, off-loop execution, no real import
# ---------------------------------------------------------------------------


def test_due_poll_passes_exact_approved_options_off_loop_thread_and_records_success(
    monkeypatch,
):
    async def scenario():
        sched = _make_scheduler()
        poll = _require_poll(sched)
        await poll(now=THU_1615)
        assert len(stub.calls) == 1, (
            "contract: due poll must invoke the pipeline seam exactly once"
        )
        assert stub.calls[0] == APPROVED_OPTIONS, (
            "contract: pipeline seam must receive exactly the approved "
            "production options (source=pkg, trade_date=None, "
            "crosscheck_sample=20, formal_write=True, stage_write=False)"
        )
        assert stub.thread_ids[0] != threading.get_ident(), (
            "contract: synchronous pipeline call must run off the event-loop "
            "thread (asyncio.to_thread or equivalent)"
        )
        assert sched._daily_kline_pipeline_last_success_date == dt.date(2026, 11, 5), (
            "contract: exit_code == 0 must record the Beijing date of the run"
        )

    stub = _PipelineStub([0])
    monkeypatch.setattr(
        scheduler_runtime, "run_daily_kline_pipeline", stub, raising=False
    )
    asyncio.run(scenario())
    assert sys.modules.get("scripts.daily_kline_pipeline") is _pipeline_guard["stub"], (
        "contract: real pipeline module must not be imported by the "
        "test (the fail-fast stub must still be in place)"
    )


def test_poll_before_1615_beijing_is_not_due_and_never_invokes_pipeline(monkeypatch):
    async def scenario():
        sched = _make_scheduler()
        poll = _require_poll(sched)
        await poll(now=THU_1614)
        assert stub.calls == [], (
            "contract: before 16:15 Beijing time the poll must not invoke "
            "the pipeline"
        )
        assert sched._daily_kline_pipeline_last_success_date is None, (
            "contract: an early poll must not record success"
        )

    stub = _PipelineStub([0])
    monkeypatch.setattr(
        scheduler_runtime, "run_daily_kline_pipeline", stub, raising=False
    )
    asyncio.run(scenario())


def test_poll_due_on_saturday_after_1615_invokes_pipeline(monkeypatch):
    async def scenario():
        sched = _make_scheduler()
        poll = _require_poll(sched)
        await poll(now=SAT_1615)
        assert len(stub.calls) == 1, (
            "contract: natural-calendar-day policy has no weekend/holiday "
            "filter; Saturday 16:15 Beijing is due"
        )

    stub = _PipelineStub([0])
    monkeypatch.setattr(
        scheduler_runtime, "run_daily_kline_pipeline", stub, raising=False
    )
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4. Same-Beijing-date success suppression / retryability
# ---------------------------------------------------------------------------


def test_second_poll_same_beijing_day_suppressed_until_next_natural_day(
    monkeypatch,
):
    async def scenario():
        sched = _make_scheduler()
        poll = _require_poll(sched)
        await poll(now=THU_1615)
        assert len(stub.calls) == 1
        await poll(now=THU_LATE)
        assert len(stub.calls) == 1, (
            "contract: a successful run suppresses further polls on the "
            "same Beijing calendar date"
        )
        await poll(now=FRI_1615)
        assert len(stub.calls) == 2, (
            "contract: the next natural day is due again after 16:15"
        )
        assert sched._daily_kline_pipeline_last_success_date == dt.date(2026, 11, 6), (
            "contract: success state advances to the new Beijing date"
        )

    stub = _PipelineStub([0])
    monkeypatch.setattr(
        scheduler_runtime, "run_daily_kline_pipeline", stub, raising=False
    )
    asyncio.run(scenario())


def test_nonzero_exit_code_is_not_success_and_remains_retryable(monkeypatch):
    async def scenario():
        sched = _make_scheduler()
        poll = _require_poll(sched)
        await poll(now=THU_1615)
        assert len(stub.calls) == 1
        assert sched._daily_kline_pipeline_last_success_date is None, (
            "contract: nonzero exit_code must not record success"
        )
        await poll(now=THU_1615 + dt.timedelta(minutes=1))
        assert len(stub.calls) == 2, (
            "contract: a nonzero result stays retryable on the next poll"
        )
        await poll(now=THU_1615 + dt.timedelta(minutes=2))
        assert len(stub.calls) == 3
        assert sched._daily_kline_pipeline_last_success_date == dt.date(2026, 11, 5), (
            "contract: a later exit_code == 0 still records success"
        )
        await poll(now=THU_1615 + dt.timedelta(minutes=3))
        assert len(stub.calls) == 3, (
            "contract: after success the rest of the day is suppressed"
        )

    stub = _PipelineStub([1, 1, 0])
    monkeypatch.setattr(
        scheduler_runtime, "run_daily_kline_pipeline", stub, raising=False
    )
    asyncio.run(scenario())


def test_pipeline_error_is_not_success_and_remains_retryable(monkeypatch):
    async def scenario():
        sched = _make_scheduler()
        poll = _require_poll(sched)
        try:
            await poll(now=THU_1615)
        except Exception:
            pass  # propagating is allowed; the loop wrapper owns retry
        assert len(stub.calls) == 1
        assert sched._daily_kline_pipeline_last_success_date is None, (
            "contract: a raised pipeline error must not record success"
        )
        await poll(now=THU_1615 + dt.timedelta(minutes=1))
        assert len(stub.calls) == 2, (
            "contract: a failed run stays retryable on the next poll"
        )
        assert sched._daily_kline_pipeline_last_success_date == dt.date(2026, 11, 5)

    stub = _PipelineStub([RuntimeError("pipeline exploded"), 0])
    monkeypatch.setattr(
        scheduler_runtime, "run_daily_kline_pipeline", stub, raising=False
    )
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 2. The runtime consults the existing policy with an aware injected clock
# ---------------------------------------------------------------------------


class _PolicySpy:
    """Delegating spy around the real ``daily_kline_pipeline_due``."""

    def __init__(self, real):
        self._real = real
        self.calls = []

    def __call__(self, now, last_successful_run_date):
        self.calls.append((now, last_successful_run_date))
        return self._real(now, last_successful_run_date)


def test_runtime_consults_real_policy_with_aware_injected_clock(monkeypatch):
    async def scenario():
        sched = _make_scheduler()
        poll = _require_poll(sched)
        await poll(now=THU_1615)
        assert len(spy.calls) == 1, (
            "contract: a poll must consult daily_kline_pipeline_due exactly "
            "once, passing the injected now and the recorded last date"
        )
        assert spy.calls[0] == (THU_1615, None), (
            "contract: first poll passes the aware injected now and no "
            "last success date"
        )
        await poll(now=THU_LATE)
        assert len(spy.calls) == 2
        assert spy.calls[1] == (THU_LATE, dt.date(2026, 11, 5)), (
            "contract: the recorded Beijing success date feeds back into "
            "the policy on the next poll"
        )
        for now_arg, _last in spy.calls:
            assert now_arg.tzinfo is not None and now_arg.utcoffset() is not None, (
                "contract: the policy clock must be timezone-aware"
            )

    real = scheduler_config.daily_kline_pipeline_due
    spy = _PolicySpy(real)
    monkeypatch.setattr(
        scheduler_runtime, "daily_kline_pipeline_due", spy, raising=False
    )
    monkeypatch.setattr(scheduler_config, "daily_kline_pipeline_due", spy)
    stub = _PipelineStub([0])
    monkeypatch.setattr(
        scheduler_runtime, "run_daily_kline_pipeline", stub, raising=False
    )
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 2b. The default clock: poll() with no injected now
# ---------------------------------------------------------------------------


def test_poll_without_now_uses_aware_beijing_default_clock_once(monkeypatch):
    """``poll()`` with no ``now`` must read an aware Beijing clock itself.

    Deterministic regardless of wall-clock time: the fake policy always
    returns not-due, so the pipeline seam can never be invoked (the
    fail-fast autouse seam is the backstop); this test never depends on
    whether the real current time is past 16:15 Beijing.
    """

    class _NotDuePolicySpy:
        """Fake ``daily_kline_pipeline_due`` that always returns False."""

        def __init__(self):
            self.calls = []

        def __call__(self, now, last_successful_run_date):
            self.calls.append((now, last_successful_run_date))
            return False

    async def scenario():
        sched = _make_scheduler()
        poll = _require_poll(sched)
        await poll()  # now=None: the runtime must obtain the clock itself
        assert len(spy.calls) == 1, (
            "contract: a poll without an injected now must consult "
            "daily_kline_pipeline_due exactly once"
        )
        received_now, received_last = spy.calls[0]
        assert isinstance(received_now, dt.datetime), (
            "contract: the default clock value passed to "
            "daily_kline_pipeline_due must be a datetime"
        )
        assert (
            received_now.tzinfo is not None
            and received_now.utcoffset() is not None
        ), (
            "contract: the default clock value must be timezone-aware"
        )
        assert received_now.utcoffset() == dt.timedelta(hours=8), (
            "contract: with now=None the runtime must obtain the clock "
            "itself as aware Beijing time (UTC offset +08:00)"
        )
        assert received_last is None, (
            "contract: the first poll passes no last success date"
        )
        assert stub.calls == [], (
            "contract: a not-due poll (fake policy) must never invoke "
            "the pipeline seam"
        )

    spy = _NotDuePolicySpy()
    monkeypatch.setattr(
        scheduler_runtime, "daily_kline_pipeline_due", spy, raising=False
    )
    monkeypatch.setattr(scheduler_config, "daily_kline_pipeline_due", spy)
    stub = _PipelineStub([0])
    monkeypatch.setattr(
        scheduler_runtime, "run_daily_kline_pipeline", stub, raising=False
    )
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 5. Cancellation exits the loop cleanly
# ---------------------------------------------------------------------------


def test_daily_loop_cancellation_exits_cleanly():
    async def scenario():
        sched = _make_scheduler()
        sched._running = True
        loop_method = getattr(sched, "_schedule_daily_kline_pipeline", None)
        assert loop_method is not None, (
            "contract: TaskScheduler._schedule_daily_kline_pipeline missing"
        )
        started = asyncio.Event()

        async def blocking_poll(now=None):
            started.set()
            await asyncio.Event().wait()  # blocks until cancelled

        sched._poll_daily_kline_pipeline_once = blocking_poll
        task = asyncio.create_task(loop_method())
        await asyncio.wait_for(started.wait(), 5.0)
        task.cancel()
        await asyncio.wait_for(
            asyncio.gather(task, return_exceptions=True), 5.0
        )
        assert task.done(), (
            "contract: a cancelled daily pipeline loop must exit cleanly"
        )

    asyncio.run(scenario())
