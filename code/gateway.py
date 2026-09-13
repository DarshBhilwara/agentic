import json
import os
import re
import redis, uuid
from fastapi import FastAPI, Depends, HTTPException, Header
from telemetry import span, task_counter

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

@app.post("/tasks")
def submit_task(prompt: str, user: str = Depends(verify_key)):
    task_counter.add(1, {"component": "gateway", "operation": "submit"})
    with span("agent.task.submit", user=user) as s:
        task_id = str(uuid.uuid4())
        r.hset(f"task:{task_id}", mapping={"id": task_id, "user": user, "prompt": prompt, "status": "pending"})
        r.lpush("task_queue", task_id)
        s.set_attribute("task.id", task_id)
        return {"task_id": task_id, "status": "queued"}

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
