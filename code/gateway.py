from fastapi import FastAPI, Depends, HTTPException, Header
import redis, uuid

app = FastAPI()
r = redis.Redis(host='redis-service', port=6379, db=0)

def verify_key(x_api_key: str = Header(...)):
    if x_api_key == "corp-secret-token-123":
        return "emp-iiitd"
    raise HTTPException(status_code=401, detail="Invalid Enterprise Token")

@app.post("/tasks")
def submit_task(prompt: str, user: str = Depends(verify_key)):
    task_id = str(uuid.uuid4())
    r.hset(f"task:{task_id}", mapping={
        "id": task_id, "user": user, "prompt": prompt, "status": "pending"
    })
    r.lpush("task_queue", task_id)
    return {"task_id": task_id, "status": "queued"}

@app.get("/tasks/{task_id}")
def get_task(task_id: str, user: str = Depends(verify_key)):
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
