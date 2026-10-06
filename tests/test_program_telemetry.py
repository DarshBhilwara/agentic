import json
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

import fakeredis
import pytest
from fastapi.testclient import TestClient
from openai.types.chat import ChatCompletion

import agent
import gateway
import worker
from program_telemetry import ProgramTelemetry, StepLimitExceeded, TaskTimeout, collect_engine_metrics
from telemetry_ledger import TelemetryLedger


def completion(calls=None, *, usage=True, finish="stop"):
    return ChatCompletion.model_validate({
        "id": "chatcmpl-test", "object": "chat.completion", "created": 1, "model": "qwen",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else finish,
                     "message": {"role": "assistant", "content": None if calls else "Done", "tool_calls": calls}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110} if usage else None})


def tool(name="calculate_density", arguments='{"mass": 4, "volume": 2}', id="call-1"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": arguments}}


@pytest.fixture
def fake_model(monkeypatch):
    client = Mock()
    monkeypatch.setattr(agent, "OpenAI", Mock(return_value=client))
    monkeypatch.setattr(agent, "collect_engine_metrics", lambda *args: None)
    return client.chat.completions.create


def test_steps_include_all_tools_and_terminal_response(tmp_path, fake_model):
    fake_model.side_effect = [completion([tool(), tool(id="call-2")]), completion()]
    events = []
    measured = ProgramTelemetry("program", on_step=lambda step: events.append(step) if step["step_status"] != "Running" else None)
    result, conversation = agent.run("fix", "alice", workspace=str(tmp_path), measurements=measured)
    summary = measured.summary("Success", 400)
    assert result == "Done"
    assert summary["inference_steps"] == 2
    assert summary["tool_calls"] == 2
    assert len(events[0]["tools"]) == 2
    assert events[1]["step_t_acting_ms"] == 0
    assert events[0]["context_tokens"] == 110
    assert summary["total_prompt_tokens"] == 200
    assert summary["context_tokens"] == 110  # not repeated-context double counting
    assert summary["total_prefill_time_ms"] is None
    assert summary["kv_recomputed_tokens"] is None
    assert summary["total_tool_time_ms"] <= summary["total_tool_wait_time_ms"]
    assert agent.RUNTIME_CONTEXT.get() is None
    assert [m["role"] for m in conversation] == ["system", "user", "assistant", "tool", "tool", "assistant"]
    requests = fake_model.call_args_list
    assert ".1." in requests[0].kwargs["extra_headers"]["X-Request-Id"]
    assert ".2." in requests[1].kwargs["extra_headers"]["X-Request-Id"]


@pytest.mark.parametrize("call", [tool(arguments="bad json"), tool(name="missing"),
    tool(name="read_file", arguments='{"path":"missing.txt"}'),
    tool(name="execute_command", arguments='{"command":"exit 3"}')])
def test_tool_errors_are_measured(tmp_path, fake_model, call):
    fake_model.side_effect = [completion([call]), completion()]
    measured = ProgramTelemetry("p")
    agent.run("fix", "alice", workspace=str(tmp_path), measurements=measured)
    assert measured.steps[0].tools[0]["tool_status"] == "Failed"
    assert measured.summary()["tool_failures"] == 1


def test_limit_is_failure_and_partial_steps_survive(tmp_path, fake_model):
    fake_model.return_value = completion([tool()])
    measured = ProgramTelemetry("p")
    with pytest.raises(StepLimitExceeded):
        agent.run("fix", "alice", workspace=str(tmp_path), measurements=measured, max_steps=2)
    assert len(measured.steps) == 2


def test_timeout_in_tool_is_not_swallowed(tmp_path, fake_model, monkeypatch):
    fake_model.return_value = completion([tool()])
    monkeypatch.setitem(agent.IMPLEMENTATIONS, "calculate_density", Mock(side_effect=TaskTimeout("deadline")))
    measured = ProgramTelemetry("p")
    with pytest.raises(TaskTimeout):
        agent.run("fix", "alice", workspace=str(tmp_path), measurements=measured)
    assert measured.steps[0].step_status == "Timeout"
    assert measured.steps[0].tools[0]["tool_status"] == "Timeout"


def test_inference_failure_has_partial_step(tmp_path, fake_model):
    fake_model.side_effect = RuntimeError("engine offline")
    measured = ProgramTelemetry("p")
    with pytest.raises(RuntimeError):
        agent.run("fix", "alice", workspace=str(tmp_path), measurements=measured)
    assert measured.steps[0].step_status == "Failed"
    assert measured.steps[0].llm_inference_time_ms >= 0


def test_missing_token_usage_is_unknown(tmp_path, fake_model):
    fake_model.return_value = completion(usage=False)
    measured = ProgramTelemetry("p")
    agent.run("fix", "alice", workspace=str(tmp_path), measurements=measured)
    assert measured.summary()["total_prompt_tokens"] is None


def test_coding_task_without_changes_is_rejected(tmp_path, fake_model):
    import subprocess
    subprocess.run(["git", "-C", str(tmp_path), "init"], check=True, capture_output=True)
    (tmp_path / "README").write_text("fixture")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    subprocess.run([
        "git", "-C", str(tmp_path), "-c", "user.name=Test", "-c",
        "user.email=test@example.com", "commit", "-m", "base",
    ], check=True, capture_output=True)
    fake_model.side_effect = [completion(), completion()]
    measured = ProgramTelemetry("p")
    with pytest.raises(RuntimeError, match="without making workspace changes"):
        agent.run("fix", "alice", workspace=str(tmp_path), measurements=measured,
                  require_workspace_changes=True)
    assert len(measured.steps) == 2


def test_request_join_does_not_mix_concurrent_programs():
    r = fakeredis.FakeRedis()
    r.set("telemetry:inference:A", json.dumps({"prefill_time_ms": 12, "kv_recomputed_tokens": 0}))
    r.set("telemetry:inference:B", json.dumps({"prefill_time_ms": 99, "kv_recomputed_tokens": 32}))
    for request, expected in (("A", 12), ("B", 99)):
        with ProgramTelemetry(request).step(1) as record:
            collect_engine_metrics(r, request, record, 0)
            assert record.prefill_time_ms == expected
            assert record.engine_metrics_status == "available"


@pytest.fixture
def backend(monkeypatch, tmp_path):
    r = fakeredis.FakeRedis()
    monkeypatch.setattr(gateway, "r", r)
    monkeypatch.setattr(gateway, "ledger", TelemetryLedger(r))
    monkeypatch.setenv("AGENT_API_KEYS", '{"alice-token":"alice", "bob-token":"bob"}')
    monkeypatch.setenv("AGENT_WORKSPACE_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_WORKSPACE_CONTAINER_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_RESULTS_ROOT", str(tmp_path / "results"))
    return r, TestClient(gateway.app), tmp_path


def submit(backend, **params):
    r, client, path = backend
    headers = {"X-API-Key": "alice-token"}
    session = client.post("/sessions", params={"workspace": str(path)}, headers=headers).json()
    response = client.post(f"/sessions/{session['session_id']}/messages",
                           params={"prompt": "fix", **params}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_gateway_worker_program_export(backend, fake_model):
    r, client, path = backend
    fake_model.side_effect = [completion([tool()]), completion()]
    submitted = submit(backend, benchmark="swe-bench-pro", case_id="case1")
    r.hset(f"task:{submitted['task_id']}", "submitted_at_unix", time.time() - 2)
    worker.process_task(r, submitted["task_id"])
    response = client.get(f"/telemetry/programs/{submitted['program_id']}", headers={"X-API-Key": "alice-token"})
    assert response.status_code == 200
    exported = response.json()
    assert exported["summary"]["task_status"] == "Success"
    assert exported["summary"]["inference_steps"] == 2
    assert exported["summary"]["task_wait_time_ms"] >= 1900
    assert exported["summary"]["task_completion_time_ms"] >= exported["summary"]["task_wait_time_ms"]
    assert exported["summary"]["instance_id"] == "case1"
    assert len(exported["steps"]) == 2
    assert client.get(f"/telemetry/runs/{submitted['run_id']}", headers={"X-API-Key": "bob-token"}).status_code == 404
    assert r.ttl(f"telemetry:run:{submitted['run_id']}") > 0


def test_queue_timeout_does_not_call_model(backend, fake_model):
    r, client, path = backend
    submitted = submit(backend, timeout_seconds=1)
    r.hset(f"task:{submitted['task_id']}", "submitted_at_unix", time.time() - 10)
    worker.process_task(r, submitted["task_id"])
    record = TelemetryLedger(r).run(submitted["run_id"])
    assert record["summary"]["task_status"] == "Timeout"
    assert record["summary"]["inference_steps"] == 0
    fake_model.assert_not_called()


def test_worker_reports_exhaustion_as_failed(backend, fake_model):
    fake_model.return_value = completion([tool()])
    r, client, path = backend
    submitted = submit(backend, max_steps=1)
    worker.process_task(r, submitted["task_id"])
    assert TelemetryLedger(r).run(submitted["run_id"])["summary"]["task_status"] == "Failed"


def test_summary_and_steps_survive_event_trimming(monkeypatch):
    import telemetry_ledger
    monkeypatch.setattr(telemetry_ledger, "MAX_EVENTS_PER_RUN", 2)
    ledger = TelemetryLedger(fakeredis.FakeRedis())
    ledger.start_run("run", **{"user.id": "alice"})
    ledger.save_summary("run", {"inference_steps": 9})
    for index in range(1, 10):
        ledger.emit("step.completed", run_id="run")
        ledger.save_step("run", {"step_index": index})
    assert len(ledger.events("run")) == 2
    assert len(ledger.steps("run")) == 9
    assert ledger.run("run")["summary"]["inference_steps"] == 9


def test_reaper_finalizes_abandoned_worker_without_replaying(backend):
    from task_reaper import expire_tasks
    r, client, path = backend
    submitted = submit(backend, timeout_seconds=1)
    task_id, run_id = submitted["task_id"], submitted["run_id"]
    r.hset(f"task:{task_id}", mapping={"submitted_at_unix": time.time() - 40, "status": "processing"})
    r.zadd("task_deadlines", {task_id: time.time() - 35})
    r.brpoplpush("task_queue", "task_processing", timeout=1)
    ledger = TelemetryLedger(r)
    from dataclasses import asdict
    from program_telemetry import StepRecord
    ledger.save_step(run_id, asdict(StepRecord(task_id, 1)))
    expire_tasks(r)
    summary = ledger.run(run_id)["summary"]
    assert summary["task_status"] == "Timeout"
    assert summary["inference_steps"] == 1
    assert summary["total_llm_inference_time_ms"] is None
    assert summary["total_tool_time_ms"] is None
    assert summary["timing_complete"] is False
    assert r.llen("task_processing") == 0


def test_swe_runner_exports_patch_and_program_metrics(backend, fake_model, monkeypatch, tmp_path):
    import run_swe_bench_pro as benchmark
    r, client, root = backend
    fake_model.side_effect = [completion([tool(name="write_file", arguments='{"path":"new.py","content":"answer = 42\\n"}')]), completion()]

    def prepare(case, workspace):
        workspace.mkdir(parents=True)
        benchmark.git(workspace, "init")
        (workspace / "README").write_text("fixture")
        benchmark.git(workspace, "add", ".")
        benchmark.git(workspace, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base")
        case["base_commit"] = benchmark.git(workspace, "rev-parse", "HEAD").strip()

    class LocalSession:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def request(self, method, url, timeout, **kwargs):
            response = client.request(method, url, headers=self.headers, **kwargs)
            if method == "POST" and "/messages" in url and response.status_code == 200:
                worker.process_task(r, response.json()["task_id"])
            return response

    monkeypatch.setattr(benchmark, "prepare_checkout", prepare)
    monkeypatch.setattr(benchmark.requests, "Session", LocalSession)
    args = NS(workspace_root=tmp_path / "checkouts", output=tmp_path / "exports", gateway_url="http://testserver",
              config="default", max_steps=10, task_timeout=60, poll_grace=5, poll_interval=0.01)
    case = {"instance_id": "case1", "repo": "owner/repo", "base_commit": "a" * 40, "problem_statement": "Fix"}
    record = benchmark.run_case(case, args, "experiment", "pinned-revision", "alice-token")
    assert "runner_error" not in record, record
    assert record["inference_steps"] == 2
    assert record["task_status"] == "Success"
    assert record["resolved"] is None
    assert record["evaluation_status"] == "NotEvaluated"
    assert record["patch_generated"] is True
    assert record["patch_bytes"] > 0
    patch = next(args.output.glob("*/model.patch")).read_text()
    assert "new.py" in patch and "+answer = 42" in patch
