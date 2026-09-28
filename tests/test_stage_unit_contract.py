from types import SimpleNamespace
import pytest
from services.operations.stage_contract import require_stage_contract, QMT_SDK_UNIT_CONTRACT


class Client:
    def __init__(self, comment='', nonempty=False):
        self.comment=comment; self.nonempty=nonempty; self.commands=[]
    def query(self, sql, **kwargs):
        return SimpleNamespace(result_rows=[(self.comment,)] if 'system.tables' in sql else [(1,)] if self.nonempty else [])
    def command(self, sql):
        self.commands.append(sql); self.comment=QMT_SDK_UNIT_CONTRACT


def test_nonempty_legacy_stage_is_preserved_even_when_initializing():
    client=Client(nonempty=True)
    with pytest.raises(RuntimeError,match='legacy_nonempty_stage'):
        require_stage_contract(client,'legacy_stage',initialize_empty=True)
    assert client.commands==[]


def test_empty_stage_is_marked_and_verified():
    client=Client()
    require_stage_contract(client,'stock_repair.new_stage',initialize_empty=True)
    assert client.comment==QMT_SDK_UNIT_CONTRACT and len(client.commands)==1


def test_apply_or_validate_cannot_adopt_unmarked_stage():
    with pytest.raises(RuntimeError,match='mismatch'):
        require_stage_contract(Client(),'legacy_stage')


def test_wrong_contract_cannot_be_relabelled():
    with pytest.raises(RuntimeError,match='mismatch'):
        require_stage_contract(Client('other-units'),'stage',initialize_empty=True)


def test_valid_stage_needs_no_mutation():
    client=Client(QMT_SDK_UNIT_CONTRACT,True)
    require_stage_contract(client,'stage')
    assert not client.commands


def test_invalid_identifier_is_rejected_before_query():
    with pytest.raises(ValueError,match='identifier'):
        require_stage_contract(None,'stock.stage; DROP DATABASE stock')
