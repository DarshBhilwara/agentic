import gc
from enum import Enum
from types import SimpleNamespace as NS

import agentic_vllm


class Device(Enum):
    GPU = 1
    CPU = 2


class Block:
    def __init__(self, block_id):
        self.block_id = block_id


class Allocator:
    def __init__(self):
        self._allocators = {Device.GPU: object(), Device.CPU: object()}
        self._block_ids_to_allocator = {
            1: self._allocators[Device.GPU],
            2: self._allocators[Device.CPU],
            3: self._allocators[Device.GPU],
        }

    def get_physical_block_id(self, device, block_id):
        ids = sorted(block_id for block_id, allocator in self._block_ids_to_allocator.items()
                     if allocator is self._allocators[device])
        return ids.index(block_id)


class Manager:
    def __init__(self):
        self.block_allocator = Allocator()
        self.block_tables = {}


class Group:
    def __init__(self, request_id, *seqs):
        self.request_id = request_id
        self._seqs = list(seqs)

    def get_seqs(self):
        return list(self._seqs)


def group(request_id, *seqs):
    return Group(request_id, *seqs)


def test_exclusive_counts_exclude_cross_request_shared_prefix_blocks():
    tracker = agentic_vllm._KVLifecycleTracker()
    manager = Manager()
    first = NS(seq_id=1)
    second = NS(seq_id=2)
    manager.block_tables = {1: NS(blocks=[Block(1), Block(3)]),
                            2: NS(blocks=[Block(1), Block(2)])}

    first_group = group("chatcmpl-program.1.n1", first)
    second_group = group("chatcmpl-external.1.n2", second)
    tracker.allocated(manager, first_group)
    tracker.allocated(manager, second_group)
    result = tracker.snapshot("chatcmpl-program.1.n1")

    assert result["gpu"] == 1
    assert result["cpu"] == 0
    assert result["gpu_peak"] == 1
    assert result["shared_peak"] == 1


def test_swap_and_recompute_lifecycle_are_separate_counters():
    tracker = agentic_vllm._KVLifecycleTracker()
    manager = Manager()
    seq = NS(seq_id=1)
    request_id = "chatcmpl-program.1.n1"
    manager.block_tables = {1: NS(blocks=[Block(1), Block(3)])}
    seq_group = group(request_id, seq)
    tracker.allocated(manager, seq_group)

    tracker.before_change(manager)
    manager.block_tables[1] = NS(blocks=[Block(2)])
    tracker.swapped(manager, seq_group, [(0, 0)], "out")
    tracker.before_change(manager)
    manager.block_tables[1] = NS(blocks=[Block(1)])
    tracker.swapped(manager, seq_group, [(0, 0)], "in")
    assert tracker.recompute_candidates(manager, seq_group) == 1
    tracker.record_recompute_discard(request_id, 1)

    result = tracker.snapshot(request_id)
    assert result["swap_out"] == 1
    assert result["swap_in"] == 1
    assert result["recompute_discard"] == 1


def test_non_agent_groups_are_tracked_for_exclusivity_but_not_exported():
    tracker = agentic_vllm._KVLifecycleTracker()
    manager = Manager()
    seq = NS(seq_id=4)
    manager.block_tables = {4: NS(blocks=[Block(1)])}
    request_group = group("external-request-id", seq)
    tracker.allocated(manager, request_group)

    assert tracker.snapshot("external-request-id") is None


def test_unknown_sequence_sharing_a_block_invalidates_exclusivity():
    tracker = agentic_vllm._KVLifecycleTracker()
    manager = Manager()
    tracked = NS(seq_id=1)
    manager.block_tables = {1: NS(blocks=[Block(1)]),
                            99: NS(blocks=[Block(1)])}
    request_group = group("chatcmpl-program.1.n1", tracked)
    tracker.allocated(manager, request_group)

    assert tracker.snapshot("chatcmpl-program.1.n1")["error"] is True


def test_tracking_error_invalidates_samples_for_the_manager():
    tracker = agentic_vllm._KVLifecycleTracker()
    manager = Manager()
    seq = NS(seq_id=1)
    manager.block_tables = {1: NS(blocks=[Block(1)])}
    request_id = "chatcmpl-program.1.n1"
    request_group = group(request_id, seq)
    tracker.allocated(manager, request_group)
    tracker.invalidate_manager(manager)

    assert tracker.snapshot(request_id)["error"] is True


def test_aborted_request_state_is_released_and_owner_becomes_unknown():
    tracker = agentic_vllm._KVLifecycleTracker()
    manager = Manager()
    seq = NS(seq_id=1)
    request_id = "chatcmpl-program.1.n1"
    manager.block_tables = {1: NS(blocks=[Block(1)])}
    request_group = group(request_id, seq)
    tracker.allocated(manager, request_group)
    tracker.discard_request(request_id)

    assert tracker.snapshot(request_id) is None
    assert tracker._managers[id(manager)][1]["owners"][1] is None


def test_request_state_is_released_if_native_trace_hook_never_runs():
    tracker = agentic_vllm._KVLifecycleTracker()
    manager = Manager()
    seq = NS(seq_id=1)
    request_id = "chatcmpl-program.1.n1"
    manager.block_tables = {1: NS(blocks=[Block(1)])}
    request_group = group(request_id, seq)
    tracker.allocated(manager, request_group)
    del request_group
    gc.collect()

    assert tracker.snapshot(request_id) is None
