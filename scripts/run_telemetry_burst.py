#!/usr/bin/env python3
"""Submit a simultaneous burst of identical CSV tasks and collect timings.

Run on the agent host after saving a fresh ready on-mode deployment snapshot.
This client submits each task once, records its IDs, and never changes cluster state.
"""
import argparse
import hashlib
import json
import os
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from export_telemetry_sample import Gateway, read_token, safe_error
from run_telemetry_comparison import _number, _workload_sha256, snapshot_metadata
from run_telemetry_sample import PROMPT, _seed_workspace


def _write(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def _submission_snapshot(items):
    return [{key: item.get(key) for key in ("index", "workspace", "session_id", "agent_id")}
            for item in items]


def _capability(api):
    if api.request("GET", "/capabilities").get("coding_completion_overrides") is not True:
        raise ValueError("runtime burst requires gateway completion overrides")


def _run_one(index, item, args, token, ready, ready_state, release, abort, release_monotonic):
    row = item
    api = None
    startup_error = None
    try:
        api = Gateway(args.gateway, token, min(args.timeout, 60))
    except Exception as exc:
        startup_error = safe_error(exc)
    with ready:
        if startup_error:
            row["stage"] = "client_setup_failed"
            row["runner_error"] = startup_error
        ready_state[0] += 1
        ready.notify_all()
    release.wait()
    if abort.is_set():
        if row.get("stage") != "client_setup_failed":
            row["stage"] = "not_submitted"
        row["task_status"] = "NotSubmitted"
        _write(args.output / ("attempt-%02d.json" % index), row)
        return row
    if startup_error:
        _write(args.output / ("attempt-%02d.json" % index), row)
        return row
    row["submission_started_unix"] = time.time()
    row["submission_started_monotonic"] = time.monotonic()
    try:
        submitted = api.request("POST", "/sessions/%s/messages" % row["session_id"],
                                params={"prompt": PROMPT, "max_steps": args.max_steps,
                                        "timeout_seconds": args.timeout, "coding_only": "true",
                                        "require_workspace_changes": "false",
                                        "require_command_execution": "false"})
        row.update({key: submitted.get(key) for key in ("task_id", "program_id", "run_id", "turn_id")})
        row["submitted_unix"] = time.time()
        row["submission_ack_monotonic"] = time.monotonic()
        row["stage"] = "poll"
        _write(args.output / ("attempt-%02d.json" % index), row)
        if not row.get("task_id"):
            raise RuntimeError("submission response has no task ID; outcome is ambiguous")
        deadline = time.monotonic() + args.timeout + 45
        task = None
        while time.monotonic() < deadline:
            task = api.request("GET", "/tasks/%s" % row["task_id"])
            state = str(task.get("status", "unknown")).lower()
            if state in {"completed", "failed", "timeout", "cancelled"}:
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        if task is None or str(task.get("status", "")).lower() not in {"completed", "failed", "timeout", "cancelled"}:
            raise TimeoutError("task did not reach terminal status; backend may still be running")
        row["runtime_status"] = task.get("status")
        row["task_status"] = task.get("task_status", task.get("status"))
        row["task_end_to_end_time_ms"] = _number(task.get("task_end_to_end_time_ms"))
        row["task_completion_time_ms"] = _number(task.get("task_completion_time_ms"))
        row["task_wait_time_ms"] = _number(task.get("task_wait_time_ms"))
        row["task_result_nonempty"] = isinstance(task.get("result"), str) and bool(task["result"].strip())
        row["terminal_observed_unix"] = time.time()
        row["client_elapsed_since_release_ms"] = (time.monotonic() - release_monotonic[0]) * 1000
        row["stage"] = "complete"
        _write(args.output / ("task-%02d.json" % index), {
            key: task.get(key) for key in ("id", "task_id", "run_id", "status", "task_status", "error",
                "task_wait_time_ms", "task_end_to_end_time_ms", "task_completion_time_ms",
                "require_workspace_changes", "require_command_execution")})
    except Exception as exc:
        row["stage"] = "unknown" if row.get("task_id") or row.get("submission_started_unix") else "submit_failed"
        row["task_status"] = "Unknown"
        row["runner_error"] = safe_error(exc)
        row["backend_may_still_be_running"] = bool(row.get("submission_started_unix"))
    _write(args.output / ("attempt-%02d.json" % index), row)
    return row


def run(args):
    if not 1 <= args.requests <= 100 or not 1 <= args.timeout <= 3600 or not 1 <= args.max_steps <= 1000:
        raise ValueError("requests must be 1..100, timeout 1..3600, and max-steps 1..1000")
    output = args.output.expanduser().resolve()
    args.output = output
    results_root = Path(__file__).resolve().parents[1] / "benchmark-results"
    try:
        output.relative_to(results_root.resolve())
    except ValueError:
        raise ValueError("--output must be a new directory under benchmark-results")
    deployment = snapshot_metadata(args.deployment_snapshot, "on")
    token = read_token(args.token_file)
    api = Gateway(args.gateway, token, min(args.timeout, 60))
    _capability(api)
    output.mkdir(parents=True, exist_ok=False)
    workspace_root = args.workspace_root.expanduser().resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    metadata = {"started_unix": time.time(), "requested_requests": args.requests,
                "mode": "on", "eligibility": "runtime", "timeout_seconds": args.timeout,
                "max_steps": args.max_steps, "deployment": deployment,
                "workload_sha256": _workload_sha256(),
                "prompt_sha256": hashlib.sha256(PROMPT.encode("utf-8")).hexdigest(),
                "completion_requirements": {"require_workspace_changes": False,
                                             "require_command_execution": False},
                "submission_model": "all sessions/workspaces prepared first; POST requests released together",
                "timing_note": "client burst duration is wall time from release until all terminal states are observed; task timers are worker reported"}
    _write(output / "experiment.json", metadata)
    items = []
    try:
        for index in range(1, args.requests + 1):
            workspace = workspace_root / str(uuid.uuid4())
            _seed_workspace(workspace)
            session = api.request("POST", "/sessions", params={"workspace": str(workspace)})
            item = {"index": index, "workspace": str(workspace),
                    "session_id": session.get("session_id"), "agent_id": session.get("agent_id"),
                    "stage": "prepared", "task_status": "NotSubmitted"}
            if not item["session_id"]:
                raise RuntimeError("session creation returned no session ID")
            items.append(item)
            _write(output / ("attempt-%02d.json" % index), item)
            _write(output / "preparation.json", {"prepared": len(items), "requested": args.requests,
                                                  "attempts": items})
    except Exception as exc:
        metadata.update({"state": "preparation_failed", "error": safe_error(exc),
                         "prepared": len(items), "completed_unix": time.time()})
        _write(output / "experiment.json", metadata)
        return 2

    release = threading.Event()
    rows = []
    with ThreadPoolExecutor(max_workers=args.requests) as pool:
        ready = threading.Condition()
        ready_state = [0]
        abort = threading.Event()
        release_monotonic = [None]
        try:
            futures = {pool.submit(_run_one, item["index"], item, args, token, ready, ready_state, release,
                                   abort, release_monotonic): item for item in items}
            # Persist immutable identifiers only; worker rows are mutable.
            submissions = _submission_snapshot(items)
            _write(output / "submission.json", {"released": False, "attempts": submissions})
            with ready:
                all_ready = ready.wait_for(lambda: ready_state[0] >= len(items), timeout=120)
            if not all_ready:
                abort.set()
                release.set()
                metadata.update({"state": "client_setup_timeout", "completed_unix": time.time()})
                for future in as_completed(futures):
                    rows.append(future.result())
                _write(output / "experiment.json", metadata)
                return 2
            if any(item.get("stage") == "client_setup_failed" for item in items):
                abort.set()
                metadata.update({"state": "client_setup_failed", "completed_unix": time.time()})
                release.set()
                for future in as_completed(futures):
                    rows.append(future.result())
                _write(output / "experiment.json", metadata)
                _write(output / "submission.json", {"released": False,
                                                      "attempts": submissions})
                return 2
            manifest_written_unix = time.time()
            _write(output / "submission.json", {"released": True,
                                                  "release_manifest_written_unix": manifest_written_unix,
                                                  "attempts": submissions})
            metadata["released_unix"] = time.time()
            release_monotonic[0] = time.monotonic()
            release.set()
        except BaseException:
            abort.set()
            release.set()
            raise
        try:
            for future in as_completed(futures):
                try:
                    row = future.result()
                except Exception as exc:
                    row = futures[future]
                    row.update({"stage": "unknown", "task_status": "Unknown",
                                "runner_error": safe_error(exc), "backend_may_still_be_running": True})
                rows.append(row)
                print("%02d/%02d stage=%s status=%s task_id=%s" %
                      (row["index"], args.requests, row.get("stage"), row.get("task_status"), row.get("task_id")),
                      flush=True)
        except KeyboardInterrupt:
            print("Interrupted: submitted tasks may still be running; inspect saved task IDs and queue before any new burst.",
                  flush=True)
            raise
    elapsed_ms = (time.monotonic() - release_monotonic[0]) * 1000
    ack_times = [row["submission_ack_monotonic"] for row in rows
                 if isinstance(row.get("submission_ack_monotonic"), (int, float))]
    terminal = [row for row in rows if row.get("stage") == "complete"]
    # Fetch summaries only after every submitted task is terminal. This field
    # is not exposed on the task hash in the deployed API.
    if len(terminal) == args.requests:
        for row in rows:
            if row.get("stage") != "complete" or not row.get("run_id"):
                continue
            try:
                summary_api = Gateway(args.gateway, token, min(args.timeout, 60))
                summary = summary_api.request("GET", "/telemetry/runs/%s" % row["run_id"]).get("summary") or {}
                row["task_wait_time_ms"] = _number(summary.get("task_wait_time_ms"))
                row["summary_task_status"] = summary.get("task_status")
                _write(output / ("attempt-%02d.json" % row["index"]), row)
            except Exception as exc:
                row["telemetry_summary_error"] = safe_error(exc)
                _write(output / ("attempt-%02d.json" % row["index"]), row)
    statuses = {}
    for row in rows:
        status = str(row.get("task_status", "Unknown"))
        statuses[status] = statuses.get(status, 0) + 1
    metrics = {}
    for key in ("task_wait_time_ms", "task_end_to_end_time_ms", "task_completion_time_ms"):
        values = [row[key] for row in terminal if _number(row.get(key)) is not None]
        metrics[key] = {"count": len(values), "mean_ms": statistics.mean(values) if values else None,
                        "median_ms": statistics.median(values) if values else None,
                        "values_ms": values}
    all_terminal = len(terminal) == args.requests
    summary = {"requested_requests": args.requests, "recorded_attempts": len(rows),
               "terminal_attempts": len(terminal), "status_counts": statuses,
               "all_requests_terminal": all_terminal,
               "burst_wall_time_ms": elapsed_ms,
               "submission_ack_spread_ms": ((max(ack_times) - min(ack_times)) * 1000
                                             if len(ack_times) >= 2 else None),
               "terminal_requests_per_second": len(terminal) / (elapsed_ms / 1000) if elapsed_ms > 0 else None,
               "worker_timings": metrics,
               "limitations": "Ten client submissions share two workers, so at most two run at once. Single burst; throughput uses client-observed terminal completions. Queue drain is not asserted. Telemetry capture completeness is not checked; results do not establish capacity or telemetry overhead under concurrency."}
    _write(output / "results.json", {"attempts": sorted(rows, key=lambda item: item["index"]), "summary": summary})
    _write(output / "summary.json", summary)
    metadata.update({"completed_unix": time.time(),
                     "state": "complete" if all_terminal else "incomplete_unknown_tasks",
                     "recorded_attempts": len(rows)})
    _write(output / "experiment.json", metadata)
    print(json.dumps(summary, indent=2))
    return 0 if len(terminal) == args.requests else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=10)
    parser.add_argument("--deployment-snapshot", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, default=Path("/home/agentic/agentic/benchmark-workspaces"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gateway", default=os.getenv("AGENTCTL_GATEWAY_URL", "http://localhost:30080"))
    parser.add_argument("--token-file", type=Path, default=Path("~/.agentic_token"))
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--max-steps", type=int, default=20)
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        print(json.dumps({"state": "failed", "error": safe_error(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
