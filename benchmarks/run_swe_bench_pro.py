#!/usr/bin/env python3
"""Run SWE-bench Pro programs through the agent-node gateway; export traces.

This is a telemetry/patch-generation experiment, not the official test grader.
Run on the agent host, with --workspace-root inside its shared project mount.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import requests


AGENT_PROJECT_ROOT = Path(os.getenv("AGENT_PROJECT_ROOT", "/home/agentic/agentic"))


def prompt_for(case):
    # Explicit allowlist: never send patch, test_patch, fail_to_pass,
    # pass_to_pass, or grading scripts to the model.
    parts = ["Fix the issue in the current repository. Inspect the code, implement the fix, "
             "and run relevant tests available in the repository. Leave your changes in the working tree."]
    for key in ("problem_statement", "requirements", "interface"):
        if case.get(key):
            parts.append(f"{key}:\n{case[key]}")
    return "\n\n".join(parts)


def git(workspace, *args):
    return subprocess.run(["git", "-C", str(workspace), *args], check=True,
                          text=True, capture_output=True, timeout=300).stdout


def prepare_checkout(case, workspace):
    repo, commit = case["repo"], case["base_commit"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError(f"Invalid repository: {repo}")
    if not re.fullmatch(r"[a-fA-F0-9]{40}", commit):
        raise ValueError("base_commit must be a full commit SHA")
    workspace.mkdir(parents=True, exist_ok=False)
    git(workspace, "init")
    git(workspace, "remote", "add", "origin", f"https://github.com/{repo}.git")
    git(workspace, "fetch", "--depth=1", "origin", commit)
    git(workspace, "checkout", "--detach", "FETCH_HEAD")
    if git(workspace, "rev-parse", "HEAD").strip().lower() != commit.lower():
        raise RuntimeError("Checkout does not match base_commit")


def load_cases(args):
    if args.dataset_jsonl:
        content = args.dataset_jsonl.read_bytes()
        cases = [json.loads(line) for line in content.decode().splitlines() if line.strip()]
        revision = "sha256:" + hashlib.sha256(content).hexdigest()
    else:
        if not args.revision:
            raise ValueError("Supply --revision (dataset commit/tag), or --dataset-jsonl")
        from datasets import load_dataset
        from huggingface_hub import HfApi
        revision = HfApi().dataset_info("ScaleAI/SWE-bench_Pro", revision=args.revision).sha
        cases = list(load_dataset("ScaleAI/SWE-bench_Pro", args.config,
                                  split="test", revision=revision))
    if args.instance_id:
        wanted = set(args.instance_id)
        cases = [case for case in cases if case["instance_id"] in wanted]
        missing = wanted - {case["instance_id"] for case in cases}
        if missing:
            raise ValueError(f"Instances not found: {sorted(missing)}")
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        raise ValueError("No benchmark instances selected")
    ids = [case["instance_id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate instance IDs in dataset")
    return cases, revision


def export_trace(request, run_id, destination):
    offset = 0
    with (destination / "events.jsonl").open("w") as events:
        while True:
            data = request("GET", f"/telemetry/runs/{run_id}", params={"offset": offset, "limit": 1000})
            for event in data["events"]:
                events.write(json.dumps(event) + "\n")
            offset += len(data["events"])
            if len(data["events"]) < 1000:
                break
    (destination / "steps.json").write_text(json.dumps(data["steps"], indent=2))
    (destination / "run.json").write_text(json.dumps(data["run"], indent=2))
    return data["summary"]


def run_case(case, args, experiment_id, revision, token):
    instance_id = case["instance_id"]
    key = hashlib.sha256(instance_id.encode()).hexdigest()[:20]
    destination = args.output / key
    destination.mkdir(parents=True, exist_ok=False)
    workspace = args.workspace_root / experiment_id / key
    record = {"experiment_id": experiment_id, "instance_id": instance_id,
              "dataset": "ScaleAI/SWE-bench_Pro", "dataset_revision": revision,
              "dataset_config": args.config, "repo": case["repo"], "base_commit": case["base_commit"],
              "evaluation_status": "NotEvaluated", "resolved": None,
              "environment": "shared-agent-worker-checkout"}
    with requests.Session() as http:
        http.headers["X-API-Key"] = token

        def request(method, path, **kwargs):
            response = http.request(method, args.gateway_url.rstrip("/") + path, timeout=30, **kwargs)
            response.raise_for_status()
            return response.json()

        try:
            setup_started = time.perf_counter()
            prepare_checkout(case, workspace)
            record["setup_time_ms"] = (time.perf_counter() - setup_started) * 1000
            session = request("POST", "/sessions", params={"workspace": str(workspace)})
            submitted = request("POST", f"/sessions/{session['session_id']}/messages", params={
                "prompt": prompt_for(case), "benchmark": "swe-bench-pro", "case_id": instance_id,
                "max_steps": args.max_steps, "timeout_seconds": args.task_timeout, "coding_only": True})
            record.update({key: submitted[key] for key in ("program_id", "task_id", "run_id")})
            # Save submission immediately for recovery after a client interruption.
            (destination / "submission.json").write_text(json.dumps(record, indent=2))
            deadline = time.monotonic() + args.task_timeout + args.poll_grace
            while True:
                task = request("GET", f"/tasks/{submitted['task_id']}")
                if task["status"] in {"completed", "failed", "timeout"}:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Runner stopped polling; backend completion not confirmed. Use submission.json to recover.")
                time.sleep(args.poll_interval)
            record.update(export_trace(request, submitted["run_id"], destination))
            if task.get("error"):
                record["task_error"] = task["error"]
            # Include new files and changes committed by the agent, relative to
            # the dataset base. Only this newly created checkout is modified.
            git(workspace, "add", "--intent-to-add", "--", ".")
            patch = git(workspace, "diff", "--binary", case["base_commit"], "--")
            prediction = {"instance_id": instance_id, "patch": patch, "prefix": experiment_id}
            (destination / "prediction.json").write_text(json.dumps(prediction))
            (destination / "model.patch").write_text(patch)
        except Exception as exc:
            record["runner_error"] = str(exc)
            # A network/client failure is not evidence that the program failed.
            record.setdefault("task_status", "Unknown" if record.get("program_id") else "NotSubmitted")
        (destination / "summary.json").write_text(json.dumps(record, indent=2))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-jsonl", type=Path)
    parser.add_argument("--revision", help="Pinned Hugging Face dataset commit or tag")
    parser.add_argument("--config", default="default", help="Hugging Face dataset config (default: default)")
    parser.add_argument("--instance-id", action="append")
    parser.add_argument("--limit", type=int, default=1, help="0 runs all selected cases")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--task-timeout", type=int, default=1800)
    parser.add_argument("--poll-grace", type=int, default=120)
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument("--workspace-root", type=Path,
                        default=AGENT_PROJECT_ROOT / "benchmark-workspaces")
    parser.add_argument("--output", type=Path,
                        default=AGENT_PROJECT_ROOT / "benchmark-results" /
                        time.strftime("swe-pro-%Y%m%d-%H%M%S"),
                        help="New output directory outside agent checkouts")
    parser.add_argument("--gateway-url", default=os.getenv("AGENTCTL_GATEWAY_URL", "http://localhost:30080"))
    args = parser.parse_args()
    if args.concurrency < 1 or args.limit < 0 or args.poll_interval <= 0:
        parser.error("concurrency and poll-interval must be positive; limit must be nonnegative")
    if not 1 <= args.max_steps <= 1000 or not 1 <= args.task_timeout <= 86400:
        parser.error("max-steps must be 1..1000 and task-timeout 1..86400")
    token = os.getenv("AGENT_API_KEY")
    if not token:
        try:
            token = Path("~/.agentic_token").expanduser().read_text().strip()
        except OSError:
            parser.error("Set AGENT_API_KEY or log in with agentctl")
    try:
        cases, revision = load_cases(args)
    except (ValueError, ImportError) as exc:
        parser.error(str(exc))
    args.workspace_root = args.workspace_root.resolve()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    experiment_id = "swe-pro-" + uuid.uuid4().hex[:12]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(run_case, case, args, experiment_id, revision, token) for case in cases]
        records = []
        with (args.output / "programs.jsonl").open("w") as stream:
            for future in concurrent.futures.as_completed(futures):
                record = future.result()
                records.append(record)
                stream.write(json.dumps(record) + "\n")
                stream.flush()
                print(json.dumps(record), flush=True)
    predictions = [json.loads(path.read_text()) for path in sorted(args.output.glob("*/prediction.json"))]
    (args.output / "predictions.json").write_text(json.dumps(predictions, indent=2))
    if any(record.get("runner_error") for record in records):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
