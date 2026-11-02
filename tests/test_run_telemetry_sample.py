import json
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
import subprocess
import pytest

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_telemetry_sample as sample


def test_ambiguous_submit_is_not_retried_and_session_identity_is_saved(tmp_path, monkeypatch):
    fake_script = tmp_path / "repo" / "scripts" / "run_telemetry_sample.py"
    fake_script.parent.mkdir(parents=True)
    fake_script.touch()
    monkeypatch.setattr(sample, "__file__", str(fake_script))

    class Api:
        def __init__(self, *args):
            self.messages = 0
            self.message_params = None

        def request(self, method, path, **kwargs):
            if path == "/sessions":
                return {"session_id": "session-x", "agent_id": "agent-x"}
            if path == "/sessions/session-x/messages":
                self.messages += 1
                self.message_params = kwargs.get("params", {})
                raise requests.Timeout("private prompt must not be reported")
            raise AssertionError(path)

    api = Api()
    monkeypatch.setattr(sample, "Gateway", lambda *args: api)
    args = Namespace(timeout=10, max_steps=20, output=tmp_path / "repo" / "benchmark-results" / "sample",
                     workspace_root=tmp_path / "workspaces", gateway="http://gateway",
                     token_file=tmp_path / "token")
    with patch.object(sample, "read_token", return_value="secret"):
        assert sample.run(args) == 2
    failure = json.loads((args.output / "failure.json").read_text())
    assert failure["session_id"] == "session-x"
    assert failure["task_status"] == "unknown"
    assert "private prompt" not in json.dumps(failure)
    assert api.messages == 1
    assert api.message_params["require_command_execution"] == "true"


def test_react_steps_join_request_and_call_ids_to_real_step_inventory(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    report = {"runs": [{"summary": {"inference_steps": 2, "total_prompt_tokens": 50},
        "steps": [{"step_index": 1, "step_time_ms": 10,
                   "llm_inference_time_ms": 8, "engine_metrics_status": "available",
                   "time_to_first_token_ms": 3, "prefix_cached_tokens": 16,
                   "kv_measurement": "partial", "model_forward_time_ms": 2,
                   "kv_gpu_exclusive_peak_blocks": 4, "kv_swap_out_exclusive_blocks": 2,
                   "tools": [{"tool_index": 1, "tool_name": "execute_command",
                              "tool_ttf_ms": 3, "tool_latency_ms": 4}]}]}]}
    correlation = {"correlation": {"scope_counts": {"step:1": 12},
        "identity_inventory": {"summary": {"tool_calls": 1}, "steps": [
            {"step_index": 1, "request_id": "req-1", "step_status": "Success",
             "tools": [{"name": "execute_command", "call_id": "call-1", "index": 1, "status": "Success"}]},
            {"step_index": 2, "request_id": "req-2", "step_status": "Success", "tools": []}]}}}
    (evidence / "report.json").write_text(json.dumps(report))
    (evidence / "correlation.json").write_text(json.dumps(correlation))
    steps, stats = sample._make_react_steps(evidence)
    assert steps[0]["request_id"] == "req-1"
    assert steps[0]["tools"][0]["call_id"] == "call-1"
    assert steps[0]["kernel_scope_count"] == 12
    assert steps[0]["tools"][0]["tool_latency_ms"] == 4
    assert steps[0]["tools"][0]["tool_ttf_ms"] == 3
    assert steps[0]["time_to_first_token_ms"] == 3
    assert steps[0]["prefix_cached_tokens"] == 16
    assert steps[0]["model_forward_time_ms"] == 2
    assert steps[0]["kv_measurement"] == "partial"
    assert steps[0]["kv_gpu_exclusive_peak_blocks"] == 4
    assert steps[0]["kv_swap_out_exclusive_blocks"] == 2
    assert stats == {"inference_steps": 2, "total_prompt_tokens": 50}
    assert not sample.sample_is_valid(steps, True)
    valid_steps = steps + [
        {"step_index": 3, "request_id": "req-3", "step_status": "Success",
         "tools": [{"name": "execute_command", "call_id": "call-2"}]},
        {"step_index": 4, "request_id": "req-4", "step_status": "Success", "tools": []}]
    assert sample.sample_is_valid(valid_steps, True)


def test_narrative_only_one_model_step_is_not_a_react_sample():
    assert not sample.sample_is_valid([{"step_index": 1, "request_id": "r1",
                                        "step_status": "Success", "tools": []}], True)
    assert not sample.sample_is_valid([
        {"step_index": 1, "request_id": "r1", "step_status": "Success",
         "tools": [{"name": "write_file"}]},
        {"step_index": 2, "request_id": "r2", "step_status": "Success", "tools": []}], True)
    assert not sample.sample_is_valid([
        {"step_index": 1, "request_id": "r1", "step_status": "Success",
         "tools": [{"name": "execute_command"}]},
        {"step_index": 2, "request_id": "r2", "step_status": "Failed",
         "tools": [{"name": "execute_command"}]}], True)


def test_timeout_skips_host_verification():
    with patch.object(sample, "_host_verify") as verify:
        result = sample._verification_for("Timeout", Path("unused"))
    verify.assert_not_called()
    assert result == {"passed": False, "skipped_reason": "runtime_task_not_successful", "commands": []}


@pytest.mark.parametrize("report", [None, "not json", '{"average":0}'])
def test_missing_or_invalid_agent_report_has_explicit_failure(tmp_path, report):
    (tmp_path / "analyze.py").write_text(sample.SEED_ANALYZER.replace("total // len(rows)", "total / len(rows)"))
    (tmp_path / "sales.csv").write_text(sample.DATA)
    # Agent-owned tests must not determine the independent grading outcome.
    (tmp_path / "test_analyze.py").write_text("raise RuntimeError('modified tests')")
    if report is not None:
        (tmp_path / "report.json").write_text(report)
    result = sample._host_verify(tmp_path)
    assert not result["passed"]
    assert "agent_report_valid" in result["failure_reasons"]
    assert next(check for check in result["commands"] if check["name"] == "unit_tests")["passed"]
    assert ("agent_report_exists" in result["failure_reasons"]) == (report is None)


def test_independent_grading_uses_original_tests_and_preserves_workspace(tmp_path):
    (tmp_path / "analyze.py").write_text(sample.SEED_ANALYZER.replace("total // len(rows)", "total / len(rows)"))
    (tmp_path / "sales.csv").write_text(sample.DATA)
    (tmp_path / "test_analyze.py").write_text("raise RuntimeError('modified tests')")
    report = '{"count":2,"total":150,"average":75,"totals_by_region":{"North":150}}'
    (tmp_path / "report.json").write_text(report)
    result = sample._host_verify(tmp_path)
    assert result["passed"]
    assert result["failure_reasons"] == []
    assert (tmp_path / "report.json").read_text() == report
    assert (tmp_path / "test_analyze.py").read_text() == "raise RuntimeError('modified tests')"


@pytest.mark.parametrize("mutate_report", [False, True])
def test_host_verification_checks_agent_report_unchanged(tmp_path, monkeypatch, mutate_report):
    report_path = tmp_path / "report.json"
    original = '{"count":2,"total":150,"average":75,"totals_by_region":{"North":150}}\n'
    report_path.write_text(original)

    def fake_run(argv, cwd, timeout):
        if mutate_report:
            (Path(cwd) / "report.json").write_text('{"changed":true}')
        if "--output" in argv:
            Path(argv[argv.index("--output") + 1]).write_text(
                '{"count":2,"total":150,"average":75,"totals_by_region":{"North":150}}')
        stdout = '{"count":3,"total":3.25,"average":1.0833333333333333,"totals_by_region":{"A":1.25,"B":2.0}}'
        if argv[-1].endswith("empty_input.csv"):
            stdout = '{"count":0,"total":0,"average":null,"totals_by_region":{}}'
        failed = argv[-1].endswith("invalid_amount.csv") or argv[-1].endswith("missing_amount.csv")
        return subprocess.CompletedProcess(argv, 1 if failed else 0, stdout, ""), False

    monkeypatch.setattr(sample, "_run_process_group", fake_run)
    verification = sample._host_verify(tmp_path)
    assert verification["passed"] is (not mutate_report)
    assert (report_path.read_text() == original) is (not mutate_report)
    assert not list(tmp_path.glob("verify_*.csv"))


def test_timed_out_host_command_kills_and_reaps_its_process_group(monkeypatch):
    class ChildProcess:
        pid = 4321
        returncode = None

        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("command", timeout)
            self.returncode = -9
            return "", ""

    child = ChildProcess()
    killed = []

    def popen(*args, **kwargs):
        child.kwargs = kwargs
        return child

    monkeypatch.setattr(sample.subprocess, "Popen", popen)
    monkeypatch.setattr(sample.os, "killpg", lambda pid, signum: killed.append((pid, signum)))
    result, timed_out = sample._run_process_group(["python"], Path("."), 1)
    assert timed_out
    assert result.returncode == -9
    assert child.calls == 2
    assert child.kwargs["start_new_session"] is True
    assert killed == [(4321, sample.signal.SIGKILL)]
