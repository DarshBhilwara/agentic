import json
import time
from contextlib import nullcontext
import yaml

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from console_api import DEFAULTS, _kernel_snapshot, _sample_kernel_records, attach_console, render_files
from collection_policy import program_capture_enabled
from kernel.collector import disabled_response


class FakeRedis:
    def lrange(self, *_args): return []
    def llen(self, *_args): return 0
    def hgetall(self, key):
        if key == "kernel:node:n1:status":
            return {b"heartbeat_unix": str(time.time()).encode(), b"status": b"idle", b"capture_enabled": b"false"}
        return {}
    def scan_iter(self, **_kwargs): return iter((b"kernel:node:n1:status",))


class FakeLedger:
    def list_runs(self, user, limit): return [{"id": "mine"}]
    def run(self, run_id): return {"id": run_id, "user.id": "alice", "summary": {"steps": 1}}
    def events(self, *_args, **_kwargs): return [{"type": "task.queued", "payload": "private"}]
    def steps(self, _run_id): return []
    def graph(self, _run_id): return {"nodes": []}


def test_config_preview_maps_toggles_into_manifests():
    rendered = render_files({**DEFAULTS, "program_enabled": False,
        "semantic_enabled": False, "kernel_enabled": False, "traces_enabled": False,
        "worker_replicas": 3})
    assert [item["name"] for item in rendered] == ["platform.yaml", "console-access.yaml", "telemetry.yaml",
        "kernel-telemetry.yaml", "agent-node.yaml", "inference-node.yaml", "settings.json"]
    files = {item["name"]: item["content"] for item in rendered}
    assert "AGENT_PROGRAM_TELEMETRY_ENABLED" in files["agent-node.yaml"]
    assert "replicas: 3" in files["agent-node.yaml"]
    assert "KERNEL_CAPTURE_ENABLED" in files["kernel-telemetry.yaml"]
    collector = next(d for d in yaml.safe_load_all(files["kernel-telemetry.yaml"])
                     if d and d.get("kind") == "DaemonSet")
    collector_env = collector["spec"]["template"]["spec"]["containers"][0]["env"]
    names = [entry["name"] for entry in collector_env]
    assert len(names) == len(set(names))
    assert next(entry["value"] for entry in collector_env
                if entry["name"] == "KERNEL_CAPTURE_ENABLED") == "false"
    telemetry = files["telemetry.yaml"]
    assert "pipelines: {}" in telemetry or "traces:" not in telemetry
    assert "console-access.yaml" in files
    gateway = next(d for d in yaml.safe_load_all(files["agent-node.yaml"]) if d and d.get("metadata", {}).get("name") == "api-gateway")
    gateway_env = {e["name"]: e.get("value") for e in gateway["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert gateway_env["CONSOLE_K8S_DISCOVERY"] == "false"


def test_config_preview_renders_resource_paths_and_independent_signal_gates():
    config = {**DEFAULTS, "agent_root": "/srv/agent", "inference_cache": "/models/cache", "model_name": "test-model",
              "inference_syscalls": False, "host_metrics": False, "traces_enabled": False,
              "metrics_enabled": False, "logs_enabled": False, "retention_days": 7}
    files = {item["name"]: item["content"] for item in render_files(config)}
    agent_docs = list(yaml.safe_load_all(files["agent-node.yaml"]))
    worker = next(d for d in agent_docs if d and d.get("metadata", {}).get("name") == "agent-worker")
    worker_env = {e["name"]: e.get("value") for e in worker["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert worker_env["AGENT_TELEMETRY_RETENTION_SECONDS"] == "604800"
    assert worker_env["KERNEL_TELEMETRY_ENABLED"] == "true"
    assert worker_env["AGENT_WORKSPACE_HOST_ROOT"] == "/srv/agent"
    inference_docs = list(yaml.safe_load_all(files["inference-node.yaml"]))
    vllm = next(d for d in inference_docs if d and d.get("metadata", {}).get("name") == "vllm")
    assert "telemetry.agentic.io/syscalls" not in vllm["spec"]["template"]["metadata"]["labels"]
    assert vllm["spec"]["template"]["spec"]["volumes"][0]["hostPath"]["path"] == "/models/cache"
    assert next(e["value"] for e in vllm["spec"]["template"]["spec"]["containers"][0]["env"] if e["name"] == "INFERENCE_MODEL_NAME") == "test-model"
    telemetry_docs = list(yaml.safe_load_all(files["telemetry.yaml"]))
    gateway_cm = next(d for d in telemetry_docs if d and d.get("metadata", {}).get("name") == "otel-collector-gateway-config")
    pipelines = yaml.safe_load(gateway_cm["data"]["config.yaml"])["service"]["pipelines"]
    assert "traces" not in pipelines and pipelines
    gateway_deployment = next(d for d in telemetry_docs if d and d.get("metadata", {}).get("name") == "otel-collector-gateway")
    assert gateway_deployment["spec"]["replicas"] == 0
    node_cm = next(d for d in telemetry_docs if d and d.get("metadata", {}).get("name") == "otel-collector-node-config")
    assert "hostmetrics" not in yaml.safe_load(node_cm["data"]["config.yaml"])["receivers"]
    platform_docs = list(yaml.safe_load_all(files["platform.yaml"]))
    assert not any(d and d.get("metadata", {}).get("name") == "otel-node-collector" for d in platform_docs)


def test_device_discovery_config_controls_gateway_env():
    files = {item["name"]: item["content"] for item in render_files({**DEFAULTS, "device_discovery_enabled": True})}
    gateway = next(d for d in yaml.safe_load_all(files["agent-node.yaml"]) if d and d.get("metadata", {}).get("name") == "api-gateway")
    env = {e["name"]: e.get("value") for e in gateway["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["CONSOLE_K8S_DISCOVERY"] == "true"


def test_console_ownership_reports_and_offline_discovery(monkeypatch):
    app = FastAPI()
    def auth(x_api_key: str = "alice"):
        if x_api_key != "alice":
            from fastapi import HTTPException
            raise HTTPException(401)
        return "alice"
    attach_console(app, auth, FakeRedis, FakeLedger)
    client = TestClient(app)
    assert client.get("/console/runs").json()["runs"] == [{"id": "mine"}]
    assert client.get("/console/runs/owned").status_code == 200
    foreign = FakeLedger()
    foreign.run = lambda _run_id: {"id": "foreign", "user.id": "bob"}
    app2 = FastAPI(); attach_console(app2, auth, FakeRedis, lambda: foreign)
    assert TestClient(app2).get("/console/runs/foreign").status_code == 404
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("CONSOLE_K8S_DISCOVERY", "false")
    result = client.get("/console/devices").json()
    assert result["source"] == "unavailable" and result["nodes"] == []
    assert result["collectors"][0]["status"] == "idle" and result["collectors"][0]["heartbeat_ok"] is True


def test_config_rejects_unknown_and_invalid_values():
    from console_api import validate_config
    from fastapi import HTTPException
    with pytest.raises(HTTPException): validate_config({"unbounded_command": "id"})
    with pytest.raises(HTTPException): validate_config({"worker_replicas": True})
    with pytest.raises(HTTPException): validate_config({"vllm_url": "http://host/v1?token=secret"})


def test_runtime_program_gate(monkeypatch):
    monkeypatch.setenv("AGENT_PROGRAM_TELEMETRY_ENABLED", "false")
    assert not program_capture_enabled()


def test_disabled_collector_reports_idle_and_rejects_capture_commands():
    ping = disabled_response("ping")
    assert ping["ok"] is True and ping["status"] == "idle" and ping["capture_enabled"] is False
    rejected = disabled_response("register")
    assert rejected["ok"] is False and "disabled" in rejected["error"]


def test_reports_filter_content_for_all_formats_and_escape_csv_formulas():
    ledger = FakeLedger()
    ledger.run = lambda run_id: {"id": run_id, "user.id": "alice", "status": "completed", "summary": {"inference_steps": 1, "prompt": "private-secret"}, "workspace.id": "/secret/path"}
    ledger.steps = lambda _run: [{"step_index": 1, "step_status": "Success", "step_time_ms": 4, "prompt": "private-secret", "tools": [{"tool_name": "=FORMULA()", "tool_status": "Success", "tool_latency_ms": 2, "arguments": "private-secret"}]}]
    ledger.events = lambda *_a, **_k: [{"id": "e", "type": "tool.completed", "payload": {"result": "private-secret"}, "attributes": {"tool_name": "read_file", "result": "private-secret", "prompt": "private-secret"}}]
    # Exercise serialization directly; authentication/ownership has separate API coverage.
    class ReportApp:
        def include_router(self, router):
            self.routes = router.routes
    app = ReportApp(); attach_console(app, lambda: "alice", FakeRedis, lambda: ledger)
    report = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/console/reports")
    for fmt in ("json", "markdown", "csv"):
        response = report({"run_ids": ["run-1"], "format": fmt}, user="alice")
        assert response.status_code == 200
        text = response.body.decode()
        assert "private-secret" not in text and "/secret/path" not in text
        if fmt == "csv": assert "'=FORMULA()" in text


def test_kernel_report_sample_includes_late_tool_scope():
    rows = []
    for pair in range(500):
        timestamp = pair * 2
        common = {"collector_id": "c1", "pid": 10, "tid": 10, "syscall_id": pair}
        rows.extend([
            {**common, "type": "entry", "monotonic_ns": timestamp, "entry_monotonic_ns": timestamp},
            {**common, "type": "exit", "monotonic_ns": timestamp + 1, "entry_monotonic_ns": timestamp},
        ])
    rows.extend([
        {"type": "entry", "monotonic_ns": 1000, "entry_monotonic_ns": 1000,
         "collector_id": "c1", "pid": 10, "tid": 10, "syscall_id": 2,
         "step_index": 1, "tool_name": "execute_command", "tool_call_id": "call-1"},
        {"type": "exit", "monotonic_ns": 1001, "entry_monotonic_ns": 1000,
         "collector_id": "c1", "pid": 10, "tid": 10, "syscall_id": 2,
         "step_index": 1, "tool_name": "execute_command", "tool_call_id": "call-1"},
    ])

    class SnapshotRedis:
        lrange_calls = 0
        def lrange(self, _key, start, stop):
            self.lrange_calls += 1
            assert start == 0 and stop == -1
            return [json.dumps(row).encode() for row in rows]
        def hgetall(self, _key): return {b"events": b"1002"}

    redis_client = SnapshotRedis()
    rows[0], rows[2] = rows[2], rows[0]
    snapshot = _kernel_snapshot(redis_client, "run-1")
    sampled = snapshot["records"]
    tool_records = [row for row in sampled if row.get("tool_call_id") == "call-1"]
    assert len(sampled) == 500
    assert snapshot["sampled"] is True and snapshot["truncated"] is True
    assert [(row["type"], row["step_index"], row["tool_name"]) for row in tool_records] == [
        ("entry", 1, "execute_command"), ("exit", 1, "execute_command")]
    assert [row["monotonic_ns"] for row in sampled] == sorted(row["monotonic_ns"] for row in sampled)
    paired = {}
    for row in sampled:
        key = (row["collector_id"], row["pid"], row["tid"], row["entry_monotonic_ns"], row["syscall_id"])
        paired.setdefault(key, set()).add(row["type"])
    assert all(types == {"entry", "exit"} for types in paired.values())
    assert len(paired) == len(sampled) // 2
    assert redis_client.lrange_calls == 1


def test_kernel_report_sorts_small_snapshots_stably_by_monotonic_time():
    rows = [
        {"id": "late", "monotonic_ns": 20},
        {"id": "first-tie", "monotonic_ns": 10},
        {"id": "second-tie", "monotonic_ns": 10},
    ]
    assert [row["id"] for row in _sample_kernel_records(rows)] == [
        "first-tie", "second-tie", "late"]


def test_kernel_report_keeps_unmatched_exit_when_pairs_fill_sample():
    rows = []
    for pair in range(300):
        timestamp = pair * 2
        common = {"collector_id": "c1", "pid": 10, "tid": 10, "syscall_id": pair}
        rows.extend([
            {**common, "type": "entry", "monotonic_ns": timestamp, "entry_monotonic_ns": timestamp},
            {**common, "type": "exit", "monotonic_ns": timestamp + 1, "entry_monotonic_ns": timestamp},
        ])
    rows.append({"collector_id": "c1", "pid": 10, "tid": 10, "type": "unmatched_exit",
                 "monotonic_ns": 600, "entry_monotonic_ns": None, "syscall_id": 999,
                 "step_index": 3, "tool_name": "execute_command", "tool_call_id": "call-gap"})

    sampled = _sample_kernel_records(rows)
    assert len(sampled) == 500
    assert any(row.get("type") == "unmatched_exit" and row.get("tool_call_id") == "call-gap"
               for row in sampled)


def test_worker_finishes_when_program_and_semantic_capture_are_off(monkeypatch, tmp_path):
    import fakeredis
    import worker
    from telemetry_ledger import TelemetryLedger
    redis_client = fakeredis.FakeRedis()
    redis_client.hset("task:t1", mapping={"id": "t1", "status": "pending", "user": "alice", "session_id": "s1",
        "agent_id": "a1", "agent_run_id": "ar1", "run_id": "r1", "root_run_id": "r1", "created_at": "2026-10-18T00:00:00+00:00",
        "submitted_at_unix": str(time.time()), "prompt": "do work", "trace_context": "{}", "workspace": str(tmp_path)})
    monkeypatch.setenv("AGENT_PROGRAM_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("AGENT_SEMANTIC_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("AGENT_RESULTS_ROOT", str(tmp_path))
    monkeypatch.setattr(worker, "kernel_telemetry_from_env", lambda: type("Kernel", (), {"scope": lambda *_a, **_k: nullcontext()})())
    monkeypatch.setattr(worker, "run", lambda *_a, **_k: ("complete", []))
    worker.process_task(redis_client, "t1")
    ledger = TelemetryLedger(redis_client)
    assert redis_client.hget("task:t1", "status") == b"completed"
    assert "summary" not in ledger.run("r1")
    assert ledger.steps("r1") == []


def test_semantic_gate_filters_graph_events_but_keeps_tool_events(monkeypatch):
    import fakeredis
    from telemetry_ledger import TelemetryLedger
    monkeypatch.setenv("AGENT_SEMANTIC_TELEMETRY_ENABLED", "false")
    ledger = TelemetryLedger(fakeredis.FakeRedis())
    ledger.start_run("r1", **{"user.id": "alice"})
    suppressed = ledger.emit("procedural_graph.observed", run_id="r1", **{"user.id": "alice"})
    emitted = ledger.emit("tool.completed", run_id="r1", **{"user.id": "alice"})
    assert suppressed.get("suppressed") is True
    assert emitted["type"] == "tool.completed"
    assert [event["type"] for event in ledger.events("r1")] == ["tool.completed"]


def test_all_detail_off_keeps_minimal_lifecycle_and_drops_model_tool_content(monkeypatch):
    import fakeredis
    from telemetry_ledger import TelemetryLedger
    monkeypatch.setenv("AGENT_PROGRAM_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("AGENT_SEMANTIC_TELEMETRY_ENABLED", "false")
    client = fakeredis.FakeRedis()
    ledger = TelemetryLedger(client)
    ledger.start_run("r1", **{"user.id": "alice"})
    for event_type in ("model.completed", "tool.completed"):
        result = ledger.emit(event_type, run_id="r1", attributes={"result": "secret-raw", "tool_name": "read_file"},
                             payload={"prompt": "secret-raw", "result": "secret-raw"})
        assert result["suppressed"] is True
    ledger.emit("task.queued", run_id="r1", attributes={"prompt": "secret-raw"},
                payload={"prompt": "secret-raw"}, **{"user.id": "alice"})
    ledger.emit("task.completed", run_id="r1", attributes={"task_status": "Success", "tool_calls": 3, "result": "secret-raw"},
                payload={"result": "secret-raw"}, **{"user.id": "alice"})
    stored = "".join(value.decode() for value in client.lrange("telemetry:run:r1:events", 0, -1))
    assert "secret-raw" not in stored
    events = ledger.events("r1")
    assert [event["type"] for event in events] == ["task.queued", "task.completed"]
    assert all("payload" not in event for event in events)
    assert "tool_calls" not in events[-1].get("attributes", {})
    assert events[-1]["attributes"]["task_status"] == "Success"


def test_all_detail_off_suppresses_optional_spans(monkeypatch):
    import telemetry
    monkeypatch.setenv("AGENT_PROGRAM_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("AGENT_SEMANTIC_TELEMETRY_ENABLED", "false")
    calls = []
    class RecordingTracer:
        def start_as_current_span(self, name, **_kwargs):
            calls.append(name)
            return nullcontext(type("Span", (), {"set_attribute": lambda *_a: None})())
    monkeypatch.setattr(telemetry, "tracer", RecordingTracer())
    with telemetry.span("agent.tool", input="secret-raw"):
        pass
    with telemetry.span("agent.inference", prompt="secret-raw"):
        pass
    with telemetry.span("agent.task.timeout", termination_reason="raw"):
        pass
    with telemetry.intent_span("secret-raw", user="alice", agent_id="a", session_id="s", turn_id="t", turn_number=1):
        pass
    assert calls == []


@pytest.mark.parametrize("semantic_enabled", [True, False])
def test_delegation_always_queues_child_with_semantic_capture_independent(monkeypatch, semantic_enabled):
    import fakeredis
    import agent
    from telemetry_ledger import TelemetryLedger
    redis_client = fakeredis.FakeRedis()
    ledger = TelemetryLedger(redis_client)
    ledger.start_run("parent-run", **{"user.id": "alice"})
    monkeypatch.setenv("AGENT_SEMANTIC_TELEMETRY_ENABLED", str(semantic_enabled).lower())
    monkeypatch.setenv("AGENT_PROGRAM_TELEMETRY_ENABLED", "true")
    context = {"user": "alice", "session_id": "session", "task_id": "parent-task", "run_id": "parent-run",
        "root_run_id": "parent-run", "agent_id": "parent-agent", "agent_run_id": "parent-agent-run",
        "workspace": "/workspace", "turn_number": 1, "timeout_seconds": 60, "max_steps": 5,
        "event_sink": lambda event_type, **kwargs: ledger.emit(event_type, run_id="parent-run", **kwargs),
        "redis": redis_client}
    token = agent.RUNTIME_CONTEXT.set(context)
    try:
        result = json.loads(agent.delegate_to_agent({"task": "child work", "agent_name": "helper"}, "/workspace"))
    finally:
        agent.RUNTIME_CONTEXT.reset(token)
    child = redis_client.hgetall(f"task:{result['task_id']}")
    assert result["status"] == "queued"
    assert child[b"prompt"] == b"child work" and child[b"status"] == b"pending"
    assert redis_client.lrange("task_queue", 0, -1) == [result["task_id"].encode()]
    if semantic_enabled:
        assert redis_client.hgetall("telemetry:run:parent-run:graph")
    else:
        assert not redis_client.hgetall("telemetry:run:parent-run:graph")
