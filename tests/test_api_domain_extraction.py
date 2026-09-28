import importlib
import json
from pathlib import Path
import pytest

SAMPLES=json.loads((Path(__file__).parent/'fixtures/api_domain_regression.json').read_text(encoding='utf-8'))

@pytest.mark.parametrize('sample',SAMPLES,ids=lambda row:row['function'])
def test_pre_refactor_observed_outputs(sample):
    module=importlib.import_module(sample['module'].removesuffix('.py').replace('/','.'))
    assert getattr(module,sample['function'])(*sample['args'])==sample['expected']


def test_monitor_persistence_preserves_source_and_bounds_payload(tmp_path):
    from services.g3.monitor_state import save_monitor_state
    from services.operations.health import write_snapshot
    state={'last_result':{'ok':False,'error':'x'*2000,'private_large':list(range(1000))},'enabled':True}
    target=tmp_path/'state.json'
    save_monitor_state(target,state,lambda path,payload:write_snapshot(payload,path),compact=True)
    actual=json.loads(target.read_text(encoding='utf-8'))
    assert actual['last_result']['compacted'] is True
    assert 'private_large' not in actual['last_result']
    assert len(state['last_result']['error'])==2000 and 'private_large' in state['last_result']


def test_qmt_position_display_follows_machine_exit_contract():
    from services.trading.broker_parser import _qmtmini_position_to_broker_holding
    from strategies.contracts import formal_g3_score88_contract
    row=_qmtmini_position_to_broker_holding({'stock_code':'000001.SZ','volume':100,'market_value':1200})
    contract=formal_g3_score88_contract()
    assert row['shares']==100 and row['current_price']==12
    assert f"{contract['exit']['hard_stop_loss_pct']:.0%}" in row['exit_contract']
    assert f"{contract['exit']['take_profit_pct']:.0%}" in row['exit_contract']
    assert contract['strategy_id'] in row['exit_contract']
    assert '12%' not in row['exit_contract']
