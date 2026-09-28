from __future__ import annotations

import subprocess


DOCKER = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
EXCLUDED_NON_STOCK_CODES = {"161907.SZ"}


def query(sql: str) -> str:
    result = subprocess.run(
        [DOCKER, "exec", "aistock-clickhouse", "clickhouse-client", "--query", sql],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def counts(table: str, month: str) -> dict[str, int]:
    sql = f"SELECT code,count() FROM stock.{table} FINAL WHERE toYYYYMM(datetime)={month} GROUP BY code FORMAT TSV"
    out = query(sql)
    return {
        line.split("\t")[0]: int(line.split("\t")[1])
        for line in out.splitlines()
        if "\t" in line and line.split("\t")[0] not in EXCLUDED_NON_STOCK_CODES
    }


def main() -> None:
    existing = {
        line.strip()
        for line in query("SELECT name FROM system.tables WHERE database='stock' FORMAT TSV").splitlines()
    }
    months = [f"{year}{month:02d}" for year in range(2020, 2027) for month in range(1, 13)]
    months = [month for month in months if month <= "202608"]
    differences = []
    for month in months:
        candidate_table = f"kline_minute_30_candidate_{month}"
        if candidate_table not in existing:
            continue
        official = counts("kline_minute_30", month)
        candidate = counts(candidate_table, month)
        diff = {
            code: official.get(code, 0) - candidate.get(code, 0)
            for code in set(official) | set(candidate)
            if official.get(code, 0) != candidate.get(code, 0)
        }
        if diff:
            differences.append((month, diff))
    print(f"months_with_diff={len(differences)}")
    for month, diff in differences:
        print(month, " ".join(f"{code}:{delta:+d}" for code, delta in sorted(diff.items())))


if __name__ == "__main__":
    main()
