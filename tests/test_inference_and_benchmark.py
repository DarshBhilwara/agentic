import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

import agentic_vllm
import run_swe_bench_pro as benchmark


def test_engine_timing_units_and_single_token_decode():
    sequence = NS(get_output_len=lambda: 1, data=NS(get_num_cached_tokens=lambda: 12))
    group = NS(request_id="chatcmpl-program.2.nonce",
               get_seqs=lambda: [sequence], get_finished_seqs=lambda: [sequence], num_seqs=lambda: 1,
               metrics=NS(first_scheduled_time=10.02, first_token_time=10.05,
                          finished_time=10.05, arrival_time=10, scheduler_time=0.003,
                          model_execute_time=0.004, time_in_queue=0.02, model_forward_time=8.5))
    metrics = agentic_vllm.request_metrics(group)
    assert metrics["step_t_reasoning_ms"] == 8.5
    assert metrics["prefill_time_ms"] == pytest.approx(30)
    assert metrics["decode_time_ms"] == 0
    assert metrics["inference_queue_time_ms"] == 20
    assert metrics["kv_recomputed_tokens"] is None
    assert metrics["model_forward_time_ms"] == 8.5
    assert metrics["model_execute_time_ms"] == 4
    assert metrics["scheduler_time_ms"] == 3
    assert metrics["time_to_first_token_ms"] == pytest.approx(50)
    assert metrics["end_to_end_latency_ms"] == pytest.approx(50)
    assert metrics["output_tokens"] == 1
    assert metrics["output_tokens_per_second"] is None
    assert metrics["prefix_cached_tokens"] == 12
    assert metrics["sequence_count"] == 1
    assert metrics["measurement_source"] == "vllm-0.8.5-v0"
    assert agentic_vllm.request_identity(group.request_id) == ("program", 2)


def test_kv_lifecycle_is_unknown_without_request_scoped_ownership():
    seq = NS(get_output_len=lambda: 2, data=NS(get_num_cached_tokens=lambda: 0))
    group = NS(request_id="chatcmpl-program.1.nonce", get_seqs=lambda: [seq],
               get_finished_seqs=lambda: [seq], num_seqs=lambda: 1,
               metrics=NS(first_scheduled_time=10, first_token_time=10.1,
                          finished_time=10.2, arrival_time=10, scheduler_time=None,
                          model_execute_time=None, time_in_queue=0, model_forward_time=5))
    metrics = agentic_vllm.request_metrics(group)
    for field in ("kv_recomputed_tokens", "kv_retained_tokens", "kv_evicted_tokens",
                  "kv_restored_tokens", "kv_memory_tier", "kv_residency",
                  "kv_eviction_cause", "cache_affinity_hit"):
        assert metrics[field] is None
    assert metrics["kv_measurement"] == "unavailable"
    assert metrics["kv_measurement_reason"]


def test_output_token_rate_excludes_first_token_and_requires_single_sequence():
    def group(sequences):
        return NS(request_id="chatcmpl-program.1.nonce", get_seqs=lambda: sequences,
                  get_finished_seqs=lambda: sequences, num_seqs=lambda: len(sequences),
                  metrics=NS(first_scheduled_time=10, first_token_time=10.1,
                             finished_time=11.1, arrival_time=10, scheduler_time=None,
                             model_execute_time=None, time_in_queue=0, model_forward_time=5))

    sequence = NS(get_output_len=lambda: 2, data=NS(get_num_cached_tokens=lambda: 0))
    assert agentic_vllm.request_metrics(group([sequence]))["output_tokens_per_second"] == pytest.approx(1)
    other = NS(get_output_len=lambda: 2, data=NS(get_num_cached_tokens=lambda: 0))
    assert agentic_vllm.request_metrics(group([sequence, other]))["output_tokens_per_second"] is None


def test_benchmark_prompt_allowlist():
    case = {"problem_statement": "Fix a bug", "requirements": "Support zero", "interface": "f(x)",
            "patch": "GOLD_SECRET", "test_patch": "HIDDEN_TESTS", "fail_to_pass": "SECRET_CASE"}
    prompt = benchmark.prompt_for(case)
    assert all(case[key] in prompt for key in ("problem_statement", "requirements", "interface"))
    assert all(case[key] not in prompt for key in ("patch", "test_patch", "fail_to_pass"))


def test_request_measurements_join_and_aggregate_without_lifecycle_estimates():
    from program_telemetry import ProgramTelemetry, collect_engine_metrics

    metrics = {"time_to_first_token_ms": 12, "end_to_end_latency_ms": 30,
               "scheduler_time_ms": 2, "model_forward_time_ms": 4,
               "model_execute_time_ms": 5, "prefix_cached_tokens": 16,
               "output_tokens": 3, "measurement_source": "vllm-0.8.5-v0",
               "kv_measurement": "partial", "kv_measurement_reason": "block-manager observations",
               "kv_gpu_exclusive_peak_blocks": 4, "kv_gpu_exclusive_block_ms": 12.5,
               "kv_cpu_exclusive_block_ms": 2.5, "kv_swap_out_exclusive_blocks": 2,
               "kv_swap_in_exclusive_blocks": 1, "kv_recompute_discarded_exclusive_blocks": 3,
               "kv_residency": "block_manager_observed"}
    requested = []

    def get(key):
        requested.append(key)
        return json.dumps(metrics)

    measured = ProgramTelemetry("p")
    with measured.step(1) as record:
        collect_engine_metrics(NS(get=get), "chatcmpl-p.1.nonce", record, 0)
    assert requested == ["telemetry:inference:chatcmpl-p.1.nonce"]
    assert record.measurement_source == "vllm-0.8.5-v0"
    assert record.kv_measurement_reason == "block-manager observations"
    assert record.kv_gpu_exclusive_peak_blocks == 4
    assert record.kv_residency == "block_manager_observed"
    assert record.kv_retained_tokens is None
    summary = measured.summary()
    assert summary["total_model_forward_time_ms"] == 4
    assert summary["total_time_to_first_token_ms"] == 12
    assert summary["total_prefix_cached_tokens"] == 16
    assert summary["total_output_tokens"] == 3
    assert summary["total_kv_gpu_exclusive_block_ms"] == 12.5
    assert summary["total_kv_cpu_exclusive_block_ms"] == 2.5
    assert summary["total_kv_swap_out_exclusive_blocks"] == 2
    assert summary["total_kv_swap_in_exclusive_blocks"] == 1
    assert summary["total_kv_recompute_discarded_exclusive_blocks"] == 3


def test_dataset_local_revision_and_selection(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps({"instance_id": "one"}) + "\n" + json.dumps({"instance_id": "two"}) + "\n")
    args = NS(dataset_jsonl=path, instance_id=["two"], limit=1)
    cases, revision = benchmark.load_cases(args)
    assert cases == [{"instance_id": "two"}]
    assert revision.startswith("sha256:")
    args.instance_id = ["missing"]
    with pytest.raises(ValueError):
        benchmark.load_cases(args)


def test_benchmark_exports_all_event_pages(tmp_path):
    def request(method, path, params):
        if path.endswith("/kernel"):
            records = [{"syscall_id": i} for i in range(
                params["offset"], min(params["offset"] + params["limit"], 503))]
            return {"records": records, "total": 503, "retained": 503, "truncated": False,
                    "status": {"perf_lost": 0}}
        events = [{"id": str(i)} for i in range(params["offset"], min(params["offset"] + 1000, 1002))]
        return {"events": events, "steps": [{"step_index": 1}], "run": {"status": "completed"},
                "summary": {"program_id": "p", "inference_steps": 1}}
    result = benchmark.export_trace(request, "run", tmp_path)
    assert result["program_id"] == "p"
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 1002
    assert len((tmp_path / "kernel.jsonl").read_text().splitlines()) == 503
    assert json.loads((tmp_path / "kernel-status.json").read_text())["truncated"] is False


def test_two_node_manifests_and_persistence():
    root = Path(__file__).resolve().parents[1]
    objects = {}
    for path in (root / "manifests").glob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text()):
            objects[(doc["kind"], doc["metadata"]["name"])] = doc
    for name in ("agent-worker", "api-gateway", "redis"):
        spec = objects[("Deployment", name)]["spec"]["template"]["spec"]
        assert spec["nodeSelector"] == {"agentic.io/role": "agent"}
    inference = objects[("Deployment", "vllm")]["spec"]["template"]["spec"]
    assert inference["nodeSelector"] == {"agentic.io/role": "inference"}
    env = {v["name"]: v.get("value") for v in inference["containers"][0]["env"]}
    assert env["VLLM_USE_V1"] == "0"
    assert "--enable-prefix-caching" in inference["containers"][0]["args"][0]
    redis = objects[("Deployment", "redis")]["spec"]["template"]["spec"]
    assert redis["volumes"][0]["persistentVolumeClaim"]["claimName"] == "agentic-redis"
