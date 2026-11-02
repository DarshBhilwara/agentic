import json

import fakeredis
from fastapi.testclient import TestClient

import gateway
from telemetry_ledger import TelemetryLedger


def _backend(monkeypatch, tmp_path):
    redis_client = fakeredis.FakeRedis()
    monkeypatch.setattr(gateway, "r", redis_client)
    monkeypatch.setattr(gateway, "ledger", TelemetryLedger(redis_client))
    monkeypatch.setenv("AGENT_API_KEYS", '{"token":"alice"}')
    monkeypatch.setenv("AGENT_WORKSPACE_HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_WORKSPACE_CONTAINER_ROOT", str(tmp_path))
    return redis_client, TestClient(gateway.app)


def _submit(client, **params):
    headers = {"X-API-Key": "token"}
    session = client.post("/sessions", headers=headers).json()
    response = client.post(
        f"/sessions/{session['session_id']}/messages",
        params={"prompt": "return a short answer", **params}, headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_workspace_completion_requirement_defaults_to_coding_only(monkeypatch, tmp_path):
    redis_client, client = _backend(monkeypatch, tmp_path)

    coding = _submit(client, coding_only=True)
    regular = _submit(client)

    assert redis_client.hget(f"task:{coding['task_id']}", "require_workspace_changes") == b"1"
    assert redis_client.hget(f"task:{regular['task_id']}", "require_workspace_changes") == b"0"


def test_runtime_only_override_keeps_coding_tools_enabled(monkeypatch, tmp_path):
    redis_client, client = _backend(monkeypatch, tmp_path)

    submitted = _submit(client, coding_only=True, require_workspace_changes=False)
    task = redis_client.hgetall(f"task:{submitted['task_id']}")

    assert task[b"require_workspace_changes"] == b"0"
    assert task[b"enabled_tools"] == json.dumps(
        ["execute_command", "read_file", "write_file", "list_directory"]
    ).encode()
    assert task[b"require_command_execution"] == b"0"


def test_explicit_workspace_requirement_works_without_coding_tools(monkeypatch, tmp_path):
    redis_client, client = _backend(monkeypatch, tmp_path)

    submitted = _submit(client, require_workspace_changes=True)
    task = redis_client.hgetall(f"task:{submitted['task_id']}")

    assert task[b"require_workspace_changes"] == b"1"
    assert task[b"enabled_tools"] == b""


def test_capabilities_endpoint_requires_authentication(monkeypatch, tmp_path):
    _, client = _backend(monkeypatch, tmp_path)

    assert client.get("/capabilities").status_code == 422
    response = client.get("/capabilities", headers={"X-API-Key": "token"})
    assert response.status_code == 200
    assert response.json() == {"schema_version": 1, "coding_completion_overrides": True}
