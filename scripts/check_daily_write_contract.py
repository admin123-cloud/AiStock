"""Static, read-only audit of formal ``kline_daily`` mutation surfaces.

The detector intentionally errs on the side of reporting a mutation.  It scans
the source tree only; it never imports application modules or opens a database.
Every reported surface must either call the shared fail-closed blocker or run
the exact-key DataFrame/key validator before the mutation.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


FORMAL_TABLES = {"kline_daily", "stock.kline_daily"}
MUTATION_SQL = re.compile(
    r"\b(?:insert\s+into|alter\s+table|delete\s+from|truncate\s+table|"
    r"optimize\s+table|replace\s+partition)\s+"
    r"(?:[`\"]?stock[`\"]?\.)?[`\"]?kline_daily[`\"]?\b",
    re.IGNORECASE,
)
DYNAMIC_MUTATION_SQL = re.compile(
    r"\b(?:insert\s+into|alter\s+table|delete\s+from|truncate\s+table|"
    r"optimize\s+table|replace\s+partition)\s+\{([A-Za-z_]\w*)\}",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Mutation:
    line: int
    kind: str
    snippet: str


def _static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and all(
        isinstance(value, ast.Constant) and isinstance(value.value, str)
        for value in node.values
    ):
        return "".join(value.value for value in node.values)
    return None


def _literal_names(tree: ast.AST) -> dict[str, str]:
    values: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            resolved = _static_string(value) if value is not None else None
            if resolved is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    values[target.id] = resolved
    return values


def _is_formal_table(value: str | None) -> bool:
    return str(value or "").strip().strip("`").strip('"').lower() in FORMAL_TABLES


def _line_snippet(lines: list[str], line: int) -> str:
    return lines[line - 1].strip()[:240] if 0 < line <= len(lines) else ""


def _scan_file(path: Path) -> tuple[list[Mutation], dict[str, bool]]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Mutation(exc.lineno or 1, "syntax_error", str(exc))], {
            "hard_block": False,
            "exact_key_guard": False,
            "scope_builder": False,
            "permit_reference": False,
        }

    literal_names = _literal_names(tree)
    formal_dynamic_names = {
        *[name for name, value in literal_names.items() if _is_formal_table(value)],
        *re.findall(
            r"\b([A-Za-z_]\w*)\s*(?:==|=)\s*['\"](?:stock\.)?kline_daily['\"]",
            source,
            flags=re.IGNORECASE,
        ),
    }
    mutations: dict[tuple[int, str], Mutation] = {}

    def add(line: int, kind: str, snippet: str) -> None:
        mutations.setdefault((line, kind), Mutation(line, kind, snippet[:240]))

    for line_no, line in enumerate(lines, start=1):
        if MUTATION_SQL.search(line):
            add(line_no, "literal_sql", line.strip())
        for name in DYNAMIC_MUTATION_SQL.findall(line):
            if _is_formal_table(literal_names.get(name)) or name in formal_dynamic_names:
                add(line_no, "dynamic_sql", line.strip())

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func_name = ""
        if isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
        elif isinstance(node.func, ast.Name):
            func_name = node.func.id
        if func_name not in {"insert", "insert_df", "command", "execute", "exec_driver_sql"}:
            continue
        segment = ast.get_source_segment(source, node) or ""
        first = node.args[0]
        first_value = _static_string(first)
        if isinstance(first, ast.Name):
            first_value = literal_names.get(first.id)
        is_formal = _is_formal_table(first_value)
        # SQLAlchemy ``insert(KlineDaily)`` is a formal ORM mutation.
        if isinstance(first, ast.Call) and isinstance(first.func, ast.Name):
            if first.func.id in {"KlineDaily", "KlineDailyModel"}:
                is_formal = True
            elif (
                first.func.id == "insert"
                and first.args
                and isinstance(first.args[0], ast.Name)
                and first.args[0].id in {"KlineDaily", "KlineDailyModel"}
            ):
                is_formal = True
        if not is_formal and MUTATION_SQL.search(segment):
            is_formal = True
        if is_formal:
            add(getattr(node, "lineno", 1), "call", segment.replace("\n", " ").strip())

    guards = {
        "hard_block": "block_unreviewed_daily_write" in source,
        "exact_key_guard": bool(re.search(r"\bvalidate_daily_(?:dataframe|keys)\b", source)),
        "scope_builder": bool(re.search(r"\b(?:make_daily_write_scope|load_current_stock_scope)\b", source)),
        "permit_reference": "exact_key_permit" in source,
    }
    return sorted(mutations.values(), key=lambda item: (item.line, item.kind)), guards


def iter_python_files(root: Path) -> Iterable[Path]:
    for folder in ("api", "research", "scheduler", "scripts", "services", "utils"):
        base = root / folder
        if not base.exists():
            continue
        yield from sorted(base.rglob("*.py"))


def audit(root: Path) -> dict:
    files: list[dict] = []
    for path in iter_python_files(root):
        mutations, guards = _scan_file(path)
        if not mutations:
            continue
        if guards["hard_block"]:
            classification = "hard_blocked"
        elif guards["exact_key_guard"]:
            classification = "exact_key_guarded_fail_closed"
        else:
            classification = "unverified"
        files.append(
            {
                "file": str(path.relative_to(root)).replace("\\", "/"),
                "classification": classification,
                "guards": guards,
                "mutations": [asdict(item) for item in mutations],
            }
        )
    return {
        "schema_version": 1,
        "root": str(root),
        "formal_tables": sorted(FORMAL_TABLES),
        "file_count": len(files),
        "unverified_count": sum(item["classification"] == "unverified" for item in files),
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    report = audit(args.root.resolve())
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 1 if report["unverified_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
