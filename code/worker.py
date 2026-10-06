"""One task per worker process; scale replicas for concurrent programs."""
import json
import logging
import os
import signal
import time
from contextlib import contextmanager
from datetime import datetime

import redis
from openai import APITimeoutError

from agent import run
from program_telemetry import ProgramTelemetry, TaskTimeout
from telemetry import parent_context, safe_content, span, task_counter, task_error_counter, program_duration, program_queue, program_steps
from telemetry_ledger import TelemetryLedger, now


@contextmanager
def task_deadline(seconds):
    def expired(_signum, _frame):
        raise TaskTimeout("Task deadline exceeded")
    if seconds <= 0:
        raise TaskTimeout("Task deadline exceeded while queued")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def process_task(r, task_id):
    ledger = TelemetryLedger(r)
    task = {k.decode(): v.decode() for k, v in r.hgetall(f"task:{task_id}").items()}
    if not task or task.get("status") in {"completed", "failed", "timeout"}:
        return
    run_id = task.get("run_id", task_id)
    user, session_id = task["user"], task.get("session_id", task_id)
    started = time.perf_counter()
    submitted = float(task.get("submitted_at_unix") or datetime.fromisoformat(task["created_at"]).timestamp())
    wait_ms = max(0, (time.time() - submitted) * 1000)
    identity = {"program_id": task_id, "user.id": user, "session.id": session_id,
                "task.id": task_id, "agent.id": task.get("agent_id", "legacy-agent"),
                "agent.run.id": task.get("agent_run_id", task_id),
                "agent.parent.run.id": task.get("parent_agent_run_id", "")}
    if not ledger.run(run_id):
        ledger.start_run(run_id, **identity)

    def emit(event_type, **kwargs):
        return ledger.emit(event_type, run_id=run_id, **{**identity, **kwargs})

    def save_step(step):
        ledger.save_step(run_id, step)
        ledger.save_summary(run_id, measurements.summary())
        emit("step.started" if step["step_status"] == "Running" else "step.completed", attributes=step)

    measurements = ProgramTelemetry(task_id, task_wait_time_ms=wait_ms, on_step=save_step)
    r.hset(f"task:{task_id}", mapping={"status": "processing", "task_status": "Running", "started_at": now()})
    ledger.save_summary(run_id, measurements.summary())
    status, result, error = "Failed", "", ""
    with parent_context(json.loads(task.get("trace_context", "{}"))):
        with span("agent.program", program_id=task_id, **{"run.id": run_id, "benchmark.case.id": task.get("case_id", "")}) as current:
            emit("task.started", attributes={"task_wait_time_ms": wait_ms})
            try:
                timeout_seconds = float(task.get("timeout_seconds", 1800))
                with task_deadline(timeout_seconds - wait_ms / 1000):
                    conversation_key = f"session:{session_id}:agent:{identity['agent.id']}:messages"
                    stored = r.get(conversation_key)
                    workspace = task.get("workspace", f"/workspace/users/{user}")
                    if workspace == f"/workspace/users/{user}":
                        os.makedirs(workspace, exist_ok=True)
                    task_counter.add(1, {"component": "worker"})
                    result, conversation = run(
                        task["prompt"], user, workspace=workspace, session_id=session_id,
                        agent_id=identity["agent.id"], turn_id=task.get("turn_id", task_id),
                        turn_number=int(task.get("turn_number", 1)),
                        conversation=json.loads(stored) if stored else None,
                        benchmark=task.get("benchmark"), case_id=task.get("case_id"),
                        task_id=task_id, run_id=run_id, root_run_id=task.get("root_run_id", run_id),
                        agent_run_id=identity["agent.run.id"], parent_agent_run_id=identity["agent.parent.run.id"],
                        agent_name=task.get("agent_name", "enterprise-agent"), event_sink=emit,
                        redis_client=r, measurements=measurements,
                        max_steps=int(task.get("max_steps", 100)), timeout_seconds=timeout_seconds,
                        enabled_tools=json.loads(task["enabled_tools"]) if task.get("enabled_tools") else None)
                    r.set(conversation_key, json.dumps(conversation), ex=2592000)
                    artifact_dir = os.path.join(os.getenv("AGENT_RESULTS_ROOT", "/workspace/users"), user)
                    os.makedirs(artifact_dir, exist_ok=True)
                    artifact = os.path.join(artifact_dir, f"{task_id}.txt")
                    with open(artifact, "w") as stream:
                        stream.write(result)
                    emit("artifact.written", attributes={"artifact.kind": "task_result", "artifact.path": artifact})
                status = "Success"
            except (TimeoutError, APITimeoutError) as exc:
                status, error = "Timeout", str(exc)
            except Exception as exc:
                error = str(exc)
                current.record_exception(exc)
                logging.exception("Program %s failed", task_id)
            finally:
                # A reaper may already have finalized an unresponsive worker.
                # Terminal Timeout is never upgraded to Success on late return.
                if r.hget(f"task:{task_id}", "status") == b"timeout":
                    return
                elapsed_ms = wait_ms + (time.perf_counter() - started) * 1000
                summary = measurements.summary(status, elapsed_ms)
                summary.update({"benchmark": task.get("benchmark", ""), "instance_id": task.get("case_id", "")})
                ledger.save_summary(run_id, summary)
                labels = {"task_status": status, "benchmark": task.get("benchmark", "")}
                program_duration.record(elapsed_ms, labels)
                program_queue.record(wait_ms, labels)
                program_steps.record(summary["inference_steps"], labels)
                for key, value in summary.items():
                    if value is not None:
                        current.set_attribute(key, value)
                legacy_status = {"Success": "completed", "Failed": "failed", "Timeout": "timeout"}[status]
                if status != "Success":
                    task_error_counter.add(1, {"component": "worker", "task_status": status})
                emit("task." + legacy_status, attributes=summary,
                     payload={"result": safe_content(result), "error": safe_content(error)})
                ledger.finish_run(run_id, legacy_status, task_status=status)
                # Publish terminal task status after all telemetry is stored.
                r.hset(f"task:{task_id}", mapping={"status": legacy_status, "task_status": status,
                       "result": result, "error": error, "completed_at": now(),
                       "task_completion_time_ms": elapsed_ms})
                r.zrem("task_deadlines", task_id)


def main():
    r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis-service:6379/0"), socket_keepalive=True)
    logging.basicConfig(level=logging.INFO)
    logging.info("Agent worker listening for programs")
    while True:
        try:
            task = r.brpoplpush("task_queue", "task_processing", timeout=5)
            if task:
                process_task(r, task.decode())
                r.lrem("task_processing", 0, task)
        except (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError):
            logging.exception("Redis unavailable")
            time.sleep(1)
        except Exception:
            logging.exception("Worker failed processing a queue entry")


if __name__ == "__main__":
    main()
