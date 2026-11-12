"""Offline execution contracts for the kline_daily schema migration runner."""

from __future__ import annotations

import json
import re
from ast import literal_eval

import pytest

from scripts.execute_kline_daily_schema_migration import (
    CANDIDATE_TABLE,
    DEFAULT_JOURNAL_PATH,
    PRE_MIGRATION_TABLE,
    REVERSE_SWAP_SQL,
    SOURCE_TABLE,
    SWAP_SQL,
    JsonlJournal,
    _insert_month_sql,
    main,
    run_preflight,
)

EPOCH = "1970-01-01 00:00:00"
LOCK_TABLE = "stock.write_lock_kline_daily_schema_migration"
KLINE_COLUMNS = {
    "code": "String",
    "trade_date": "Date",
    "open": "Float64",
    "high": "Float64",
    "low": "Float64",
    "close": "Float64",
    "volume": "Float64",
    "amount": "Float64",
    "amplitude": "Float64",
    "change_pct": "Float64",
    "change_amount": "Float64",
    "turnover_rate": "Float64",
    "created_at": "Nullable(DateTime)",
    "id": "UInt64",
}
ALLOWED_TABLES = {
    SOURCE_TABLE,
    CANDIDATE_TABLE,
    PRE_MIGRATION_TABLE,
    LOCK_TABLE,
    "system.tables",
    "system.columns",
    "system.parts",
}


def _row(code, trade_date, *, row_id, created_at):
    return {
        "code": code,
        "trade_date": trade_date,
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": float(row_id),
        "amount": 10.5,
        "amplitude": 0.1,
        "change_pct": 0.2,
        "change_amount": 0.3,
        "turnover_rate": 0.4,
        "created_at": created_at,
        "id": row_id,
    }


def _default_rows():
    return [
        _row("000001.SZ", "2020-01-02", row_id=1, created_at="2026-10-01 08:00:00"),
        _row("600000.SH", "2020-01-03", row_id=2, created_at=None),
        _row("000001.SZ", "2020-02-05", row_id=3, created_at="2026-10-02 09:00:00"),
    ]


class _Result:
    def __init__(self, names, rows):
        self.column_names = names
        self.result_rows = rows


class _MigrationClient:
    """Stateful offline ClickHouse double: moves rows for CREATE/INSERT/RENAME/DROP."""

    def __init__(
        self,
        *,
        rows=None,
        unique_keys=None,
        neeq_rows=0,
        engine_full="ReplacingMergeTree ORDER BY (code, trade_date)",
        partition_key="",
        sorting_key="code, trade_date",
        lose_candidate_row_month=None,
        post_swap_engine_full=None,
        duplicate_candidate_id=False,
    ):
        rows = _default_rows() if rows is None else [dict(row) for row in rows]
        self.snapshot_rows = [dict(row) for row in rows]
        self.tables = {
            SOURCE_TABLE: {
                "engine": "ReplacingMergeTree",
                "engine_full": engine_full,
                "partition_key": partition_key,
                "sorting_key": sorting_key,
                "create_table_query": "CREATE TABLE stock.kline_daily ...",
                "columns": dict(KLINE_COLUMNS),
                "rows": rows,
            },
            "stock.daily_date_universe_registry": {
                "engine": "ReplacingMergeTree",
                "engine_full": "ReplacingMergeTree(published_at)",
                "partition_key": "",
                "sorting_key": "code, dataset_id",
                "create_table_query": "",
                "columns": {"code": "String", "dataset_id": "String"},
                "rows": [],
            },
            "stock.daily_date_universe_member": {
                "engine": "MergeTree",
                "engine_full": "MergeTree",
                "partition_key": "toYYYYMM(trade_date)",
                "sorting_key": "dataset_id, code, trade_date",
                "create_table_query": "",
                "columns": {"code": "String", "trade_date": "Date"},
                "rows": [],
            },
        }
        self.lock_tables: dict[str, list[dict]] = {}
        self.unique_keys = unique_keys
        self.neeq_rows = neeq_rows
        self.calls: list[tuple[str, str]] = []
        # failure injections
        self.lose_candidate_row_month = lose_candidate_row_month
        self.post_swap_engine_full = post_swap_engine_full
        self.duplicate_candidate_id = duplicate_candidate_id

    # ------------------------------------------------------------- commands
    def command(self, sql):
        self._guard(sql)
        self.calls.append(("command", sql))
        statement = " ".join(sql.split())
        if statement.startswith("CREATE TABLE "):
            name = statement.split()[2]
            if name in self.tables or name in self.lock_tables:
                raise RuntimeError(f"Table {name} already exists")
            if name.startswith("stock.write_lock_"):
                self.lock_tables[name] = []
                return None
            engine_at = statement.find("ENGINE")
            self.tables[name] = {
                "engine": "ReplacingMergeTree",
                "engine_full": statement[engine_at + len("ENGINE"):].lstrip("= ").strip(),
                "partition_key": self._between(statement, "PARTITION BY", "ORDER BY"),
                "sorting_key": statement[statement.rfind("ORDER BY") + len("ORDER BY"):].strip(),
                "create_table_query": statement,
                "columns": dict(KLINE_COLUMNS, created_at="DateTime"),
                "rows": [],
            }
            return None
        if statement.startswith("INSERT INTO "):
            if statement.startswith("INSERT INTO stock.write_lock_"):
                match = re.match(r"INSERT INTO (\S+) \(([^)]*)\) VALUES \((.*)\)$", statement)
                assert match, statement
                fields = [field.strip() for field in match.group(2).split(",")]
                values = literal_eval(f"({match.group(3)})")
                self.lock_tables[match.group(1)].append(dict(zip(fields, values)))
                return None
            match = re.match(
                r"INSERT INTO (stock\.kline_daily__migration_candidate) \([^)]*\) SELECT .* "
                r"FROM (stock\.kline_daily) WHERE toYYYYMM\(trade_date\) = (\d+)$",
                statement,
            )
            assert match, statement
            month = int(match.group(3))
            batch = []
            for row in self.tables[SOURCE_TABLE]["rows"]:
                if int(row["trade_date"][:7].replace("-", "")) == month:
                    copied = dict(row)
                    if copied["created_at"] is None:
                        copied["created_at"] = EPOCH
                    copied["id"] = (
                        0 if self.duplicate_candidate_id
                        else hash((copied["code"], copied["trade_date"])) & 0xFFFFFFFFFFFFFFFF
                    )
                    batch.append(copied)
            candidate_rows = self.tables[CANDIDATE_TABLE]["rows"]
            candidate_rows.extend(batch)
            if self.lose_candidate_row_month == month and candidate_rows:
                candidate_rows.pop()
            return None
        if statement.startswith("DROP TABLE "):
            rest = statement[len("DROP TABLE "):]
            if_exists = rest.startswith("IF EXISTS")
            name = rest.replace("IF EXISTS ", "", 1).strip() if if_exists else rest.strip()
            if name in self.tables:
                del self.tables[name]
            elif name in self.lock_tables:
                del self.lock_tables[name]
            elif not if_exists:
                raise RuntimeError(f"unknown table {name}")
            return None
        if statement.startswith("RENAME TABLE "):
            for pair in statement[len("RENAME TABLE "):].split(","):
                source, target = [part.strip() for part in pair.split(" TO ")]
                if source not in self.tables:
                    raise RuntimeError(f"unknown table {source}")
                self.tables[target] = self.tables.pop(source)
            return None
        raise AssertionError(f"unexpected command: {statement}")

    # -------------------------------------------------------------- queries
    def query(self, sql, settings=None):
        self._guard(sql)
        self.calls.append(("query", sql))
        statement = " ".join(sql.split())
        if "count() AS table_count" in statement:
            match = re.search(r"name='([^']+)'", statement)
            exists = bool(match) and (
                f"stock.{match.group(1)}" in self.tables or f"stock.{match.group(1)}" in self.lock_tables
            )
            return _Result(["table_count"], [(1 if exists else 0,)])
        if "FROM stock.write_lock_" in statement:
            match = re.search(r"FROM (stock\.write_lock_[a-z0-9_]+)", statement)
            rows = sorted(self.lock_tables.get(match.group(1), []), key=lambda row: row["acquired_at"], reverse=True)[:1]
            fields = ("scope", "owner", "purpose", "acquired_at", "host", "pid")
            return _Result(list(fields), [tuple(row[field] for field in fields) for row in rows])
        if "FROM system.tables" in statement and " name IN (" in statement:
            rows = []
            for name in ("kline_daily", "daily_date_universe_registry", "daily_date_universe_member"):
                entry = self.tables.get(f"stock.{name}")
                if entry is None:
                    continue
                rows.append((
                    "stock", name, entry["engine"], entry["engine_full"], entry["partition_key"],
                    entry["sorting_key"], entry["sorting_key"], entry["create_table_query"],
                ))
            return _Result(
                ["database", "name", "engine", "engine_full", "partition_key", "sorting_key", "primary_key", "create_table_query"],
                rows,
            )
        if "SELECT name, engine_full FROM system.tables" in statement:
            entry = self.tables.get(SOURCE_TABLE) or {}
            engine_full = self.post_swap_engine_full or entry.get("engine_full") or ""
            return _Result(["name", "engine_full"], [("kline_daily", engine_full)])
        if "FROM system.columns" in statement:
            rows = []
            for name in ("kline_daily", "daily_date_universe_registry", "daily_date_universe_member"):
                entry = self.tables.get(f"stock.{name}")
                if entry is None:
                    continue
                rows.extend(
                    ("stock", name, column, column_type, "", "", 0, 1, 1)
                    for column, column_type in entry["columns"].items()
                )
            return _Result(
                ["database", "table", "name", "type", "default_kind", "default_expression", "is_in_partition_key", "is_in_sorting_key", "is_in_primary_key"],
                rows,
            )
        if "FROM system.parts" in statement:
            rows = self.tables[SOURCE_TABLE]["rows"]
            return _Result(
                ["database", "table", "partition", "active_parts", "rows", "bytes", "min_date", "max_date"],
                [("stock", "kline_daily", "tuple()", 1, len(rows), 100, "2020-01-02", "2020-02-05")],
            )
        if "lower(name) LIKE" in statement:
            return _Result(["database", "name", "engine", "partition_key", "sorting_key"], [])
        if "candidate_unique_ids" in statement:
            rows = self.tables[CANDIDATE_TABLE]["rows"]
            return _Result(
                ["rows", "candidate_unique_ids"],
                [(len(rows), len({row["id"] for row in rows}))],
            )
        if "uniqExact(id)" in statement:
            rows = self.tables[SOURCE_TABLE]["rows"]
            unique_keys = self.unique_keys if self.unique_keys is not None else len({(row["code"], row["trade_date"]) for row in rows})
            nulls = sum(1 for row in rows if row["created_at"] is None)
            return _Result(
                ["rows", "unique_keys", "unique_ids", "null_created_at", "nonnull_created_at", "target_neeq_rejected_rows", "first_trade_date", "last_trade_date"],
                [(
                    len(rows), unique_keys, len({row["id"] for row in rows}), nulls,
                    len(rows) - nulls, self.neeq_rows,
                    min(row["trade_date"] for row in rows), max(row["trade_date"] for row in rows),
                )],
            )
        if "uniqExact(toYYYYMM" in statement:
            return _Result(["distinct_month_partitions"], [(len(self._months()),)])
        if "SELECT DISTINCT toYYYYMM(trade_date) AS month" in statement:
            return _Result(["month"], [(month,) for month in self._months()])
        if "cityHash64(code" in statement:
            match = re.search(r"FROM (stock\.[a-z0-9_]+) WHERE toYYYYMM\(trade_date\) = (\d+)", statement)
            rows = [row for row in self.tables[match.group(1)]["rows"] if self._month_of(row) == int(match.group(2))]
            return _Result(["rows", "checksum"], [(len(rows), self._checksum(rows))])
        if "AS min_trade_date" in statement:
            match = re.search(r"FROM (stock\.[a-z0-9_]+)$", statement)
            rows = self.tables[match.group(1)]["rows"]
            return _Result(
                ["rows", "unique_keys", "min_trade_date", "max_trade_date"],
                [(
                    len(rows), len({(row["code"], row["trade_date"]) for row in rows}),
                    min(row["trade_date"] for row in rows), max(row["trade_date"] for row in rows),
                )],
            )
        if "ORDER BY code, trade_date LIMIT 3" in statement:
            match = re.search(r"FROM (stock\.[a-z0-9_]+) ORDER BY", statement)
            rows = sorted(self.tables[match.group(1)]["rows"], key=lambda row: (row["code"], row["trade_date"]))[:3]
            return _Result(
                ["code", "trade_date", "open", "close", "volume", "created_at"],
                [
                    (row["code"], row["trade_date"], row["open"], row["close"], row["volume"], row["created_at"] or EPOCH)
                    for row in rows
                ],
            )
        if "count() AS raw_rows" in statement:
            match = re.search(r"FROM (stock\.[a-z0-9_]+)$", statement)
            return _Result(["raw_rows"], [(len(self.tables[match.group(1)]["rows"]),)])
        if "count() AS final_rows" in statement:
            match = re.search(r"FROM (stock\.[a-z0-9_]+) FINAL", statement)
            rows = self.tables[match.group(1)]["rows"]
            return _Result(["final_rows"], [(len({(row["code"], row["trade_date"]) for row in rows}),)])
        raise AssertionError(f"unexpected query: {statement}")

    # -------------------------------------------------------------- helpers
    def _guard(self, sql):
        statement = " ".join(sql.split())
        verb = statement.split()[0]
        assert verb in {"CREATE", "INSERT", "SELECT", "RENAME", "DROP"}, statement
        for token in set(re.findall(r"\b(?:stock|system)\.[A-Za-z0-9_]+", statement)):
            assert token in ALLOWED_TABLES, token

    @staticmethod
    def _between(text, start_marker, end_marker):
        start = text.find(start_marker)
        end = text.find(end_marker, start)
        if start < 0:
            return ""
        return text[start + len(start_marker):end].strip() if end > 0 else text[start + len(start_marker):].strip()

    @staticmethod
    def _month_of(row):
        return int(row["trade_date"][:7].replace("-", ""))

    def _months(self):
        return sorted({self._month_of(row) for row in self.tables[SOURCE_TABLE]["rows"]})

    @staticmethod
    def _checksum(rows):
        total = 0
        for row in rows:
            digest = hash(
                (
                    row["code"], row["trade_date"], row["open"], row["high"], row["low"], row["close"],
                    row["volume"], row["amount"], row["created_at"] or EPOCH,
                )
            )
            total = (total + digest) % 2**62
        return total

    def commands(self):
        return [sql for kind, sql in self.calls if kind == "command"]


def _first(client, needle, *, kind=None):
    for index, (call_kind, sql) in enumerate(client.calls):
        if needle in sql and (kind is None or call_kind == kind):
            return index
    raise AssertionError(f"call not found: {needle!r}")


def _events(journal_path):
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _names(journal_path):
    return [event["event"] for event in _events(journal_path)]


def test_plan_mode_prints_plan_and_issues_no_writes(tmp_path, capsys):
    client = _MigrationClient()
    journal = tmp_path / "journal.jsonl"

    assert main(["--plan", "--journal", str(journal)], client=client) == 0

    assert {kind for kind, _ in client.calls} == {"query"}
    assert all(sql.lstrip().upper().startswith("SELECT") for _, sql in client.calls)
    assert not journal.exists()
    plan = json.loads(capsys.readouterr().out)
    assert plan["status"] == "ready"
    assert plan["readonly"] is True
    assert plan["months"] == [202001, 202002]
    assert "stock.kline_daily__migration_candidate" in plan["candidate_ddl"]
    assert plan["commands_issued"] == {"ddl": 0, "dml": 0, "rename": 0, "lock": 0}
    assert len(plan["steps"]) >= 7


def test_plan_mode_reports_blockers_without_writes(tmp_path, capsys):
    client = _MigrationClient(neeq_rows=1)
    journal = tmp_path / "journal.jsonl"

    assert main(["--plan", "--journal", str(journal)], client=client) == 1

    assert {kind for kind, _ in client.calls} == {"query"}
    assert not journal.exists()
    plan = json.loads(capsys.readouterr().out)
    assert plan["status"] == "blocked"
    assert "source_rows_violate_target_neeq_constraints" in plan["blockers"]


def test_execute_happy_path_follows_step_order_and_releases_lock(tmp_path, capsys):
    client = _MigrationClient()
    journal = tmp_path / "journal.jsonl"

    assert main(["--execute", "--journal", str(journal)], client=client) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed"
    assert result["months"] == [202001, 202002]

    markers = [
        ("command", f"CREATE TABLE {LOCK_TABLE}"),
        ("command", f"INSERT INTO {LOCK_TABLE} (scope"),
        ("query", f"FROM {LOCK_TABLE} ORDER BY acquired_at DESC"),
        ("query", "FROM system.tables"),
        ("query", "uniqExact(id)"),
        ("query", "SELECT DISTINCT toYYYYMM(trade_date) AS month"),
        ("command", f"DROP TABLE IF EXISTS {CANDIDATE_TABLE}"),
        ("command", f"CREATE TABLE {CANDIDATE_TABLE}"),
    ]
    positions = [_first(client, needle, kind=kind) for kind, needle in markers]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)

    previous = positions[-1]
    for month in (202001, 202002):
        insert_index = _first(client, f"WHERE toYYYYMM(trade_date) = {month}", kind="command")
        source_verify = _first(client, f"FROM stock.kline_daily WHERE toYYYYMM(trade_date) = {month}", kind="query")
        candidate_verify = _first(client, f"FROM {CANDIDATE_TABLE} WHERE toYYYYMM(trade_date) = {month}", kind="query")
        assert previous < insert_index < source_verify < candidate_verify
        previous = candidate_verify

    swap_index = _first(client, "RENAME TABLE", kind="command")
    engine_index = _first(client, "SELECT name, engine_full FROM system.tables", kind="query")
    raw_index = _first(client, "count() AS raw_rows", kind="query")
    final_index = _first(client, "count() AS final_rows", kind="query")
    sample_indexes = [
        index
        for index, (kind, sql) in enumerate(client.calls)
        if kind == "query" and "ORDER BY code, trade_date LIMIT 3" in sql
    ]
    drop_lock_index = _first(client, f"DROP TABLE {LOCK_TABLE}", kind="command")
    assert len(sample_indexes) == 2
    assert previous < swap_index < engine_index < raw_index < final_index
    assert sample_indexes[0] < swap_index < sample_indexes[1] < drop_lock_index
    assert drop_lock_index == max(index for index, (kind, _) in enumerate(client.calls) if kind == "command")

    insert_commands = [sql for sql in client.commands() if sql.startswith(f"INSERT INTO {CANDIDATE_TABLE}")]
    assert len(insert_commands) == 2
    assert all("ifNull(created_at, toDateTime('1970-01-01 00:00:00')) AS created_at" in sql for sql in insert_commands)
    assert all(", created_at, id)" in sql for sql in insert_commands)

    # table states after the swap
    assert CANDIDATE_TABLE not in client.tables
    promoted = client.tables[SOURCE_TABLE]
    assert promoted["engine_full"].startswith("ReplacingMergeTree(created_at)")
    assert len(promoted["rows"]) == 3
    assert all(row["created_at"] is not None for row in promoted["rows"])
    assert next(row for row in promoted["rows"] if row["code"] == "600000.SH")["created_at"] == EPOCH
    assert len({row["id"] for row in promoted["rows"]}) == len(promoted["rows"])
    assert client.tables[PRE_MIGRATION_TABLE]["rows"] == client.snapshot_rows
    assert client.lock_tables == {}

    assert _names(journal) == [
        "migration_started",
        "lock_acquired",
        "preflight_completed",
        "candidate_created",
        "month_batch_inserted",
        "month_batch_verified",
        "month_batch_inserted",
        "month_batch_verified",
        "candidate_id_uniqueness_verified",
        "totals_verified",
        "tables_swapped",
        "post_swap_verified",
        "migration_completed",
        "lock_released",
        "migration_finished",
    ]


def test_execute_blocks_on_incompatible_source_before_any_candidate_ddl(tmp_path, capsys):
    client = _MigrationClient(unique_keys=2)
    journal = tmp_path / "journal.jsonl"

    assert main(["--execute", "--journal", str(journal)], client=client) == 1

    assert not any(f"CREATE TABLE {CANDIDATE_TABLE}" in sql for _, sql in client.calls)
    assert not any("RENAME TABLE" in sql for _, sql in client.calls)
    assert CANDIDATE_TABLE not in client.tables
    assert client.tables[SOURCE_TABLE]["rows"] == client.snapshot_rows
    assert client.lock_tables == {}
    names = _names(journal)
    assert "preflight_completed" in names and "migration_failed" in names and "lock_released" in names
    preflight = next(event for event in _events(journal) if event["event"] == "preflight_completed")
    assert preflight["status"] == "blocked"
    assert "source_rows_exceed_unique_exact_keys" in preflight["blockers"]
    failed = next(event for event in _events(journal) if event["event"] == "migration_failed")
    assert "preflight blocked" in failed["error"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"


@pytest.mark.parametrize("keep_candidate", [False, True])
def test_execute_pre_swap_verification_failure_keeps_old_table(tmp_path, capsys, keep_candidate):
    client = _MigrationClient(lose_candidate_row_month=202001)
    journal = tmp_path / "journal.jsonl"
    args = ["--execute", "--journal", str(journal)]
    if keep_candidate:
        args.append("--keep-candidate")

    assert main(args, client=client) == 1

    assert not any("RENAME TABLE" in sql for _, sql in client.calls)
    assert client.tables[SOURCE_TABLE]["rows"] == client.snapshot_rows
    assert (CANDIDATE_TABLE in client.tables) is keep_candidate
    assert client.lock_tables == {}
    names = _names(journal)
    assert "migration_failed" in names and "lock_released" in names
    assert ("candidate_dropped_on_failure" in names) is (not keep_candidate)
    failed = next(event for event in _events(journal) if event["event"] == "migration_failed")
    assert "month_batch_verification_mismatch:202001" in failed["error"]


def test_execute_post_swap_verification_failure_reverses_rename(tmp_path, capsys):
    client = _MigrationClient(
        post_swap_engine_full="ReplacingMergeTree(id) PARTITION BY toYYYYMM(trade_date) ORDER BY (code, trade_date)"
    )
    journal = tmp_path / "journal.jsonl"

    assert main(["--execute", "--journal", str(journal)], client=client) == 1

    renames = [sql for sql in client.commands() if sql.startswith("RENAME TABLE")]
    assert renames == [SWAP_SQL, REVERSE_SWAP_SQL]
    assert client.tables[SOURCE_TABLE]["rows"] == client.snapshot_rows
    assert client.tables[SOURCE_TABLE]["engine_full"] == "ReplacingMergeTree ORDER BY (code, trade_date)"
    assert PRE_MIGRATION_TABLE not in client.tables
    assert CANDIDATE_TABLE in client.tables  # kept for forensics after the rollback
    assert client.lock_tables == {}
    names = _names(journal)
    assert "post_swap_verification_failed" in names
    assert "reverse_rename_completed" in names
    assert "migration_failed" in names and "lock_released" in names
    failed = next(event for event in _events(journal) if event["event"] == "migration_failed")
    assert "engine_full_prefix_mismatch" in failed["error"]


def test_execute_refuses_when_lock_already_held(tmp_path, capsys):
    client = _MigrationClient()
    client.lock_tables[LOCK_TABLE] = [
        {
            "scope": "kline_daily_schema_migration",
            "owner": "nightly-job",
            "purpose": "other work",
            "acquired_at": "2026-11-01 11:59:00",
            "host": "worker-1",
            "pid": 999,
        }
    ]
    journal = tmp_path / "journal.jsonl"

    assert main(["--execute", "--journal", str(journal)], client=client) == 1

    commands = client.commands()
    assert len(commands) == 1
    assert commands[0].startswith(f"CREATE TABLE {LOCK_TABLE}")
    assert CANDIDATE_TABLE not in client.tables
    assert client.tables[SOURCE_TABLE]["rows"] == client.snapshot_rows
    assert LOCK_TABLE in client.lock_tables and client.lock_tables[LOCK_TABLE][0]["owner"] == "nightly-job"
    names = _names(journal)
    assert "migration_failed" in names and "lock_acquired" not in names
    payload = json.loads(capsys.readouterr().out)
    assert "LockHeldError" in payload["error"]


def test_jsonl_journal_atomic_segments_and_default_path(tmp_path):
    assert DEFAULT_JOURNAL_PATH.as_posix() == (
        "F:/AiStock/data/data/runtime/schema_migration/kline_daily_20261101/journal.jsonl"
    )

    journal = JsonlJournal(tmp_path / "nested" / "journal.jsonl")
    journal.write({"event": "a", "value": 1})
    journal.write({"event": "b", "value": 2})

    lines = (tmp_path / "nested" / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["event"] == "a" and first["ts"]
    assert second["event"] == "b" and second["seq"] == first["seq"] + 1
    assert list(tmp_path.rglob("*.tmp")) == []

    class _Unserializable:
        def __str__(self):
            raise ValueError("boom")

    with pytest.raises(Exception):
        journal.write({"event": "c", "payload": _Unserializable()})
    assert len((tmp_path / "nested" / "journal.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert list(tmp_path.rglob("*.tmp")) == []


def test_insert_month_sql_regenerates_deterministic_id():
    sql = _insert_month_sql(202610)
    assert "cityHash64(code, trade_date) AS id" in sql


def test_preflight_allows_sentinel_zero_source_ids_under_id_regeneration():
    rows = _default_rows()
    for row in rows:
        row["id"] = 0  # legacy pipelines wrote rows with the id=0 sentinel
    client = _MigrationClient(rows=rows)

    preflight = run_preflight(client)

    assert preflight["status"] == "ready"
    assert "source_rows_exceed_unique_ids" not in preflight["blockers"]
    assert preflight["id_regeneration"]["strategy"] == "cityHash64(code, trade_date)"
    assert preflight["id_regeneration"]["rows_without_unique_source_id"] == len(rows) - 1


def test_execute_blocks_when_candidate_ids_not_unique(tmp_path, capsys):
    client = _MigrationClient(duplicate_candidate_id=True)
    journal = tmp_path / "journal.jsonl"

    assert main(["--execute", "--journal", str(journal)], client=client) == 1

    assert not any(sql.startswith("RENAME TABLE") for sql in client.commands())
    assert client.tables[SOURCE_TABLE]["rows"] == client.snapshot_rows
    assert CANDIDATE_TABLE not in client.tables
    assert client.lock_tables == {}
    names = _names(journal)
    assert "migration_failed" in names and "lock_released" in names
    failed = next(event for event in _events(journal) if event["event"] == "migration_failed")
    assert "candidate_id_uniqueness_mismatch" in failed["error"]
