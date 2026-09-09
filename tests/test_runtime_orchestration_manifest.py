import json
from pathlib import Path


def test_each_manifest_artifact_has_one_named_owner():
    path = Path(__file__).resolve().parents[1] / "config" / "runtime_orchestration_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    artifacts = manifest["artifacts"]
    assert len({item["artifact"] for item in artifacts}) == len(artifacts)
    assert all(item["owner"] and item["trigger"] and item["verifier"] for item in artifacts)
