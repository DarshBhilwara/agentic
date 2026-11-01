#!/usr/bin/env python3
"""Measure sequential end-to-end agent task overhead with kernel capture on/off.

Run on the agent host. Apply the selected settings and collect a fresh read-only
Kubernetes snapshot before each invocation; this tool never changes the cluster.
"""
import argparse
import copy
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests


SMOKE_TEXT = "agentic telemetry overhead smoke"
PROMPT = ("Use write_file to create overhead-smoke.txt containing exactly the JSON string "
          f"{json.dumps(SMOKE_TEXT)} with no newline. Then use execute_command to run "
          "cat overhead-smoke.txt. Finish after confirming the command output, without changing the file.")


def _items(snapshot):
    value = json.loads(Path(snapshot).read_text())
    return value.get("items", [value])


def _resource(items, kind, name):
    for item in items:
        if item.get("kind", "").lower() == kind.lower() and item.get("metadata", {}).get("name") == name:
            return item
    raise ValueError(f"snapshot is missing {kind}/{name}")


def _env(container):
    return {entry["name"]: entry.get("value") for entry in container.get("env", [])}


def _canonical_resource(resource):
    value = copy.deepcopy(resource.get("spec", {}))
    template = value.get("template", {})
    annotations = template.get("metadata", {}).get("annotations") or {}
    annotations.pop("kubectl.kubernetes.io/restartedAt", None)
    containers = template.get("spec", {}).get("containers", [])
    for container in containers:
        for entry in container.get("env", []):
            if entry.get("name") in {"KERNEL_TELEMETRY_ENABLED", "KERNEL_CAPTURE_ENABLED"}:
                entry["value"] = "<kernel-setting>"
    return value


def snapshot_metadata(path, setting):
    items = _items(path)
    worker = _resource(items, "Deployment", "agent-worker")
    collector = _resource(items, "DaemonSet", "kernel-telemetry")
    wc = next((container for container in worker["spec"]["template"]["spec"]["containers"]
               if container.get("name") == "worker"), None)
    cc = next((container for container in collector["spec"]["template"]["spec"]["containers"]
               if container.get("name") == "collector"), None)
    if wc is None or cc is None:
        raise ValueError("snapshot is missing the worker or collector container")
    expected = "true" if setting == "enabled" else "false"
    if _env(wc).get("KERNEL_TELEMETRY_ENABLED", "").lower() != expected:
        raise ValueError("agent-worker KERNEL_TELEMETRY_ENABLED does not match requested setting")
    if _env(cc).get("KERNEL_CAPTURE_ENABLED", "").lower() != expected:
        raise ValueError("kernel-telemetry KERNEL_CAPTURE_ENABLED does not match requested setting")
    for resource in (worker, collector):
        spec, status = resource.get("spec", {}), resource.get("status", {})
        if status.get("observedGeneration", 0) < resource.get("metadata", {}).get("generation", 0):
            raise ValueError("snapshot contains a workload that has not observed its latest generation")
    desired_workers = worker.get("spec", {}).get("replicas", 0)
    worker_status = worker.get("status", {})
    if desired_workers < 1 or worker_status.get("readyReplicas", 0) != desired_workers:
        raise ValueError("agent-worker snapshot is not fully ready")
    if worker_status.get("updatedReplicas", 0) != desired_workers or worker_status.get("unavailableReplicas", 0) != 0:
        raise ValueError("agent-worker snapshot does not show the complete updated replica set")
    collector_status = collector.get("status", {})
    desired_collectors = collector_status.get("desiredNumberScheduled", 0)
    if desired_collectors < 1 or collector_status.get("numberReady", 0) != desired_collectors:
        raise ValueError("kernel-telemetry snapshot is not fully ready")
    if (collector_status.get("updatedNumberScheduled", 0) != desired_collectors or
            collector_status.get("numberUnavailable", 0) != 0):
        raise ValueError("kernel-telemetry snapshot does not show the complete updated daemon set")
    comparable = {"worker": _canonical_resource(worker),
                  "collector": _canonical_resource(collector)}
    vllm = next((item for item in items if item.get("kind") == "Deployment" and
                 item.get("metadata", {}).get("name") == "vllm"), None)
    if not vllm:
        raise ValueError("snapshot is missing Deployment/vllm")
    vllm_spec = vllm.get("spec", {})
    vllm_status = vllm.get("status", {})
    desired_vllm = vllm_spec.get("replicas", 0)
    vllm_template = vllm_spec.get("template", {}).get("spec", {})
    vllm_container = next((container for container in vllm_template.get("containers", [])
                           if container.get("name") == "vllm"), None)
    if not vllm_container:
        raise ValueError("snapshot is missing the vllm container")
    if (vllm_status.get("observedGeneration", 0) < vllm.get("metadata", {}).get("generation", 0) or
            desired_vllm < 1 or vllm_status.get("readyReplicas", 0) != desired_vllm or
            vllm_status.get("updatedReplicas", 0) != desired_vllm or
            vllm_status.get("unavailableReplicas", 0) != 0):
        raise ValueError("vllm snapshot does not show a fully updated ready deployment")
    result = {
        "snapshot_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        "comparable_config_sha256": hashlib.sha256(json.dumps(comparable, sort_keys=True,
                                                                separators=(",", ":")).encode()).hexdigest(),
        "worker": {"image": wc.get("image"), "replicas": worker.get("spec", {}).get("replicas"),
                   "ready_replicas": worker_status.get("readyReplicas"),
                   "updated_replicas": worker_status.get("updatedReplicas"),
                   "node_selector": worker.get("spec", {}).get("template", {}).get("spec", {}).get("nodeSelector"),
                   "kernel_telemetry_enabled": expected},
        "collector": {"image": cc.get("image"), "desired": desired_collectors,
                      "ready": collector_status.get("numberReady"),
                      "updated": collector_status.get("updatedNumberScheduled"), "capture_enabled": expected},
    }
    result["vllm"] = {"image": vllm_container.get("image"),
                      "spec_sha256": hashlib.sha256(json.dumps(
                          _canonical_resource(vllm), sort_keys=True,
                          separators=(",", ":")).encode()).hexdigest()}
    if not result["vllm"]["image"]:
        raise ValueError("vllm image is missing from deployment snapshot")
    return result


def _safe_error(exc):
    response = getattr(exc, "response", None)
    return {"type": type(exc).__name__, "http_status": response.status_code if response is not None else None}


class Gateway:
    def __init__(self, url, token, timeout):
        self.url, self.timeout = url.rstrip("/"), timeout
        self.http = requests.Session()
        self.http.headers["X-API-Key"] = token

    def request(self, method, path, **kwargs):
        response = self.http.request(method, self.url + path, timeout=self.timeout, **kwargs)
        response.raise_for_status()
        return response.json()


def _collector_snapshot(api, health_run_id, node, setting):
    payload = api.request("GET", f"/telemetry/runs/{health_run_id}/kernel", params={"limit": 1})
    health = payload.get("node_health") or {}
    if health.get("node") != node:
        raise ValueError(f"health run does not resolve to collector node {node}")
    if health.get("heartbeat_ok") is not True:
        raise ValueError(f"collector heartbeat for {node} is stale or unavailable")
    enabled = health.get("capture_enabled")
    if isinstance(enabled, bool):
        pass
    elif isinstance(enabled, str) and enabled.lower() in {"true", "false"}:
        enabled = enabled.lower() == "true"
    else:
        raise ValueError(f"collector heartbeat capture state for {node} is unavailable")
    if enabled != (setting == "enabled"):
        raise ValueError(f"collector heartbeat capture state for {node} does not match {setting}")
    return {key: health.get(key) for key in (
        "node", "heartbeat_unix", "heartbeat_age_seconds", "heartbeat_ok", "capture_enabled",
        "events", "perf_lost", "map_overflow", "queue_drops", "export_errors",
        "discovery_errors", "identity_misses")}


def _run_one(api, workspace_root, output, index, warmup, timeout, node, setting):
    attempt_id = str(uuid.uuid4())
    workspace = workspace_root / attempt_id
    workspace.mkdir(parents=True, exist_ok=False)
    result = {"attempt_id": attempt_id, "index": index, "warmup": warmup, "setting": setting,
              "workspace": str(workspace), "started_unix": time.time(), "task_status": "Unknown"}
    submitted_task = False
    try:
        session = api.request("POST", "/sessions", params={"workspace": str(workspace)})
        submitted = api.request("POST", f"/sessions/{session['session_id']}/messages",
                                params={"prompt": PROMPT, "timeout_seconds": timeout, "max_steps": 4})
        result.update({key: submitted.get(key) for key in ("task_id", "program_id", "run_id")})
        submitted_task = bool(result.get("task_id"))
        if not submitted_task:
            raise RuntimeError("gateway response did not include a task ID")
        (output / f"{attempt_id}.submission.json").write_text(json.dumps(result, indent=2))
        deadline = time.monotonic() + timeout + 45
        while True:
            task = api.request("GET", f"/tasks/{result['task_id']}")
            if task.get("status") in {"completed", "failed", "timeout"}:
                result["task_status"] = {"completed": "Success", "failed": "Failed", "timeout": "Timeout"}[task["status"]]
                result["task_error_present"] = bool(task.get("error"))
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("task did not reach a terminal state before the runner deadline")
            time.sleep(1)
        result["finished_unix"] = time.time()
        result["runner_wall_time_ms"] = (result["finished_unix"] - result["started_unix"]) * 1000
        run_id = result.get("run_id")
        if run_id:
            try:
                trace = api.request("GET", f"/telemetry/runs/{run_id}")
                summary = trace.get("summary") or (trace.get("run") or {}).get("summary") or {}
                result["program"] = {key: summary.get(key) for key in (
                    "task_completion_time_ms", "task_wait_time_ms", "inference_steps", "completed_steps",
                    "tool_calls", "tool_failures", "total_prompt_tokens", "total_completion_tokens")}
                result["steps"] = [{key: step.get(key) for key in (
                    "step_index", "step_status", "step_time_ms", "llm_inference_time_ms",
                    "tool_calls", "prompt_tokens", "completion_tokens", "engine_metrics_status")}
                    | {"tools": [{key: tool.get(key) for key in ("tool_name", "tool_status", "tool_latency_ms")}
                                  for tool in step.get("tools", [])]}
                    for step in trace.get("steps", [])]
                kernel = api.request("GET", f"/telemetry/runs/{run_id}/kernel", params={"limit": 1})
                result["kernel_status"] = kernel.get("status")
                result["kernel_node_health"] = kernel.get("node_health")
                result["kernel_total"] = kernel.get("total")
                result["kernel_retained"] = kernel.get("retained")
                result["kernel_page_record_count"] = len(kernel.get("records", []))
                result["kernel_truncated"] = kernel.get("truncated")
            except Exception as exc:
                result["telemetry_error"] = _safe_error(exc)
        try:
            file_path = workspace / "overhead-smoke.txt"
            result["file_verified"] = file_path.is_file() and file_path.read_text() == SMOKE_TEXT
        except Exception as exc:
            result["file_verification_error"] = _safe_error(exc)
    except Exception as exc:
        result["runner_error"] = _safe_error(exc)
        if submitted_task:
            result["task_status"] = "Unknown"
        result["finished_unix"] = time.time()
        result["runner_wall_time_ms"] = (result["finished_unix"] - result["started_unix"]) * 1000
    (output / f"{attempt_id}.json").write_text(json.dumps(result, indent=2))
    print(f"{index:02d} {'warmup' if warmup else 'sample'} {result['task_status']} {attempt_id}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setting", choices=("enabled", "disabled"), required=True)
    parser.add_argument("--deployment-snapshot", type=Path, required=True,
                        help="kubectl get deploy,daemonset -o json captured after rollout")
    parser.add_argument("--health-run-id", required=True,
                        help="existing terminal run on the target node, used to read live collector health")
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new, empty experiment directory")
    parser.add_argument("--gateway", default=os.getenv("AGENTCTL_GATEWAY_URL", "http://localhost:30080"))
    parser.add_argument("--node", default="agent-01")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if args.repetitions < 1 or args.warmups < 0 or args.timeout < 1:
        parser.error("repetitions and timeout must be positive; warmups cannot be negative")
    if args.output.exists():
        parser.error("output path must not already exist")
    config = snapshot_metadata(args.deployment_snapshot, args.setting)
    args.workspace_root.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True)
    token_path = Path.home() / ".agentic_token"
    token = token_path.read_text().strip()
    if not token:
        parser.error("~/.agentic_token is empty; authenticate with agentctl login first")
    api = Gateway(args.gateway, token, min(30, args.timeout))
    metadata = {"setting": args.setting, "node": args.node, "repetitions": args.repetitions,
                "warmups": args.warmups, "concurrency": 1, "timeout_seconds": args.timeout,
                "deployment": config,
                "workload_sha256": hashlib.sha256((PROMPT + "\0" + SMOKE_TEXT).encode()).hexdigest(),
                "measurement": {"primary": "task_completion_time_ms (gateway submission to terminal task processing; includes queue wait)",
                                "secondary": "runner_wall_time_ms (client session creation through terminal poll)",
                                "workload": "sequential fixed write_file + execute_command task; model output adds variability"},
                "collector_health_counters": "per-run kernel node_health is a cumulative asynchronous snapshot; missing fields remain unknown"}
    (args.output / "experiment.json").write_text(json.dumps(metadata, indent=2))
    try:
        metadata["health_run_id"] = args.health_run_id
        metadata["collector_before"] = _collector_snapshot(api, args.health_run_id, args.node, args.setting)
    except Exception as exc:
        metadata["collector_before_error"] = _safe_error(exc)
        (args.output / "experiment.json").write_text(json.dumps(metadata, indent=2))
        parser.error("collector preflight failed; see experiment.json")
    complete = True
    rows = []
    for index in range(args.warmups + args.repetitions):
        row = _run_one(api, args.workspace_root, args.output, index + 1,
                       index < args.warmups, args.timeout, args.node, args.setting)
        rows.append(row)
        with (args.output / "results.jsonl").open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        if row.get("runner_error") or row.get("task_status") in {"Unknown", "Timeout"}:
            complete = False
        if not complete:
            break
    try:
        metadata["collector_after"] = _collector_snapshot(api, args.health_run_id, args.node, args.setting)
        metadata["collector_postflight_ok"] = True
    except Exception as exc:
        metadata["collector_after_error"] = _safe_error(exc)
        metadata["collector_postflight_ok"] = False
    (args.output / "experiment.json").write_text(json.dumps(metadata, indent=2))
    return 0 if complete and metadata["collector_postflight_ok"] and len(rows) == args.warmups + args.repetitions else 1


if __name__ == "__main__":
    sys.exit(main())
