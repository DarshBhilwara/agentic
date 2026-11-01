import json
from unittest.mock import Mock

import pytest

import run_telemetry_overhead as overhead


def snapshot(worker_value="true", collector_value="true"):
    return {"kind": "List", "items": [
        {"kind": "Deployment", "metadata": {"name": "agent-worker", "generation": 2},
         "spec": {"replicas": 2, "template": {"spec": {"containers": [
             {"name": "worker", "image": "agentic-worker:tag", "env": [{"name": "KERNEL_TELEMETRY_ENABLED", "value": worker_value}]}]}}},
         "status": {"observedGeneration": 2, "readyReplicas": 2, "updatedReplicas": 2, "unavailableReplicas": 0}},
        {"kind": "DaemonSet", "metadata": {"name": "kernel-telemetry", "generation": 3},
         "spec": {"template": {"spec": {"containers": [
             {"name": "collector", "image": "agentic-kernel:tag", "env": [{"name": "KERNEL_CAPTURE_ENABLED", "value": collector_value}]}]}}},
         "status": {"observedGeneration": 3, "desiredNumberScheduled": 2, "numberReady": 2,
                    "updatedNumberScheduled": 2, "numberUnavailable": 0}},
        {"kind": "Deployment", "metadata": {"name": "vllm", "generation": 4},
         "spec": {"replicas": 1, "template": {"spec": {"containers": [
             {"name": "vllm", "image": "agentic-vllm:tag"}]}}},
         "status": {"observedGeneration": 4, "readyReplicas": 1,
                    "updatedReplicas": 1, "unavailableReplicas": 0}},
    ]}


def test_snapshot_requires_both_capture_switches_to_match(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot("false", "true")))
    with pytest.raises(ValueError, match="kernel-telemetry KERNEL_CAPTURE_ENABLED"):
        overhead.snapshot_metadata(path, "disabled")


def test_snapshot_rejects_rollout_that_is_not_ready(tmp_path):
    data = snapshot()
    data["items"][0]["status"]["readyReplicas"] = 1
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="not fully ready"):
        overhead.snapshot_metadata(path, "enabled")


def test_comparable_hash_ignores_capture_switch_and_restart_timestamp(tmp_path):
    hashes = []
    for enabled, stamp in (("true", "old"), ("false", "new")):
        data = snapshot(enabled, enabled)
        for item in data["items"]:
            item["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[
                "kubectl.kubernetes.io/restartedAt"] = stamp
        path = tmp_path / f"snapshot-{enabled}.json"
        path.write_text(json.dumps(data))
        hashes.append(overhead.snapshot_metadata(path, "enabled" if enabled == "true" else "disabled")
                      ["comparable_config_sha256"])
    assert hashes[0] == hashes[1]


def test_snapshot_records_only_allowlisted_configuration(tmp_path):
    data = snapshot()
    data["items"][0]["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "AGENT_API_KEY", "value": "secret"})
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(data))
    result = overhead.snapshot_metadata(path, "enabled")
    assert result["worker"]["image"] == "agentic-worker:tag"
    assert "secret" not in json.dumps(result)
    assert result["collector"]["capture_enabled"] == "true"


def test_gateway_error_is_sanitized():
    class Response:
        status_code = 401

    exc = overhead.requests.HTTPError("token=secret https://host.invalid/path")
    exc.response = Response()
    safe = overhead._safe_error(exc)
    assert safe == {"type": "HTTPError", "http_status": 401}
    assert "secret" not in json.dumps(safe)


def test_collector_health_comes_from_selected_run_and_preserves_all_counters():
    api = Mock()
    api.request.return_value = {"node_health": {"node": "agent-01", "heartbeat_ok": True,
        "capture_enabled": "false", "events": "8", "perf_lost": "1", "map_overflow": "2",
        "queue_drops": "3", "export_errors": "4", "discovery_errors": "5", "identity_misses": "6"}}
    health = overhead._collector_snapshot(api, "old-run", "agent-01", "disabled")
    assert api.request.call_args.args == ("GET", "/telemetry/runs/old-run/kernel")
    assert health["queue_drops"] == "3" and health["identity_misses"] == "6"
    assert health["capture_enabled"] == "false"


def test_collector_health_rejects_wrong_node_or_missing_capture_state():
    api = Mock()
    api.request.return_value = {"node_health": {"node": "inference", "heartbeat_ok": True,
                                                "capture_enabled": True}}
    with pytest.raises(ValueError, match="does not resolve"):
        overhead._collector_snapshot(api, "run", "agent-01", "enabled")
    api.request.return_value = {"node_health": {"node": "agent-01", "heartbeat_ok": True}}
    with pytest.raises(ValueError, match="unavailable"):
        overhead._collector_snapshot(api, "run", "agent-01", "disabled")


def test_failed_terminal_task_keeps_identity_and_records_limits(tmp_path, monkeypatch):
    api = Mock()
    api.request.side_effect = [
        {"session_id": "session-1"},
        {"task_id": "task-1", "program_id": "program-1", "run_id": "run-1"},
        {"status": "failed", "error": "private prompt text"},
        {"summary": {"task_completion_time_ms": 22}, "steps": []},
        {"status": {"events": 4}, "node_health": {"queue_drops": 0}, "total": 4,
         "retained": 4, "records": [], "truncated": False},
    ]
    workspace_root, output = tmp_path / "workspaces", tmp_path / "out"
    workspace_root.mkdir(); output.mkdir()
    result = overhead._run_one(api, workspace_root, output, 1, False, 17, "agent-01", "enabled")
    submit = api.request.call_args_list[1]
    assert submit.kwargs["params"]["timeout_seconds"] == 17
    assert submit.kwargs["params"]["max_steps"] == 4
    assert result["task_status"] == "Failed"
    assert result["task_id"] == "task-1" and result["run_id"] == "run-1"
    assert result["task_error_present"] is True
    assert result["kernel_total"] == 4 and result["kernel_retained"] == 4
    assert "private prompt text" not in json.dumps(result)
    saved = json.loads(next(output.glob("*.json")).read_text())
    assert saved["task_status"] == "Failed"


def test_nonterminal_poll_timeout_preserves_task_and_stops_as_unknown(tmp_path, monkeypatch):
    api = Mock()
    api.request.side_effect = [
        {"session_id": "session-2"},
        {"task_id": "task-2", "program_id": "program-2", "run_id": "run-2"},
        {"status": "processing"},
    ]
    workspace_root, output = tmp_path / "workspaces", tmp_path / "out"
    workspace_root.mkdir(); output.mkdir()
    ticks = iter((0, 46))
    monkeypatch.setattr(overhead.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(overhead.time, "sleep", lambda _seconds: None)
    result = overhead._run_one(api, workspace_root, output, 1, False, 1, "agent-01", "enabled")
    assert result["task_status"] == "Unknown"
    assert result["task_id"] == "task-2" and result["run_id"] == "run-2"
    assert result["runner_error"]["type"] == "TimeoutError"
    assert "task-2" in (output / f"{result['attempt_id']}.submission.json").read_text()
    assert len(api.request.call_args_list) == 3


def test_terminal_timeout_preserves_identity_for_operator_followup(tmp_path):
    api = Mock()
    api.request.side_effect = [
        {"session_id": "session-4"},
        {"task_id": "task-4", "program_id": "program-4", "run_id": "run-4"},
        {"status": "timeout", "error": "deadline"},
        {"summary": {}, "steps": []},
        {"status": {}, "node_health": {}, "total": 0, "retained": 0, "records": [], "truncated": False},
    ]
    workspace_root, output = tmp_path / "workspaces", tmp_path / "out"
    workspace_root.mkdir(); output.mkdir()
    result = overhead._run_one(api, workspace_root, output, 1, False, 17, "agent-01", "enabled")
    assert result["task_status"] == "Timeout"
    assert result["task_id"] == "task-4" and result["run_id"] == "run-4"
    assert result["task_error_present"] is True


def test_telemetry_fetch_error_does_not_replace_terminal_status(tmp_path):
    api = Mock()
    api.request.side_effect = [
        {"session_id": "session-3"},
        {"task_id": "task-3", "program_id": "program-3", "run_id": "run-3"},
        {"status": "completed"},
        overhead.requests.HTTPError("sensitive response body"),
    ]
    workspace_root, output = tmp_path / "workspaces", tmp_path / "out"
    workspace_root.mkdir(); output.mkdir()
    result = overhead._run_one(api, workspace_root, output, 1, False, 17, "agent-01", "enabled")
    assert result["task_status"] == "Success"
    assert result["telemetry_error"]["type"] == "HTTPError"
    assert "sensitive response body" not in json.dumps(result)
