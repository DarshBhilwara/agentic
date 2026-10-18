import json
import os
import struct
from collections import OrderedDict
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from kernel_telemetry import KernelTelemetry, KernelTelemetryError
from kernel import collector
from kernel.collector import pod_matches_cgroup, scan_inference_cgroups
from program_telemetry import ProgramTelemetry
import agent


def test_nested_scope_updates_are_acknowledged_and_restored(monkeypatch, tmp_path):
    path = str(tmp_path / "collector.sock")
    seen = []
    class FakeSocket:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def settimeout(self, _timeout): pass
        def connect(self, _path): pass
        def sendall(self, payload):
            command = json.loads(payload)
            seen.append(command)
            answer = {"ok": True, "cgroup_id": 77} if command["op"] == "register" else {"ok": True}
            self.response = (json.dumps(answer) + "\n").encode()
        def recv(self, _size):
            response, self.response = self.response, b""
            return response

    monkeypatch.setattr("kernel_telemetry.socket.socket", lambda *_args: FakeSocket())
    telemetry = KernelTelemetry(path, required=True)
    with telemetry.scope(run_id="run-1", program_id="program-1"):
        with telemetry.scope(step_index=2):
            pass
    with telemetry.scope(run_id="run-2", program_id="program-2"):
        pass
    assert [command["op"] for command in seen] == [
        "recover", "register", "scope", "scope", "scope",
        "recover", "register", "scope",
    ]
    assert seen[1]["scope"]["run_id"] == "run-1"
    assert seen[2]["scope"]["step_index"] == "2"
    assert seen[3]["scope"]["program_id"] == "program-1"
    assert seen[4]["scope"] == {}
    assert seen[6]["scope"]["run_id"] == "run-2"


def test_required_collector_failure_is_fatal(monkeypatch, tmp_path):
    class MissingSocket:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def settimeout(self, _timeout): pass
        def connect(self, _path): raise FileNotFoundError("no collector")
    monkeypatch.setattr("kernel_telemetry.socket.socket", lambda *_args: MissingSocket())
    client = KernelTelemetry(str(tmp_path / "missing.sock"), required=True)
    with pytest.raises(KernelTelemetryError, match="collector unavailable"):
        client._send({"op": "ping"})


def test_disabled_kernel_client_does_not_touch_socket(monkeypatch):
    def fail_socket(*_args):
        raise AssertionError("disabled telemetry must not open a socket")
    monkeypatch.setattr("kernel_telemetry.socket.socket", fail_socket)
    client = KernelTelemetry(required=False)
    with client.scope(run_id="r1", program_id="p1"):
        pass
    assert client._cgroup is None


def test_program_steps_use_kernel_scopes():
    seen = []
    class Recorder:
        @contextmanager
        def scope(self, **fields):
            seen.append(fields)
            yield
    with ProgramTelemetry("p1", kernel=Recorder()).step(3):
        pass
    assert seen == [{"program_id": "p1", "step_index": 3}]


def test_tool_execution_adds_call_scope(tmp_path):
    seen = []
    class Recorder:
        @contextmanager
        def scope(self, **fields):
            seen.append(fields)
            yield
    record = SimpleNamespace(program_id="p1", step_index=4, kernel=Recorder(), tools=[])
    call = {"id": "call-9", "name": "calculate_density", "arguments": '{"mass": 4, "volume": 2}'}
    result = agent._execute_tool(call, str(tmp_path), record, agent.TOOLS)
    assert "2" in result
    assert seen == [{"program_id": "p1", "step_index": 4,
                     "tool_name": "calculate_density", "tool_index": 1,
                     "tool_call_id": "call-9"}]


def test_inference_discovery_matches_only_enabled_pod_cgroups(tmp_path):
    uid = "7a1a2b3c-44dd-55ee-8899-aabbccddeeff"
    cgroup_path = f"/kubepods.slice/kubepods-burstable-pod{uid.replace('-', '_')}.slice/cri-containerd-abc.scope"
    proc = tmp_path / "proc" / "123"
    proc.mkdir(parents=True)
    (proc / "cgroup").write_text(f"0::/{cgroup_path.lstrip('/')}\n")
    cgroup_root = tmp_path / "cgroups"
    leaf = cgroup_root / cgroup_path.lstrip("/")
    leaf.mkdir(parents=True)
    pod = {"metadata": {"uid": uid, "name": "vllm-0", "namespace": "agentic-ai",
                        "labels": {"telemetry.agentic.io/syscalls": "enabled", "app": "vllm"}},
           "status": {"phase": "Running", "containerStatuses": []}}
    assert pod_matches_cgroup(cgroup_path, pod)
    found = scan_inference_cgroups([pod], str(tmp_path / "proc"), str(cgroup_root))
    assert leaf.stat().st_ino in found
    assert found[leaf.stat().st_ino]["scope_kind"] == "inference"
    assert found[leaf.stat().st_ino].get("run_id") is None
    pod["metadata"]["labels"]["telemetry.agentic.io/syscalls"] = "disabled"
    assert scan_inference_cgroups([pod], str(tmp_path / "proc"), str(cgroup_root)) == {}


def test_kernel_endpoint_checks_run_ownership_and_reports_retention(monkeypatch):
    import gateway
    from fastapi import HTTPException

    class FakeLedger:
        def run(self, run_id):
            return {"user.id": "alice"} if run_id == "owned" else None

    class FakeRedis:
        def lrange(self, key, start, end):
            return [b'{"type":"entry"}']

        def hgetall(self, key):
            return {b"events": b"10005", b"perf_lost": b"2", b"node": b"worker-a"}

        def llen(self, key):
            return 10000

    monkeypatch.setattr(gateway, "ledger", FakeLedger())
    monkeypatch.setattr(gateway, "r", FakeRedis())
    response = gateway.get_kernel_telemetry("owned", offset=0, limit=1, user="alice")
    assert response["records"] == [{"type": "entry"}]
    assert response["total"] == 10005
    assert response["truncated"] is True
    with pytest.raises(HTTPException) as error:
        gateway.get_kernel_telemetry("owned", user="bob")
    assert error.value.status_code == 404


def test_empty_kernel_stream_has_explicit_empty_health(monkeypatch):
    import gateway

    class EmptyRedis:
        def lrange(self, *_args): return []
        def hgetall(self, *_args): return {}
        def llen(self, *_args): return 0
    class OwnedLedger:
        def run(self, _run_id): return {"user.id": "alice"}

    monkeypatch.setattr(gateway, "ledger", OwnedLedger())
    monkeypatch.setattr(gateway, "r", EmptyRedis())
    response = gateway.get_kernel_telemetry("owned", user="alice")
    assert response["records"] == []
    assert response["status"] == {}
    assert response["node_health"] == {}
    assert response["truncated"] is False


def test_syscall_exit_carries_entry_timestamp_and_scope(monkeypatch):
    monkeypatch.setattr(collector, "IDENTITIES", {9: ({"run_id": "run-1", "program_id": "p1"}, 0)})
    base = {"kind": 0, "scope": 9, "ts": 100, "duration": 0, "id": 257,
            "cgroup": 41, "args": [1, 2, 3, 4, 5, 6], "ret": 0,
            "pid": 3, "tid": 4, "uid": 1000, "comm": b"python\0"}
    entry = collector.event_record(SimpleNamespace(**base), 1000)
    exit_event = collector.event_record(SimpleNamespace(**{**base, "kind": 1, "ts": 175, "duration": 75, "ret": 5}), 1000)
    assert entry["entry_monotonic_ns"] == exit_event["entry_monotonic_ns"] == 100
    assert exit_event["run_id"] == "run-1"
    assert exit_event["ret"] == 5
    assert exit_event["collector_id"] == entry["collector_id"]
    unmatched = collector.event_record(SimpleNamespace(**{**base, "kind": 2, "ts": 200, "duration": 0}), 1000)
    assert unmatched["args"] is None
    assert unmatched["duration_ns"] is None


def test_scope_server_rejects_overlap_and_mismatched_cgroup(monkeypatch):
    class FakeConnection:
        def __init__(self, command):
            self.request = (json.dumps(command) + "\n").encode()
            self.response = b""
            self.fail_ack = False
        def settimeout(self, _timeout): pass
        def getsockopt(self, *_args): return struct.pack("3i", os.getpid(), os.getuid(), os.getgid())
        def recv(self, _size):
            request, self.request = self.request, b""
            return request
        def sendall(self, response):
            if self.fail_ack: raise BrokenPipeError("peer closed")
            self.response = response
        def close(self): pass

    class Map(dict):
        def __setitem__(self, key, value):
            assert int(value.value) in collector.IDENTITIES
            super().__setitem__(int(key.value), int(value.value))
        def pop(self, key, default=None): return super().pop(int(key.value), default)

    monkeypatch.setattr(collector, "cgroup_for_pid", lambda _pid: 77)
    monkeypatch.setattr(collector, "ACTIVE_CGROUPS", {77: 10})
    pid = os.getpid()
    monkeypatch.setattr(collector, "OWNERS", {77: (pid, collector.process_start_time(pid))})
    monkeypatch.setattr(collector, "IDENTITIES", OrderedDict())
    bpf = {"active": Map()}
    overlap = FakeConnection({"op": "register", "scope": {"scope_id": 11, "program_id": "new"}})
    collector.serve(overlap, bpf)
    assert json.loads(overlap.response)["ok"] is False
    mismatch = FakeConnection({"op": "scope", "cgroup_id": 78, "scope": {"scope_id": 12}})
    collector.serve(mismatch, bpf)
    assert json.loads(mismatch.response)["ok"] is False
    assert bpf["active"] == {}
    monkeypatch.setattr(collector, "ACTIVE_CGROUPS", {})
    monkeypatch.setattr(collector, "OWNERS", {})
    first = FakeConnection({"op": "register", "scope": {"scope_id": 21, "program_id": "p1"}})
    first.fail_ack = True  # collector state changes before this acknowledgement is lost
    collector.serve(first, bpf)
    assert bpf["active"][77] == 21
    recover = FakeConnection({"op": "recover"})
    collector.serve(recover, bpf)
    assert json.loads(recover.response)["ok"] is True
    assert bpf["active"] == {}
    second = FakeConnection({"op": "register", "scope": {"scope_id": 22, "program_id": "p2"}})
    collector.serve(second, bpf)
    assert json.loads(second.response)["ok"] is True
    assert bpf["active"][77] == 22
    clear = FakeConnection({"op": "scope", "cgroup_id": 77, "scope": {}})
    clear.fail_ack = True
    collector.serve(clear, bpf)
    assert bpf["active"] == {}
    recovered_clear = FakeConnection({"op": "recover"})
    collector.serve(recovered_clear, bpf)
    assert json.loads(recovered_clear.response)["ok"] is True

    broken_ping = FakeConnection({"op": "ping"})
    broken_ping.fail_ack = True
    collector.serve(broken_ping, bpf)  # broken ACK transport must not escape serve


def test_stale_worker_cgroup_filter_is_removed(monkeypatch):
    class ActiveMap(dict):
        def pop(self, key, default=None): return super().pop(int(key.value), default)

    bpf = {"active": ActiveMap({88: 31})}
    monkeypatch.setattr(collector, "ACTIVE_CGROUPS", {88: 31})
    monkeypatch.setattr(collector, "OWNERS", {88: (123, "old")})
    monkeypatch.setattr(collector, "IDENTITIES", {31: ({"scope_id": 31}, 0)})
    monkeypatch.setattr(collector, "cgroup_for_pid", lambda _pid: 99)
    collector.prune_worker_scopes(bpf)
    assert bpf["active"] == {}
    assert collector.ACTIVE_CGROUPS == {}
    assert collector.OWNERS == {}
    assert collector.IDENTITY_LOCK and 31 in collector.IDENTITIES


def test_redis_export_is_bounded_and_archive_rotation_is_bounded(monkeypatch, tmp_path):
    class Pipeline:
        def __init__(self): self.calls = []
        def __getattr__(self, name):
                return lambda *args, **kwargs: self.calls.append((name, *args, kwargs))
        def execute(self): return self.calls
    class FakeRedis:
        def pipeline(self): self.pipe = Pipeline(); return self.pipe

    client = FakeRedis()
    collector.export_run_event(client, {"run_id": "r1", "type": "entry"})
    assert any(call[:4] == ("ltrim", "kernel:run:r1", -10000, -1) for call in client.pipe.calls)
    assert any(call[0] == "hincrby" for call in client.pipe.calls)
    collector.mark_run_active(client, "r1")
    assert any(call[0] == "hsetnx" and call[1:3] == ("kernel:run:r1:status", "events")
               for call in client.pipe.calls)
    monkeypatch.setattr(collector, "MAX_BYTES", 1)
    monkeypatch.setattr(collector, "MAX_FILES", 3)
    archive = tmp_path / "node.jsonl"
    archive.write_text("current")
    (tmp_path / "node.jsonl.1").write_text("one")
    (tmp_path / "node.jsonl.2").write_text("two")
    (tmp_path / "node.jsonl.3").write_text("three")
    collector.rotate_archive(str(archive))
    assert archive.with_name("node.jsonl.1").read_text() == "current"
    assert archive.with_name("node.jsonl.2").read_text() == "one"
    assert archive.with_name("node.jsonl.3").read_text() == "two"
