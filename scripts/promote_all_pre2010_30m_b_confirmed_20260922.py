import time

import clickhouse_connect


client = clickhouse_connect.get_client(
    host="localhost", port=8123, username="default"
)
client.command("SET max_partitions_per_insert_block=1000")

all_keys = "stock.pre2010_30m_b_confirmed_keys_20260922"
bad_keys = "stock.pre2010_30m_b_bad_keys_20260922"
raw = "stock.pre2010_bc_reaggregate_30m_raw_20260922_v4"
keys = "stock.pre2010_30m_b_all_confirmed_clean_keys_20260922"
candidate = "stock.pre2010_30m_b_all_confirmed_candidate_clean_20260922"
formal = "stock.kline_minute_30"
backup = "stock.kline_minute_30_repair_backup_b_all_confirmed_20260922"

for table in (keys, candidate, backup):
    client.command(f"DROP TABLE IF EXISTS {table}")

client.command(
    f"""
    CREATE TABLE {keys}
    (
        code String,
        trade_date Date
    ) ENGINE = MergeTree
    ORDER BY (code, trade_date)
    AS
    SELECT k.code, k.trade_date
    FROM {all_keys} AS k
    LEFT JOIN {bad_keys} AS b
      ON k.code = b.code AND k.trade_date = b.trade_date
    WHERE b.code = ''
    """
)
client.command(f"CREATE TABLE {candidate} AS {formal}")
client.command(
    f"""
    INSERT INTO {candidate}
    SELECT r.*
    FROM {raw} AS r
    INNER JOIN {keys} AS k
      ON r.code = k.code AND toDate(r.datetime) = k.trade_date
    """
)

invalid = int(
    client.query(
        f"""
        SELECT countIf(
            open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
            OR high < greatest(open, close)
            OR low > least(open, close)
            OR volume < 0 OR amount < 0
        )
        FROM {candidate}
        """
    ).result_rows[0][0]
)
if invalid:
    raise RuntimeError(f"candidate has {invalid} invalid rows")

client.command(f"CREATE TABLE {backup} AS {formal}")
client.command(
    f"""
    INSERT INTO {backup}
    SELECT f.*
    FROM {formal} AS f
    INNER JOIN {keys} AS k
      ON f.code = k.code AND toDate(f.datetime) = k.trade_date
    """
)

old_rows = int(
    client.query(
        f"""
        SELECT count()
        FROM {formal} AS f
        INNER JOIN {keys} AS k
          ON f.code = k.code AND toDate(f.datetime) = k.trade_date
        """
    ).result_rows[0][0]
)
client.command(
    f"ALTER TABLE {formal} DELETE WHERE (code, toDate(datetime)) IN "
    f"(SELECT code, trade_date FROM {keys})"
)

pending = 1
for _ in range(300):
    pending = int(
        client.query(
            """
            SELECT count()
            FROM system.mutations
            WHERE database = 'stock'
              AND table = 'kline_minute_30'
              AND is_done = 0
            """
        ).result_rows[0][0]
    )
    if pending == 0:
        break
    time.sleep(1)
if pending:
    raise RuntimeError("formal 30m delete mutation did not finish within 300s")

client.command(f"INSERT INTO {formal} SELECT * FROM {candidate}")

keys_count = int(client.query(f"SELECT count() FROM {keys}").result_rows[0][0])
candidate_count = int(client.query(f"SELECT count() FROM {candidate}").result_rows[0][0])
backup_count = int(client.query(f"SELECT count() FROM {backup}").result_rows[0][0])
formal_count = int(
    client.query(
        f"""
        SELECT count()
        FROM {formal} AS f
        INNER JOIN {keys} AS k
          ON f.code = k.code AND toDate(f.datetime) = k.trade_date
        """
    ).result_rows[0][0]
)
print(
    {
        "clean_keys": keys_count,
        "old_rows_backed_up": old_rows,
        "candidate_rows": candidate_count,
        "backup_rows": backup_count,
        "formal_selected_rows": formal_count,
        "invalid_candidate_rows": invalid,
    }
)
