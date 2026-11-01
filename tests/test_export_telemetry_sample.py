import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import export_telemetry_sample as inspect


def test_gateway_timeout_auth_and_safe_error(monkeypatch):
    class Response:
        status_code = 503

        def raise_for_status(self):
            raise requests.HTTPError("https://private.invalid?token=secret", response=self)

    session = Mock()
    session.headers = {}
    session.request.return_value = Response()
    monkeypatch.setattr(requests, "Session", lambda: session)
    api = inspect.Gateway("http://gateway", "api-secret", 4)
    with pytest.raises(requests.HTTPError) as caught:
        api.request("GET", "/console/config")
    assert session.headers["X-API-Key"] == "api-secret"
    assert session.request.call_args.kwargs["timeout"] == 4
    safe = inspect.safe_error(caught.value)
    assert safe == {"type": "HTTPError", "http_status": 503}
    assert "secret" not in json.dumps(safe)
    assert "private.invalid" not in json.dumps(safe)
    assert session.request.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("url", ["ftp://gateway", "http://user:pass@gateway", "http://gateway/?token=x"])
def test_gateway_rejects_unsafe_base_urls(url):
    with pytest.raises(ValueError, match="service URL"):
        inspect.Gateway(url, "token", 3)


def _kernel_record(index, *, event_type="entry", step=None, tool=None, call_id=None):
    return {"collector_id": "collector-1", "node": "agent-01", "type": event_type,
            "run_id": "run-1", "program_id": "program-1", "step_index": step,
            "tool_name": tool, "tool_call_id": call_id, "pid": 11, "tid": 11,
            "syscall_id": 257 if tool else index, "entry_monotonic_ns": 1000 if tool else index,
            "timestamp": f"time-{index}",
            "args": [0, 0, 0, 0, 0, 0], "ret": 0 if event_type != "entry" else None,
            "duration_ns": 20 if event_type == "exit" else None}


class ExportApi:
    def __init__(self, status="completed"):
        self.calls = []
        self.records = [_kernel_record(i) for i in range(499)]
        self.records.extend([
            _kernel_record(499, step=1, tool="write_file", call_id="call-write"),
            _kernel_record(500, event_type="exit", step=1, tool="write_file", call_id="call-write"),
            _kernel_record(501, event_type="unmatched_exit", step=1,
                           tool="execute_command", call_id="call-exec"),
        ])
        self.status = status

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET" and path == "/telemetry/runs/run-1":
            return {"run": {"id": "run-1", "program_id": "program-1", "status": self.status,
                            "workspace.id": "DO_NOT_EXPORT_WORKSPACE"},
                    "summary": {"program_id": "program-1", "task_status": self.status},
                    "steps": [{"program_id": "program-1", "step_index": 1, "request_id": "request-1",
                               "step_status": "Success", "prompt": "DO_NOT_EXPORT_PROMPT",
                               "tools": [{"tool_name": "write_file", "tool_call_id": "call-write",
                                          "tool_index": 1, "tool_status": "Success",
                                          "arguments": "DO_NOT_EXPORT_ARGUMENTS"}]}],
                    "events": [{"id": "event-1", "type": "tool.started", "timestamp": "now",
                                "run.id": "run-1", "program_id": "program-1",
                                "payload": {"prompt": "DO_NOT_EXPORT_PROMPT"},
                                "attributes": {"step_index": 1, "tool_name": "write_file",
                                               "tool_call_id": "call-write", "tool.arguments": "DO_NOT_EXPORT_ARGUMENTS"}}],
                    "procedural_graph": {"nodes": ["model:1"], "edges": []}}
        if method == "POST" and path == "/console/reports":
            return {"runs": [{"run": {"id": "run-1", "status": self.status},
                              "summary": {"task_status": self.status},
                              "steps": [], "events": [], "procedural_graph": {"nodes": [], "edges": []},
                              "kernel": {"records": [], "status": {}}}]}
        if method == "GET" and path.endswith("/kernel"):
            offset, limit = kwargs["params"]["offset"], kwargs["params"]["limit"]
            return {"records": self.records[offset:offset + limit], "total": 502, "retained": 502,
                    "truncated": False, "status": {"events": 502}, "node_health": {"node": "agent-01"}}
        raise AssertionError((method, path))


def test_export_terminal_run_pages_kernel_and_correlates_across_page_boundary(tmp_path):
    api = ExportApi()
    output = tmp_path / "fresh-export"
    result = inspect.export_run(api, "run-1", output)
    lines = (output / "kernel.jsonl").read_text().splitlines()
    correlation = json.loads((output / "correlation.json").read_text())
    assert result["exported_records"] == 502
    assert len(lines) == 502
    assert [call[1] for call in api.calls if call[0] == "POST"] == ["/console/reports"]
    assert any(call[2].get("params", {}).get("offset") == 500 for call in api.calls if call[0] == "GET")
    details = correlation["correlation"]
    assert details["selected_run_id"] == "run-1"
    assert details["selected_program_id"] == "program-1"
    # The real report serializer omits program_id; the owned run supplies it.
    assert details["run"]["program_id"] == "program-1"
    assert details["scope_counts"]["tool:1:write_file:call-write"] == 2
    assert details["matched_pair_count"] == 1
    assert details["representative_pairs"][0]["tool_call_id"] == "call-write"
    assert details["representative_pairs"][0]["args"] == [0, 0, 0, 0, 0, 0]
    assert details["representative_pairs"][0]["ret"] == 0
    assert details["representative_pairs"][0]["entry_timestamp"] == "time-499"
    assert details["representative_pairs"][0]["exit_timestamp"] == "time-500"
    assert details["unmatched_exits"][0]["tool_call_id"] == "call-exec"
    assert details["kernel_tool_call_ids"] == ["call-exec", "call-write"]
    assert details["step_tool_call_ids"] == ["call-write"]
    assert details["identity_inventory"]["steps"][0]["tools"][0]["call_id"] == "call-write"
    assert details["identity_inventory"]["semantic_events"][0]["tool_call_id"] == "call-write"
    serialized = (output / "correlation.json").read_text()
    assert "DO_NOT_EXPORT_PROMPT" not in serialized
    assert "DO_NOT_EXPORT_ARGUMENTS" not in serialized
    assert "DO_NOT_EXPORT_WORKSPACE" not in serialized
    assert correlation["kernel"]["initial_retained"] == correlation["kernel"]["final_retained"] == 502
    assert "queue_drops" not in correlation["kernel"]["initial_status"]
    assert "prompt" not in (output / "report.json").read_text()


def test_export_rejects_nonterminal_run_before_privacy_report(tmp_path):
    api = ExportApi(status="running")
    with pytest.raises(ValueError, match="must be terminal"):
        inspect.export_run(api, "run-1", tmp_path / "export")
    assert not any(method == "POST" for method, _, _ in api.calls)


def test_export_preserves_retention_trimming_and_warns_if_total_changes(tmp_path):
    api = ExportApi()
    request = api.request

    def retained_request(method, path, **kwargs):
        result = request(method, path, **kwargs)
        if path.endswith("/kernel"):
            result["total"] = 2955 if kwargs["params"]["limit"] == 1 else 2954
            result["truncated"] = True
        return result

    api.request = retained_request
    output = tmp_path / "retained-export"
    inspect.export_run(api, "run-1", output)
    data = json.loads((output / "correlation.json").read_text())
    assert data["kernel"]["initial_trimmed_records"] == 2452
    assert data["kernel"]["initial_truncated"] is True
    assert any("total count changed" in warning for warning in data["kernel"]["warnings"])
    assert any("retention" in warning for warning in data["kernel"]["warnings"])


def test_syscall_pairs_require_complete_keys_and_warn_on_unpaired_exit():
    records = [
        {"type": "entry", "collector_id": None, "pid": 1, "tid": 1,
         "entry_monotonic_ns": 5, "syscall_id": 2},
        {"type": "exit", "collector_id": None, "pid": 1, "tid": 1,
         "entry_monotonic_ns": 5, "syscall_id": 2},
        {"type": "exit", "collector_id": "c", "pid": 3, "tid": 3,
         "entry_monotonic_ns": 6, "syscall_id": 3},
    ]
    result = inspect.correlate(records, "run", "program")
    assert result["matched_pair_count"] == 0
    assert any("lack pairing identity" in warning for warning in result["warnings"])
    assert any("EXIT records" in warning for warning in result["warnings"])


def test_pair_retains_exit_scope_mismatch_as_warning():
    entry = _kernel_record(1, step=1, tool="write_file", call_id="call-1")
    exit_record = _kernel_record(2, event_type="exit", step=2, tool="execute_command", call_id="call-2")
    result = inspect.correlate([entry, exit_record], "run-1", "program-1")
    assert result["matched_pair_count"] == 1
    assert result["representative_pairs"][0]["scope_mismatch"] is True
    assert any("scope differs" in warning for warning in result["warnings"])


