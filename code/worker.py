import time

import redis

from agent import run
from telemetry import span, task_counter, task_error_counter


r = redis.Redis(host="redis-service", port=6379, db=0, socket_timeout=None, socket_keepalive=True)

print("Agent worker booted. Listening to queue...")
while True:
    try:
        task_data = r.brpop("task_queue", timeout=30)
        if not task_data:
            continue
        task_id = task_data[1].decode()
        r.hset(f"task:{task_id}", "status", "processing")
        task = r.hgetall(f"task:{task_id}")
        try:
            user = task[b"user"].decode()
            with span("agent.queue.task", task_id=task_id, user=user):
                task_counter.add(1, {"component": "worker"})
                result = run(task[b"prompt"].decode(), user)
            r.hset(f"task:{task_id}", mapping={"status": "completed", "result": result})
            with open(f"/workspace/users/{user}/{task_id}.txt", "w") as file:
                file.write(result)
        except Exception as exc:
            task_error_counter.add(1, {"component": "worker"})
            r.hset(f"task:{task_id}", mapping={"status": "failed", "error": str(exc)})
    except (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError):
        time.sleep(1)
