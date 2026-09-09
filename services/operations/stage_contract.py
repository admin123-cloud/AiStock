"""Prevent legacy staged rows from crossing a source-unit contract change."""
import re

QMT_SDK_UNIT_CONTRACT = 'qmt-sdk-volume-lots-amount-yuan-v1'


def require_stage_contract(client, table, *, initialize_empty=False):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?', table):
        raise ValueError('invalid_stage_identifier')
    database, name = table.split('.') if '.' in table else ('', table)
    rows = client.query(
        'SELECT comment FROM system.tables WHERE database=if({db:String}=\'\',currentDatabase(),{db:String}) '
        'AND name={name:String}', parameters={'db': database, 'name': name}).result_rows
    if len(rows) != 1:
        raise RuntimeError('stage_contract_missing_table')
    if rows[0][0] == QMT_SDK_UNIT_CONTRACT:
        return
    if not initialize_empty or rows[0][0]:
        raise RuntimeError('stage_unit_contract_mismatch; preserve legacy stage and fetch into a new table')
    if client.query(f'SELECT 1 FROM {table} LIMIT 1').result_rows:
        raise RuntimeError('legacy_nonempty_stage; unit conversion or reset is not authorized')
    client.command(f"ALTER TABLE {table} MODIFY COMMENT '{QMT_SDK_UNIT_CONTRACT}'")
    require_stage_contract(client, table)
