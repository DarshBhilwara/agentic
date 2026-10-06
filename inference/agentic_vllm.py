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
from collections import OrderedDict

LOG = logging.getLogger(__name__)
HISTORY_LIMIT = 10000
_history = OrderedDict()
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
        "kv_measurement": "previous_step_full_blocks_missing_from_prefix_cache",
    }


def matching_prefix_tokens(previous, current, block_size):
    matched = 0
    for left, right in zip(previous, current):
        if left != right:
            break
        matched += block_size
    return matched


def _block_hashes(seq, computed_only=False):
    from vllm.core.block.prefix_caching_block import PrefixCachingBlock
    tokens = seq.get_token_ids()
    length = seq.data.get_num_computed_tokens() if computed_only else len(tokens)
    previous = None
    hashes = []
    for offset in range(0, length - seq.block_size + 1, seq.block_size):
        previous = PrefixCachingBlock.hash_block_tokens(
            is_first_block=offset == 0, prev_block_hash=previous,
            cur_block_token_ids=list(tokens[offset:offset + seq.block_size]), extra_hash=seq.extra_hash())
        hashes.append(previous)
    return hashes


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
        program_id, step = request_identity(group.request_id)
        # Only first admission: preemption must not overwrite inter-step loss.
        if program_id and not hasattr(group, "_agentic_recomputed"):
            group._agentic_recomputed = None
            try:
                seqs = group.get_seqs()
                if manager.enable_caching and len(seqs) == 1:
                    seq = seqs[0]
                    previous = _history.get(program_id)
                    if step == 1:
                        group._agentic_recomputed = 0
                    elif previous and previous[0] == step - 1:
                        reusable = matching_prefix_tokens(previous[1], _block_hashes(seq), seq.block_size)
                        cached = manager.get_num_cached_tokens(seq)
                        group._agentic_recomputed = max(0, reusable - cached)
            except Exception:
                LOG.exception("KV measurement unavailable for %s", group.request_id)
        return allocate(manager, group)

    def measured_trace(engine, group):
        # Normal tracing remains intact; exporter failures cannot stop inference.
        try:
            program_id, step = request_identity(group.request_id)
            if program_id:
                data = request_metrics(group)
                seqs = group.get_finished_seqs()
                if len(seqs) == 1:
                    _history[program_id] = (step, _block_hashes(seqs[0], computed_only=True))
                    _history.move_to_end(program_id)
                    while len(_history) > HISTORY_LIMIT:
                        _history.popitem(last=False)
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
