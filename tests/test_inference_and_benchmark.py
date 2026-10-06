import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

import agentic_vllm
import run_swe_bench_pro as benchmark


def test_engine_timing_units_and_single_token_decode():
    group = NS(request_id="chatcmpl-program.2.nonce", _agentic_recomputed=16,
               metrics=NS(first_scheduled_time=10.02, first_token_time=10.05,
                          finished_time=10.05, time_in_queue=0.02, model_forward_time=8.5))
    metrics = agentic_vllm.request_metrics(group)
    assert metrics["step_t_reasoning_ms"] == 8.5
    assert metrics["prefill_time_ms"] == pytest.approx(30)
    assert metrics["decode_time_ms"] == 0
    assert metrics["inference_queue_time_ms"] == 20
    assert metrics["kv_recomputed_tokens"] == 16
    assert agentic_vllm.request_identity(group.request_id) == ("program", 2)


def test_kv_matching_does_not_count_new_or_changed_context():
    assert agentic_vllm.matching_prefix_tokens([1, 2, 3], [1, 2, 4, 5], 16) == 32
    assert agentic_vllm.matching_prefix_tokens([1, 2], [8, 2], 16) == 0
    assert agentic_vllm.matching_prefix_tokens([], [1], 16) == 0


def test_benchmark_prompt_allowlist():
    case = {"problem_statement": "Fix a bug", "requirements": "Support zero", "interface": "f(x)",
            "patch": "GOLD_SECRET", "test_patch": "HIDDEN_TESTS", "fail_to_pass": "SECRET_CASE"}
    prompt = benchmark.prompt_for(case)
    assert all(case[key] in prompt for key in ("problem_statement", "requirements", "interface"))
    assert all(case[key] not in prompt for key in ("patch", "test_patch", "fail_to_pass"))


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
        events = [{"id": str(i)} for i in range(params["offset"], min(params["offset"] + 1000, 1002))]
        return {"events": events, "steps": [{"step_index": 1}], "run": {"status": "completed"},
                "summary": {"program_id": "p", "inference_steps": 1}}
    result = benchmark.export_trace(request, "run", tmp_path)
    assert result["program_id"] == "p"
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 1002


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
