import ast
import importlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('old,new', [
    ('services.runtime_health','services.operations.health'),
    ('services.data_delivery','services.operations.delivery'),
    ('services.operations_incidents','services.operations.incidents'),
    ('utils.strategy_contracts','strategies.contracts'),
    ('utils.g3_mainwave_confirmation','strategies.g3.confirmation'),
    ('utils.g3_minute_visibility','strategies.g3.minute_visibility'),
    ('utils.exceptions','core.exceptions'),
])
def test_legacy_import_keeps_same_module_identity(old,new):
    assert importlib.import_module(old) is importlib.import_module(new)


def test_exceptions_can_be_caught_across_old_and_new_boundaries():
    from core.exceptions import AiStockException
    from utils.exceptions import DataSourceException
    with pytest.raises(AiStockException,match='QMT'):
        raise DataSourceException('QMT','unavailable',{'retry':1})


def test_reporting_preserves_percent_null_and_scalar_semantics():
    from research.common.reporting import percent_text, markdown_table, numpy_json_default, timestamp_json_default
    assert percent_text(.12345)=='12.35%'
    assert percent_text(None)==percent_text(float('nan'))==''
    assert markdown_table(pd.DataFrame())=='_无数据_'
    rendered=markdown_table(pd.DataFrame([{'return':.12345,'amount':2.34567,'name':None}]),{'return'})
    assert '12.35%' in rendered and '2.3457' in rendered and 'None' not in rendered
    assert numpy_json_default(np.int64(7))==7
    assert numpy_json_default(np.bool_(True)) is True
    assert numpy_json_default(pd.NaT) is None
    assert timestamp_json_default(pd.Timestamp('2026-09-09'))=='2026-09-09T00:00:00'


def test_relocated_experiments_are_discoverable_without_importing_them(capsys):
    from research.__main__ import main
    catalog=json.loads((ROOT/'research/catalog.json').read_text(encoding='utf-8'))
    names=[]
    for item in catalog['research_moves']:
        target=ROOT/item['new']
        assert target.resolve().is_relative_to(ROOT/'research')
        assert target.is_file() and len(item['sha256'])==64
        ast.parse(target.read_text(encoding='utf-8'))
        names.append(Path(item['old']).stem)
    assert len(names)==len(set(names))
    assert main(['--resolve',names[0]])==0
    assert catalog['research_moves'][0]['new'] in capsys.readouterr().out


def test_data_delivery_and_g3_use_identical_completed_sessions():
    from services.operations.delivery import bar_times
    from strategies.g3.minute_visibility import valid_bar_times
    for period in (5,15,30,60):
        assert set(bar_times(period))==valid_bar_times(period)
    assert valid_bar_times(1)==set()  # Existing G3 supported-period contract.
