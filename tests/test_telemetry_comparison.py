import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_telemetry_comparison as comparison


def _batch(path, mode, values, config="same", eligibility="verified"):
    path.mkdir()
    (path / "experiment.json").write_text(json.dumps({
        "mode": mode, "repetitions": 5, "warmups": 1,
        "timeout_seconds": 300, "max_steps": 20,
        "measurement": {"eligibility_policy": eligibility},
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


def test_compare_withholds_overhead_when_eligibility_policies_differ(tmp_path):
    on, off, output = tmp_path / "on", tmp_path / "off", tmp_path / "comparison.json"
    _batch(on, "on", [120] * 5, eligibility="runtime")
    _batch(off, "off", [100] * 5, eligibility="verified")

    assert comparison.compare(NS(on=on, off=off, output=output)) == 1
    result = json.loads(output.read_text())
    assert result["batch_checks"]["eligibility_policy"] is False
    assert result["eligibility_policy"] is None
    assert result["comparison_status"] == "ineligible"


def test_runtime_eligibility_uses_returned_answer_not_verifier(tmp_path):
    eligible, reasons = comparison._eligibility(
        "off", "runtime", False, "Success", "incorrect but nonempty", 123.0, False, False)

    assert eligible is True
    assert reasons == []


def test_completion_requirements_are_separate_from_workspace_tool_restriction():
    assert comparison._completion_requirements("runtime") == {
        "require_workspace_changes": False, "require_command_execution": False}
    assert comparison._completion_requirements("verified") == {
        "require_workspace_changes": True, "require_command_execution": True}


def test_runtime_preflight_rejects_old_gateway_before_submission():
    class Api:
        def __init__(self, supported):
            self.calls = []
            self.supported = supported
        def request(self, method, route):
            self.calls.append((method, route))
            return {"coding_completion_overrides": self.supported}
    old = Api(False)
    with pytest.raises(ValueError, match="updated gateway"):
        comparison._check_runtime_capability(old, "runtime")
    assert old.calls == [("GET", "/capabilities")]
    current = Api(True)
    comparison._check_runtime_capability(current, "runtime")
    comparison._check_runtime_capability(old, "verified")
    assert old.calls == [("GET", "/capabilities")]


@pytest.mark.parametrize("missing", [None, "engine", "kernel", "program"])
def test_answer_only_capture_still_requires_program_engine_and_kernel(missing):
    result = comparison._capture_check(
        {"inference_steps": 1, "tool_calls": 0}, [] if missing == "program" else [{"tools": []}],
        0 if missing == "engine" else 1, 0 if missing == "kernel" else 10,
        {"program": 10}, "runtime")
    assert result["captured"] is (missing is None)
    assert not result["tool_scopes_required"]


def test_tool_using_runtime_capture_and_verified_capture_require_tool_scopes():
    summary = {"inference_steps": 1, "tool_calls": 1}
    assert not comparison._capture_check(summary, [{"tools": [{"tool_name": "read_file"}]}],
                                         1, 10, {}, "runtime")["captured"]
    assert not comparison._capture_check({"inference_steps": 1, "tool_calls": 0}, [{"tools": []}],
                                         1, 10, {}, "verified")["captured"]


def test_runtime_verifier_exception_is_diagnostic_only(tmp_path, monkeypatch):
    def fail(_workspace):
        raise OSError("host verification unavailable")
    monkeypatch.setattr(comparison, "_host_verify", fail)
    result = comparison._verify_attempt(tmp_path, "Success", "runtime")
    assert result["passed"] is False
    assert result["diagnostic_error"]["type"] == "OSError"
    with pytest.raises(OSError):
        comparison._verify_attempt(tmp_path, "Success", "verified")


def test_runtime_compare_reports_policy_and_overhead(tmp_path):
    on, off = tmp_path / "on", tmp_path / "off"
    _batch(on, "on", [110] * 5, eligibility="runtime")
    _batch(off, "off", [100] * 5, eligibility="runtime")
    output = tmp_path / "comparison.json"
    assert comparison.compare(NS(on=on, off=off, output=output)) == 0
    result = json.loads(output.read_text())
    assert result["eligibility_policy"] == "runtime"
    assert result["overhead_percent"] == 10


@pytest.mark.parametrize(("status", "answer", "timer", "expected_reason"), [
    ("Success", "  \n", 123.0, "empty_task_result"),
    ("Failed", "some answer", 123.0, "runtime_not_successful"),
    ("Success", "some answer", None, "missing_primary_timing"),
])
def test_runtime_eligibility_excludes_blank_failed_or_untimed_tasks(status, answer, timer, expected_reason):
    eligible, reasons = comparison._eligibility(
        "off", "runtime", False, status, answer, timer, False, False)

    assert eligible is False
    assert expected_reason in reasons


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


@pytest.mark.parametrize("defect", ["program_off", "missing_exporter", "old_startup", "missing_readiness"])
def test_snapshot_rejects_incomplete_on_configuration(tmp_path, defect):
    path, items = _snapshot(tmp_path)
    assert comparison.snapshot_metadata(path, "on")["mode"] == "on"
    target = "vllm" if defect in {"old_startup", "missing_readiness"} else "agent-worker"
    item = next(item for item in items if item["metadata"]["name"] == target)
    container = item["spec"]["template"]["spec"]["containers"][0]
    if defect == "old_startup":
        container["args"] = ["exec python3 -m vllm.entrypoints.openai.api_server --collect-detailed-traces all"]
    elif defect == "missing_readiness":
        container.pop("readinessProbe")
    else:
        flag = "AGENT_PROGRAM_TELEMETRY_ENABLED" if defect == "program_off" else "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"
        next(entry for entry in container["env"] if entry["name"] == flag)["value"] = "false" if defect == "program_off" else ""
    path.write_text(json.dumps({"items": items}))
    with pytest.raises(ValueError):
        comparison.snapshot_metadata(path, "on")


def test_comparable_config_normalizes_init_mode_but_keeps_other_drift(tmp_path):
    path, items = _snapshot(tmp_path)
    worker = next(item for item in items if item["metadata"]["name"] == "agent-worker")
    init = worker["spec"]["template"]["spec"]["initContainers"][0]
    init["env"] = [{"name": "AGENT_TELEMETRY_ENABLED", "value": "true"},
                   {"name": "KEEP_SETTING", "value": "original"}]
    original = comparison._canonical(worker)
    init["env"][0]["value"] = "false"
    assert comparison._canonical(worker) == original
    init["env"][1]["value"] = "changed"
    assert comparison._canonical(worker) != original
    init["env"][1]["value"] = "original"
    init["image"] = "different-image"
    assert comparison._canonical(worker) != original


def test_inference_startup_patch_matches_source_manifest():
    root = Path(__file__).resolve().parents[1]
    patch = json.loads((root / "manifests" / "inference-telemetry-switch.patch.json").read_text())
    deployment = next(item for item in yaml.safe_load_all((root / "manifests" / "inference-node.yaml").read_text())
                      if item and item.get("kind") == "Deployment" and item["metadata"]["name"] == "vllm")
    assert patch["spec"]["template"]["spec"]["containers"][0]["args"] == deployment["spec"]["template"]["spec"]["containers"][0]["args"]
