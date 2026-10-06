"""Finalize expired queued/abandoned tasks without replaying tool side effects."""
import logging
import os
import time

import redis

from program_telemetry import ProgramTelemetry, StepRecord
from telemetry_ledger import TelemetryLedger, now


def expire_tasks(r, *, grace_seconds=30):
    ledger = TelemetryLedger(r)
    for raw_id in r.zrangebyscore("task_deadlines", "-inf", time.time() - grace_seconds):
        task_id = raw_id.decode()
        task = {k.decode(): v.decode() for k, v in r.hgetall(f"task:{task_id}").items()}
        if task and task.get("status") not in {"completed", "failed", "timeout"}:
            run_id = task.get("run_id", task_id)
            elapsed = max(0, (time.time() - float(task["submitted_at_unix"])) * 1000)
            previous = ledger.run(run_id)
            old_summary = (previous or {}).get("summary", {})
            measurements = ProgramTelemetry(task_id, task_wait_time_ms=old_summary.get("task_wait_time_ms", elapsed))
            if task.get("status") == "pending":
                measurements.task_wait_time_ms = elapsed
            for step in ledger.steps(run_id):
                if step["step_status"] == "Running":
                    step["step_status"] = "Timeout"
                    # Worker died before closing the step. Its duration and
                    # partial inference/tool timings are not recoverable.
                    step["llm_inference_time_ms"] = None
                    step["step_time_ms"] = None
                    step["step_t_acting_ms"] = None
                    ledger.save_step(run_id, step)
                measurements.steps.append(StepRecord(**step))
            summary = measurements.summary("Timeout", elapsed)
            if any(step.step_time_ms is None for step in measurements.steps):
                summary["total_tool_time_ms"] = None
            summary.update({"benchmark": task.get("benchmark", ""), "instance_id": task.get("case_id", ""),
                            "termination_reason": "deadline_exceeded_or_worker_lost",
                            "timing_complete": False})
            if not previous:
                ledger.start_run(run_id, **{"program_id": task_id, "user.id": task["user"], "task.id": task_id})
            ledger.save_summary(run_id, summary)
            ledger.emit("task.timeout", run_id=run_id, program_id=task_id, attributes=summary)
            ledger.finish_run(run_id, "timeout", task_status="Timeout")
            r.hset(f"task:{task_id}", mapping={"status": "timeout", "task_status": "Timeout",
                   "completed_at": now(), "error": "Deadline exceeded or worker lost", "task_completion_time_ms": elapsed})
        r.zrem("task_deadlines", task_id)
        r.lrem("task_processing", 0, task_id)
        r.lrem("task_queue", 0, task_id)


def main():
    r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis-service:6379/0"))
    while True:
        try:
            expire_tasks(r)
        except Exception:
            logging.exception("Task deadline scan failed")
        time.sleep(5)


if __name__ == "__main__":
    main()
