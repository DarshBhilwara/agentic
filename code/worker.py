import json
import time
import redis
from agent import run
from telemetry import parent_context, span, task_counter, task_error_counter

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
            session_id = task.get(b"session_id", task_id.encode()).decode()
            agent_id = task.get(b"agent_id", b"legacy-agent").decode()
            turn_id = task.get(b"turn_id", task_id.encode()).decode()
            turn_number = int(task.get(b"turn_number", b"1"))
            conversation_key = f"session:{session_id}:messages"
            stored = r.get(conversation_key)
            conversation = json.loads(stored) if stored else None
            with parent_context(json.loads(task.get(b"trace_context", b"{}"))) as context:
                with span("agent.queue.task", context=context, task_id=task_id, user=user,
                          agent_id=agent_id, session_id=session_id):
                    task_counter.add(1, {"component": "worker"})
                    result, conversation = run(
                        task[b"prompt"].decode(), user,
                        session_id=session_id,
                        agent_id=agent_id,
                        turn_id=turn_id,
                        turn_number=turn_number,
                        conversation=conversation,
                    )
            r.set(conversation_key, json.dumps(conversation))
            r.hset(f"task:{task_id}", mapping={"status": "completed", "result": result, "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            with open(f"/workspace/users/{user}/{task_id}.txt", "w") as file:
                file.write(result)
        except Exception as exc:
            task_error_counter.add(1, {"component": "worker"})
            r.hset(f"task:{task_id}", mapping={"status": "failed", "error": str(exc)})
    except (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError):
        time.sleep(1)
