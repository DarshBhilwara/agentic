"""Request measurements for vLLM 0.8.5 V0, single sequence, prefix caching.

Hooks are deliberately version-locked. Engine timing is measured on the
inference node and joined by request ID, never by global metric subtraction.
"""
import json
import logging
import os
import queue
import threading
import time

LOG = logging.getLogger(__name__)
_outbox = queue.Queue(maxsize=10000)
_registered = False


def request_identity(request_id):
    try:
        prefix = "chatcmpl-"
        value = request_id[len(prefix):] if request_id.startswith(prefix) else request_id
        program_id, step, nonce = value.rsplit(".", 2)
        return program_id, int(step)
    except (ValueError, AttributeError):
        return None, None


def duration_ms(end, start):
    return max(0.0, (end - start) * 1000) if end is not None and start is not None else None


def request_metrics(group):
    metrics = group.metrics
    return {
        "request_id": group.request_id,
        "source": "vllm-0.8.5-v0",
        # vLLM records CUDA model forward timing in milliseconds. This is
        # batch-attributed GPU time, not exclusive GPU occupancy per request.
        "step_t_reasoning_ms": metrics.model_forward_time,
        "prefill_time_ms": duration_ms(metrics.first_token_time, metrics.first_scheduled_time),
        "decode_time_ms": duration_ms(metrics.finished_time, metrics.first_token_time),
        "inference_queue_time_ms": None if metrics.time_in_queue is None else metrics.time_in_queue * 1000,
        "kv_recomputed_tokens": getattr(group, "_agentic_recomputed", None),
        # vLLM 0.8.5 does not expose per-request tier residency or eviction
        # causes; retain explicit nulls until a supported engine hook exists.
        "kv_retained_tokens": getattr(group, "_agentic_retained", None),
        "kv_evicted_tokens": getattr(group, "_agentic_evicted", None),
        "kv_restored_tokens": getattr(group, "_agentic_restored", None),
        "kv_memory_tier": getattr(group, "_agentic_memory_tier", None),
        "kv_residency": getattr(group, "_agentic_residency", None),
        "kv_eviction_cause": getattr(group, "_agentic_eviction_cause", None),
        "cache_affinity_hit": getattr(group, "_agentic_affinity_hit", None),
        "inference_replica_id": os.getenv("INFERENCE_REPLICA_ID", os.getenv("HOSTNAME")),
        "kv_measurement": getattr(group, "_agentic_kv_status", "unavailable"),
        "kv_measurement_reason": getattr(group, "_agentic_kv_reason", "no per-request lifecycle attribution exposed"),
    }


def _publish_loop():
    import redis
    client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis-service:6379/0"),
                                 socket_timeout=1, socket_connect_timeout=1)
    while True:
        request_id, data = _outbox.get()
        try:
            client.set(f"telemetry:inference:{request_id}", json.dumps(data), ex=3600)
        except Exception:
            LOG.exception("Could not persist inference measurements for %s", request_id)
        finally:
            _outbox.task_done()


def register():
    global _registered
    if _registered:
        return
    import vllm
    if vllm.__version__ != "0.8.5" or os.getenv("VLLM_USE_V1") != "0":
        raise RuntimeError("agentic telemetry requires vLLM 0.8.5 with VLLM_USE_V1=0")
    from vllm.core.block_manager import SelfAttnBlockSpaceManager
    from vllm.engine.llm_engine import LLMEngine

    allocate = SelfAttnBlockSpaceManager.allocate
    create_trace = LLMEngine.create_trace_span

    def measured_allocate(manager, group):
        # v0.8.5 V0 exposes allocation at block-manager scope, but does not
        # attach free/swap ownership to a request. Never infer per-request KV
        # values from prefix hashes; preserve nulls and state the limitation.
        group._agentic_kv_status = "unavailable"
        group._agentic_kv_reason = "vLLM 0.8.5 V0 allocation hook has no request-scoped free/swap attribution"
        return allocate(manager, group)

    def measured_trace(engine, group):
        # Normal tracing remains intact; exporter failures cannot stop inference.
        try:
            program_id, step = request_identity(group.request_id)
            if program_id:
                data = request_metrics(group)
                try:
                    _outbox.put_nowait((group.request_id, data))
                except queue.Full:
                    LOG.error("Inference telemetry outbox full; dropped %s", group.request_id)
        except Exception:
            LOG.exception("Inference measurement unavailable for %s", group.request_id)
        return create_trace(engine, group)

    SelfAttnBlockSpaceManager.allocate = measured_allocate
    LLMEngine.create_trace_span = measured_trace
    threading.Thread(target=_publish_loop, daemon=True, name="agentic-telemetry").start()
    _registered = True
    LOG.info("Agentic per-request telemetry hooks installed")
