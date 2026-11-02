import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_telemetry_comparison as comparison


def _series(tmp_path):
    root = tmp_path / "series"
    root.mkdir()
    plan = {"version": 1, "created_unix": 1, "repetitions": 5, "warmups": 1,
            "eligibility": "runtime", "timeout_seconds": 300, "max_steps": 20,
            "completion_requirements": comparison._completion_requirements("runtime"),
            "workload_sha256": "workload", "schedule": comparison._block_schedule()}
    (root / "plan.json").write_text(json.dumps(plan))
    _, _, digest = comparison._read_block_plan(root / "plan.json")
    for batch in plan["schedule"]:
        path = root / batch["directory"]
        path.mkdir(parents=True)
        meta = {"mode": batch["mode"], "plan_sha256": digest, "plan_batch": batch["batch"],
                "plan_block": batch["block"], "workload_sha256": "workload",
                "repetitions": 5, "warmups": 1, "timeout_seconds": 300, "max_steps": 20,
                "measurement": {"eligibility_policy": "runtime"},
                "completion_requirements": comparison._completion_requirements("runtime"),
                "deployment": {"comparable_sha256": "config"},
                "started_unix": batch["batch"] * 10, "completed_unix": batch["batch"] * 10 + 5}
        (path / "experiment.json").write_text(json.dumps(meta))
        rows = [{"index": i + 1, "mode": batch["mode"], "stage": "complete", "warmup": i == 0,
                 "eligible": i != 0, "task_status": "Success", "task_result_nonempty": True,
                 "completion_requirements_match": True,
                 "task_id": "%s-task-%s" % (batch["batch"], i),
                 "run_id": "%s-run-%s" % (batch["batch"], i),
                 "task_end_to_end_time_ms": 999999 if i == 0 else (110 if batch["mode"] == "on" else 100),
                 "telemetry_capture_check": {"captured": batch["mode"] == "on"}}
                for i in range(6)]
        (path / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    return root


def test_complete_series_excludes_warmups_and_reports_paired_interval(tmp_path):
    root = _series(tmp_path)
    output = root / "aggregate.json"
    assert comparison.compare_blocks(NS(root=root, output=output)) == 0
    result = json.loads(output.read_text())
    assert result["on"]["eligible_repetitions"] == result["off"]["eligible_repetitions"] == 50
    assert result["on"]["mean_ms"] == 110
    assert result["off"]["mean_ms"] == 100
    assert result["overhead_ms"] == result["overhead_percent"] == 10
    assert result["counts"]["on"]["warmups"] == result["counts"]["off"]["warmups"] == 10
    assert result["confidence_interval"]["overhead_ms"] == [10, 10]
    assert result["confidence_interval"]["overhead_percent"] == [10, 10]
    with pytest.raises(ValueError, match="already exists"):
        comparison.compare_blocks(NS(root=root, output=output))


@pytest.mark.parametrize("defect", ["missing_batch", "failure", "duplicate", "config", "policy",
                                    "order", "capture", "nan", "warmup_failed", "warmup_contract"])
def test_series_withholds_estimate_for_invalid_or_incomplete_data(tmp_path, defect):
    root = _series(tmp_path)
    batch = root / "block-02/off"
    metadata = batch / "experiment.json"
    results = batch / "results.jsonl"
    rows = [json.loads(line) for line in results.read_text().splitlines()]
    meta = json.loads(metadata.read_text())
    if defect == "missing_batch":
        metadata.unlink()
    elif defect == "failure":
        rows[1]["task_status"] = "Failed"
    elif defect == "duplicate":
        rows[1]["task_id"] = "1-task-0"
    elif defect == "config":
        meta["deployment"]["comparable_sha256"] = "different"
    elif defect == "policy":
        meta["measurement"]["eligibility_policy"] = "verified"
    elif defect == "order":
        meta["started_unix"] = 0
    elif defect == "capture":
        results = root / "block-01/on/results.jsonl"
        rows = [json.loads(line) for line in results.read_text().splitlines()]
        rows[1]["telemetry_capture_check"]["captured"] = False
    elif defect == "nan":
        rows[1]["task_end_to_end_time_ms"] = float("nan")
    elif defect == "warmup_failed":
        rows[0]["task_status"] = "Failed"
    elif defect == "warmup_contract":
        rows[0]["completion_requirements_match"] = False
    if defect != "missing_batch":
        metadata.write_text(json.dumps(meta))
    results.write_text("".join(json.dumps(row) + "\n" for row in rows))
    output = root / "aggregate.json"
    assert comparison.compare_blocks(NS(root=root, output=output)) == 1
    result = json.loads(output.read_text())
    assert result["reasons"]
    assert result["overhead_ms"] is result["overhead_percent"] is result["confidence_interval"] is None


def test_plan_order_is_balanced_and_batch_guard_requires_predecessor(tmp_path, monkeypatch):
    root = _series(tmp_path)
    schedule = comparison._block_schedule()
    assert sum(item["mode"] == "on" for item in schedule[::2]) == 5
    assert len(schedule) == 20
    monkeypatch.setattr(comparison, "_workload_sha256", lambda: "workload")
    args = NS(plan=root / "plan.json", batch=2, mode="off", output=root / "block-01/off",
              repetitions=5, warmups=1, eligibility="runtime", timeout=300, max_steps=20)
    assert comparison._planned_batch(args)["plan_batch"] == 2
    meta_path = root / "block-01/on/experiment.json"
    meta = json.loads(meta_path.read_text())
    meta.pop("completed_unix")
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="previous planned batch"):
        comparison._planned_batch(args)
    args.mode = "on"
    with pytest.raises(ValueError, match="do not match"):
        comparison._planned_batch(args)
