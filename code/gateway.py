import json
import os
import re
import redis, uuid
from datetime import datetime, timezone
from fastapi import FastAPI, Depends, HTTPException, Header
from telemetry import inject_context, span, task_counter

app = FastAPI()
r = redis.Redis(host='redis-service', port=6379, db=0)


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
    host_root = os.path.realpath(os.getenv("AGENT_WORKSPACE_HOST_ROOT", ""))
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
                   case_id: str = "", user: str = Depends(verify_key)):
    session = _session(session_id, user)
    task_counter.add(1, {"component": "gateway", "operation": "submit"})
    with span("agent.task.submit", user=user) as s:
        task_id = str(uuid.uuid4())
        turn_id = str(uuid.uuid4())
        turn_number = r.hincrby(f"session:{session_id}", "turn", 1)
        r.hset(f"task:{task_id}", mapping={
            "id": task_id, "user": user, "prompt": prompt, "status": "pending",
            "session_id": session_id, "agent_id": session["agent_id"],
            "workspace": session["workspace"],
            "benchmark": benchmark, "case_id": case_id,
            "turn_id": turn_id, "turn_number": turn_number,
            "created_at": _now(), "trace_context": json.dumps(inject_context()),
        })
        r.lpush("task_queue", task_id)
        s.set_attribute("task.id", task_id)
        s.set_attribute("agent.session.id", session_id)
        return {"task_id": task_id, "status": "queued", "turn_id": turn_id}


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
