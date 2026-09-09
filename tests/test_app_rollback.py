import json
import pytest
from scripts import manage_app_rollback as rollback


def test_modified_rollback_configuration_blocks_before_docker(tmp_path, monkeypatch):
    compose = tmp_path / 'rollback.compose.json'
    compose.write_text('{}')
    record = tmp_path / 'rollback.json'
    record.write_text(json.dumps({'compose':str(compose),'compose_sha256':rollback.digest(compose)}))
    compose.write_text('{"changed":true}')
    called = []
    monkeypatch.setattr(rollback,'run',lambda *a,**k:called.append(a))
    with pytest.raises(RuntimeError,match='Compose changed'):
        rollback.verify(record)
    assert called == []
