#!/usr/bin/env python3
"""Run and compare five sequential coding tasks with telemetry on or off.

Run each mode on the agent host after applying that mode and saving a fresh
read-only Kubernetes snapshot. The script never changes cluster state.
"""
import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

from export_telemetry_sample import Gateway, export_run, read_token, safe_error
from run_telemetry_sample import DATA, PROMPT, SEED_ANALYZER, SEED_TESTS, _host_verify, _seed_workspace


MODE_FLAGS = {
    "worker": "AGENT_TELEMETRY_ENABLED",
    "gateway": "AGENT_TELEMETRY_ENABLED",
    "reaper": "AGENT_TELEMETRY_ENABLED",
    "collector": "KERNEL_CAPTURE_ENABLED",
    "inference": "AGENT_INFERENCE_TELEMETRY_ENABLED",
    "inference_tracing": "AGENT_INFERENCE_TRACING_ENABLED",
}
SUBORDINATE_FLAGS = {
    "worker": ("AGENT_PROGRAM_TELEMETRY_ENABLED", "AGENT_SEMANTIC_TELEMETRY_ENABLED",
               "KERNEL_TELEMETRY_ENABLED"),
    "gateway": ("AGENT_PROGRAM_TELEMETRY_ENABLED", "AGENT_SEMANTIC_TELEMETRY_ENABLED"),
    "reaper": ("AGENT_PROGRAM_TELEMETRY_ENABLED", "AGENT_SEMANTIC_TELEMETRY_ENABLED"),
}


def _items(path):
    value = json.loads(Path(path).read_text())
    return value.get("items", [value])


def _find(items, kind, name):
    for item in items:
        if item.get("kind", "").lower() == kind.lower() and item.get("metadata", {}).get("name") == name:
            return item
    raise ValueError("snapshot is missing %s/%s" % (kind, name))


def _container(resource, name):
    spec = resource.get("spec", {}).get("template", {}).get("spec", {})
    for container in spec.get("containers", []):
        if container.get("name") == name:
            return container
    raise ValueError("workload is missing container %s" % name)


def _env(container):
    return {entry["name"]: str(entry.get("value", ""))
            for entry in container.get("env", []) if "name" in entry}


def _bool(value, label):
    if str(value).lower() not in {"true", "false"}:
        raise ValueError("snapshot has no explicit boolean for %s" % label)
    return str(value).lower() == "true"


def _canonical(resource):
    """Keep image/spec identity, dropping only rollout noise and experiment flags."""
    value = json.loads(json.dumps(resource.get("spec", {})))
    template = value.get("template", {})
    metadata = template.get("metadata", {})
    annotations = metadata.get("annotations") or {}
    annotations.pop("kubectl.kubernetes.io/restartedAt", None)
    template.get("metadata", {})["annotations"] = annotations
    flags = set(MODE_FLAGS.values())
    pod_spec = template.get("spec", {})
    for container in [*pod_spec.get("containers", []), *pod_spec.get("initContainers", [])]:
        container["env"] = sorted((entry for entry in container.get("env", [])
                                   if entry.get("name") not in flags),
                                  key=lambda entry: entry.get("name", ""))
    return value


def snapshot_metadata(path, mode):
    items = _items(path)
    expected = mode == "on"
    resources = {
        "worker": (_find(items, "Deployment", "agent-worker"), "worker"),
        "gateway": (_find(items, "Deployment", "api-gateway"), "gateway"),
        "reaper": (_find(items, "Deployment", "task-reaper"), "reaper"),
        "inference": (_find(items, "Deployment", "vllm"), "vllm"),
    }
    envs, comparable = {}, {}
    for role, (resource, container_name) in resources.items():
        container = _container(resource, container_name)
        if not container.get("image"):
            raise ValueError("snapshot has no image for %s" % role)
        envs[role] = _env(container)
        comparable[role] = _canonical(resource)
        spec, status = resource.get("spec", {}), resource.get("status", {})
        desired = spec.get("replicas", 0)
        if (not desired or status.get("observedGeneration", 0) < resource.get("metadata", {}).get("generation", 0)
                or status.get("readyReplicas", 0) != desired
                or status.get("updatedReplicas", 0) != desired
                or status.get("unavailableReplicas", 0) != 0):
            raise ValueError("%s snapshot is not a fully updated ready workload" % role)

    collector = _find(items, "DaemonSet", "kernel-telemetry")
    collector_container = _container(collector, "collector")
    if not collector_container.get("image"):
        raise ValueError("snapshot has no image for kernel collector")
    collector_env = _env(collector_container)
    comparable["collector"] = _canonical(collector)
    desired = collector.get("status", {}).get("desiredNumberScheduled", 0)
    status = collector.get("status", {})
    if (not desired or status.get("observedGeneration", 0) < collector.get("metadata", {}).get("generation", 0)
            or status.get("numberReady", 0) != desired
            or status.get("updatedNumberScheduled", 0) != desired
            or status.get("numberUnavailable", 0) != 0):
        raise ValueError("kernel collector snapshot is not fully updated and ready")

    actual = {
        "worker": _bool(envs["worker"].get(MODE_FLAGS["worker"]), "worker telemetry"),
        "gateway": _bool(envs["gateway"].get(MODE_FLAGS["gateway"]), "gateway telemetry"),
        "reaper": _bool(envs["reaper"].get(MODE_FLAGS["reaper"]), "reaper telemetry"),
        "collector": _bool(collector_env.get(MODE_FLAGS["collector"]), "kernel capture"),
        "inference": _bool(envs["inference"].get(MODE_FLAGS["inference"]), "inference measurements"),
        "inference_tracing": _bool(envs["inference"].get(MODE_FLAGS["inference_tracing"]), "inference traces"),
    }
    if any(value != expected for value in actual.values()):
        raise ValueError("deployment snapshot telemetry flags do not all match mode %s: %s" % (mode, actual))
    for role, names in SUBORDINATE_FLAGS.items():
        for name in names:
            if not _bool(envs[role].get(name), "%s %s" % (role, name)):
                raise ValueError("comparison requires %s=true in %s for both modes" % (name, role))
    if not _bool(collector_env.get("KERNEL_INFERENCE_SYSCALLS_ENABLED"), "inference syscall capture"):
        raise ValueError("comparison requires inference syscall collection enabled in both modes")
    patch_path = Path(__file__).resolve().parents[1] / "manifests" / "inference-telemetry-switch.patch.json"
    expected_args = json.loads(patch_path.read_text())["spec"]["template"]["spec"]["containers"][0]["args"]
    inference_container = _container(resources["inference"][0], "vllm")
    if inference_container.get("command") != ["sh", "-c"] or inference_container.get("args") != expected_args:
        raise ValueError("vLLM must use the conditional tracing startup patch before either batch")
    probe = inference_container.get("readinessProbe", {}).get("httpGet", {})
    if probe.get("path") != "/health" or probe.get("port") != 8000:
        raise ValueError("vLLM requires the /health readiness probe on port 8000 before either batch")
    if expected:
        for role in ("worker", "gateway", "reaper"):
            for signal in ("TRACES", "METRICS", "LOGS"):
                if not envs[role].get("OTEL_EXPORTER_OTLP_%s_ENDPOINT" % signal):
                    raise ValueError("on-mode requires configured %s endpoint in %s" % (signal, role))
        if not _bool(collector_env.get("KERNEL_CAPTURE_ENABLED"), "kernel capture"):
            raise ValueError("on-mode requires kernel capture enabled")
        if not _bool(envs["inference"].get("AGENT_INFERENCE_TELEMETRY_ENABLED"), "inference capture"):
            raise ValueError("on-mode requires inference measurements enabled")
        if not envs["inference"].get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
            raise ValueError("on-mode requires an inference trace endpoint")
    comparable["images_and_config_sha256"] = hashlib.sha256(
        json.dumps(comparable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"mode": mode, "flags": actual, "workloads": {
        role: {"image": _container(resource, container_name).get("image"),
               "replicas": resource.get("spec", {}).get("replicas"),
               "ready": resource.get("status", {}).get("readyReplicas")}
        for role, (resource, container_name) in resources.items()},
        "collector_image": collector_container.get("image"),
        "collector_ready": status.get("numberReady"),
        "comparable_sha256": comparable["images_and_config_sha256"],
        "snapshot_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


def _write(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def _workload_sha256():
    files = {"sales.csv": DATA, "analyze.py": SEED_ANALYZER, "test_analyze.py": SEED_TESTS}
    value = {"prompt": PROMPT,
             "files": {name: hashlib.sha256(body.encode("utf-8")).hexdigest()
                       for name, body in files.items()}}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _eligibility(mode, policy, warmup, task_status, task_result, primary_ms,
                 verification_passed, capture_verified):
    """Return a policy-specific eligibility decision and its exclusion reasons."""
    reasons = []
    if warmup:
        reasons.append("warmup")
    if task_status != "Success":
        reasons.append("runtime_not_successful")
    if policy == "runtime" and not (isinstance(task_result, str) and task_result.strip()):
        reasons.append("empty_task_result")
    if _number(primary_ms) is None:
        reasons.append("missing_primary_timing")
    if policy == "verified" and verification_passed is not True:
        reasons.append("independent_verification_failed")
    if mode == "on" and capture_verified is not True:
        reasons.append("telemetry_capture_unverified")
    return not reasons, reasons


def _verify_attempt(workspace, task_status, policy):
    if task_status != "Success":
        return {"passed": False, "skipped_reason": "runtime_task_not_successful", "commands": []}
    try:
        return _host_verify(workspace)
    except Exception as exc:
        if policy != "runtime":
            raise
        return {"passed": False, "diagnostic_error": safe_error(exc), "commands": []}


def run_mode(args):
    config = snapshot_metadata(args.deployment_snapshot, args.mode)
    output = args.output.expanduser().resolve()
    root = Path(__file__).resolve().parents[1] / "benchmark-results"
    try:
        output.relative_to(root.resolve())
    except ValueError:
        raise ValueError("--output must be a new directory under benchmark-results")
    output.mkdir(parents=True, exist_ok=False)
    workspace_root = args.workspace_root.expanduser().resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    api = Gateway(args.gateway, read_token(args.token_file), 30)
    metadata = {"mode": args.mode, "repetitions": args.repetitions, "warmups": args.warmups,
                "timeout_seconds": args.timeout, "max_steps": args.max_steps,
                "deployment": config, "workload_sha256": _workload_sha256(),
                "measurement": {"primary": "worker task_end_to_end_time_ms, includes queue wait and synchronous ledger finalization",
                                "secondary": "worker task_completion_time_ms, existing task timing before final ledger writes",
                                "client_observed": "monotonic submit-to-terminal time, includes polling interval",
                                "eligibility_policy": args.eligibility,
                                "eligibility": ("terminal Success, nonempty task result, and finite primary timer; verifier is diagnostic only"
                                                if args.eligibility == "runtime" else
                                                "terminal Success, independent fixture verification passed, and finite primary timer"),
                                "order": "sequential; one warmup before measured runs"}}
    _write(output / "experiment.json", metadata)
    rows = []
    for index in range(args.warmups + args.repetitions):
        warmup = index < args.warmups
        row = {"index": index + 1, "warmup": warmup, "mode": args.mode,
               "workspace": None, "task_status": "Unknown", "stage": "preparation"}
        workspace = workspace_root / str(uuid.uuid4())
        row["workspace"] = str(workspace)
        try:
            _seed_workspace(workspace)
            row["stage"] = "session"
            session = api.request("POST", "/sessions", params={"workspace": str(workspace)})
            row["session_id"] = session.get("session_id")
            row["stage"] = "submission"
            submitted_at = time.monotonic()
            submitted = api.request("POST", "/sessions/%s/messages" % row["session_id"],
                                    params={"prompt": PROMPT, "max_steps": args.max_steps,
                                            "timeout_seconds": args.timeout,
                                            "coding_only": "true", "require_command_execution": "true"})
            row.update({key: submitted.get(key) for key in ("task_id", "program_id", "run_id")})
            if not row.get("task_id"):
                raise RuntimeError("submission response has no task ID; outcome is ambiguous")
            row["stage"] = "poll"
            row["submitted_unix"] = time.time()
            _write(output / ("attempt-%02d.json" % (index + 1)), row)
            deadline = time.monotonic() + args.timeout + 45
            task = None
            while time.monotonic() < deadline:
                task = api.request("GET", "/tasks/%s" % row["task_id"])
                state = str(task.get("status", "unknown")).lower()
                if state in {"completed", "failed", "timeout", "cancelled"}:
                    break
                time.sleep(min(2, max(0, deadline - time.monotonic())))
            if task is None or str(task.get("status", "")).lower() not in {"completed", "failed", "timeout", "cancelled"}:
                raise TimeoutError("task did not reach terminal status; do not resubmit")
            row["task_status"] = task.get("task_status", task.get("status"))
            row["runtime_status"] = task.get("status")
            row["task_result_nonempty"] = isinstance(task.get("result"), str) and bool(task["result"].strip())
            row["task_end_to_end_time_ms"] = _number(task.get("task_end_to_end_time_ms"))
            row["task_completion_time_ms"] = _number(task.get("task_completion_time_ms"))
            row["client_submit_to_terminal_ms"] = (time.monotonic() - submitted_at) * 1000
            # Preserve terminal diagnostics outside the measured task boundary.
            _write(output / ("task-%02d.json" % (index + 1)), {
                key: task.get(key) for key in ("id", "task_id", "run_id", "status", "task_status",
                    "error", "task_end_to_end_time_ms", "task_completion_time_ms")})
            row["stage"] = "verification"
            verification = _verify_attempt(workspace, row["task_status"], args.eligibility)
            _write(output / ("verification-%02d.json" % (index + 1)), verification)
            row["verification"] = {"passed": verification.get("passed", False),
                                   "checks": [{"name": item.get("name"), "returncode": item.get("returncode"),
                                               "passed": item.get("passed")}
                                              for item in verification.get("commands", [])]}
            if args.mode == "on":
                try:
                    evidence = output / ("evidence-%02d" % (index + 1))
                    row["telemetry_export"] = export_run(api, row["run_id"], evidence)
                    report = json.loads((evidence / "report.json").read_text())["runs"][0]
                    correlation = json.loads((evidence / "correlation.json").read_text())["correlation"]
                    summary = report.get("summary", {})
                    scopes = correlation.get("scope_counts", {})
                    steps = report.get("steps", [])
                    measured = api.request("GET", "/telemetry/runs/%s" % row["run_id"],
                                           params={"offset": 0, "limit": 10000})
                    engine_steps = sum(step.get("engine_metrics_status") == "available"
                                       for step in measured.get("steps", []))
                    step_count = len(steps)
                    row["telemetry_capture_check"] = {
                        "inference_steps": summary.get("inference_steps"),
                        "step_records_exported": step_count,
                        "engine_measured_steps": engine_steps,
                        "kernel_records_exported": row["telemetry_export"].get("exported_records"),
                        "tool_scope_count": sum(count for key, count in scopes.items()
                                                 if key.startswith("tool:")),
                        "captured": bool(summary.get("inference_steps", 0) > 0 and
                                         step_count == summary.get("inference_steps") and
                                         engine_steps == summary.get("inference_steps") and
                                         row["telemetry_export"].get("exported_records", 0) > 0 and
                                         any(key.startswith("tool:") and count > 0
                                             for key, count in scopes.items()))}
                except Exception as exc:
                    row["telemetry_export_error"] = safe_error(exc)
            capture_verified = row.get("telemetry_capture_check", {}).get("captured") is True
            row["eligible"], row["exclusion_reasons"] = _eligibility(
                args.mode, args.eligibility, warmup, row["task_status"], task.get("result"),
                row["task_end_to_end_time_ms"], verification.get("passed"), capture_verified)
            row["stage"] = "complete"
        except Exception as exc:
            row["runner_error"] = safe_error(exc)
            row["task_status"] = "Unknown" if row.get("task_id") else row.get("task_status", "Unknown")
            row["eligible"] = False
            row["stage"] = row.get("stage", "unknown")
            rows.append(row)
            _write(output / ("attempt-%02d.json" % (index + 1)), row)
            _append(output / "results.jsonl", row)
            print("Stopped at attempt %d after %s; inspect its IDs before continuing." % (index + 1, row["stage"]))
            break
        rows.append(row)
        _write(output / ("attempt-%02d.json" % (index + 1)), row)
        _append(output / "results.jsonl", row)
        print("%02d/%02d %s status=%s eligible=%s" %
              (index + 1, args.warmups + args.repetitions, "warmup" if warmup else "sample",
               row["task_status"], row["eligible"]))
    eligible = [row["task_end_to_end_time_ms"] for row in rows if row.get("eligible")]
    summary = {"mode": args.mode, "requested_repetitions": args.repetitions,
               "eligible_repetitions": len(eligible), "eligible_task_end_to_end_time_ms": eligible,
               "mean_ms": statistics.mean(eligible) if eligible else None,
               "median_ms": statistics.median(eligible) if eligible else None,
               "stdev_ms": statistics.stdev(eligible) if len(eligible) >= 2 else None}
    _write(output / "summary.json", summary)
    return 0 if len(rows) == args.warmups + args.repetitions and len(eligible) == args.repetitions else 1


def _append(path, row):
    with Path(path).open("a") as stream:
        stream.write(json.dumps(row) + "\n")


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def compare(args):
    on_meta = json.loads((args.on / "experiment.json").read_text())
    off_meta = json.loads((args.off / "experiment.json").read_text())
    on_rows = [json.loads(line) for line in (args.on / "results.jsonl").read_text().splitlines() if line]
    off_rows = [json.loads(line) for line in (args.off / "results.jsonl").read_text().splitlines() if line]
    on_policy = on_meta.get("eligibility_policy", on_meta.get("measurement", {}).get("eligibility_policy", "verified"))
    off_policy = off_meta.get("eligibility_policy", off_meta.get("measurement", {}).get("eligibility_policy", "verified"))
    meta_checks = {
        "mode_labels": on_meta.get("mode") == "on" and off_meta.get("mode") == "off",
        "workload": on_meta.get("workload_sha256") == off_meta.get("workload_sha256"),
        "timeout": on_meta.get("timeout_seconds") == off_meta.get("timeout_seconds"),
        "max_steps": on_meta.get("max_steps") == off_meta.get("max_steps"),
        "requested_repetitions": on_meta.get("repetitions") == 5 and off_meta.get("repetitions") == 5,
        "eligibility_policy": on_policy == off_policy,
    }
    on_values = [row["task_end_to_end_time_ms"] for row in on_rows if row.get("eligible") and not row.get("warmup")]
    off_values = [row["task_end_to_end_time_ms"] for row in off_rows if row.get("eligible") and not row.get("warmup")]
    on_capture_count = sum(row.get("telemetry_capture_check", {}).get("captured") is True
                           for row in on_rows if not row.get("warmup"))
    on_eligible_capture_count = sum(row.get("eligible") and row.get("telemetry_capture_check", {}).get("captured") is True
                                    for row in on_rows if not row.get("warmup"))
    result = {"on_config_sha256": on_meta["deployment"]["comparable_sha256"],
              "off_config_sha256": off_meta["deployment"]["comparable_sha256"],
              "on_eligible": len(on_values), "off_eligible": len(off_values),
              "eligibility_policy": on_policy if on_policy == off_policy else None,
              "on_capture_verified": on_capture_count,
              "on_eligible_capture_verified": on_eligible_capture_count, "on_capture_export_errors": sum(
                  bool(row.get("telemetry_export_error")) for row in on_rows if not row.get("warmup")),
              "batch_checks": meta_checks,
              "required_per_mode": 5, "on_mean_ms": statistics.mean(on_values) if on_values else None,
              "off_mean_ms": statistics.mean(off_values) if off_values else None,
              "on_median_ms": statistics.median(on_values) if on_values else None,
              "off_median_ms": statistics.median(off_values) if off_values else None,
              "on_stdev_ms": statistics.stdev(on_values) if len(on_values) > 1 else None,
              "off_stdev_ms": statistics.stdev(off_values) if len(off_values) > 1 else None}
    same = result["on_config_sha256"] == result["off_config_sha256"]
    enough = result["on_eligible"] == 5 and result["off_eligible"] == 5
    captures = on_eligible_capture_count >= 5
    same_task_settings = all(meta_checks.values())
    if same and enough and captures and same_task_settings and result["off_mean_ms"] > 0:
        result["overhead_ms"] = result["on_mean_ms"] - result["off_mean_ms"]
        result["overhead_percent"] = result["overhead_ms"] / result["off_mean_ms"] * 100
        result["comparison_status"] = "eligible"
    else:
        result["overhead_ms"] = None
        result["overhead_percent"] = None
        result["comparison_status"] = "ineligible"
        reasons = []
        if not same: reasons.append("comparable deployment images/config differ")
        if not enough:
            reasons.append("fewer than five %s-eligible runs per mode" % on_policy)
        if not captures: reasons.append("fewer than five eligible on-mode runs exported program and kernel telemetry")
        if not same_task_settings: reasons.append("mode, workload, timeout, max-steps, repetition or eligibility-policy settings differ")
        if result["off_mean_ms"] == 0: reasons.append("off-mode mean runtime is zero")
        result["reason"] = "; ".join(reasons)
    if args.output.exists():
        raise ValueError("comparison output already exists; choose a new path")
    _write(args.output, result)
    print(json.dumps(result, indent=2))
    return 0 if result["comparison_status"] == "eligible" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="run one telemetry mode sequentially")
    run_parser.add_argument("--mode", choices=("on", "off"), required=True)
    run_parser.add_argument("--deployment-snapshot", type=Path, required=True)
    run_parser.add_argument("--workspace-root", type=Path, default=Path("/home/agentic/agentic/benchmark-workspaces"))
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--gateway", default=os.getenv("AGENTCTL_GATEWAY_URL", "http://localhost:30080"))
    run_parser.add_argument("--token-file", type=Path, default=Path("~/.agentic_token"))
    run_parser.add_argument("--repetitions", type=int, default=5)
    run_parser.add_argument("--warmups", type=int, default=1)
    run_parser.add_argument("--timeout", type=int, default=300)
    run_parser.add_argument("--max-steps", type=int, default=20)
    run_parser.add_argument("--eligibility", choices=("verified", "runtime"), default="verified")
    compare_parser = sub.add_parser("compare", help="compare the on/off summaries")
    compare_parser.add_argument("--on", type=Path, required=True)
    compare_parser.add_argument("--off", type=Path, required=True)
    compare_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "compare":
        try:
            return compare(args)
        except Exception as exc:
            print(json.dumps({"state": "failed", "error": safe_error(exc)}), file=sys.stderr)
            return 2
    if args.repetitions != 5 or args.warmups < 0 or args.timeout < 30 or args.max_steps < 1:
        parser.error("use exactly five measured runs, zero or more warmups, timeout >=30, and max-steps >=1")
    try:
        return run_mode(args)
    except Exception as exc:
        print(json.dumps({"state": "failed", "error": safe_error(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
