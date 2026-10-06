"""Typed program/step measurements. Unknown engine measurements stay null."""

from __future__ import annotations

import json
import time
import logging
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field

from telemetry import span


class TaskTimeout(TimeoutError):
    pass


class StepLimitExceeded(RuntimeError):
    pass


ENGINE_FIELDS = (
    "step_t_reasoning_ms", "prefill_time_ms", "decode_time_ms",
    "inference_queue_time_ms", "kv_recomputed_tokens",
)


@dataclass
class StepRecord:
    program_id: str
    step_index: int
    step_status: str = "Running"
    request_id: str | None = None
    step_time_ms: float = 0
    llm_inference_time_ms: float = 0
    step_t_reasoning_ms: float | None = None
    step_t_acting_ms: float = 0
    prefill_time_ms: float | None = None
    decode_time_ms: float | None = None
    inference_queue_time_ms: float | None = None
    context_tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    kv_recomputed_tokens: int | None = None
    engine_metrics_status: str = "unavailable"
    tools: list = field(default_factory=list)


class ProgramTelemetry:
    def __init__(self, program_id, *, task_wait_time_ms=0, on_step=None):
        self.program_id = program_id
        self.task_wait_time_ms = task_wait_time_ms
        self.on_step = on_step
        self.steps = []

    @contextmanager
    def step(self, index):
        record = StepRecord(self.program_id, index)
        self.steps.append(record)
        started = time.perf_counter()
        if self.on_step:
            self.on_step(asdict(record))
        with span("agent.step", program_id=self.program_id, step_index=index) as current:
            try:
                yield record
                record.step_status = "Success"
            except TimeoutError:
                record.step_status = "Timeout"
                raise
            except BaseException:
                record.step_status = "Failed"
                raise
            finally:
                record.step_time_ms = (time.perf_counter() - started) * 1000
                values = asdict(record)
                for key, value in values.items():
                    if value is not None and key != "tools":
                        current.set_attribute(key, value)
                if self.on_step:
                    self.on_step(values)

    def summary(self, status="Running", completion_time_ms=None):
        def total(name):
            values = [getattr(step, name) for step in self.steps]
            return sum(values) if all(value is not None for value in values) else None

        return {
            "program_id": self.program_id,
            "task_status": status,
            "task_completion_time_ms": completion_time_ms,
            "task_wait_time_ms": self.task_wait_time_ms,
            "inference_steps": len(self.steps),
            "completed_steps": sum(s.step_status == "Success" for s in self.steps),
            "tool_calls": sum(len(s.tools) for s in self.steps),
            "tool_failures": sum(t["tool_status"] != "Success" for s in self.steps for t in s.tools),
            "total_llm_inference_time_ms": total("llm_inference_time_ms"),
            "total_tool_time_ms": sum(t["tool_latency_ms"] for s in self.steps for t in s.tools),
            "total_tool_wait_time_ms": total("step_t_acting_ms"),
            "total_reasoning_time_ms": total("step_t_reasoning_ms"),
            "total_prefill_time_ms": total("prefill_time_ms"),
            "total_decode_time_ms": total("decode_time_ms"),
            "total_inference_queue_time_ms": total("inference_queue_time_ms"),
            "total_prompt_tokens": total("prompt_tokens"),
            "total_completion_tokens": total("completion_tokens"),
            "context_tokens": self.steps[-1].context_tokens if self.steps else None,
            "peak_context_tokens": max((s.context_tokens for s in self.steps if s.context_tokens is not None), default=None),
            "kv_recomputed_tokens": total("kv_recomputed_tokens"),
            "engine_measured_steps": sum(s.engine_metrics_status == "available" for s in self.steps),
        }


def collect_engine_metrics(redis_client, request_id, record, wait_seconds=0.5):
    """Join by the exact request ID; never use global Prometheus counter deltas."""
    if redis_client is None:
        return
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            raw = redis_client.get(f"telemetry:inference:{request_id}")
        except TaskTimeout:
            raise
        except Exception:
            logging.exception("Inference metrics unavailable for %s", request_id)
            return
        if raw:
            metrics = json.loads(raw)
            for key in ENGINE_FIELDS:
                setattr(record, key, metrics.get(key))
            record.engine_metrics_status = "available"
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(0.01)
