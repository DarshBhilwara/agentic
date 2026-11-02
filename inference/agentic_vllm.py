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
import weakref

LOG = logging.getLogger(__name__)
_outbox = queue.Queue(maxsize=10000)
_registered = False
_kv_tracker = None
_UNKNOWN_OWNER = object()


class _KVLifecycleTracker:
    """Conservatively track block-table ownership at vLLM hook boundaries."""

    def __init__(self, max_requests=10000):
        self._lock = threading.RLock()
        self._managers = {}
        self._requests = {}
        self._request_groups = {}
        self._max_requests = max_requests

    def _manager(self, manager):
        key = id(manager)
        entry = self._managers.get(key)
        if entry is None or entry[0]() is not manager:
            manager_state = {"owners": {}, "last": None, "broken": False}
            def discard(ref, key=key):
                with self._lock:
                    current = self._managers.get(key)
                    if current is not None and current[0] is ref:
                        for request_id in current[1]["owners"].values():
                            request = self._requests.get(request_id)
                            if request is not None:
                                request["error"] = True
                                request["manager_counts"].pop(key, None)
                        self._managers.pop(key, None)
            entry = (weakref.ref(manager, discard), manager_state)
            self._managers[key] = entry
        return key, entry[1]

    def _state(self, request_id, now):
        state = self._requests.get(request_id)
        if state is None:
            if len(self._requests) >= self._max_requests:
                return None
            state = {"last": now, "gpu": 0, "cpu": 0, "gpu_ms": 0.0,
                     "cpu_ms": 0.0, "gpu_peak": 0, "cpu_peak": 0,
                     "shared_peak": 0, "swap_out": 0, "swap_in": 0,
                     "recompute_discard": 0,
                     "error": False, "manager_counts": {}}
            self._requests[request_id] = state
        return state

    @staticmethod
    def _block_tier(manager, block_id):
        allocator = manager.block_allocator
        block_allocator = allocator._block_ids_to_allocator[block_id]
        for device, candidate in allocator._allocators.items():
            if candidate is block_allocator:
                return "gpu" if device.name == "GPU" else "cpu"
        raise KeyError(block_id)

    def _advance_manager(self, manager):
        now = time.monotonic()
        manager_id, state = self._manager(manager)
        previous = state["last"]
        if previous is not None:
            elapsed_ms = max(0.0, (now - previous) * 1000)
            for request_id, counts in state.get("counts", {}).items():
                request = self._requests.get(request_id)
                if request is not None:
                    request["gpu_ms"] += counts[0] * elapsed_ms
                    request["cpu_ms"] += counts[1] * elapsed_ms

        if state.get("broken"):
            for request_id in set(state["owners"].values()) | set(state.get("counts", {})):
                request = self._requests.get(request_id)
                if request is not None:
                    request["error"] = True
            state["last"] = now
            return

        owners = {}
        try:
            for seq_id, table in manager.block_tables.items():
                request_id = state["owners"].get(seq_id)
                for block in table.blocks:
                    if block.__class__.__name__ == "NullBlock":
                        continue
                    block_id = getattr(block, "block_id", None)
                    if block_id is None:
                        continue
                    tier = self._block_tier(manager, block_id)
                    owners.setdefault((tier, block_id), set()).add(
                        request_id if request_id is not None else _UNKNOWN_OWNER)
        except Exception:
            affected = set(state["owners"].values()) | set(state.get("counts", {}))
            state["broken"] = True
            for request_id in state["owners"].values():
                request = self._requests.get(request_id)
                if request is not None:
                    request["error"] = True
            state["last"] = now
            state["counts"] = {}
            state["exclusive_ids"] = {}
            for request_id in affected:
                request = self._requests.get(request_id)
                if request is not None:
                    request["manager_counts"].pop(manager_id, None)
                    request["error"] = True
            for request in self._requests.values():
                request["gpu"] = sum(value[0] for value in request["manager_counts"].values())
                request["cpu"] = sum(value[1] for value in request["manager_counts"].values())
            return

        counts = {}
        shared = {}
        exclusive_ids = {}
        for (tier, _block_id), request_ids in owners.items():
            if _UNKNOWN_OWNER in request_ids:
                for request_id in request_ids:
                    request = self._requests.get(request_id)
                    if request is not None:
                        request["error"] = True
            for request_id in request_ids:
                request = self._requests.get(request_id)
                if request is None:
                    continue
                if len(request_ids) == 1:
                    counts.setdefault(request_id, [0, 0])[0 if tier == "gpu" else 1] += 1
                    exclusive_ids.setdefault(request_id, set()).add(_block_id)
                else:
                    shared[request_id] = shared.get(request_id, 0) + 1
        for request_id, values in counts.items():
            request = self._requests[request_id]
            request["manager_counts"][manager_id] = values
        for request_id, count in shared.items():
            request = self._requests.get(request_id)
            if request is not None:
                request["shared_peak"] = max(request["shared_peak"], count)
        # Owners without active tables have zero exclusive residency.
        for request_id in set(state["owners"].values()) | set(state.get("counts", {})):
            request = self._requests.get(request_id)
            if request is not None and request_id not in counts:
                request["manager_counts"].pop(manager_id, None)
        state["counts"] = counts
        state["exclusive_ids"] = exclusive_ids
        state["last"] = now
        for request in self._requests.values():
            gpu = sum(value[0] for value in request["manager_counts"].values())
            cpu = sum(value[1] for value in request["manager_counts"].values())
            request["gpu"], request["cpu"] = gpu, cpu
            request["gpu_peak"] = max(request["gpu_peak"], gpu)
            request["cpu_peak"] = max(request["cpu_peak"], cpu)
        return manager_id

    def before_change(self, manager):
        with self._lock:
            self._advance_manager(manager)

    def invalidate_manager(self, manager):
        with self._lock:
            try:
                _key, state = self._manager(manager)
            except Exception:
                # Without a manager state, invalidate every retained sample.
                for request in self._requests.values():
                    request["error"] = True
                return
            state["broken"] = True
            for request_id in set(state["owners"].values()) | set(state.get("counts", {})):
                request = self._requests.get(request_id)
                if request is not None:
                    request["error"] = True

    def discard_request(self, request_id, group_ref=None):
        with self._lock:
            current_ref = self._request_groups.get(request_id)
            if group_ref is not None and current_ref is not group_ref:
                return
            self._requests.pop(request_id, None)
            self._request_groups.pop(request_id, None)
            for _ref, state in self._managers.values():
                for seq_id, owner in list(state["owners"].items()):
                    if owner == request_id:
                        state["owners"][seq_id] = None

    def _invalidate_hook_error(self, manager, request_id=None):
        try:
            self.invalidate_manager(manager)
            if request_id is not None:
                with self._lock:
                    request = self._requests.get(request_id)
                    if request is not None:
                        request["error"] = True
        except Exception:
            LOG.exception("Could not invalidate KV lifecycle measurements")
            with self._lock:
                for request in self._requests.values():
                    request["error"] = True

    def allocated(self, manager, group):
        with self._lock:
            _key, state = self._manager(manager)
            request_id = group.request_id
            if request_identity(request_id)[0] is not None:
                request = self._state(request_id, time.monotonic())
                if request is not None and request_id not in self._request_groups:
                    try:
                        self._request_groups[request_id] = weakref.ref(
                            group, lambda ref, rid=request_id: self.discard_request(rid, ref))
                    except TypeError:
                        request["error"] = True
            for seq in group.get_seqs():
                state["owners"][seq.seq_id] = request_id
            self._advance_manager(manager)

    def forked(self, manager, parent_seq, child_seq):
        with self._lock:
            _key, state = self._manager(manager)
            owner = state["owners"].get(parent_seq.seq_id)
            if owner is not None:
                state["owners"][child_seq.seq_id] = owner
            self._advance_manager(manager)

    def freed(self, manager, seq):
        with self._lock:
            _key, state = self._manager(manager)
            request_id = state["owners"].pop(seq.seq_id, None)
            self._advance_manager(manager)
            if request_id is not None and request_id not in state["owners"].values():
                state["counts"].pop(request_id, None)
                state["exclusive_ids"].pop(request_id, None)
                request = self._requests.get(request_id)
                if request is not None:
                    request["manager_counts"].pop(id(manager), None)

    def swapped(self, manager, group, mapping, direction):
        with self._lock:
            _key, state = self._manager(manager)
            request_id = group.request_id
            request = self._requests.get(request_id)
            if request is not None:
                # Returned IDs are the block manager's scheduler-issued
                # physical-ID mapping; count distinct exclusive blocks only.
                ids = set()
                exclusive_ids = state.get("exclusive_ids", {}).get(request_id, set())
                for source, target in mapping:
                    source_device = "GPU" if direction == "out" else "CPU"
                    allocator = manager.block_allocator
                    source_enum = next(device for device in allocator._allocators
                                       if device.name == source_device)
                    source_allocator = allocator._allocators[source_enum]
                    for block_id in exclusive_ids:
                        if (allocator._block_ids_to_allocator.get(block_id) is source_allocator
                                and allocator.get_physical_block_id(source_enum, block_id) == source):
                            ids.add(block_id)
                request["swap_out" if direction == "out" else "swap_in"] += len(ids)
            self._advance_manager(manager)

    def recompute_candidates(self, manager, group):
        with self._lock:
            self._advance_manager(manager)
            _key, state = self._manager(manager)
            request_id = group.request_id
            ids = state.get("exclusive_ids", {}).get(request_id, set())
            return len(ids) if request_id in self._requests else None

    def record_recompute_discard(self, request_id, count):
        if count is None:
            return
        with self._lock:
            state = self._requests.get(request_id)
            if state is not None:
                state["recompute_discard"] += count

    def snapshot(self, request_id, consume=False):
        with self._lock:
            for ref, _manager_state in list(self._managers.values()):
                manager = ref()
                if manager is not None:
                    self._advance_manager(manager)
            state = self._requests.get(request_id)
            if state is None:
                return None
            result = {"gpu": state["gpu"], "cpu": state["cpu"],
                      "gpu_peak": state["gpu_peak"], "cpu_peak": state["cpu_peak"],
                      "gpu_ms": state["gpu_ms"], "cpu_ms": state["cpu_ms"],
                      "shared_peak": state["shared_peak"],
                      "swap_out": state["swap_out"], "swap_in": state["swap_in"],
                      "recompute_discard": state["recompute_discard"],
                      "error": state["error"]}
            if consume:
                self._requests.pop(request_id, None)
                self._request_groups.pop(request_id, None)
            return result


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


def request_metrics(group, consume_kv=False):
    metrics = group.metrics
    sequences = group.get_seqs()
    output_tokens = sum(seq.get_output_len() for seq in group.get_finished_seqs())
    decode_seconds = (metrics.finished_time - metrics.first_token_time
                      if metrics.finished_time is not None and metrics.first_token_time is not None
                      else None)
    sequence_count = group.num_seqs()
    cached_tokens = sum(seq.data.get_num_cached_tokens() for seq in sequences)
    forward_ms = metrics.model_forward_time
    kv = _kv_tracker.snapshot(group.request_id, consume=consume_kv) if _kv_tracker else None
    kv_valid = kv is not None and not kv["error"]
    return {
        "request_id": group.request_id,
        "source": "vllm-0.8.5-v0",
        # vLLM records CUDA model forward timing in milliseconds. This is
        # batch-attributed GPU time, not exclusive GPU occupancy per request.
        "measurement_source": "vllm-0.8.5-v0",
        "step_t_reasoning_ms": forward_ms,
        "model_forward_time_ms": forward_ms,
        "model_execute_time_ms": (metrics.model_execute_time * 1000
                                   if metrics.model_execute_time is not None else None),
        "scheduler_time_ms": (metrics.scheduler_time * 1000
                               if metrics.scheduler_time is not None else None),
        "time_to_first_token_ms": duration_ms(metrics.first_token_time, metrics.arrival_time),
        "end_to_end_latency_ms": duration_ms(metrics.finished_time, metrics.arrival_time),
        "output_tokens": output_tokens,
        # The first token is emitted at the start of this interval. vLLM's
        # request timing has no per-sequence first-token timestamps, so expose
        # this rate only for the pinned single-sequence serving configuration.
        "output_tokens_per_second": ((output_tokens - 1) / decode_seconds
                                      if sequence_count == 1 and output_tokens > 1
                                      and decode_seconds is not None and decode_seconds > 0 else None),
        "prefix_cached_tokens": cached_tokens,
        "sequence_count": sequence_count,
        "prefill_time_ms": duration_ms(metrics.first_token_time, metrics.first_scheduled_time),
        "decode_time_ms": duration_ms(metrics.finished_time, metrics.first_token_time),
        "inference_queue_time_ms": None if metrics.time_in_queue is None else metrics.time_in_queue * 1000,
        # Token-level lifecycle and cache affinity have no honest source here.
        "kv_recomputed_tokens": None,
        "kv_retained_tokens": None,
        "kv_evicted_tokens": None,
        "kv_restored_tokens": None,
        "cache_affinity_hit": None,
        "kv_gpu_exclusive_blocks_at_finish": kv["gpu"] if kv_valid else None,
        "kv_cpu_exclusive_blocks_at_finish": kv["cpu"] if kv_valid else None,
        "kv_gpu_exclusive_peak_blocks": kv["gpu_peak"] if kv_valid else None,
        "kv_cpu_exclusive_peak_blocks": kv["cpu_peak"] if kv_valid else None,
        "kv_gpu_exclusive_block_ms": kv["gpu_ms"] if kv_valid else None,
        "kv_cpu_exclusive_block_ms": kv["cpu_ms"] if kv_valid else None,
        "kv_swap_out_exclusive_blocks": kv["swap_out"] if kv_valid else None,
        "kv_swap_in_exclusive_blocks": kv["swap_in"] if kv_valid else None,
        "kv_recompute_discarded_exclusive_blocks": kv["recompute_discard"] if kv_valid else None,
        "kv_shared_blocks_observed": kv["shared_peak"] if kv_valid else None,
        "kv_memory_tier": ("mixed" if kv_valid and kv["gpu"] and kv["cpu"] else
                           "gpu" if kv_valid and kv["gpu"] else
                           "cpu" if kv_valid and kv["cpu"] else None),
        "kv_residency": "block_manager_observed" if kv_valid else None,
        "kv_eviction_cause": (
            "mixed" if kv_valid and kv["swap_out"] and kv["recompute_discard"] else
            "swap" if kv_valid and kv["swap_out"] else
            "recompute" if kv_valid and kv["recompute_discard"] else None),
        "inference_replica_id": os.getenv("INFERENCE_REPLICA_ID", os.getenv("HOSTNAME")),
        "kv_measurement": "partial" if kv_valid else "unavailable",
        "kv_measurement_reason": (
            "logical exclusive block residency and scheduler-issued swap mappings; "
            "sampled at block-manager hooks, not physical tensor bytes or confirmed DMA"
            if kv_valid else
            "request-scoped block ownership unavailable or lifecycle tracker failed"),
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
    global _registered, _kv_tracker
    if _registered:
        return
    if os.getenv("AGENT_INFERENCE_TELEMETRY_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        LOG.info("Agentic per-request inference telemetry hooks disabled")
        return
    import vllm
    if vllm.__version__ != "0.8.5" or os.getenv("VLLM_USE_V1") != "0":
        raise RuntimeError("agentic telemetry requires vLLM 0.8.5 with VLLM_USE_V1=0")
    from vllm.core.block_manager import SelfAttnBlockSpaceManager
    from vllm.engine.llm_engine import LLMEngine
    from vllm.core.scheduler import Scheduler

    _kv_tracker = _KVLifecycleTracker()
    allocate = SelfAttnBlockSpaceManager.allocate
    free = SelfAttnBlockSpaceManager.free
    fork = SelfAttnBlockSpaceManager.fork
    append_slots = SelfAttnBlockSpaceManager.append_slots
    swap_out = SelfAttnBlockSpaceManager.swap_out
    swap_in = SelfAttnBlockSpaceManager.swap_in
    preempt_recompute = Scheduler._preempt_by_recompute
    abort_request = LLMEngine.abort_request
    create_trace = LLMEngine.create_trace_span

    def measured_allocate(manager, group):
        tracking_failed = False
        try:
            _kv_tracker.before_change(manager)
        except Exception:
            LOG.exception("Could not sample KV state before allocation")
            tracking_failed = True
            _kv_tracker._invalidate_hook_error(manager, group.request_id)
        try:
            result = allocate(manager, group)
        except Exception:
            _kv_tracker._invalidate_hook_error(manager, group.request_id)
            raise
        try:
            _kv_tracker.allocated(manager, group)
        except Exception:
            LOG.exception("Could not track KV allocation for %s", group.request_id)
            _kv_tracker._invalidate_hook_error(manager, group.request_id)
        if tracking_failed:
            _kv_tracker._invalidate_hook_error(manager, group.request_id)
        return result

    def measured_free(manager, seq):
        try:
            _kv_tracker.before_change(manager)
        except Exception:
            LOG.exception("Could not sample KV state before free")
            _kv_tracker._invalidate_hook_error(manager)
        try:
            result = free(manager, seq)
        except Exception:
            _kv_tracker._invalidate_hook_error(manager)
            raise
        try:
            _kv_tracker.freed(manager, seq)
        except Exception:
            LOG.exception("Could not track KV free for sequence %s", seq.seq_id)
            _kv_tracker._invalidate_hook_error(manager)
        return result

    def measured_fork(manager, parent_seq, child_seq):
        try:
            _kv_tracker.before_change(manager)
        except Exception:
            LOG.exception("Could not sample KV state before fork")
            _kv_tracker._invalidate_hook_error(manager)
        try:
            result = fork(manager, parent_seq, child_seq)
        except Exception:
            _kv_tracker._invalidate_hook_error(manager)
            raise
        try:
            _kv_tracker.forked(manager, parent_seq, child_seq)
        except Exception:
            LOG.exception("Could not track KV fork for sequence %s", child_seq.seq_id)
            _kv_tracker._invalidate_hook_error(manager)
        return result

    def measured_append_slots(manager, seq, *args, **kwargs):
        try:
            _kv_tracker.before_change(manager)
        except Exception:
            LOG.exception("Could not sample KV state before append")
            _kv_tracker._invalidate_hook_error(manager)
        try:
            result = append_slots(manager, seq, *args, **kwargs)
        except Exception:
            _kv_tracker._invalidate_hook_error(manager)
            raise
        try:
            _kv_tracker.before_change(manager)
        except Exception:
            LOG.exception("Could not sample KV state after append")
            _kv_tracker._invalidate_hook_error(manager)
        return result

    def measured_swap(manager, group, original, direction, *args, **kwargs):
        try:
            _kv_tracker.before_change(manager)
        except Exception:
            LOG.exception("Could not sample KV state before swap %s", direction)
            _kv_tracker._invalidate_hook_error(manager, group.request_id)
        try:
            result = original(manager, group, *args, **kwargs)
        except Exception:
            _kv_tracker._invalidate_hook_error(manager, group.request_id)
            raise
        try:
            _kv_tracker.swapped(manager, group, result, direction)
        except Exception:
            LOG.exception("Could not track KV swap %s for %s", direction, group.request_id)
            _kv_tracker._invalidate_hook_error(manager, group.request_id)
        return result

    def measured_swap_out(manager, group, *args, **kwargs):
        return measured_swap(manager, group, swap_out, "out", *args, **kwargs)

    def measured_swap_in(manager, group, *args, **kwargs):
        return measured_swap(manager, group, swap_in, "in", *args, **kwargs)

    def measured_preempt_recompute(scheduler, group):
        try:
            discarded = _kv_tracker.recompute_candidates(scheduler.block_manager, group)
        except Exception:
            LOG.exception("Could not sample KV state before recompute preemption")
            _kv_tracker._invalidate_hook_error(scheduler.block_manager, group.request_id)
            discarded = None
        try:
            result = preempt_recompute(scheduler, group)
        except Exception:
            _kv_tracker._invalidate_hook_error(scheduler.block_manager, group.request_id)
            raise
        try:
            _kv_tracker.record_recompute_discard(group.request_id, discarded)
        except Exception:
            LOG.exception("Could not record KV recompute discard for %s", group.request_id)
            _kv_tracker._invalidate_hook_error(scheduler.block_manager, group.request_id)
        return result

    def measured_abort_request(engine, request_ids):
        ids = [request_ids] if isinstance(request_ids, str) else list(request_ids)
        result = abort_request(engine, request_ids if isinstance(request_ids, str) else ids)
        for request_id in ids:
            try:
                _kv_tracker.discard_request(request_id)
            except Exception:
                LOG.exception("Could not clean KV telemetry for aborted request %s", request_id)
        return result

    def measured_trace(engine, group):
        # Normal tracing remains intact; exporter failures cannot stop inference.
        try:
            program_id, step = request_identity(group.request_id)
            if program_id:
                data = request_metrics(group, consume_kv=True)
                try:
                    _outbox.put_nowait((group.request_id, data))
                except queue.Full:
                    LOG.error("Inference telemetry outbox full; dropped %s", group.request_id)
        except Exception:
            LOG.exception("Inference measurement unavailable for %s", group.request_id)
            try:
                _kv_tracker.discard_request(group.request_id)
            except Exception:
                LOG.exception("Could not clean failed inference measurement for %s", group.request_id)
        return create_trace(engine, group)

    SelfAttnBlockSpaceManager.allocate = measured_allocate
    SelfAttnBlockSpaceManager.free = measured_free
    SelfAttnBlockSpaceManager.fork = measured_fork
    SelfAttnBlockSpaceManager.append_slots = measured_append_slots
    SelfAttnBlockSpaceManager.swap_out = measured_swap_out
    SelfAttnBlockSpaceManager.swap_in = measured_swap_in
    Scheduler._preempt_by_recompute = measured_preempt_recompute
    LLMEngine.abort_request = measured_abort_request
    LLMEngine.create_trace_span = measured_trace
    threading.Thread(target=_publish_loop, daemon=True, name="agentic-telemetry").start()
    _registered = True
    LOG.info("Agentic per-request telemetry hooks installed")
