import json

import pytest

from scripts import run_minute_candidates_v2 as runner


def _baseline(months):
    return {"fingerprint": "abc", "months": [{"month": month} for month in months]}


def test_build_plan_is_243_isolated_candidate_commands():
    months = [f"{2020 + n // 12:04d}{n % 12 + 1:02d}" for n in range(81)]
    plan = runner.build_plan("run_1", months)
    assert len(plan) == 243
    assert {item["period"] for item in plan} == set(runner.PERIODS)
    assert all(item["command"][2] == "candidate" for item in plan)


def test_load_baseline_requires_exactly_81_unique_months(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    target = root / "minute_migration_v2"
    target.mkdir(parents=True)
    months = [f"{2020 + n // 12:04d}{n % 12 + 1:02d}" for n in range(81)]
    (target / "baseline_run_1.json").write_text(json.dumps(_baseline(months)), encoding="utf-8")
    monkeypatch.setattr(runner, "runtime_path", lambda: root)
    baseline, actual = runner.load_baseline("run_1")
    assert baseline["fingerprint"] == "abc"
    assert actual == sorted(months)


@pytest.mark.parametrize("months", [[], ["202001"] * 81, ["202600"] + ["202001"] * 80])
def test_load_baseline_rejects_bad_coverage(tmp_path, monkeypatch, months):
    root = tmp_path / "runtime"
    target = root / "minute_migration_v2"
    target.mkdir(parents=True)
    (target / "baseline_run_1.json").write_text(json.dumps(_baseline(months)), encoding="utf-8")
    monkeypatch.setattr(runner, "runtime_path", lambda: root)
    with pytest.raises(SystemExit):
        runner.load_baseline("run_1")
