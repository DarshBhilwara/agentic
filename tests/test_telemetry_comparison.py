import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

import run_telemetry_comparison as comparison


def _batch(path, mode, values, config="same"):
    path.mkdir()
    (path / "experiment.json").write_text(json.dumps({
        "mode": mode, "repetitions": 5, "warmups": 1,
        "timeout_seconds": 300, "max_steps": 20,
        "workload_sha256": "same-workload",
        "deployment": {"comparable_sha256": config}}))
    rows = [{"warmup": False, "eligible": True, "task_end_to_end_time_ms": value}
            for value in values]
    if mode == "on":
        for row in rows:
            row["telemetry_capture_check"] = {"captured": True}
    (path / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_compare_reports_mean_median_and_overhead_only_with_five_verified_runs(tmp_path):
    on, off, output = tmp_path / "on", tmp_path / "off", tmp_path / "comparison.json"
    _batch(on, "on", [120, 110, 130, 100, 140])
    _batch(off, "off", [100, 100, 100, 100, 100])

    assert comparison.compare(NS(on=on, off=off, output=output)) == 0
    result = json.loads(output.read_text())
    assert result["comparison_status"] == "eligible"
    assert result["on_mean_ms"] == 120
    assert result["off_mean_ms"] == 100
    assert result["overhead_ms"] == 20
    assert result["overhead_percent"] == 20


def test_compare_withholds_overhead_for_ineligible_or_different_configs(tmp_path):
    on, off, output = tmp_path / "on", tmp_path / "off", tmp_path / "comparison.json"
    _batch(on, "on", [120] * 5)
    _batch(off, "off", [100] * 4, config="different")

    assert comparison.compare(NS(on=on, off=off, output=output)) == 1
    result = json.loads(output.read_text())
    assert result["comparison_status"] == "ineligible"
    assert result["overhead_percent"] is None


def test_compare_withholds_overhead_when_task_settings_differ(tmp_path):
    on, off, output = tmp_path / "on", tmp_path / "off", tmp_path / "comparison.json"
    _batch(on, "on", [120] * 5)
    _batch(off, "off", [100] * 5)
    metadata = json.loads((off / "experiment.json").read_text())
    metadata["workload_sha256"] = "different-workload"
    (off / "experiment.json").write_text(json.dumps(metadata))

    assert comparison.compare(NS(on=on, off=off, output=output)) == 1
    result = json.loads(output.read_text())
    assert result["comparison_status"] == "ineligible"
    assert result["overhead_ms"] is None


def _snapshot(tmp_path):
    root = Path(__file__).resolve().parents[1]
    items = []
    wanted = {"api-gateway", "agent-worker", "task-reaper", "vllm", "kernel-telemetry"}
    for name in ("agent-node.yaml", "inference-node.yaml", "kernel-telemetry.yaml"):
        for item in yaml.safe_load_all((root / "manifests" / name).read_text()):
            if (not item or item.get("kind") not in {"Deployment", "DaemonSet"}
                    or item.get("metadata", {}).get("name") not in wanted):
                continue
            item["metadata"]["generation"] = 1
            if item["kind"] == "Deployment":
                desired = item["spec"].get("replicas", 1)
                item["spec"]["replicas"] = desired
                item["status"] = {"observedGeneration": 1, "readyReplicas": desired, "updatedReplicas": desired}
            else:
                item["status"] = {"observedGeneration": 1, "desiredNumberScheduled": 2,
                                  "numberReady": 2, "updatedNumberScheduled": 2}
            container = item["spec"]["template"]["spec"]["containers"][0]
            env = {entry["name"]: entry for entry in container.get("env", [])}
            for flag in ("AGENT_TELEMETRY_ENABLED", "AGENT_PROGRAM_TELEMETRY_ENABLED",
                         "AGENT_SEMANTIC_TELEMETRY_ENABLED"):
                env[flag] = {"name": flag, "value": "true"}
            container["env"] = list(env.values())
            items.append(item)
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"items": items}))
    return path, items


@pytest.mark.parametrize("defect", ["program_off", "missing_exporter", "old_startup"])
def test_snapshot_rejects_incomplete_on_configuration(tmp_path, defect):
    path, items = _snapshot(tmp_path)
    assert comparison.snapshot_metadata(path, "on")["mode"] == "on"
    target = "vllm" if defect == "old_startup" else "agent-worker"
    item = next(item for item in items if item["metadata"]["name"] == target)
    container = item["spec"]["template"]["spec"]["containers"][0]
    if defect == "old_startup":
        container["args"] = ["exec python3 -m vllm.entrypoints.openai.api_server --collect-detailed-traces all"]
    else:
        flag = "AGENT_PROGRAM_TELEMETRY_ENABLED" if defect == "program_off" else "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"
        next(entry for entry in container["env"] if entry["name"] == flag)["value"] = "false" if defect == "program_off" else ""
    path.write_text(json.dumps({"items": items}))
    with pytest.raises(ValueError):
        comparison.snapshot_metadata(path, "on")


def test_inference_startup_patch_matches_source_manifest():
    root = Path(__file__).resolve().parents[1]
    patch = json.loads((root / "manifests" / "inference-telemetry-switch.patch.json").read_text())
    deployment = next(item for item in yaml.safe_load_all((root / "manifests" / "inference-node.yaml").read_text())
                      if item and item.get("kind") == "Deployment" and item["metadata"]["name"] == "vllm")
    assert patch["spec"]["template"]["spec"]["containers"][0]["args"] == deployment["spec"]["template"]["spec"]["containers"][0]["args"]
