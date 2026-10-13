import json
import hashlib
from unittest.mock import Mock

import fakeredis
import pytest
from openai.types.chat import ChatCompletion
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import agent
import telemetry
import worker
from program_telemetry import ProgramTelemetry
from telemetry_ledger import TelemetryLedger


def _tracer(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "tracer", provider.get_tracer("telemetry-test"))
    return exporter


def _completion():
    return ChatCompletion.model_validate({
        "id": "chatcmpl-test", "object": "chat.completion", "created": 1, "model": "qwen",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "Done"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })


def test_ledger_event_exports_policy_correlation_and_error(monkeypatch):
    exporter = _tracer(monkeypatch)
    monkeypatch.setattr(telemetry, "CONTENT_CAPTURE", "redacted")
    ledger = TelemetryLedger(fakeredis.FakeRedis())
    with telemetry.span("agent.program", **{"run.id": "run-1"}):
        ledger.emit("tool.failed", run_id="run-1", severity="error",
                    attributes={"tool": "read_file", "note": "password=hunter2"},
                    payload={"value": "secret token=abc123"}, **{"handoff.id": "handoff-1"})

    event_span = next(item for item in exporter.get_finished_spans() if item.name == "tool.failed")
    attrs = event_span.attributes
    assert attrs["run.id"] == "run-1"
    assert attrs["handoff.id"] == "handoff-1"
    assert attrs["telemetry.event.severity"] == "error"
    assert event_span.status.status_code.name == "ERROR"
    protected = attrs["telemetry.event.attributes"] + attrs["telemetry.event.payload"]
    assert "hunter2" not in protected and "abc123" not in protected
    assert "[REDACTED]" in protected


def test_graph_snapshot_protects_values_but_preserves_structure(monkeypatch):
    graph = {"nodes": ["model:1", "secret=abc"], "edges": [["START", "model:1"]],
             "signature": "a" * 64, "count": 2}
    monkeypatch.setattr(telemetry, "CONTENT_CAPTURE", "off")
    snapshot = json.loads(telemetry.graph_snapshot_json(graph))
    assert "nodes" in snapshot and "edges" in snapshot
    assert snapshot["count"] == 2
    assert snapshot["nodes"][0]["capture"] == "off"
    assert snapshot["nodes"][1]["sha256"] == hashlib.sha256(b"secret=abc").hexdigest()
    assert snapshot["signature"]["capture"] == "off"


def test_export_does_not_trust_invalid_capture_wrappers(monkeypatch):
    monkeypatch.setattr(telemetry, "CONTENT_CAPTURE", "off")
    invalid = {"text": "password=secret", "sha256": "attacker-value", "length": -1,
               "capture": "redacted"}
    encoded = telemetry.json_attribute(invalid)
    assert "password" not in encoded and "secret" not in encoded
    valid = {"text": "already masked", "sha256": "a" * 64, "length": 12,
             "capture": "redacted"}
    protected = json.loads(telemetry.json_attribute(valid))
    assert protected == {"sha256": "a" * 64, "length": 12, "capture": "off"}


@pytest.mark.parametrize("failure", [False, True])
def test_graph_span_exports_terminal_snapshot_on_success_and_error(monkeypatch, tmp_path, failure):
    exporter = _tracer(monkeypatch)
    client = Mock()
    monkeypatch.setattr(agent, "OpenAI", Mock(return_value=client))
    monkeypatch.setattr(agent, "collect_engine_metrics", lambda *args: None)
    if failure:
        client.chat.completions.create.side_effect = RuntimeError("model offline")
    else:
        client.chat.completions.create.return_value = _completion()
    redis_client = fakeredis.FakeRedis()
    ledger = TelemetryLedger(redis_client)
    ledger.start_run("run", **{"user.id": "alice"})

    def emit(event_type, **kwargs):
        return ledger.emit(event_type, run_id="run", **kwargs)

    kwargs = {"run_id": "run", "task_id": "task", "redis_client": redis_client,
              "event_sink": emit, "measurements": ProgramTelemetry("task")}
    if failure:
        with pytest.raises(RuntimeError, match="model offline"):
            agent.run("hello", "alice", workspace=str(tmp_path), **kwargs)
    else:
        agent.run("hello", "alice", workspace=str(tmp_path), **kwargs)

    graph_span = next(item for item in exporter.get_finished_spans() if item.name == "langgraph.graph")
    snapshot = json.loads(graph_span.attributes["procedural_graph"])
    assert snapshot["nodes"]
    assert snapshot["edges"]
    assert graph_span.attributes["procedural_graph.status"] == ("error" if failure else "success")


@pytest.mark.parametrize("failure", [False, True])
def test_program_span_exports_terminal_summary(monkeypatch, tmp_path, failure):
    exporter = _tracer(monkeypatch)
    monkeypatch.setattr(telemetry, "CONTENT_CAPTURE", "redacted")
    redis_client = fakeredis.FakeRedis()
    task_id = "task-1"
    redis_client.hset(f"task:{task_id}", mapping={
        "status": "queued", "run_id": "run-1", "user": "alice", "session_id": "session-1",
        "prompt": "hello", "workspace": str(tmp_path), "created_at": telemetry.datetime.now(
            telemetry.timezone.utc).isoformat(), "submitted_at_unix": telemetry.datetime.now(
                telemetry.timezone.utc).timestamp(), "timeout_seconds": "30",
    })
    if failure:
        monkeypatch.setattr(worker, "run", Mock(side_effect=RuntimeError("run failed")))
    else:
        monkeypatch.setattr(worker, "run", Mock(return_value=("done", [])))
    monkeypatch.setenv("AGENT_RESULTS_ROOT", str(tmp_path / "results"))
    worker.process_task(redis_client, task_id)

    program_span = next(item for item in exporter.get_finished_spans() if item.name == "agent.program")
    summary = json.loads(program_span.attributes["agent.program.summary"])
    assert program_span.attributes["agent.program.status"] == ("Failed" if failure else "Success")
    assert summary["task_status"] == ("Failed" if failure else "Success")
