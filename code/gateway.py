import json
import os
import re
import time
import redis, uuid
from datetime import datetime, timezone
from fastapi import FastAPI, Depends, HTTPException, Header
from telemetry import inject_context, redact_text, safe_content, safe_payload, span, task_counter
from telemetry_ledger import TelemetryLedger
from program_telemetry import ProgramTelemetry

app = FastAPI()
r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis-service:6379/0"))
ledger = TelemetryLedger(r)


def _api_keys():
    """Return the configured token -> user mapping.

    AGENT_API_KEYS is a JSON object, for example:
    {"alice-token": "alice", "bob-token": "bob"}
    """
    configured = os.getenv("AGENT_API_KEYS")
    if configured:
        try:
            mapping = json.loads(configured)
        except json.JSONDecodeError as exc:
            raise RuntimeError("AGENT_API_KEYS must be valid JSON") from exc
        if not isinstance(mapping, dict):
            raise RuntimeError("AGENT_API_KEYS must be a JSON object")
        return mapping
    # Backwards-compatible single-user mode for existing deployments.
    return {os.getenv("AGENT_API_KEY", "corp-secret-token-123"): "emp-iiitd"}


def _valid_user(user):
    return isinstance(user, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", user)

def verify_key(x_api_key: str = Header(...)):
    user = _api_keys().get(x_api_key)
    if _valid_user(user):
        return user
    raise HTTPException(status_code=401, detail="Invalid Enterprise Token")

def _now():
    return datetime.now(timezone.utc).isoformat()


def _workspace(path):
    requested = os.path.realpath(path or os.path.join("/workspace", "users"))
    host_root = os.path.realpath(
        os.getenv("AGENT_WORKSPACE_HOST_ROOT", "/home/agentic/agentic")
    )
    container_root = os.path.realpath(os.getenv("AGENT_WORKSPACE_CONTAINER_ROOT", "/agent-workspaces"))
    if host_root and requested == host_root:
        return container_root
    if host_root and requested.startswith(host_root + os.sep):
        return os.path.join(container_root, os.path.relpath(requested, host_root))
    if requested.startswith("/workspace/users/"):
        return requested
    raise HTTPException(400, detail="workspace must be inside the configured shared project root")


def _session(session_id, user):
    session = r.hgetall(f"session:{session_id}")
    if not session or session.get(b"user", b"").decode() != user:
        raise HTTPException(404, detail="Session not found")
    return {key.decode(): value.decode() for key, value in session.items()}


@app.post("/sessions")
def create_session(workspace: str = "", user: str = Depends(verify_key)):
    session_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    workspace = _workspace(workspace or os.path.join("/workspace/users", user))
    r.hset(f"session:{session_id}", mapping={
        "id": session_id, "user": user, "agent_id": agent_id,
        "workspace": workspace, "created_at": _now(), "turn": 0,
    })
    return {"session_id": session_id, "agent_id": agent_id}


@app.post("/sessions/{session_id}/messages")
def submit_message(session_id: str, prompt: str, benchmark: str = "",
                   case_id: str = "", max_steps: int = 100, timeout_seconds: int = 1800,
                   coding_only: bool = False, user: str = Depends(verify_key)):
    session = _session(session_id, user)
    if not 1 <= max_steps <= 1000 or not 1 <= timeout_seconds <= 86400:
        raise HTTPException(400, detail="max_steps must be 1..1000 and timeout_seconds 1..86400")
    task_counter.add(1, {"component": "gateway", "operation": "submit"})
    with span("agent.task.submit", user=user) as s:
        task_id = str(uuid.uuid4())
        turn_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        agent_run_id = str(uuid.uuid4())
        submitted_at = time.time()
        turn_number = r.hincrby(f"session:{session_id}", "turn", 1)
        r.hset(f"task:{task_id}", mapping={
            "id": task_id, "user": user, "prompt": prompt, "status": "pending",
            "program_id": task_id, "task_status": "Queued", "submitted_at_unix": submitted_at,
            "max_steps": max_steps, "timeout_seconds": timeout_seconds,
            "enabled_tools": json.dumps(["execute_command", "read_file", "write_file", "list_directory"]) if coding_only else "",
            "session_id": session_id, "agent_id": session["agent_id"],
            "workspace": session["workspace"],
            "benchmark": benchmark, "case_id": case_id,
            "turn_id": turn_id, "turn_number": turn_number,
            "run_id": run_id, "root_run_id": run_id, "agent_run_id": agent_run_id,
            "agent_name": "enterprise-agent", "parent_agent_run_id": "",
            "created_at": _now(), "trace_context": json.dumps(inject_context()),
        })
        ledger.start_run(run_id, **{
            "user.id": user, "session.id": session_id, "task.id": task_id,
            "agent.id": session["agent_id"], "agent.run.id": agent_run_id,
            "root.run.id": run_id, "workspace.id": session["workspace"],
            "benchmark.name": benchmark, "benchmark.case.id": case_id,
            "program_id": task_id,
        })
        ledger.save_summary(run_id, ProgramTelemetry(task_id).summary("Queued"))
        ledger.emit("task.queued", run_id=run_id, **{
            "user.id": user, "session.id": session_id, "task.id": task_id,
            "agent.id": session["agent_id"], "agent.run.id": agent_run_id,
        }, payload={"prompt": safe_content(prompt)})
        pipe = r.pipeline()
        pipe.zadd("task_deadlines", {task_id: submitted_at + timeout_seconds})
        pipe.lpush("task_queue", task_id)
        pipe.execute()
        s.set_attribute("task.id", task_id)
        s.set_attribute("agent.session.id", session_id)
        return {"task_id": task_id, "program_id": task_id, "run_id": run_id, "status": "queued", "turn_id": turn_id}


@app.post("/tasks")
def submit_task(prompt: str, user: str = Depends(verify_key)):
    """Compatibility endpoint for non-interactive clients."""
    session = create_session(user=user)
    return submit_message(session["session_id"], prompt, user=user)

@app.get("/tasks/{task_id}")
def get_task(task_id: str, user: str = Depends(verify_key)):
    with span("agent.task.get", task_id=task_id, user=user):
        task = r.hgetall(f"task:{task_id}")
    if not task: raise HTTPException(404)
    task_dict = {k.decode(): v.decode() for k, v in task.items()}
    if task_dict.get("user") != user: raise HTTPException(403)
    return task_dict
    
@app.get("/tasks")
def list_tasks(user: str = Depends(verify_key)):
    keys = r.keys("task:*")
    tasks = []
    for k in keys:
        task = r.hgetall(k)
        if task.get(b'user', b'').decode() == user:
            tasks.append({
                "id": task.get(b'id', b'').decode(), 
                "status": task.get(b'status', b'').decode()
            })
    return tasks


@app.get("/telemetry/runs")
def list_telemetry_runs(session_id: str = "", task_id: str = "", agent_id: str = "", limit: int = 100,
                        user: str = Depends(verify_key)):
    return {"runs": ledger.list_runs(user=user, session_id=session_id or None, task_id=task_id or None,
                                      agent_id=agent_id or None, limit=limit)}


@app.post("/telemetry/runs")
def create_telemetry_run(run: dict, user: str = Depends(verify_key)):
    """Create a run for an external runtime before it emits events."""
    session_id = run.get("session_id", "")
    if session_id:
        _session(session_id, user)
    run_id = run.get("run_id") or str(uuid.uuid4())
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", run_id):
        raise HTTPException(400, detail="run_id must be a simple identifier")
    if ledger.run(run_id):
        raise HTTPException(409, detail="Telemetry run already exists")
    fields = {
        "user.id": user,
        "session.id": session_id,
        "task.id": run.get("task_id", ""),
        "agent.id": run.get("agent_id", ""),
        "agent.run.id": run.get("agent_run_id", ""),
        "agent.parent.run.id": run.get("parent_agent_run_id", ""),
        "root.run.id": run.get("root_run_id", run_id),
        "framework.name": run.get("framework", "external"),
    }
    return {"run": ledger.start_run(run_id, **fields)}


@app.get("/telemetry/runs/{run_id}")
def get_telemetry_run(run_id: str, offset: int = 0, limit: int = 200, user: str = Depends(verify_key)):
    run = ledger.run(run_id)
    if not run or run.get("user.id") != user:
        raise HTTPException(404, detail="Telemetry run not found")
    return {"run": run, "summary": run.get("summary"), "steps": ledger.steps(run_id),
            "events": ledger.events(run_id, offset=offset, limit=limit)}


@app.get("/telemetry/programs/{program_id}")
def get_program(program_id: str, user: str = Depends(verify_key)):
    task = get_task(program_id, user=user)
    return get_telemetry_run(task.get("run_id", program_id), user=user)


@app.post("/telemetry/events")
def ingest_telemetry_event(event: dict, user: str = Depends(verify_key)):
    """Framework-neutral ingestion point for external agent runtimes."""
    event_type = event.get("type")
    run_id = event.get("run_id")
    if not isinstance(event_type, str) or not isinstance(run_id, str):
        raise HTTPException(400, detail="type and run_id are required")
    run = ledger.run(run_id)
    if not run or run.get("user.id") != user:
        raise HTTPException(404, detail="Telemetry run not found")
    identity = {key: event.get(key) for key in ("session.id", "task.id", "agent.id", "agent.run.id", "agent.parent.run.id", "handoff.id")}
    identity["user.id"] = user
    attributes = {str(key): value if isinstance(value, (int, float, bool)) else redact_text(value, limit=1024)
                  for key, value in (event.get("attributes") or {}).items()}
    return ledger.emit(event_type, run_id=run_id, attributes=attributes,
                       payload=safe_payload(event.get("payload")), severity=event.get("severity", "info"), **identity)
